import pytest

from terminal_mcp.buffer import OutputBuffer
from terminal_mcp.contracts import ToolError


def test_old_cursor_reports_dropped_bytes():
    buffer = OutputBuffer(capacity_bytes=8)
    buffer.append("pty", b"012345")
    buffer.append("pty", b"6789AB")
    page = buffer.read(0, max_bytes=4)
    assert (buffer.start_offset, buffer.end_offset, buffer.retained_bytes) == (4, 12, 8)
    assert (page.output, page.next_cursor, page.dropped_bytes, page.has_more) == ("4567", 8, 4, True)
    tail = buffer.read(page.next_cursor)
    assert (tail.output, tail.next_cursor, tail.has_more) == ("89AB", 12, False)
    assert buffer.read(8) == tail


def test_default_capacity_and_large_append_are_bounded():
    buffer = OutputBuffer()
    assert buffer.capacity_bytes == 1048576
    for _ in range(50):
        buffer.append("pty", b"x" * 1048576)
    assert buffer.retained_bytes == 1048576
    assert buffer.end_offset == 50 * 1048576
    assert buffer.read(0).dropped_bytes == 49 * 1048576


def test_incremental_utf8_and_invalid_bytes():
    buffer = OutputBuffer()
    buffer.append("pty", b"A\xe4\xb8")
    first = buffer.read(0)
    assert (first.output, first.next_cursor, first.has_more) == ("A", 1, True)
    buffer.append("pty", b"\xad\xffZ")
    second = buffer.read(first.next_cursor)
    assert (second.output, second.next_cursor) == ("中\ufffdZ", 6)
    assert buffer.read(0, max_bytes=4).output == "A中"
    assert buffer.read(1, max_bytes=4).output == "中\ufffd"


def test_utf8_paging_does_not_split_a_character():
    buffer = OutputBuffer()
    buffer.append("pty", "ab😀tail".encode())
    first = buffer.read(0, max_bytes=4)
    assert (first.output, first.next_cursor) == ("ab", 2)
    second = buffer.read(2, max_bytes=4)
    assert (second.output, second.next_cursor) == ("😀", 6)


def test_pipe_streams_decode_independently_with_one_cursor():
    buffer = OutputBuffer(mode="pipe")
    buffer.append("stdout", b"\xe4")
    buffer.append("stderr", b"ERR")
    assert buffer.read(0).next_cursor == 0
    buffer.append("stdout", b"\xb8\xad!")
    page = buffer.read(0)
    assert (page.output, page.stdout, page.stderr, page.next_cursor) == (None, "中!", "ERR", 7)
    assert buffer.read(0, max_bytes=4).next_cursor == 0


def test_close_stream_flushes_incomplete_utf8():
    buffer = OutputBuffer()
    buffer.append("pty", b"\xf0\x9f")
    assert buffer.read(0).output == ""
    buffer.close_stream("pty")
    assert (buffer.read(0).output, buffer.read(0).next_cursor) == ("\ufffd", 2)


def test_ansi_across_chunks_and_pages_preserves_raw_offsets():
    buffer = OutputBuffer()
    raw = b"\x1b[31mred\x1b[0m\r\n\x1b]0;title\x07ok\x1b]8;;url\x1b\\link\x1b]8;;\x1b\\"
    for byte in raw:
        buffer.append("pty", bytes([byte]))
    assert buffer.read(0).output == "red\noklink"
    assert buffer.read(0, strip_ansi=False).output == raw.decode()
    cursor, parts = 0, []
    for _ in range(len(raw)):
        page = buffer.read(cursor, max_bytes=4)
        parts.append(page.output)
        cursor = page.next_cursor
        if not page.has_more:
            break
    assert cursor == len(raw)
    assert "".join(parts) == "red\noklink"


def test_eviction_inside_ansi_does_not_leak_escape_payload():
    buffer = OutputBuffer(capacity_bytes=8)
    buffer.append("pty", b"\x1b]0;secret-title\x07ok")
    assert buffer.read(0).output == "ok"
    assert buffer.read(0).dropped_bytes == buffer.start_offset


def test_overwritten_utf8_prefix_is_replaced_and_progresses():
    buffer = OutputBuffer(capacity_bytes=4)
    buffer.append("pty", "中abc".encode())
    assert buffer.read(0).output == "\ufffdabc"
    assert buffer.read(0).dropped_bytes == 2


@pytest.mark.parametrize("cursor,max_bytes", [(-1, 4), (1, 4), (0, 3), (0, 1048577), (True, 4)])
def test_invalid_cursor_and_limits(cursor, max_bytes):
    with pytest.raises(ToolError) as caught:
        OutputBuffer().read(cursor, max_bytes=max_bytes)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_crlf_split_across_pages_and_appends_preserves_lone_cr():
    buffer = OutputBuffer()
    buffer.append("pty", b"abc\r")
    first = buffer.read(0, max_bytes=4)
    assert (first.output, first.next_cursor) == ("abc", 3)
    buffer.append("pty", b"\nX\rY\r")
    second = buffer.read(first.next_cursor)
    assert second.output == "\nX\rY"
    buffer.close_stream("pty")
    assert buffer.read(second.next_cursor).output == "\r"
    assert buffer.read(0, strip_ansi=False).output == "abc\r\nX\rY\r"


def test_interleaved_utf8_cutoff_is_safe_for_every_stream():
    buffer = OutputBuffer(mode="pipe")
    buffer.append("stdout", b"A\xf0")
    buffer.append("stderr", b"B\xe4")
    buffer.append("stdout", b"\x9f\x98\x80")
    first = buffer.read(0)
    assert (first.stdout, first.stderr, first.next_cursor) == ("A", "", 1)
    buffer.append("stderr", b"\xb8\xad")
    tail = buffer.read(first.next_cursor)
    assert (tail.stdout, tail.stderr, tail.next_cursor) == ("😀", "B中", 9)


def test_closed_stream_with_more_bytes_beyond_page_is_not_flushed_early():
    buffer = OutputBuffer()
    buffer.append("pty", "abc😀".encode())
    buffer.close_stream("pty")
    assert buffer.read(0, max_bytes=4).output == "abc"
    assert buffer.read(3, max_bytes=4).output == "😀"


def test_invalid_streams_and_append_after_close_are_rejected():
    buffer = OutputBuffer()
    with pytest.raises(ValueError):
        buffer.append("stdout", b"x")
    buffer.close_stream("pty")
    with pytest.raises(ValueError):
        buffer.append("pty", b"x")


@pytest.mark.parametrize("mode", ["pty", "pipe"])
def test_random_chunking_and_page_replay_match_full_decode(mode):
    import random

    rng = random.Random(42)
    stream = "pty" if mode == "pty" else "stdout"
    raw = ("alpha中😀\r\nbeta\x1b[32mgreen\x1b[0m\x1b]0;title\x1b\\\n" * 15).encode() + b"\xff\xe4"
    buffer = OutputBuffer(mode=mode)
    index = 0
    while index < len(raw):
        count = rng.randint(1, 19)
        buffer.append(stream, raw[index:index + count])
        index += count
    buffer.close_stream(stream)
    if mode == "pipe":
        buffer.close_stream("stderr")
    for strip in (True, False):
        expected = getattr(buffer.read(0, max_bytes=1048576, strip_ansi=strip), "output" if mode == "pty" else "stdout")
        cursor, parts = 0, []
        for _ in range(len(raw)):
            max_bytes = rng.randint(4, 23)
            page = buffer.read(cursor, max_bytes=max_bytes, strip_ansi=strip)
            assert page == buffer.read(cursor, max_bytes=max_bytes, strip_ansi=strip)
            assert page.next_cursor > cursor
            parts.append(page.output if mode == "pty" else page.stdout)
            cursor = page.next_cursor
            if not page.has_more:
                break
        assert cursor == len(raw)
        assert "".join(parts) == expected


def test_single_large_append_does_not_copy_the_discarded_prefix():
    import tracemalloc

    raw = b"x" * (8 * 1048576)
    buffer = OutputBuffer(capacity_bytes=4096)
    tracemalloc.start()
    try:
        buffer.append("pty", raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert buffer.retained_bytes == 4096
    assert peak < 256 * 1024
