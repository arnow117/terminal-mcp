"""Policy and audit acceptance tests over the real MCP stdio transport."""

import json
from types import SimpleNamespace

from mcp import types
import pytest

from terminal_mcp.contracts import ToolError
from terminal_mcp.server import AppContext, on_call_tool
from terminal_mcp.session import SessionManager
from test_restart import connection

pytestmark = pytest.mark.anyio


def events(state):
    return [json.loads(line) for line in (state / "audit.jsonl").read_text().splitlines()]


async def test_disallowed_cwd_starts_nothing(tmp_path):
    async with connection(tmp_path) as server:
        result = await server.call("terminal_start", {"command": "true", "cwd": "/etc"}, error="POLICY_DENIED")
        assert result["error"]["rule_id"] == "cwd-outside-roots"
        assert (await server.call("terminal_list"))["count"] == 0
        assert any(row["policy"] == {"decision": "deny", "rule_id": "cwd-outside-roots"} for row in events(server.state))


async def test_sensitive_env_is_not_inherited(tmp_path):
    async with connection(tmp_path, env={"GITHUB_TOKEN": "inherited-private-value"}) as server:
        result = await server.call("terminal_start", {"command": "env", "mode": "pipe", "wait_ms": 2000, "until": "(?!)"})
        assert "GITHUB_TOKEN" not in result["stdout"]
        assert "inherited-private-value" not in result["stdout"]
        await server.call("terminal_start", {"command": "true", "env": {"MY_PASSWORD": "explicit-private-value"}}, error="POLICY_DENIED")
        audit = (server.state / "audit.jsonl").read_text()
        assert "inherited-private-value" not in audit and "explicit-private-value" not in audit
        assert any(row["policy"]["decision"] == "filter" and row["policy"]["rule_id"] == "sensitive-env" for row in events(server.state))


async def test_dangerous_start_and_complete_write_are_denied_and_audited(tmp_path):
    async with connection(tmp_path) as server:
        await server.call("terminal_start", {"command": "rm -rf /"}, error="POLICY_DENIED")
        started = await server.call("terminal_start", {"command": "exec cat", "mode": "pipe"})
        sid = started["session_id"]
        await server.call("terminal_write", {"session_id": sid, "text": "rm -rf /\n"}, error="POLICY_DENIED")
        await server.call("terminal_write", {"session_id": sid, "text": "rm -"})
        await server.call("terminal_write", {"session_id": sid, "text": "rf /"})
        await server.call("terminal_write", {"session_id": sid, "keys": ["Enter"]}, error="POLICY_DENIED")
        page = await server.call("terminal_read", {"session_id": sid, "cursor": 0, "wait_ms": 100})
        assert "\n" not in page["stdout"] and "\r" not in page["stdout"]
        rows = events(server.state)
        denials = [row for row in rows if row["policy"] == {"decision": "deny", "rule_id": "rm-root"}]
        assert {row["tool"] for row in denials} == {"terminal_start", "terminal_write"}
        assert sum(row["command"] == "rm -rf /" for row in denials) >= 3


async def test_audit_has_required_metadata_without_output_body(tmp_path):
    async with connection(tmp_path) as server:
        result = await server.call("terminal_start", {"command": "exec cat", "mode": "pipe", "env": {"SAFE": "private-env-value"}})
        sid = result["session_id"]
        await server.call("terminal_write", {"session_id": sid, "text": "private-output-fragment", "wait_ms": 1000})
        await server.call("terminal_read", {"session_id": sid, "cursor": 0})
        await server.call("terminal_list")
        await server.call("terminal_kill", {"session_id": sid})
        await server.call("terminal_read", {"session_id": sid, "cursor": -1}, error="INVALID_ARGUMENT")
        rows = events(server.state)
        assert {row["tool"] for row in rows} >= {"terminal_start", "terminal_read", "terminal_write", "terminal_list", "terminal_kill"}
        required = {"timestamp", "tool", "session_id", "command", "cwd", "mode", "parameter_summary", "policy", "result", "duration_ms"}
        for row in rows:
            assert set(row) == required
            assert set(row["policy"]) == {"decision", "rule_id"}
            assert set(row["result"]) == {"status", "exit_code", "signal", "error_code"}
            assert row["duration_ms"] >= 0
        assert any(row["result"]["error_code"] == "INVALID_ARGUMENT" for row in rows)
        assert any(row["result"]["status"] == "signaled" for row in rows)
        raw = (server.state / "audit.jsonl").read_text()
        assert "private-env-value" not in raw and "private-output-fragment" not in raw
        assert all(key not in row for row in rows for key in ("output", "stdout", "stderr", "env"))
        assert (server.state / "audit.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("component", ["audit", "registry"])
async def test_persistence_failure_refuses_new_processes(config, monkeypatch, component):
    manager = SessionManager(config)
    try:
        await manager.initialize()
        def fail(event):
            raise OSError("Synthetic persistence failure")
        target, method = (manager.audit, "log") if component == "audit" else (manager.registry, "write")
        monkeypatch.setattr(target, method, fail)
        context = SimpleNamespace(lifespan_context=AppContext(manager))
        result = await on_call_tool(context, types.CallToolRequestParams(name="terminal_start", arguments={"command": "sleep 60"}))
        assert result.is_error and result.structured_content["error"]["code"] == "INTERNAL_ERROR"
        assert manager.sessions == {}
        monkeypatch.undo()
        # A transient repair does not silently clear the safety latch.
        with pytest.raises(ToolError) as error:
            await manager.start(command="sleep 60")
        assert error.value.code == "INTERNAL_ERROR"
        assert manager.sessions == {}
    finally:
        monkeypatch.undo()
        await manager.shutdown_all()


async def test_post_spawn_registry_failure_kills_unrecorded_process(config, monkeypatch):
    from terminal_mcp.procinfo import process_identity

    manager = SessionManager(config)
    try:
        await manager.initialize()
        original = manager.registry.write

        def fail_after_spawn(records):
            records = list(records)
            if any(row.leader_pid is not None for row in records):
                raise OSError("Synthetic post-spawn persistence failure")
            original(records)

        monkeypatch.setattr(manager.registry, "write", fail_after_spawn)
        with pytest.raises(ToolError) as error:
            await manager.start(command="exec sleep 60", mode="pipe")
        assert error.value.code == "INTERNAL_ERROR"
        session, = manager.sessions.values()
        assert session.pid is not None
        assert process_identity(session.pid) is None
        assert session.reaper_task.done()
        assert session.group_cleaned
    finally:
        monkeypatch.undo()
        await manager.shutdown_all()


async def test_cancelled_wait_is_audited_without_killing_session(tmp_path):
    import asyncio
    import anyio

    async with connection(tmp_path) as server:
        result = await server.call("terminal_start", {"command": "exec cat", "mode": "pipe"})
        sid = result["session_id"]
        waiter = asyncio.create_task(server.client.call_tool("terminal_read", {
            "session_id": sid, "cursor": 0, "wait_ms": 10000,
        }))
        await anyio.sleep(.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with anyio.fail_after(2):
            while not any(row["result"]["error_code"] == "CANCELLED" for row in events(server.state)):
                await anyio.sleep(.01)
        assert (await server.call("terminal_list", {"session_id": sid}))["sessions"][0]["status"] == "running"


async def test_audit_failure_does_not_block_owned_process_cleanup(config, monkeypatch):
    from terminal_mcp.procinfo import process_identity

    manager = SessionManager(config)
    try:
        started = await manager.start(command="exec sleep 60", mode="pipe")

        def fail(event):
            raise OSError("Synthetic unavailable audit storage")

        monkeypatch.setattr(manager.audit, "log", fail)
        context = SimpleNamespace(lifespan_context=AppContext(manager))
        result = await on_call_tool(context, types.CallToolRequestParams(
            name="terminal_kill", arguments={"session_id": started["session_id"]},
        ))
        assert result.is_error and result.structured_content["error"]["code"] == "INTERNAL_ERROR"
        assert process_identity(started["pid"]) is None
        assert manager.sessions[started["session_id"]].group_cleaned
    finally:
        monkeypatch.undo()
        await manager.shutdown_all()
