"""Bounded raw output storage with independent, replayable byte cursors."""

import codecs
from collections import deque
from copy import copy
from dataclasses import dataclass
import re

from .contracts import ToolError


@dataclass(frozen=True)
class OutputPage:
    output: str | None
    stdout: str | None
    stderr: str | None
    next_cursor: int
    dropped_bytes: int
    has_more: bool


class _AnsiFilter:
    """Keep only parser state, never an unbounded OSC payload."""

    def __init__(self):
        self.state = "text"

    def feed(self, text: str, *, collect: bool = True) -> str:
        parts = []
        index = 0
        while index < len(text):
            if self.state == "text":
                end = text.find("\x1b", index)
                if end < 0:
                    end = len(text)
                if collect:
                    parts.append(text[index:end])
                index = end
                if index < len(text):
                    self.state = "escape"
                    index += 1
            elif self.state == "escape":
                char = text[index]
                index += 1
                if char == "[":
                    self.state = "csi"
                elif char == "]":
                    self.state = "osc"
                else:
                    if collect:
                        parts.append("\x1b" + char)
                    self.state = "text"
            elif self.state == "csi":
                match = re.search(r"[@-~]", text[index:])
                if match is None:
                    break
                index += match.end()
                self.state = "text"
            elif self.state == "osc":
                match = re.search("[\x07\x1b]", text[index:])
                if match is None:
                    break
                index += match.end()
                self.state = "text" if text[index - 1] == "\x07" else "osc_escape"
            else:
                char = text[index]
                index += 1
                if char in ("\\", "\x07"):
                    self.state = "text"
                elif char != "\x1b":
                    self.state = "osc"
        return "".join(parts)

    def advance(self, data: bytes | bytearray) -> None:
        # ASCII control bytes cannot occur inside UTF-8 continuation bytes.
        self.feed(data.decode("latin-1"), collect=False)


@dataclass
class _Chunk:
    start: int
    stream: str
    data: bytearray


class OutputBuffer:
    """Synchronous storage; the session layer owns async wakeups and exit waits."""

    def __init__(self, capacity_bytes: int = 1048576, *, mode: str = "pty"):
        if type(capacity_bytes) is not int or not 4 <= capacity_bytes <= 1048576:
            raise ValueError("Buffer capacity must be from 4 through 1048576 bytes")
        if mode not in ("pty", "pipe"):
            raise ValueError("Buffer mode must be pty or pipe")
        self.capacity_bytes = capacity_bytes
        self.mode = mode
        self.start_offset = self.end_offset = 0
        self._streams = ("pty",) if mode == "pty" else ("stdout", "stderr")
        self._chunks: deque[_Chunk] = deque()
        self._closed: set[str] = set()
        self._stream_ends = dict.fromkeys(self._streams, 0)
        self._initial_ansi = {stream: _AnsiFilter() for stream in self._streams}

    @property
    def retained_bytes(self) -> int:
        return self.end_offset - self.start_offset

    def _check_stream(self, stream: str) -> None:
        if stream not in self._streams:
            raise ValueError("Stream does not belong to this buffer mode")

    def append(self, stream: str, data: bytes) -> None:
        self._check_stream(stream)
        if stream in self._closed:
            raise ValueError("Cannot append to a closed stream")
        if not isinstance(data, bytes):
            raise TypeError("Output data must be bytes")
        if not data:
            return
        new_end = self.end_offset + len(data)
        new_start = max(self.start_offset, new_end - self.capacity_bytes)
        while self._chunks and self._chunks[0].start < new_start:
            chunk = self._chunks[0]
            count = min(len(chunk.data), new_start - chunk.start)
            self._initial_ansi[chunk.stream].advance(chunk.data[:count])
            if count == len(chunk.data):
                self._chunks.popleft()
            else:
                del chunk.data[:count]
                chunk.start += count
        skipped = max(0, new_start - self.end_offset)
        if skipped:
            for index in range(0, skipped, 8192):
                self._initial_ansi[stream].advance(data[index:min(index + 8192, skipped)])
        offset = self.end_offset + skipped
        index = skipped
        # Coalesce small reads while keeping every allocation bounded by 8 KiB.
        while index < len(data):
            if not self._chunks or self._chunks[-1].stream != stream or len(self._chunks[-1].data) >= 8192:
                self._chunks.append(_Chunk(offset, stream, bytearray()))
            chunk = self._chunks[-1]
            count = min(8192 - len(chunk.data), len(data) - index)
            chunk.data.extend(data[index:index + count])
            index += count
            offset += count
        self.start_offset, self.end_offset = new_start, new_end
        self._stream_ends[stream] = new_end

    def close_stream(self, stream: str) -> None:
        """Mark EOF so a final incomplete sequence is emitted as U+FFFD."""
        self._check_stream(stream)
        self._closed.add(stream)

    def _segments(self, start: int, end: int):
        for chunk in self._chunks:
            if chunk.start >= end:
                break
            left, right = max(start, chunk.start), min(end, chunk.start + len(chunk.data))
            if left < right:
                yield chunk.stream, left, chunk.data[left - chunk.start:right - chunk.start]

    def _decode_window(self, start: int, end: int, strip_ansi: bool):
        decoders = {stream: codecs.getincrementaldecoder("utf-8")("replace") for stream in self._streams}
        texts = {stream: [] for stream in self._streams}
        pending = {stream: [] for stream in self._streams}
        last = {}
        for stream, offset, data in self._segments(start, end):
            decoder = decoders[stream]
            texts[stream].append(decoder.decode(data))
            count = len(decoder.getstate()[0])
            positions = pending[stream] + list(range(offset + max(0, len(data) - 3), offset + len(data)))
            pending[stream] = positions[-count:] if count else []
            last[stream] = (offset + len(data) - 1, data[-1])
        cutoff = end
        for stream in self._streams:
            final = stream in self._closed and end >= self._stream_ends[stream]
            if final:
                texts[stream].append(decoders[stream].decode(b"", final=True))
            elif pending[stream]:
                cutoff = min(cutoff, pending[stream][0])
            # Hold a trailing CR only in stripped mode, so split CRLF is one LF.
            if strip_ansi and not final and stream in last and last[stream][1] == 13:
                cutoff = min(cutoff, last[stream][0])
        return {stream: "".join(parts) for stream, parts in texts.items()}, cutoff

    def read(self, cursor: int, max_bytes: int = 65536, strip_ansi: bool = True) -> OutputPage:
        if type(cursor) is not int or not 0 <= cursor <= self.end_offset:
            raise ToolError("INVALID_ARGUMENT", "cursor must be an integer between zero and the current output end.")
        if type(max_bytes) is not int or not 4 <= max_bytes <= 1048576:
            raise ToolError("INVALID_ARGUMENT", "max_bytes must be an integer from 4 through 1048576.")
        if type(strip_ansi) is not bool:
            raise ToolError("INVALID_ARGUMENT", "strip_ansi must be a boolean.")
        start = max(cursor, self.start_offset)
        end = min(self.end_offset, start + max_bytes)
        while True:
            texts, cutoff = self._decode_window(start, end, strip_ansi)
            if cutoff == end:
                break
            # An earlier cutoff can expose an incomplete character in another stream.
            end = cutoff
        if strip_ansi:
            filters = {stream: copy(parser) for stream, parser in self._initial_ansi.items()}
            for stream, _, data in self._segments(self.start_offset, start):
                filters[stream].advance(data)
            texts = {stream: filters[stream].feed(text).replace("\r\n", "\n") for stream, text in texts.items()}
        return OutputPage(
            output=texts.get("pty"), stdout=texts.get("stdout"), stderr=texts.get("stderr"),
            next_cursor=end, dropped_bytes=start - cursor, has_more=end < self.end_offset,
        )
