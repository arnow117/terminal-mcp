"""Wire contracts shared by pure components and the MCP tool layer."""

from copy import deepcopy
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
STATUSES = ("running", "exited", "signaled", "timed_out", "failed_to_start", "lost")
WAIT_REASONS = ("immediate", "output", "until", "exited", "deadline")
KEY_NAMES = ("Enter", "Tab", "Backspace", "Esc", "C-c", "C-d", "Up", "Down", "Left", "Right")
ERROR_HINTS = {
    "INVALID_ARGUMENT": "Correct the tool arguments; use the previous next_cursor and stay within the documented limits.",
    "SESSION_NOT_FOUND": "Use terminal_list(include_exited=true) to find a retained session; do not reuse expired IDs.",
    "SESSION_EXITED": "Read retained output with terminal_read, or create a new session with terminal_start.",
    "SESSION_LOST": "The original server no longer owns this session. Query the record or start a new session.",
    "POLICY_DENIED": "Check the rule ID in message and choose an allowed directory or safe command. MVP cannot request an override.",
    "LIMIT_REACHED": "Use terminal_kill(remove=true) to clean up unused sessions, or ask the local user to adjust configuration.",
    "START_FAILED": "Check the local OS reason in message. A missing command normally produces shell output and exit code 127.",
    "INTERNAL_ERROR": "Retry once; if the error persists, retain the timestamp and session_id for local audit inspection.",
}
ERROR_CODES = tuple(ERROR_HINTS)


class ToolError(Exception):
    """Expected business failure, independent of the MCP transport envelope."""

    def __init__(self, code: str, message: str, *, hint: str | None = None,
                 rule_id: str | None = None, session_id: str | None = None,
                 status: str | None = None):
        if code not in ERROR_CODES:
            raise ValueError("Unknown error code")
        if code == "START_FAILED" and (not session_id or status != "failed_to_start"):
            raise ValueError("START_FAILED requires a retained session ID and failed_to_start status")
        self.code, self.message = code, message
        self.hint = hint or ERROR_HINTS[code]
        self.rule_id, self.session_id, self.status = rule_id, session_id, status
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        error = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.rule_id is not None:
            error["rule_id"] = self.rule_id
        result: dict[str, Any] = {"error": error}
        if self.code == "START_FAILED":
            result.update(session_id=self.session_id, status=self.status)
        return result


def _object(properties: dict, required=None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _field(kind, description, **constraints):
    return {"type": kind, "description": description, **constraints}


def _integer(description, minimum, maximum, **kwargs):
    return _field("integer", description, minimum=minimum, maximum=maximum, **kwargs)


_SESSION_ID = _field("string", "Retained session UUID returned by terminal_start.", format="uuid")
_NULL_STRING = {"type": ["string", "null"]}
_NONNEGATIVE = {"type": "integer", "minimum": 0}
_STATUS = {"type": "string", "enum": list(STATUSES)}
_COMMON_RESULT = {"session_id": _SESSION_ID, "status": _STATUS,
                  "exit_code": {"type": ["integer", "null"]}, "signal": _NULL_STRING}
_PAGE = {"output": _NULL_STRING, "stdout": _NULL_STRING, "stderr": _NULL_STRING,
         "next_cursor": _NONNEGATIVE, "dropped_bytes": _NONNEGATIVE,
         "has_more": {"type": "boolean"},
         "wait_reason": {"type": "string", "enum": list(WAIT_REASONS)}}
_WAIT_INPUT = {
    "wait_ms": _integer("Wait up to this many milliseconds; 0 returns immediately. Cancellation stops only this wait, not the process.", 0, 30000, default=0),
    "until": _field(["string", "null"], "Python re pattern on new, unstripped output; wait for a match, exit, or deadline. Prefer simple prompt patterns.", maxLength=1024, default=None),
    "strip_ansi": _field("boolean", "Strip ANSI CSI/OSC and normalize CRLF to LF without changing raw byte cursors.", default=True),
}

_INPUTS = {
    "terminal_start": _object({
        "command": _field(["string", "null"], "Null starts the filtered $SHELL -l (fallback /bin/sh); otherwise run /bin/sh -c. 1-32768 UTF-8 bytes, no NUL.", minLength=1, maxLength=32768, default=None),
        "cwd": _field(["string", "null"], "Existing directory inside configured allowed_roots after realpath resolution; default is the server startup directory.", default=None),
        "env": _field(["object", "null"], "Up to 128 POSIX names; values at most 4096 UTF-8 bytes, total at most 65536, no NUL. Explicit sensitive names are denied.", maxProperties=128, propertyNames={"pattern": "^[A-Za-z_][A-Za-z0-9_]*$"}, additionalProperties={"type": "string", "maxLength": 4096}, default=None),
        "mode": _field("string", "PTY provides a controlling terminal with merged output; pipe separates stdout and stderr.", enum=["pty", "pipe"], default="pty"),
        "cols": _integer("PTY columns; recorded but not applied in pipe mode.", 20, 500, default=80),
        "rows": _integer("PTY rows; recorded but not applied in pipe mode.", 5, 200, default=24),
        "timeout_s": _integer("Hard session timeout; defaults to configured default_timeout_s (3600). Cannot exceed the local configured maximum.", 1, 86400),
        "name": _field(["string", "null"], "Optional display name, at most 128 Unicode characters; never passed as a shell argument.", maxLength=128, default=None),
        **_WAIT_INPUT,
    }, []),
    "terminal_read": _object({
        "session_id": _SESSION_ID,
        "cursor": _field("integer", "Absolute raw byte offset, from 0 through the current output end; reuse next_cursor to avoid rereading.", minimum=0),
        "max_bytes": _integer("Maximum raw bytes consumed; UTF-8 boundaries may reduce this amount.", 4, 1048576, default=65536),
        **_WAIT_INPUT,
    }, ["session_id", "cursor"]),
    "terminal_write": _object({
        "session_id": _SESSION_ID,
        "text": _field("string", "UTF-8 text without automatic newline or NUL; text plus encoded keys must not exceed 65536 bytes.", maxLength=65536, default=""),
        "keys": _field("array", "Named keys appended after text, in order; at least text or keys must be nonempty.", items={"type": "string", "enum": list(KEY_NAMES)}, maxItems=32, default=[]),
        **_WAIT_INPUT,
    }, ["session_id"]),
    "terminal_list": _object({
        "session_id": _field(["string", "null"], "If provided, query just this UUID regardless of include_exited; missing IDs return SESSION_NOT_FOUND.", format="uuid", default=None),
        "include_exited": _field("boolean", "Include retained exited and lost sessions (default retention 600 seconds); false lists only running sessions.", default=True),
    }, []),
    "terminal_kill": _object({
        "session_id": _SESSION_ID,
        "signal": _field("string", "Initial Unix process-group signal; KILL bypasses the grace period.", enum=["TERM", "INT", "HUP", "KILL"], default="TERM"),
        "grace_ms": _integer("Grace period before escalating the entire group to KILL; ignored for signal=KILL.", 0, 30000, default=1000),
        "remove": _field("boolean", "After reaping, delete retained output and registry metadata; future reads or queries return SESSION_NOT_FOUND.", default=False),
    }, ["session_id"]),
}
# Nonempty writes are also expressed in the discoverable JSON Schema.
_INPUTS["terminal_write"]["anyOf"] = [
    {"required": ["text"], "properties": {"text": {"minLength": 1}}},
    {"required": ["keys"], "properties": {"keys": {"minItems": 1}}},
]

_DESCRIPTIONS = {
    "terminal_start": "Create an independent local terminal session. Save session_id and next_cursor, then use terminal_read(session_id, cursor=next_cursor) for incremental output. Default pty suits shells, REPLs and control keys; choose pipe for separate stdout/stderr. wait_ms waits only for initial output: without until return on output, otherwise on a regex match, exit or deadline. Commands run as the current user subject to local cwd, environment and command policy; this is not an OS sandbox.",
    "terminal_read": "Read new output from an absolute raw byte cursor. Reuse the previous next_cursor; concurrent readers may independently reuse the same cursor. Default is immediate; wait_ms long-polls without blocking other sessions. If the 1 MiB ring overwrote the cursor, inspect dropped_bytes and continue with next_cursor. PTY uses output; pipe uses stdout/stderr. Exited sessions retain readable output for 600 seconds by default. With until, wait for its regex, exit or deadline; request cancellation never kills the process.",
    "terminal_write": "Write UTF-8 text followed by named control keys to a running session in arrival order. Text adds no newline: use keys=[\"Enter\"] to submit shell/REPL input. PTY echoes input, so returned output can include the input itself. Return new output starting at the pre-write end; save next_cursor for terminal_read. C-c sends a foreground control byte in PTY and only stdin byte 0x03 in pipe, not a whole-session kill; use terminal_kill to reap the group. Dangerous complete lines, including cross-call accumulated input and Enter, are denied atomically. wait_ms/until waits only for the response, not for process ownership.",
    "terminal_list": "List sessions owned by this connection, or query one retained session. Use terminal_read for output and terminal_write only when status=running. By default include exited and lost records still within the 600-second retention window; include_exited=false lists only running sessions. A specific session_id bypasses that filter. Records from a previous server become lost and cannot be taken over.",
    "terminal_kill": "Terminate the entire Unix process group, not just its parent shell. Send TERM by default, wait grace_ms, then KILL any remaining group; signal=KILL acts immediately. Return the final reaped state. Already-exited sessions return their current state idempotently, while lost sessions return SESSION_LOST. remove=true discards retained output and registry records after cleanup; subsequent reads cannot recover them.",
}

_SUMMARY = _object({
    **_COMMON_RESULT, "name": _NULL_STRING, "command": _NULL_STRING,
    "cwd": {"type": "string"}, "mode": {"enum": ["pty", "pipe"]},
    "pid": {"type": ["integer", "null"]}, "pgid": {"type": ["integer", "null"]},
    "started_at": {"type": "string", "format": "date-time"},
    "ended_at": {"type": ["string", "null"], "format": "date-time"},
    "last_activity_at": {"type": "string", "format": "date-time"},
    "output_start_cursor": _NONNEGATIVE, "output_end_cursor": _NONNEGATIVE,
    "output_total_bytes": _NONNEGATIVE, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 86400},
})
_SUCCESS = {
    "terminal_start": _object({**_COMMON_RESULT, **_PAGE, "pid": {"type": "integer", "minimum": 1}, "pgid": {"type": "integer", "minimum": 1}, "mode": {"enum": ["pty", "pipe"]}, "started_at": {"type": "string", "format": "date-time"}}),
    "terminal_read": _object({**_COMMON_RESULT, **_PAGE, "ended_at": {"type": ["string", "null"], "format": "date-time"}}),
    "terminal_write": _object({**_COMMON_RESULT, **_PAGE, "written_bytes": _NONNEGATIVE}),
    "terminal_list": _object({"sessions": {"type": "array", "items": _SUMMARY}, "count": _NONNEGATIVE}),
    "terminal_kill": _object({**_COMMON_RESULT, "status": {"enum": ["exited", "signaled", "timed_out", "failed_to_start"]}, "termination_reason": {"enum": ["manual", "timeout", "shutdown"]}, "escalated": {"type": "boolean"}, "removed": {"type": "boolean"}, "ended_at": {"type": ["string", "null"], "format": "date-time"}}),
}
for _name in ("terminal_start", "terminal_read", "terminal_write"):
    _pty = {"output": {"type": "string"}, "stdout": {"type": "null"}, "stderr": {"type": "null"}}
    _pipe = {"output": {"type": "null"}, "stdout": {"type": "string"}, "stderr": {"type": "string"}}
    if _name == "terminal_start":
        _pty["mode"], _pipe["mode"] = {"const": "pty"}, {"const": "pipe"}
    _SUCCESS[_name]["oneOf"] = [{"properties": _pty}, {"properties": _pipe}]


def _error_schema(start_failed: bool) -> dict:
    codes = ["START_FAILED"] if start_failed else [code for code in ERROR_CODES if code != "START_FAILED"]
    error = _object({"code": {"enum": codes}, "message": {"type": "string", "minLength": 1},
                     "hint": {"type": "string", "minLength": 1}, "rule_id": {"type": "string"}}, ["code", "message", "hint"])
    properties = {"error": error}
    if start_failed:
        properties.update(session_id=_SESSION_ID, status={"const": "failed_to_start"})
    return _object(properties)


TOOL_SCHEMAS = {
    name: {"name": name, "description": _DESCRIPTIONS[name],
           "inputSchema": {"$schema": SCHEMA_URI, **schema},
           "outputSchema": {"$schema": SCHEMA_URI, "type": "object", "oneOf": [_SUCCESS[name], _error_schema(False), _error_schema(True)]}}
    for name, schema in _INPUTS.items()
}


def validate_arguments(tool: str, arguments: Any) -> dict[str, Any]:
    """Validate without echoing caller values, then apply static defaults only."""
    if tool not in TOOL_SCHEMAS:
        raise ToolError("INVALID_ARGUMENT", "Unknown tool name.")
    schema = TOOL_SCHEMAS[tool]["inputSchema"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    error = next(validator.iter_errors(arguments), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "arguments"
        raise ToolError("INVALID_ARGUMENT", f"Invalid {location}: violates {error.validator}.")
    result = deepcopy(arguments)
    for key, prop in schema["properties"].items():
        if key in result and prop.get("type") == "integer" and type(result[key]) is not int:
            raise ToolError("INVALID_ARGUMENT", f"{key} must be an integer.")
        if key not in result and "default" in prop:
            result[key] = deepcopy(prop["default"])
    for field, limit in (("command", 32768), ("text", 65536)):
        value = result.get(field)
        if value is not None:
            _check_bytes(value, limit, field)
    if result.get("cwd") is not None:
        _check_bytes(result["cwd"], None, "cwd")
    env = result.get("env")
    if env is not None:
        validate_environment(env)
    if result.get("until") is not None:
        try:
            re.compile(result["until"])
        except (re.error, OverflowError, RecursionError) as exc:
            raise ToolError("INVALID_ARGUMENT", "until must be a valid Python regular expression.") from exc
    if tool == "terminal_write":
        from .keys import encode_keys
        if len(result["text"].encode("utf-8")) + len(encode_keys(result["keys"])) > 65536:
            raise ToolError("INVALID_ARGUMENT", "Encoded text plus keys exceeds 65536 bytes.")
    return result


def _check_bytes(value: str, limit: int | None, field: str) -> int:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ToolError("INVALID_ARGUMENT", f"{field} must be valid UTF-8 text.") from exc
    if "\0" in value or (limit is not None and size > limit):
        raise ToolError("INVALID_ARGUMENT", f"{field} contains NUL or exceeds its UTF-8 byte limit.")
    return size


def validate_environment(env: Any) -> None:
    if not isinstance(env, dict) or len(env) > 128:
        raise ToolError("INVALID_ARGUMENT", "env must contain at most 128 string entries.")
    total = 0
    for name, value in env.items():
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None or not isinstance(value, str):
            raise ToolError("INVALID_ARGUMENT", "env requires POSIX variable names and string values.")
        total += len(name.encode()) + _check_bytes(value, 4096, "env value")
    if total > 65536:
        raise ToolError("INVALID_ARGUMENT", "env names and values exceed 65536 UTF-8 bytes.")
