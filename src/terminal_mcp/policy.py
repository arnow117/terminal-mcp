"""Best-effort accidental-misuse guardrails, not an OS security sandbox."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
import os
from pathlib import Path
import re
from typing import Mapping

from .config import Config, resolve_directory
from .contracts import ToolError, _check_bytes, validate_environment
from .keys import encode_keys


@dataclass(frozen=True)
class PreparedStart:
    command: str | None
    cwd: Path
    env: dict[str, str]
    timeout_s: int


@dataclass
class CompleteLineTracker:
    """Per-session input state; callers serialize checks together with writes."""

    pending: str = ""
    after_cr: bool = False


class Policy:
    def __init__(self, config: Config, on_decision=None):
        self.config = config
        self.rules = tuple((rule_id, re.compile(pattern)) for rule_id, pattern in config.dangerous_command_rules)
        self.on_decision = on_decision

    def _decision(self, decision: str, rule_id: str | None, command: str | None = None):
        if self.on_decision is not None:
            self.on_decision(decision, rule_id, command)

    def _denied_env(self, name: str) -> bool:
        return any(fnmatchcase(name.upper(), pattern.upper()) for pattern in self.config.env_deny_patterns)

    def check_command(self, command: str) -> None:
        for rule_id, pattern in self.rules:
            if pattern.search(command):
                self._decision("deny", rule_id, command)
                raise ToolError("POLICY_DENIED", f"Command denied by rule {rule_id}.", rule_id=rule_id)
        self._decision("allow", "command", command)

    def prepare_start(self, *, command: str | None = None, cwd: str | Path | None = None,
                      env: dict[str, str] | None = None, timeout_s: int | None = None,
                      environ: Mapping[str, str] | None = None) -> PreparedStart:
        cwd = resolve_directory(self.config.startup_cwd if cwd is None else cwd, base=self.config.startup_cwd)
        if not any(os.path.commonpath((cwd, root)) == str(root) for root in self.config.allowed_roots):
            self._decision("deny", "cwd-outside-roots")
            raise ToolError("POLICY_DENIED", "cwd denied by rule cwd-outside-roots.", rule_id="cwd-outside-roots")
        self._decision("allow", "cwd-inside-roots")
        if command is not None:
            if not isinstance(command, str) or not command:
                raise ToolError("INVALID_ARGUMENT", "command must be null or a nonempty string.")
            _check_bytes(command, 32768, "command")
            self.check_command(command)
        explicit = {} if env is None else env
        validate_environment(explicit)
        for name in explicit:
            if self._denied_env(name):
                self._decision("deny", "sensitive-env")
                raise ToolError("POLICY_DENIED", "Explicit environment name denied by rule sensitive-env.", rule_id="sensitive-env")
        prepared_env = dict(explicit)
        total = sum(len(name.encode()) + len(value.encode()) for name, value in explicit.items())
        for name, value in (os.environ if environ is None else environ).items():
            if self._denied_env(name):
                self._decision("filter", "sensitive-env")
                continue
            if name in prepared_env:
                continue
            try:
                validate_environment({name: value})
            except ToolError:
                continue
            size = len(name.encode()) + len(value.encode())
            if total + size <= 65536:
                prepared_env[name] = value
                total += size
        timeout = self.config.default_timeout_s if timeout_s is None else timeout_s
        if type(timeout) is not int or not 1 <= timeout <= self.config.max_timeout_s:
            raise ToolError("INVALID_ARGUMENT", "timeout_s exceeds the configured range.")
        return PreparedStart(command, cwd, prepared_env, timeout)

    def check_complete_lines(self, tracker: CompleteLineTracker, text: str = "",
                             keys: list[str] | None = None) -> list[str]:
        """Validate the whole write before committing pending-line state or sending bytes.

        If the subsequent OS write fails, the session layer must stop accepting writes
        or restore the tracker to the input actually sent. This helper performs no IO.
        """
        keys = [] if keys is None else keys
        if not isinstance(text, str):
            raise ToolError("INVALID_ARGUMENT", "text must be a string.")
        text_size = _check_bytes(text, 65536, "text")
        encoded_keys = encode_keys(keys)
        if text_size + len(encoded_keys) > 65536:
            raise ToolError("INVALID_ARGUMENT", "Encoded text plus keys exceeds 65536 bytes.")
        pending = list(tracker.pending)
        pending_size = len(tracker.pending.encode())
        after_cr = tracker.after_cr
        lines = []
        # Arrow keys and Esc are not shell editing emulation. Their effects are
        # explicitly outside this best-effort complete-line guardrail.
        key_text = {"Enter": "\r", "Tab": "\t", "Backspace": "\x7f", "C-c": "\x03"}
        for char in text + "".join(key_text.get(key, "") for key in keys):
            if char == "\n" and after_cr:
                after_cr = False
                continue
            after_cr = char == "\r"
            if char in "\r\n":
                line = "".join(pending)
                self.check_command(line)
                lines.append(line)
                pending, pending_size = [], 0
            elif char == "\x03":
                pending, pending_size = [], 0
            elif char in ("\x08", "\x7f"):
                if pending:
                    pending_size -= len(pending.pop().encode())
            else:
                pending_size += len(char.encode())
                if pending_size > 65536:
                    self._decision("deny", "input-line-limit")
                    raise ToolError("POLICY_DENIED", "Unsubmitted input denied by rule input-line-limit (65536 bytes).", rule_id="input-line-limit")
                pending.append(char)
        tracker.pending = "".join(pending)
        tracker.after_cr = after_cr
        return lines
