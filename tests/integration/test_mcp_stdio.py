"""Real MCP stdio contract tests; all processes and runtime paths are test-owned."""

import asyncio
import os
import shlex
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import anyio
from jsonschema import Draft202012Validator, FormatChecker
import pytest

from terminal_mcp.contracts import TOOL_SCHEMAS

pytestmark = pytest.mark.anyio
NEVER = r"(?!)"


def python_command(code):
    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"


async def call(client, tool, arguments=None, *, error=None):
    with anyio.fail_after(5):
        result = await client.call_tool(tool, arguments)
    assert result.is_error is (error is not None), result
    data = result.structured_content
    assert isinstance(data, dict)
    schema = TOOL_SCHEMAS.get(tool, TOOL_SCHEMAS["terminal_list"])["outputSchema"]
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    text = result.content[0].text
    assert text.strip()
    if error:
        assert data["error"]["code"] == error
        for key in ("code", "message", "hint"):
            assert data["error"][key] in text
    else:
        assert len(text) < 1024
        if "session_id" in data:
            assert data["session_id"] in text
            assert data["status"] in text
        else:
            assert str(data["count"]) in text
    return data


async def test_tools_list_publishes_exact_contracts(mcp_client):
    result = await mcp_client.list_tools()
    assert len(result.tools) == 5
    assert {tool.name for tool in result.tools} == set(TOOL_SCHEMAS)
    for tool in result.tools:
        expected = TOOL_SCHEMAS[tool.name]
        assert tool.input_schema == expected["inputSchema"]
        assert tool.output_schema == expected["outputSchema"]
        assert tool.description == expected["description"]
        assert tool.description.isascii() and len(tool.description) > 100
        for prop in tool.input_schema["properties"].values():
            assert prop["description"].isascii() and prop["description"]
        Draft202012Validator.check_schema(tool.input_schema)
        Draft202012Validator.check_schema(tool.output_schema)


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_start_waits_for_output_and_exit(mcp_client, mode):
    result = await call(mcp_client, "terminal_start", {
        "command": "echo hello", "mode": mode, "wait_ms": 2000, "until": NEVER,
    })
    assert result["session_id"]
    assert result["status"] == "exited" and result["exit_code"] == 0
    assert result["wait_reason"] == "exited"
    assert "hello" in (result["output"] if mode == "pty" else result["stdout"])


async def test_list_reports_nonzero_exit(mcp_client):
    started = await call(mcp_client, "terminal_start", {
        "command": "printf result; exit 3", "wait_ms": 2000, "until": NEVER,
    })
    result = await call(mcp_client, "terminal_list", {
        "session_id": started["session_id"], "include_exited": False,
    })
    assert result["count"] == 1
    assert result["sessions"][0]["status"] == "exited"
    assert result["sessions"][0]["exit_code"] == 3
    assert (await call(mcp_client, "terminal_list", {"include_exited": False}))["count"] == 0


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_missing_command_is_readable(mcp_client, mode):
    result = await call(mcp_client, "terminal_start", {
        "command": "terminal_mcp_nonexistent_executable", "mode": mode,
        "wait_ms": 2000, "until": NEVER,
    })
    assert result["status"] == "exited" and result["exit_code"] == 127
    assert "terminal_mcp_nonexistent_executable" in (result["output"] or result["stderr"])


async def test_incremental_read_has_no_duplicate_or_gap(mcp_client):
    code = "import time\nfor i in range(6):\n print(f'line-{i}', flush=True); time.sleep(.1)\ntime.sleep(1)"
    started = await call(mcp_client, "terminal_start", {"command": python_command(code), "mode": "pipe"})
    chunks = [started["stdout"]]
    cursor = started["next_cursor"]
    while "line-5\n" not in "".join(chunks):
        page = await call(mcp_client, "terminal_read", {
            "session_id": started["session_id"], "cursor": cursor, "max_bytes": 8, "wait_ms": 2000,
        })
        assert page["status"] == "running"
        assert page["dropped_bytes"] == 0
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
        chunks.append(page["stdout"])
    expected = "".join(f"line-{i}\n" for i in range(6))
    assert "".join(chunks) == expected
    assert cursor == len(expected.encode())
    page = await call(mcp_client, "terminal_read", {"session_id": started["session_id"], "cursor": cursor})
    assert page["stdout"] == "" and page["next_cursor"] == cursor


async def test_waiting_read_does_not_block_other_write(mcp_client):
    quiet = await call(mcp_client, "terminal_start", {"command": "exec sleep 60", "mode": "pipe"})
    other = await call(mcp_client, "terminal_start", {"command": "exec cat", "mode": "pipe"})
    entered = anyio.Event()

    async def wait():
        entered.set()
        await mcp_client.call_tool("terminal_read", {"session_id": quiet["session_id"], "cursor": 0, "wait_ms": 10000})

    async with anyio.create_task_group() as group:
        group.start_soon(wait)
        await entered.wait()
        await anyio.sleep(.05)
        with anyio.fail_after(.8):
            result = await call(mcp_client, "terminal_write", {
                "session_id": other["session_id"], "text": "hello\n", "wait_ms": 500, "until": "hello",
            })
        assert result["stdout"] == "hello\n"
        group.cancel_scope.cancel()
    listed = await call(mcp_client, "terminal_list", {"session_id": quiet["session_id"]})
    assert listed["sessions"][0]["status"] == "running"


async def test_old_cursor_reports_dropped_bytes(stdio_connection):
    async with stdio_connection("buffer_bytes = 128\n") as client:
        result = await call(client, "terminal_start", {
            "command": python_command("import os; os.write(1,b'x'*20000+b'END')"),
            "mode": "pipe", "wait_ms": 2000, "until": NEVER,
        })
        page = await call(client, "terminal_read", {"session_id": result["session_id"], "cursor": 0})
        assert page["dropped_bytes"] == 20003 - 128
        assert page["stdout"] == "x" * 125 + "END"
        assert page["next_cursor"] == 20003 and not page["has_more"]
        summary = (await call(client, "terminal_list", {"session_id": result["session_id"]}))["sessions"][0]
        assert summary["output_end_cursor"] - summary["output_start_cursor"] == 128
        assert summary["output_total_bytes"] == 20003


async def test_repl_write_and_until_prompt(mcp_client):
    started = await call(mcp_client, "terminal_start", {
        "command": f"exec {shlex.quote(sys.executable)} -q -i", "wait_ms": 2000, "until": ">>> ",
    })
    assert started["status"] == "running" and started["wait_reason"] == "until"
    result = await call(mcp_client, "terminal_write", {
        "session_id": started["session_id"], "text": "print(6*7)", "keys": ["Enter"],
        "wait_ms": 2000, "until": ">>> ",
    })
    assert "42\n" in result["output"]
    assert "print(6*7)" in result["output"]
    assert result["written_bytes"] == len(b"print(6*7)\r")
    assert result["wait_reason"] == "until" and result["status"] == "running"


async def test_ctrl_c_keeps_shell_running(mcp_client, tmp_path):
    started = await call(mcp_client, "terminal_start", {
        "command": "PS1='T03> ' exec /bin/sh -i", "wait_ms": 2000, "until": "T03> ",
    })
    script = tmp_path / "foreground.py"
    script.write_text("import os, time\nprint('SLEEP_READY', os.getpid(), flush=True)\ntime.sleep(100)\n")
    result = await call(mcp_client, "terminal_write", {
        "session_id": started["session_id"],
        "text": f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", "keys": ["Enter"],
        "wait_ms": 2000, "until": r"SLEEP_READY [0-9]+",
    })
    child = int(result["output"].split("SLEEP_READY ")[-1].split()[0])
    result = await call(mcp_client, "terminal_write", {
        "session_id": started["session_id"], "keys": ["C-c"], "wait_ms": 2000, "until": "T03> ",
    })
    assert result["status"] == "running" and result["wait_reason"] == "until"
    with anyio.fail_after(2):
        while True:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await anyio.sleep(.02)


async def test_pty_has_controlling_tty(mcp_client):
    code = "import os; print(os.isatty(0), os.isatty(1), os.getsid(0), os.getpgrp(), os.tcgetpgrp(0), flush=True)"
    result = await call(mcp_client, "terminal_start", {
        "command": f"test -t 1 && {python_command(code)}", "wait_ms": 2000, "until": NEVER,
    })
    assert result["mode"] == "pty" and result["exit_code"] == 0
    assert f"True True {result['pid']} {result['pid']} {result['pid']}" in result["output"]


async def test_pipe_returns_separate_stdout_and_stderr(mcp_client):
    result = await call(mcp_client, "terminal_start", {
        "command": "printf out; printf err >&2; test ! -t 1", "mode": "pipe",
        "wait_ms": 2000, "until": NEVER,
    })
    assert result["output"] is None and result["stdout"] == "out" and result["stderr"] == "err"
    assert result["exit_code"] == 0 and result["next_cursor"] == 6


async def test_exited_session_rejects_write_but_retains_output(mcp_client):
    started = await call(mcp_client, "terminal_start", {
        "command": "printf retained", "mode": "pipe", "wait_ms": 2000, "until": NEVER,
    })
    await call(mcp_client, "terminal_write", {"session_id": started["session_id"], "text": "no"}, error="SESSION_EXITED")
    result = await call(mcp_client, "terminal_read", {"session_id": started["session_id"], "cursor": 0})
    assert result["stdout"] == "retained" and result["status"] == "exited"
    for _ in range(2):
        result = await call(mcp_client, "terminal_kill", {"session_id": started["session_id"]})
        assert result["status"] == "exited" and not result["escalated"]


async def test_sessions_isolate_cwd_and_env(mcp_client, tmp_path):
    paths = [tmp_path / "left", tmp_path / "right"]
    for path in paths:
        path.mkdir()
    results = await asyncio.gather(*(call(mcp_client, "terminal_start", {
        "command": 'pwd; printf "%s" "$X"; sleep 60', "cwd": str(path), "env": {"X": value},
        "mode": "pipe", "wait_ms": 2000, "until": value,
    }) for path, value in zip(paths, ("LEFT", "RIGHT"))))
    for index, result in enumerate(results):
        assert result["stdout"] == f"{paths[index].resolve()}\n" + ("LEFT", "RIGHT")[index]
    assert results[0]["session_id"] != results[1]["session_id"]
    assert results[0]["pgid"] != results[1]["pgid"]


async def test_session_limit_preserves_existing_sessions(mcp_client):
    sessions = [await call(mcp_client, "terminal_start", {"command": "exec sleep 60", "mode": "pipe"}) for _ in range(16)]
    await call(mcp_client, "terminal_start", {"command": "true"}, error="LIMIT_REACHED")
    result = await call(mcp_client, "terminal_list", {"include_exited": False})
    assert result["count"] == 16
    assert {row["session_id"] for row in result["sessions"]} == {row["session_id"] for row in sessions}
    for row in result["sessions"]:
        assert row["status"] == "running"
        os.kill(row["pid"], 0)


async def test_until_matches_partial_line(mcp_client):
    result = await call(mcp_client, "terminal_start", {
        "command": python_command("import os,time; os.write(1,b'Password:'); time.sleep(60)"),
        "mode": "pipe", "wait_ms": 2000, "until": "Password:",
    })
    assert result["stdout"] == "Password:" and result["wait_reason"] == "until"
    assert result["status"] == "running"


async def test_wait_returns_early_when_process_exits(mcp_client):
    started = await call(mcp_client, "terminal_start", {"command": "sleep .2; exit 3", "mode": "pipe"})
    before = time.monotonic()
    result = await call(mcp_client, "terminal_read", {
        "session_id": started["session_id"], "cursor": 0, "wait_ms": 10000, "until": NEVER,
    })
    assert time.monotonic() - before < 2
    assert result["wait_reason"] == "exited" and result["status"] == "exited" and result["exit_code"] == 3


async def test_invalid_cursor_is_structured_error(mcp_client):
    started = await call(mcp_client, "terminal_start", {"command": "exec sleep 60", "mode": "pipe"})
    for cursor in (-1, 1):
        await call(mcp_client, "terminal_read", {"session_id": started["session_id"], "cursor": cursor}, error="INVALID_ARGUMENT")


@pytest.mark.parametrize("tool,arguments", [
    ("terminal_start", {"wait_ms": 30001}),
    ("terminal_start", {"cols": 19}),
    ("terminal_start", {"rows": 201}),
    ("terminal_start", {"until": "["}),
    ("terminal_start", {"unknown": True}),
    ("terminal_start", {"command": ""}),
    ("terminal_start", {"command": "x\x00"}),
    ("terminal_start", {"command": "\u4e2d" * 11000}),
    ("terminal_read", {}),
    ("terminal_read", {"session_id": "invalid", "cursor": 0}),
    ("terminal_read", {"session_id": str(uuid4()), "cursor": 0, "max_bytes": 3}),
    ("terminal_write", {"session_id": str(uuid4())}),
    ("terminal_write", {"session_id": str(uuid4()), "keys": ["Unknown"]}),
    ("terminal_write", {"session_id": str(uuid4()), "text": "ok", "unknown": 1}),
    ("terminal_list", {"include_exited": 1}),
    ("terminal_list", {"unknown": True}),
    ("terminal_kill", {"session_id": str(uuid4()), "grace_ms": -1}),
    ("terminal_kill", {"session_id": str(uuid4()), "signal": "STOP"}),
    ("terminal_kill", {"session_id": str(uuid4()), "unknown": 1}),
    ("unknown_tool", {}),
])
async def test_invalid_arguments_return_structured_errors(mcp_client, tool, arguments):
    await call(mcp_client, tool, arguments, error="INVALID_ARGUMENT")
    assert (await call(mcp_client, "terminal_list"))["count"] == 0


async def test_concurrent_reads_are_independent_and_writes_are_serial(mcp_client):
    code = "import os; data=b''\nwhile len(data)<40002: data += os.read(0,65536)\nos.write(1,data)"
    started = await call(mcp_client, "terminal_start", {"command": python_command(code), "mode": "pipe"})
    sid = started["session_id"]
    reads = [asyncio.create_task(call(mcp_client, "terminal_read", {
        "session_id": sid, "cursor": 0, "wait_ms": 2000, "until": NEVER,
    })) for _ in range(2)]
    try:
        await asyncio.gather(*(call(mcp_client, "terminal_write", {"session_id": sid, "text": char * 20000 + "\n"}) for char in "ab"))
        pages = await asyncio.gather(*reads)
    finally:
        for task in reads:
            task.cancel()
        await asyncio.gather(*reads, return_exceptions=True)
    assert pages[0] == pages[1]
    assert pages[0]["stdout"] == "a" * 20000 + "\n" + "b" * 20000 + "\n"
    assert pages[0]["next_cursor"] == 40002


async def test_kill_returns_final_state_and_remove_forgets_session(mcp_client):
    started = await call(mcp_client, "terminal_start", {"command": "exec sleep 60", "mode": "pipe"})
    result = await call(mcp_client, "terminal_kill", {"session_id": started["session_id"], "signal": "KILL", "remove": True})
    assert result["status"] == "signaled" and result["signal"] == "SIGKILL"
    assert result["removed"] and not result["escalated"]
    for tool, options in (("terminal_list", {}), ("terminal_read", {"cursor": 0}), ("terminal_kill", {})):
        await call(mcp_client, tool, {"session_id": started["session_id"], **options}, error="SESSION_NOT_FOUND")


async def test_start_failure_retains_structured_identity(mcp_client, tmp_path):
    failed = await call(mcp_client, "terminal_start", {
        "mode": "pipe", "env": {"SHELL": str(tmp_path / "missing-shell")},
    }, error="START_FAILED")
    assert failed["status"] == "failed_to_start"
    result = await call(mcp_client, "terminal_list", {"session_id": failed["session_id"]})
    assert result["sessions"][0]["status"] == "failed_to_start"
    assert result["sessions"][0]["pid"] is None


async def test_unexpected_failure_is_structured_without_details(caplog):
    from mcp import types
    from terminal_mcp.server import AppContext, on_call_tool

    manager = SimpleNamespace(list=AsyncMock(side_effect=RuntimeError("private-exception-payload")))
    context = SimpleNamespace(lifespan_context=AppContext(manager=manager))
    result = await on_call_tool(context, types.CallToolRequestParams(name="terminal_list", arguments={}))
    assert result.is_error
    assert result.structured_content["error"]["code"] == "INTERNAL_ERROR"
    Draft202012Validator(TOOL_SCHEMAS["terminal_list"]["outputSchema"]).validate(result.structured_content)
    for value in result.structured_content["error"].values():
        assert value in result.content[0].text
    assert "private-exception-payload" not in result.model_dump_json() + caplog.text


async def test_cancelled_lifespan_finishes_shutdown(config):
    from terminal_mcp.server import create_server

    finished = anyio.Event()

    async def shutdown():
        await anyio.sleep(.01)
        finished.set()

    with patch("terminal_mcp.server.SessionManager") as manager_type:
        manager_type.return_value.shutdown_all = AsyncMock(side_effect=shutdown)
        server = create_server(config)
        with anyio.CancelScope() as scope:
            async with server.lifespan(server):
                scope.cancel()
                await anyio.sleep(0)
        assert finished.is_set()
        manager_type.return_value.shutdown_all.assert_awaited_once()


@pytest.mark.parametrize("raise_in_lifespan", [False, True])
async def test_lifespan_always_shuts_down_manager(config, raise_in_lifespan):
    from terminal_mcp.server import create_server

    with patch("terminal_mcp.server.SessionManager") as manager_type:
        manager = manager_type.return_value
        manager.shutdown_all = AsyncMock()
        server = create_server(config)
        try:
            async with server.lifespan(server) as context:
                assert context.manager is manager
                if raise_in_lifespan:
                    raise RuntimeError("Test lifespan failure")
        except RuntimeError as exc:
            assert str(exc) == "Test lifespan failure"
        manager_type.assert_called_once_with(config)
        manager.shutdown_all.assert_awaited_once()
