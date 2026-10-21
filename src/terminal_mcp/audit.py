"""Metadata-only JSONL audit records shared safely by local server instances."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping


_SUMMARY_KEYS = {
    "cursor", "max_bytes", "wait_ms", "strip_ansi", "cols", "rows", "timeout_s",
    "include_exited", "signal", "grace_ms", "remove", "text_bytes", "key_count",
    "env_count", "until_length", "keys",
}


def _select(source: Any, keys: set[str]) -> dict:
    if not isinstance(source, Mapping):
        return {}
    result = {}
    for key in keys & source.keys():
        value = source[key]
        if value is None or type(value) in (str, int, float, bool):
            result[key] = value
        elif key == "keys" and isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[key] = value
    return result


class Audit:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def log(self, event: Mapping[str, Any]) -> None:
        """Write one complete line; IO failures propagate to the tool boundary."""
        record = dict.fromkeys(("tool", "session_id", "command", "cwd", "mode", "duration_ms"))
        record.update(_select(event, set(record)))
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        record["parameter_summary"] = _select(event.get("parameter_summary"), _SUMMARY_KEYS)
        record["policy"] = _select(event.get("policy"), {"decision", "rule_id"})
        record["result"] = _select(event.get("result"), {"status", "exit_code", "signal", "error_code"})
        line = (json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, line) != len(line):
                # Retrying would let another instance split this JSONL record.
                raise OSError(errno.EIO, "Incomplete audit write")
        finally:
            os.close(fd)


@dataclass
class AuditCall:
    tool: str
    arguments: dict
    manager: Any
    began: float = field(default_factory=time.monotonic)

    def event(self, *, decision="not_applicable", rule_id=None, command=None,
              result=None, error_code=None) -> dict:
        args = self.arguments
        result = result or {}
        sid = result.get("session_id", args.get("session_id"))
        session = self.manager.sessions.get(sid) if isinstance(sid, str) else None
        summary = {key: value for key, value in args.items()
                   if key in _SUMMARY_KEYS - {"keys"} and type(value) in (int, float, bool)}
        for source, target in (("env", "env_count"), ("keys", "key_count"), ("until", "until_length")):
            value = args.get(source)
            if isinstance(value, (dict, list, str)):
                summary[target] = len(value)
        if isinstance(args.get("text"), str):
            summary["text_bytes"] = len(args["text"].encode("utf-8"))
        if self.tool == "terminal_start":
            command = args.get("command") if isinstance(args.get("command"), str) else None
        return {
            "tool": self.tool, "session_id": sid,
            "command": command,
            "cwd": session.cwd if session else args.get("cwd", str(self.manager.config.startup_cwd)),
            "mode": session.mode if session else args.get("mode"),
            "parameter_summary": summary,
            "policy": {"decision": decision, "rule_id": rule_id},
            "result": {"status": result.get("status", session.status if session else None),
                       "exit_code": result.get("exit_code", session.exit_code if session else None),
                       "signal": result.get("signal", session.signal if session else None),
                       "error_code": error_code},
            "duration_ms": (time.monotonic() - self.began) * 1000,
        }


current_call: ContextVar[AuditCall | None] = ContextVar("audit_call", default=None)
