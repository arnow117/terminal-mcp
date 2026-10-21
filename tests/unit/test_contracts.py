import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from terminal_mcp.contracts import ERROR_CODES, TOOL_SCHEMAS, ToolError, validate_arguments


TOOLS = {"terminal_start", "terminal_read", "terminal_write", "terminal_list", "terminal_kill"}
SESSION_ID = "7d72dc80-b355-4ab5-85af-14fc49a90caa"


def test_five_tools_have_described_input_and_success_error_output_schemas():
    assert set(TOOL_SCHEMAS) == TOOLS
    for name, tool in TOOL_SCHEMAS.items():
        assert tool["name"] == name
        assert len(tool["description"]) > 100
        assert tool["description"].isascii()
        for key in ("inputSchema", "outputSchema"):
            Draft202012Validator.check_schema(tool[key])
            assert tool[key]["type"] == "object"
            assert tool[key]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert all(prop.get("description") for prop in tool["inputSchema"]["properties"].values())
        output = Draft202012Validator(tool["outputSchema"])
        for code in ERROR_CODES:
            extra = {"session_id": SESSION_ID, "status": "failed_to_start"} if code == "START_FAILED" else {}
            error = ToolError(code, "Test failure", **extra).as_dict()
            output.validate(error)
            assert error["error"]["hint"]
        assert not output.is_valid({"error": {"code": "UNKNOWN", "message": "bad", "hint": "fix"}})
        assert not output.is_valid({"error": {"code": "START_FAILED", "message": "bad", "hint": "fix"}})


def test_error_codes_are_stable():
    assert set(ERROR_CODES) == {"INVALID_ARGUMENT", "SESSION_NOT_FOUND", "SESSION_EXITED", "SESSION_LOST", "POLICY_DENIED", "LIMIT_REACHED", "START_FAILED", "INTERNAL_ERROR"}


def test_input_defaults_and_required_arguments():
    start = validate_arguments("terminal_start", {})
    assert (start["mode"], start["cols"], start["rows"], start["wait_ms"], start["strip_ansi"]) == ("pty", 80, 24, 0, True)
    assert start["command"] is None
    assert "timeout_s" not in start
    assert validate_arguments("terminal_list", {}) == {"session_id": None, "include_exited": True}
    assert validate_arguments("terminal_read", {"session_id": SESSION_ID, "cursor": 0})["max_bytes"] == 65536
    assert validate_arguments("terminal_kill", {"session_id": SESSION_ID})["grace_ms"] == 1000


@pytest.mark.parametrize("tool,args", [
    ("terminal_read", {}), ("terminal_read", {"session_id": SESSION_ID, "cursor": -1}),
    ("terminal_read", {"session_id": SESSION_ID, "cursor": True}),
    ("terminal_read", {"session_id": SESSION_ID, "cursor": 0, "max_bytes": 3}),
    ("terminal_start", {"wait_ms": 30001}), ("terminal_start", {"wait_ms": 1.0}),
    ("terminal_start", {"cols": 19}), ("terminal_start", {"rows": 201}),
    ("terminal_start", {"timeout_s": 86401}), ("terminal_start", {"mode": "bad"}),
    ("terminal_start", {"until": "["}), ("terminal_start", {"command": ""}),
    ("terminal_start", {"command": "中" * 11000}), ("terminal_start", {"command": "a\0b"}),
    ("terminal_start", {"name": "x" * 129}), ("terminal_start", {"unknown": 1}),
    ("terminal_start", {"env": {"1BAD": "x"}}),
    ("terminal_write", {"session_id": SESSION_ID}),
    ("terminal_write", {"session_id": SESSION_ID, "text": "x" * 65536, "keys": ["Enter"]}),
    ("terminal_write", {"session_id": SESSION_ID, "keys": ["Unknown"]}),
    ("terminal_kill", {"session_id": "not-a-uuid"}),
    ("terminal_kill", {"session_id": SESSION_ID, "signal": "QUIT"}),
])
def test_invalid_arguments_have_stable_errors(tool, args):
    with pytest.raises(ToolError) as caught:
        validate_arguments(tool, args)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_success_output_examples_and_mode_constraints():
    common = {"session_id": SESSION_ID, "status": "running", "exit_code": None, "signal": None}
    page = {"output": "hello", "stdout": None, "stderr": None, "next_cursor": 5, "dropped_bytes": 0, "has_more": False, "wait_reason": "immediate"}
    examples = {
        "terminal_start": common | page | {"pid": 42, "pgid": 42, "mode": "pty", "started_at": "2026-10-21T10:00:00Z"},
        "terminal_read": common | page | {"ended_at": None},
        "terminal_write": common | page | {"written_bytes": 2},
        "terminal_list": {"sessions": [], "count": 0},
        "terminal_kill": common | {"status": "signaled", "signal": "SIGTERM", "termination_reason": "manual", "escalated": False, "removed": False, "ended_at": "2026-10-21T10:00:00Z"},
    }
    for name, example in examples.items():
        validator = Draft202012Validator(TOOL_SCHEMAS[name]["outputSchema"])
        validator.validate(example)
        assert not validator.is_valid({})
    validator = Draft202012Validator(TOOL_SCHEMAS["terminal_start"]["outputSchema"])
    assert not validator.is_valid(examples["terminal_start"] | {"stdout": "wrong"})
    assert not validator.is_valid(examples["terminal_start"] | {"mode": "pipe"})


def test_cli_help_is_available_without_creating_state(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    completed = subprocess.run([sys.executable, "-m", "terminal_mcp", "--help"], cwd=cwd, capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0
    assert all(flag in completed.stdout for flag in ("--config", "--state-dir", "--allowed-root"))
    assert not list(cwd.iterdir())


def test_kill_can_return_retained_failed_start_without_signaling():
    validator = Draft202012Validator(TOOL_SCHEMAS["terminal_kill"]["outputSchema"])
    result = {"session_id": SESSION_ID, "status": "failed_to_start", "exit_code": None, "signal": None,
              "termination_reason": "manual", "escalated": False, "removed": False, "ended_at": "2026-10-21T10:00:00Z"}
    validator.validate(result)
    assert not validator.is_valid(result | {"status": "lost"})
    assert not validator.is_valid(result | {"status": "running"})


@pytest.mark.parametrize("arguments", [
    {"until": "x{99999999999999999999999999999999999}"},
    {"command": "\ud800"}, {"env": {"X": "\ud800"}},
    {"env": {f"X{i}": "a" * 4096 for i in range(16)}},
])
def test_invalid_encoding_and_regex_limits_are_business_errors(arguments):
    with pytest.raises(ToolError) as caught:
        validate_arguments("terminal_start", arguments)
    assert caught.value.code == "INVALID_ARGUMENT"
