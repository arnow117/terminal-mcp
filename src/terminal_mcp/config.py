"""Local configuration; loading defaults never creates files or directories."""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Mapping

from .contracts import ToolError

DEFAULT_ENV_DENY_PATTERNS = ("*TOKEN*", "*SECRET*", "*PASSWORD*", "*_KEY", "AWS_*")
DEFAULT_COMMAND_RULES = (
    ("rm-root", r"\brm\s+(?=[^\n;&|]*(?:-[^\s]*r|--recursive))(?=[^\n;&|]*(?:-[^\s]*f|--force))[^\n;&|]*\s/(?:\s|$|[;&|])"),
    ("mkfs", r"(?:^|[\s;/|&])mkfs(?:\.[A-Za-z0-9_-]+)?(?:\s|$)"),
    ("dd-device", r"\bdd\s+[^\n;&|]*\bof\s*=\s*['\"]?/dev/"),
    ("power-control", r"(?:^|[\s;/|&])(?:shutdown|reboot|halt|poweroff)(?:\s|$|[;&|])"),
    ("fork-bomb", r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ("download-shell", r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:/[^\s;|]*/)?(?:sh|bash|zsh|dash|ksh)(?:\s|$|[;&|])"),
)


@dataclass(frozen=True)
class Config:
    config_path: Path
    state_dir: Path
    startup_cwd: Path
    allowed_roots: tuple[Path, ...]
    env_deny_patterns: tuple[str, ...] = DEFAULT_ENV_DENY_PATTERNS
    dangerous_command_rules: tuple[tuple[str, str], ...] = DEFAULT_COMMAND_RULES
    max_sessions: int = 16
    buffer_bytes: int = 1048576
    default_timeout_s: int = 3600
    max_timeout_s: int = 86400
    kill_grace_ms: int = 1000
    exit_retention_s: int = 600


def default_paths(*, platform: str | None = None, home: Path | None = None,
                  environ: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "darwin":
        state = home / "Library/Application Support/terminal-mcp"
        return state / "config.toml", state
    if platform.startswith("linux"):
        config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
        state_home = Path(environ.get("XDG_STATE_HOME") or home / ".local/state")
        # The XDG specification ignores relative directory overrides.
        if not config_home.is_absolute():
            config_home = home / ".config"
        if not state_home.is_absolute():
            state_home = home / ".local/state"
        return config_home / "terminal-mcp/config.toml", state_home / "terminal-mcp"
    raise ToolError("INVALID_ARGUMENT", "Only macOS and Linux are supported.")


def parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="terminal-mcp", description="Local session-oriented terminal MCP server over stdio.")
    parser.add_argument("--config", dest="config_path", type=Path, help="Read local TOML configuration from this path.")
    parser.add_argument("--state-dir", type=Path, help="Override the local audit and per-instance registry directory.")
    parser.add_argument("--allowed-root", dest="allowed_roots", action="append", type=Path, help="Allowed realpath root (repeatable); overrides TOML roots.")
    return parser.parse_args(argv)


def resolve_directory(path: str | Path, *, base: Path) -> Path:
    try:
        path = Path(path).expanduser()
        resolved = (path if path.is_absolute() else base / path).resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("Not a directory")
        return resolved
    except (OSError, ValueError, RuntimeError) as exc:
        raise ToolError("INVALID_ARGUMENT", "cwd and allowed_roots must be existing, resolvable directories.") from exc


def load_config(*, config_path: Path | str | None = None,
                state_dir: Path | str | None = None,
                allowed_roots: list[Path | str] | None = None,
                startup_cwd: Path | str | None = None) -> Config:
    default_config, default_state = default_paths()
    startup = resolve_directory(startup_cwd if startup_cwd is not None else Path.cwd(), base=Path.cwd())
    if startup == Path("/"):
        startup = resolve_directory(Path.home(), base=Path("/"))
        print("Warning: startup cwd is /; using HOME as the default cwd and allowed root.", file=sys.stderr)
    path = Path(config_path if config_path is not None else default_config).expanduser()
    path = (path if path.is_absolute() else startup / path).resolve()
    state = Path(state_dir if state_dir is not None else default_state).expanduser()
    state = (state if state.is_absolute() else startup / state).resolve()
    try:
        with path.open("rb") as file:
            values = tomllib.load(file)
    except FileNotFoundError:
        values = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolError("INVALID_ARGUMENT", "Cannot read a valid TOML configuration.") from exc
    limits = {"max_sessions": (1, 16, 16), "buffer_bytes": (4, 1048576, 1048576),
              "default_timeout_s": (1, 86400, 3600), "max_timeout_s": (1, 86400, 86400),
              "kill_grace_ms": (0, 30000, 1000), "exit_retention_s": (0, 600, 600)}
    unknown = values.keys() - limits.keys() - {"allowed_roots", "env_deny_patterns", "dangerous_command_rules"}
    if unknown:
        raise ToolError("INVALID_ARGUMENT", "Unknown TOML configuration setting.")
    numbers = {}
    for name, (minimum, maximum, default) in limits.items():
        number = values.get(name, default)
        if type(number) is not int or not minimum <= number <= maximum:
            raise ToolError("INVALID_ARGUMENT", f"{name} must be an integer from {minimum} through {maximum}.")
        numbers[name] = number
    if numbers["default_timeout_s"] > numbers["max_timeout_s"]:
        raise ToolError("INVALID_ARGUMENT", "default_timeout_s cannot exceed max_timeout_s.")
    roots = allowed_roots if allowed_roots is not None else values.get("allowed_roots", [startup])
    if not isinstance(roots, list) or not roots or any(not isinstance(root, (str, Path)) for root in roots):
        raise ToolError("INVALID_ARGUMENT", "allowed_roots must be a nonempty list of existing directories.")
    resolved_roots = tuple(dict.fromkeys(resolve_directory(root, base=startup) for root in roots))
    patterns = values.get("env_deny_patterns", list(DEFAULT_ENV_DENY_PATTERNS))
    if not isinstance(patterns, list) or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise ToolError("INVALID_ARGUMENT", "env_deny_patterns must be a list of nonempty glob strings.")
    patterns = tuple(dict.fromkeys((*DEFAULT_ENV_DENY_PATTERNS, *patterns)))
    rules = list(DEFAULT_COMMAND_RULES)
    custom = values.get("dangerous_command_rules", [])
    if not isinstance(custom, list):
        raise ToolError("INVALID_ARGUMENT", "dangerous_command_rules must be an array of rule tables.")
    for rule in custom:
        if not isinstance(rule, dict) or set(rule) != {"id", "pattern"} or any(not isinstance(rule[key], str) or not rule[key] for key in ("id", "pattern")):
            raise ToolError("INVALID_ARGUMENT", "Each command rule needs a nonempty id and pattern.")
        try:
            re.compile(rule["pattern"])
        except (re.error, OverflowError, RecursionError) as exc:
            raise ToolError("INVALID_ARGUMENT", "Command rule pattern must be a valid regular expression.") from exc
        # Additional rules may tighten a built-in rule but never disable it.
        if (rule["id"], rule["pattern"]) not in rules:
            rules.append((rule["id"], rule["pattern"]))
    return Config(path, state, startup, resolved_roots, tuple(patterns), tuple(rules), **numbers)
