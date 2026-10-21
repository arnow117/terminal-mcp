import pytest

from terminal_mcp.contracts import ToolError
from terminal_mcp.keys import encode_keys


@pytest.mark.parametrize("name,expected", [
    ("Enter", b"\r"), ("Tab", b"\t"), ("Backspace", b"\x7f"),
    ("Esc", b"\x1b"), ("C-c", b"\x03"), ("C-d", b"\x04"),
    ("Up", b"\x1b[A"), ("Down", b"\x1b[B"),
    ("Left", b"\x1b[D"), ("Right", b"\x1b[C"),
])
def test_named_key(name, expected):
    assert encode_keys([name]) == expected


def test_order_and_empty_keys():
    assert encode_keys([]) == b""
    assert encode_keys(["Tab", "Enter", "C-c"]) == b"\t\r\x03"


@pytest.mark.parametrize("names", [["enter"], ["C-z"], ["Enter"] * 33, "Enter", [1]])
def test_unknown_or_invalid_keys(names):
    with pytest.raises(ToolError) as caught:
        encode_keys(names)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.hint
