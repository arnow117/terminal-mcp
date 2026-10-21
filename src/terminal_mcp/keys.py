"""Stable terminal key encodings; C-c is a byte, not a process-group signal."""

from .contracts import ToolError

KEY_BYTES = {
    "Enter": b"\r", "Tab": b"\t", "Backspace": b"\x7f", "Esc": b"\x1b",
    "C-c": b"\x03", "C-d": b"\x04", "Up": b"\x1b[A", "Down": b"\x1b[B",
    "Left": b"\x1b[D", "Right": b"\x1b[C",
}


def encode_keys(names: list[str]) -> bytes:
    if not isinstance(names, list) or len(names) > 32:
        raise ToolError("INVALID_ARGUMENT", "keys must be a list with at most 32 named keys.")
    if any(not isinstance(name, str) or name not in KEY_BYTES for name in names):
        raise ToolError("INVALID_ARGUMENT", "Unknown key name; use the documented case-sensitive key allowlist.")
    return b"".join(KEY_BYTES[name] for name in names)
