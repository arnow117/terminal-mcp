"""Exercise the session core with real, isolated Unix children, without MCP."""

import asyncio
from dataclasses import replace
import errno
import os
import shlex
import sys
import time

import anyio
from jsonschema import Draft202012Validator, FormatChecker
import pytest

from terminal_mcp.contracts import TOOL_SCHEMAS, ToolError
from terminal_mcp.session import SessionManager
import terminal_mcp.session as session_module

pytestmark = pytest.mark.anyio


def python_command(code):
    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def check_schema(tool, result):
    Draft202012Validator(TOOL_SCHEMAS[tool]["outputSchema"], format_checker=FormatChecker()).validate(result)


@pytest.fixture
async def manager(config):
    instance = SessionManager(config)
    try:
        with anyio.fail_after(10):
            yield instance
    finally:
        with anyio.fail_after(4, shield=True):
            await instance.shutdown_all()


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_echo_exit_and_retained_output(manager, mode):
    started = await manager.start(command="printf hello; exit 3", mode=mode, wait_ms=2000, until="never-matches")
    check_schema("terminal_start", started)
    assert started["status"] == "exited"
    assert started["exit_code"] == 3
    assert started["signal"] is None
    assert started["wait_reason"] == "exited"
    assert "hello" in (started["output"] if mode == "pty" else started["stdout"])
    listed = await manager.list(session_id=started["session_id"], include_exited=False)
    check_schema("terminal_list", listed)
    assert listed["sessions"][0]["exit_code"] == 3
    with pytest.raises(ToolError) as error:
        await manager.write(started["session_id"], text="no")
    assert error.value.code == "SESSION_EXITED"
    page = await manager.read(started["session_id"], cursor=0)
    check_schema("terminal_read", page)
    assert "hello" in (page["output"] if mode == "pty" else page["stdout"])


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_missing_command_returns_127(manager, mode):
    result = await manager.start(command="terminal_mcp_nonexistent_executable", mode=mode, wait_ms=2000, until="never-matches")
    assert result["status"] == "exited"
    assert result["exit_code"] == 127
    assert "terminal_mcp_nonexistent_executable" in (result["output"] or result["stderr"])


async def test_pty_has_controlling_tty_size_and_term(manager):
    code = "import os; print(os.isatty(1), os.getsid(0), os.getpgrp(), os.tcgetpgrp(0), os.environ['TERM'], os.get_terminal_size(1), flush=True)"
    result = await manager.start(command=f"test -t 1 && {python_command(code)}", cols=101, rows=37, wait_ms=2000, until="never-matches")
    assert result["exit_code"] == 0
    assert result["pgid"] == result["pid"]
    assert f"True {result['pid']} {result['pid']} {result['pid']} xterm-256color" in result["output"]
    assert "columns=101, lines=37" in result["output"]


async def test_default_shell_is_login_shell(manager, tmp_path):
    shell = tmp_path / "test-shell"
    shell.write_text("#!/bin/sh\nprintf 'argument=%s\\n' \"$1\"\n")
    shell.chmod(0o700)
    result = await manager.start(env={"SHELL": str(shell)}, wait_ms=5000, until="never-matches")
    assert "argument=-l" in result["output"], result
    assert result["exit_code"] == 0


async def test_pipe_separates_streams_and_has_no_tty(manager):
    result = await manager.start(command="printf out; printf err >&2; test ! -t 1", mode="pipe", wait_ms=2000, until="never-matches")
    assert result["output"] is None
    assert result["stdout"] == "out"
    assert result["stderr"] == "err"
    assert result["exit_code"] == 0
    assert result["next_cursor"] == 6


async def test_repl_until_matches_partial_line(manager):
    result = await manager.start(command=f"exec {shlex.quote(sys.executable)} -q -i", wait_ms=2000, until=">>> ")
    assert result["wait_reason"] == "until"
    result = await manager.write(result["session_id"], text="print(6*7)", keys=["Enter"], wait_ms=2000, until=">>> ")
    check_schema("terminal_write", result)
    assert "42" in result["output"]
    assert result["written_bytes"] == len(b"print(6*7)\r")
    assert result["wait_reason"] == "until"


async def test_ctrl_c_keeps_shell_running(manager, tmp_path):
    result = await manager.start(command="PS1='T02> ' exec /bin/sh -i", wait_ms=2000, until="T02> ")
    assert result["wait_reason"] == "until"
    session_id = result["session_id"]
    script = tmp_path / "foreground.py"
    script.write_text("import os, time\nprint('SLEEP_READY', os.getpid(), flush=True)\ntime.sleep(100)\n")
    result = await manager.write(session_id, text=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", keys=["Enter"], wait_ms=2000, until=r"SLEEP_READY [0-9]+")
    child = int(result["output"].split("SLEEP_READY ")[-1].split()[0])
    result = await manager.write(session_id, keys=["C-c"], wait_ms=2000, until="T02> ")
    assert result["status"] == "running"
    assert result["wait_reason"] == "until"
    with anyio.fail_after(2):
        while True:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)


async def test_until_uses_raw_text_and_is_not_limited_to_result_page(manager):
    code = "import os,time; os.write(1,b'abcdef\\x1b[31mPassword:'); time.sleep(3)"
    started = await manager.start(command=python_command(code), mode="pipe")
    result = await manager.read(started["session_id"], cursor=0, max_bytes=4, wait_ms=2000, until="\x1b\\[31mPassword:")
    assert result["wait_reason"] == "until"
    assert result["stdout"] == "abcd"
    assert result["next_cursor"] == 4
    assert result["has_more"]


async def test_wait_reasons_and_early_exit(manager):
    result = await manager.start(command="sleep 0.2; printf ready; sleep 0.3", mode="pipe")
    assert result["wait_reason"] == "immediate"
    session_id = result["session_id"]
    deadline = await manager.read(session_id, cursor=0, wait_ms=30, until="absent")
    assert deadline["wait_reason"] == "deadline"
    output = await manager.read(session_id, cursor=0, wait_ms=2000)
    assert output["wait_reason"] == "output"
    assert output["stdout"] == "ready"
    before = time.monotonic()
    exited = await manager.read(session_id, cursor=output["next_cursor"], wait_ms=10000, until="absent")
    assert exited["wait_reason"] == "exited"
    assert exited["status"] == "exited"
    assert time.monotonic() - before < 1


async def test_incremental_reads_and_concurrent_waiters(manager):
    started = await manager.start(command="printf first; sleep 0.2; printf second; sleep 1", mode="pipe", wait_ms=1000)
    assert started["stdout"] == "first"
    reads = await asyncio.gather(*(manager.read(started["session_id"], cursor=started["next_cursor"], wait_ms=1000) for _ in range(2)))
    assert reads[0] == reads[1]
    assert reads[0]["stdout"] == "second"
    assert reads[0]["next_cursor"] == 11


async def test_cancelled_wait_leaves_process_running(manager):
    started = await manager.start(command="sleep 10", mode="pipe")
    waiter = asyncio.create_task(manager.read(started["session_id"], cursor=0, wait_ms=10000))
    await asyncio.sleep(0.03)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    listed = await manager.list(session_id=started["session_id"])
    assert listed["sessions"][0]["status"] == "running"
    os.kill(started["pid"], 0)


async def test_wait_does_not_block_other_session_write(manager):
    quiet = await manager.start(command="sleep 10", mode="pipe")
    other = await manager.start(command="exec cat", mode="pipe")
    waiter = asyncio.create_task(manager.read(quiet["session_id"], cursor=0, wait_ms=10000))
    try:
        with anyio.fail_after(0.5):
            result = await manager.write(other["session_id"], text="hello\n", wait_ms=300, until="hello")
        assert result["stdout"] == "hello\n"
    finally:
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter


async def test_pty_partial_writes_and_eagain(manager, monkeypatch):
    code = "import os,tty; tty.setraw(0); os.write(1,b'READY'); data=b''\nwhile not data.endswith(b'\\n'): data += os.read(0,65536)\nos.write(1,str(len(data)).encode())"
    started = await manager.start(command=python_command(code), wait_ms=2000, until="READY")
    fd = manager.sessions[started["session_id"]].master_fd
    real_write = os.write
    calls = 0

    def partial_write(target, data):
        nonlocal calls
        if target == fd:
            calls += 1
            if calls == 1:
                raise BlockingIOError(errno.EAGAIN, "Temporarily unavailable")
            return real_write(target, data[:7])
        return real_write(target, data)

    monkeypatch.setattr(session_module.os, "write", partial_write)
    result = await manager.write(started["session_id"], text="x" * 4000 + "\n", wait_ms=2000, until="4001")
    assert result["written_bytes"] == 4001
    assert "4001" in result["output"]
    assert calls > 500


async def test_concurrent_writes_are_serial(manager):
    code = "import os; data=b''\nwhile len(data)<40002: data += os.read(0,65536)\nos.write(1,data)"
    started = await manager.start(command=python_command(code), mode="pipe")
    first = asyncio.create_task(manager.write(started["session_id"], text="a" * 20000 + "\n"))
    second = asyncio.create_task(manager.write(started["session_id"], text="b" * 20000 + "\n"))
    await asyncio.gather(first, second)
    result = await manager.read(started["session_id"], cursor=0, wait_ms=2000, until="never-matches")
    assert result["stdout"] == "a" * 20000 + "\n" + "b" * 20000 + "\n"


async def test_isolated_cwd_env_and_session_limit(config, tmp_path):
    manager = SessionManager(replace(config, max_sessions=2))
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    try:
        results = await asyncio.gather(*(manager.start(command='pwd; printf "%s" "$X"; sleep 10', cwd=str(path), env={"X": value}, mode="pipe", wait_ms=1000, until=value) for path, value in ((left, "LEFT"), (right, "RIGHT"))))
        assert str(left) in results[0]["stdout"] and "LEFT" in results[0]["stdout"]
        assert str(right) in results[1]["stdout"] and "RIGHT" in results[1]["stdout"]
        with pytest.raises(ToolError) as error:
            await manager.start(command="true")
        assert error.value.code == "LIMIT_REACHED"
        assert (await manager.list(include_exited=False))["count"] == 2
    finally:
        with anyio.fail_after(4, shield=True):
            await manager.shutdown_all()


async def test_wait_and_cursor_validation(manager):
    started = await manager.start(command="sleep 10")
    for arguments in ({"wait_ms": 30001}, {"wait_ms": -1}, {"until": "["}, {"cursor": -1}, {"cursor": 1}):
        with pytest.raises(ToolError) as error:
            await manager.read(started["session_id"], **({"cursor": 0} | arguments))
        assert error.value.code == "INVALID_ARGUMENT"


async def test_buffer_overflow_remains_bounded(config):
    manager = SessionManager(replace(config, buffer_bytes=128))
    try:
        started = await manager.start(command=python_command("import os; os.write(1,b'x'*20000+b'END')"), mode="pipe", wait_ms=2000, until="never-matches")
        page = await manager.read(started["session_id"], cursor=0)
        assert page["dropped_bytes"] == 20003 - 128
        assert page["stdout"].endswith("END")
        assert manager.sessions[started["session_id"]].buffer.retained_bytes == 128
    finally:
        with anyio.fail_after(4, shield=True):
            await manager.shutdown_all()


async def test_until_respects_pipe_arrival_order(manager):
    code = "import os,time; os.write(1,b'left'); time.sleep(.05); os.write(2,b'middle'); time.sleep(.05); os.write(1,b'right'); time.sleep(1)"
    started = await manager.start(command=python_command(code), mode="pipe", wait_ms=2000, until="leftmiddleright")
    assert started["wait_reason"] == "until"
    assert started["stdout"] == "leftright"
    assert started["stderr"] == "middle"


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_until_matches_final_utf8_replacement_at_eof(manager, mode):
    code = "import os,time; os.write(1,b'\\xe4'); time.sleep(.05)"
    started = await manager.start(command=python_command(code), mode=mode, wait_ms=2000, until="\ufffd")
    assert started["wait_reason"] == "until"
    assert "\ufffd" in (started["output"] or started["stdout"])


async def test_cancelled_write_response_wait_leaves_input_writable(manager):
    started = await manager.start(command="exec cat", mode="pipe")
    waiter = asyncio.create_task(manager.write(started["session_id"], text="first\n", wait_ms=10000, until="absent"))
    await asyncio.sleep(0.03)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    result = await manager.write(started["session_id"], text="second\n", wait_ms=1000, until="second")
    assert "second" in result["stdout"]
    assert result["status"] == "running"
