# terminal-mcp

`terminal-mcp` is a **local, session-oriented MCP server** for macOS and Linux. It gives an agent a persistent terminal process that it can start, read incrementally, write to, inspect, and terminate over stdio.

It is deliberately **not** a one-shot command wrapper. A session has its own process group, working directory, filtered environment, bounded output buffer, absolute byte cursor, status, and cleanup boundary. That makes it suitable for builds, polling long-running jobs, and interactive REPLs.

> **Tool contract authority:** a connected MCP client's `tools/list` response is authoritative for each tool's JSON schema, descriptions, defaults, and output schema. This README explains the shipped behavior and common flows; use `tools/list` when a client needs the exact contract.

## Requirements

- **macOS or Linux**. Windows is not supported.
- **Python 3.12**.
- **[uv](https://docs.astral.sh/uv/)**.
- Permission to run local processes as the current user in the selected allowed root.

## Quick start

Clone the repository, install the locked environment, then run the stdio server. Keep standard output reserved for MCP traffic; the server is intended to be launched by an MCP client, not used as an interactive shell command.

```bash
git clone https://github.com/arnow117/terminal-mcp.git
cd terminal-mcp
uv sync --locked
uv run terminal-mcp --allowed-root "$(pwd)"
```

The command supports exactly these flags:

```text
terminal-mcp [--config CONFIG_PATH] [--state-dir STATE_DIR]
             [--allowed-root ALLOWED_ROOTS]
```

| Flag | Behavior |
|---|---|
| `--config PATH` | Read local TOML configuration from `PATH`. A missing default config file is allowed. |
| `--state-dir PATH` | Override the directory holding the audit log and per-instance session registry. |
| `--allowed-root PATH` | Allow a realpath root for session `cwd`; repeat the flag for several roots. When present, CLI roots override TOML roots. |

`--allowed-root` is repeatable even though the help display shows one placeholder. All paths must resolve to existing directories. If no CLI or TOML root is supplied, the server startup directory is the allowed root and default `cwd`. If startup occurs from `/`, it instead uses `$HOME` and writes a warning to stderr.

## The five session tools

All successful calls return structured content as well as a short text summary. Expected failures return `isError: true` and a structured `error` object. The fields below are the important fields to save and reuse.

### `terminal_start`

Create one independent local session. `command: null` (the default) starts the filtered `$SHELL -l`, falling back to `/bin/sh -l`; a string is run as `/bin/sh -c <command>`.

| Key parameters | Defaults / limits |
|---|---|
| `command`, `cwd`, `env` | `command=null`; `cwd=null` means server startup directory; `env=null` inherits a filtered environment. Command is 1–32,768 UTF-8 bytes when supplied. `cwd` must realpath inside an allowed root. |
| `mode` | `pty` by default; choose `pipe` for separate stdout/stderr. PTY has a controlling terminal and merged output. |
| `cols`, `rows` | `80`, `24`; valid ranges are 20–500 and 5–200. They are recorded but not applied in pipe mode. |
| `timeout_s`, `name` | Timeout defaults to configured `default_timeout_s` (3,600 seconds by default); `name=null` is an optional display name of up to 128 characters. |
| `wait_ms`, `until`, `strip_ansi` | `0`, `null`, `true`. Waiting is capped at 30,000 ms. With no `until`, a wait returns on new output; with `until`, it returns on a regex match, exit, or deadline. |

Key return fields are `session_id`, `pid`, `pgid`, `mode`, `status`, `exit_code`, `signal`, `started_at`, output fields, `next_cursor`, `dropped_bytes`, `has_more`, and `wait_reason`.

### `terminal_read`

Read incrementally from the session's absolute raw-byte cursor.

| Key parameters | Defaults / limits |
|---|---|
| `session_id`, `cursor` | Both are required. Start at `cursor=0`, then pass back the previous `next_cursor`. Cursor must be from 0 through the current output end. |
| `max_bytes` | `65,536`; 4–1,048,576 raw bytes. UTF-8 boundaries can reduce the displayed amount. |
| `wait_ms`, `until`, `strip_ansi` | `0`, `null`, `true`; same wait semantics as `terminal_start`. `until` is a Python regular expression over new, unstripped output. |

Returns `session_id`, `status`, `exit_code`, `signal`, `ended_at`, `output` **or** `stdout` and `stderr`, `next_cursor`, `dropped_bytes`, `has_more`, and `wait_reason`. PTY mode uses `output` and makes `stdout`/`stderr` null; pipe mode makes `output` null and separates the streams. When a 1 MiB ring buffer overwrites an old cursor, `dropped_bytes` is positive; continue from `next_cursor`.

### `terminal_write`

Write text, then named keys, to a running session. Text never adds a newline automatically.

| Key parameters | Defaults / limits |
|---|---|
| `session_id` | Required. The session must still be `running`. |
| `text`, `keys` | `text=""`, `keys=[]`; at least one must be nonempty and their encoded total is at most 65,536 bytes. Named keys are `Enter`, `Tab`, `Backspace`, `Esc`, `C-c`, `C-d`, `Up`, `Down`, `Left`, and `Right`. |
| `wait_ms`, `until`, `strip_ansi` | `0`, `null`, `true`; wait only for the response after the write. |

Returns `written_bytes` plus `session_id`, status/exit fields, output fields, `next_cursor`, `dropped_bytes`, `has_more`, and `wait_reason`. PTYs normally echo input. `C-c` is terminal input, not a whole-session cleanup request; use `terminal_kill` to terminate a process group.

### `terminal_list`

Inspect sessions owned by the current server process.

| Key parameters | Defaults / limits |
|---|---|
| `session_id` | `null`; provide a UUID to query one retained session. A supplied ID is returned regardless of `include_exited`. |
| `include_exited` | `true`; `false` lists only `running` sessions. Exited and lost records normally remain for 600 seconds. |

Returns `count` and `sessions`. Each session summary contains `session_id`, `name`, `command`, `cwd`, `mode`, `pid`, `pgid`, `status`, `exit_code`, `signal`, `started_at`, `ended_at`, `last_activity_at`, `output_start_cursor`, `output_end_cursor`, `output_total_bytes`, and `timeout_s`.

### `terminal_kill`

Terminate the entire Unix process group, not only its parent process.

| Key parameters | Defaults / limits |
|---|---|
| `session_id` | Required. |
| `signal`, `grace_ms` | `TERM`, `1,000`; signal is one of `TERM`, `INT`, `HUP`, `KILL`. Grace is 0–30,000 ms and is ignored for immediate `KILL`. A surviving group is escalated to `KILL`. |
| `remove` | `false`; when true, delete retained output and registry metadata after reaping. Future reads/list calls for the ID return `SESSION_NOT_FOUND`. |

Returns `session_id`, final `status`, `exit_code`, `signal`, `termination_reason`, `escalated`, `removed`, and `ended_at`. It is idempotent for a normally exited session. A `lost` session cannot be killed by the new server.

## Typical call sequences

### Run and collect

1. Call `terminal_start` with a command and, if desired, `mode: "pipe"`.
2. Save `session_id` and `next_cursor` from the result.
3. Call `terminal_read` with that `session_id`, `cursor: next_cursor`, and an appropriate `wait_ms` until `status` is no longer `running`.
4. Read `exit_code` or `signal`; use the returned `next_cursor` for every subsequent page.

For a short command, `terminal_start` can include `wait_ms`; use `until` when the caller must wait for exit rather than merely the first output.

### Poll a long task with a cursor

1. Start the task and retain its `next_cursor`.
2. Repeatedly call `terminal_read(session_id, cursor=next_cursor, wait_ms=30000)`.
3. On every result, update the saved cursor to `next_cursor`. If `has_more` is true, read again immediately with that cursor; if `dropped_bytes` is positive, the old buffer region is unavailable and the new cursor is the recovery point.
4. Stop polling after a non-`running` status, then inspect the final exit fields.

A cancelled long-poll cancels only that request; it does not kill the session.

### Drive an interactive REPL

1. Start `python3 -i` in the default `pty` mode, optionally with `wait_ms` and `until: ">>> "` to wait for the prompt.
2. Call `terminal_write` with `text: "6 * 7"`, `keys: ["Enter"]`, and `until: ">>> "`.
3. Inspect the returned output for `42` and save `next_cursor`.
4. Use `keys: ["C-c"]` to interrupt a foreground command in a PTY, or use `terminal_read` to collect later output.

### Kill and clean up

Call `terminal_kill(session_id, signal="TERM", grace_ms=1000)`. It terminates the owned process group and escalates if necessary. Use `remove: true` after collection when the retained record/output is no longer needed. The server also cleans up all sessions when its stdio connection reaches EOF.

## Statuses, errors, and recovery

### Session statuses

| Status | Meaning | Suggested next step |
|---|---|---|
| `running` | Process group accepts normal session operations. | Read with the last cursor, write input, or kill it. |
| `exited` | Leader exited normally; retained output remains temporarily. | Read remaining output and inspect `exit_code`; writes are rejected. |
| `signaled` | Leader ended from a signal. | Inspect `signal` and retained output; start a new session if needed. |
| `timed_out` | Configured hard timeout triggered cleanup. | Read retained output, adjust `timeout_s` within config limits, then start again. |
| `failed_to_start` | Parent-side process setup failed; retained identity is returned with the error. | Inspect the local reason and correct configuration or OS conditions. |
| `lost` | A previous server process no longer owns the recorded session after restart handling. | Query its retained record if present; do not write/kill it—start a new session. |

### Stable error codes

| Code | Recovery hint |
|---|---|
| `INVALID_ARGUMENT` | Correct arguments, use the previous `next_cursor`, and remain within documented limits. |
| `SESSION_NOT_FOUND` | Use `terminal_list(include_exited=true)` while retention lasts; do not reuse expired IDs. |
| `SESSION_EXITED` | Read retained output or create a new session. |
| `SESSION_LOST` | The original server no longer owns it; query the record or create a new session. |
| `POLICY_DENIED` | Check the returned rule ID; choose an allowed directory or safe command. MVP has no override prompt. |
| `LIMIT_REACHED` | Kill/remove unused sessions or ask the local user to adjust the configured limit. |
| `START_FAILED` | Check the local OS reason. A missing shell command typically instead produces shell output and exit code 127. |
| `INTERNAL_ERROR` | Retry once; if it persists, retain the timestamp and `session_id` for local audit inspection. |

## Configuration and local state

Configuration is a flat TOML document. Omitted keys use the defaults below; unknown keys are rejected. CLI `--allowed-root` values override TOML `allowed_roots`.

```toml
# Existing, real directories only. Required only when overriding the startup directory.
allowed_roots = ["/workspace"]

# These extend the built-in sensitive-name filters; built-ins are never removed.
env_deny_patterns = ["CUSTOM_SECRET_*"]

# Additional rules may tighten command policy; built-ins are never disabled.
dangerous_command_rules = [
  { id = "no-example", pattern = "example-dangerous-command" },
]

max_sessions = 16          # integer 1..16
buffer_bytes = 1048576     # integer 4..1048576
default_timeout_s = 3600   # integer 1..86400
max_timeout_s = 86400      # integer 1..86400; must be >= default_timeout_s
kill_grace_ms = 1000       # integer 0..30000
exit_retention_s = 600     # integer 0..600
```

| Setting | Default behavior |
|---|---|
| `allowed_roots` | Startup directory realpath when neither the CLI nor TOML supplies roots (or `$HOME` if startup directory is `/`). |
| `env_deny_patterns` | Built-in patterns `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*_KEY`, and `AWS_*`; configured patterns are additive. Explicit sensitive names are denied. |
| `dangerous_command_rules` | Built-ins reject `rm -rf /`, `mkfs`, `dd` to `/dev`, power-control commands, the fork bomb, and downloading directly into a shell. Custom `{ id, pattern }` entries are additive. |
| `max_sessions` | 16 concurrently running sessions. |
| `buffer_bytes` | 1,048,576 bytes (1 MiB) per session output ring. |
| `default_timeout_s` / `max_timeout_s` | 3,600 / 86,400 seconds. Tool timeouts cannot exceed the configured maximum. |
| `kill_grace_ms` | 1,000 ms before escalation to `KILL`. |
| `exit_retention_s` | 600 seconds for exited or lost records and readable output. |

Default filesystem locations:

| Platform | Config path | State directory |
|---|---|---|
| macOS | `$HOME/Library/Application Support/terminal-mcp/config.toml` | `$HOME/Library/Application Support/terminal-mcp` |
| Linux | `${XDG_CONFIG_HOME:-$HOME/.config}/terminal-mcp/config.toml` | `${XDG_STATE_HOME:-$HOME/.local/state}/terminal-mcp` |

The state directory contains `audit.jsonl` (append-only metadata-only audit records) and per-instance registry snapshots at `sessions/<owner-pid>-<instance-id>.json`. Audit records omit output bodies and store tool/session metadata, parameter summaries, policy decisions, and result fields. State/config directories are created only when the server needs to persist a registry or audit event.

### Security boundary

`terminal-mcp` provides **best-effort guardrails, not a security sandbox**. It runs commands as the current user. Allowed-root checks, sensitive-environment filtering, dangerous-command rules, resource limits, and audit records help prevent accidental agent misuse and data leakage, but they do not confine a malicious command or fully inspect arbitrary interactive input. Do not expose it to untrusted agents, and use OS/container isolation when a stronger boundary is required.

## Lifecycle notes

- Sessions belong to **one server process and its stdio connection**. They are not a daemon and cannot be reattached after client disconnect.
- Stdio EOF, normal shutdown, handler failure, or cancellation triggers cleanup of all sessions owned by that server process.
- Each session has an independent Unix process group. Cleanup targets the group, including children.
- On a later server start, per-instance registry data is checked conservatively. Confirmed leftover process groups are cleaned and old records become `lost`; they are never adopted by the new server.
- Exited and lost sessions normally remain queryable/readable for 600 seconds. `remove: true` or retention expiry removes them.

## MCP client registration

Choose an allowed root deliberately. In the examples, replace `<repo>` with the cloned repository directory and `<dir>` with an existing directory the server may use for session `cwd` values. No credentials or secret environment variables belong in these registrations.

### Local cloned repository

The local command form is:

```bash
uv run --directory <repo> terminal-mcp --allowed-root <dir>
```

For **Manus**, create a local/custom MCP stdio server and set its command to the form above. Use `uv` as the command and split the rest into arguments if the UI has separate fields:

```text
command: uv
args: run --directory <repo> terminal-mcp --allowed-root <dir>
```

For **Claude Code**:

```bash
claude mcp add terminal-mcp -- uv run --directory <repo> terminal-mcp --allowed-root <dir>
```

For **Codex**:

```bash
codex mcp add terminal-mcp -- uv run --directory <repo> terminal-mcp --allowed-root <dir>
```

Generic `mcpServers` JSON:

```json
{
  "mcpServers": {
    "terminal-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "<repo>",
        "terminal-mcp",
        "--allowed-root",
        "<dir>"
      ]
    }
  }
}
```

### Public Git repository through `uvx`

After the public repository is available, the equivalent command is:

```bash
uvx --from git+https://github.com/arnow117/terminal-mcp terminal-mcp --allowed-root <dir>
```

For a generic JSON registration, replace the command and arguments with:

```json
{
  "mcpServers": {
    "terminal-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/arnow117/terminal-mcp",
        "terminal-mcp",
        "--allowed-root",
        "<dir>"
      ]
    }
  }
}
```

## Stories and documented flows

The product stories are defined in [`.sdlc-v1/context/REQ-001.product.md`](.sdlc-v1/context/REQ-001.product.md).

| Story | Documented flow |
|---|---|
| US1 — run and collect | Use `terminal_start`, save `session_id`/`next_cursor`, then `terminal_read` to terminal status and exit fields. |
| US2 — long task output | Use the cursor polling flow; update `next_cursor`, handle `has_more`, `dropped_bytes`, and long-poll `wait_ms`. |
| US3 — interactive control | Start the REPL in `pty`, write text plus `Enter`, wait using `until`, and use named keys such as `C-c`. |
| US4 — terminate and clean up | Use `terminal_kill` for a process group; understand automatic cleanup on stdio EOF. |
| US5 — session isolation | Supply distinct allowed `cwd` and permitted `env` values per `terminal_start`; inspect with `terminal_list`. |
| US6 — policy and audit | Configure roots/rules, recover from `POLICY_DENIED`, and inspect metadata-only `audit.jsonl`. |
| US7 — restart recovery | Understand that a restarted server cleans confirmed leftovers and reports prior records as `lost`, not adoptable. |
| US8 — manual Manus E2E | Register the local stdio server in Manus, ask the agent to start `python3 -i`, evaluate an expression, read the result, and kill the session. This is a manual end-to-end check, not a substitute for the automated tests. |

## Development and testing

Use the locked dependency set:

```bash
uv sync --locked
uv run pytest -q
```

Confirm the installed CLI flags at any time with:

```bash
uv run terminal-mcp --help
```

For restricted or offline environments with a prepared uv cache, use the matching `--offline` option, for example `uv run --offline pytest -q`.

## License

This project is licensed under the [MIT License](LICENSE).
