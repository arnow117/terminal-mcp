"""Lifecycle tests only signal children created by their own SessionManager."""

import asyncio
from dataclasses import replace
import os
import shlex
import sys
import time

import anyio
import pytest

from terminal_mcp.contracts import ToolError
from terminal_mcp.session import SessionManager
import terminal_mcp.session as session_module

pytestmark = pytest.mark.anyio
GROUP_COMMAND = "sleep 300 & a=$!; sleep 300 & b=$!; printf '%s %s %s\\n' $$ \"$a\" \"$b\"; wait"


def python_command(code):
    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.fixture
async def manager(config):
    instance = SessionManager(config)
    try:
        with anyio.fail_after(10):
            yield instance
    finally:
        with anyio.fail_after(4, shield=True):
            await instance.shutdown_all()


async def assert_pids_gone(pids, deadline):
    while True:
        live = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            live.append(pid)
        if not live:
            return
        assert time.monotonic() < deadline, f"Owned child PIDs still exist: {live}"
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_kill_reaps_whole_process_group(manager, mode):
    started = await manager.start(command=GROUP_COMMAND, mode=mode, wait_ms=2000, until=r"\d+ \d+ \d+")
    pids = [int(part) for part in (started["output"] or started["stdout"]).split()]
    deadline = time.monotonic() + 2
    result = await manager.kill(started["session_id"])
    assert result["status"] == "signaled"
    assert result["signal"] == "SIGTERM"
    assert result["termination_reason"] == "manual"
    assert not result["escalated"]
    await assert_pids_gone(pids, deadline)
    session = manager.sessions[started["session_id"]]
    assert session.master_fd is None
    assert session.reaper_task.done()
    assert all(task.done() for task in session.reader_tasks)
    with pytest.raises(ChildProcessError):
        os.waitpid(started["pid"], os.WNOHANG)


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_timeout_kills_group(manager, mode):
    started = await manager.start(command=GROUP_COMMAND, mode=mode, timeout_s=1, wait_ms=2000, until=r"\d+ \d+ \d+")
    pids = [int(part) for part in (started["output"] or started["stdout"]).split()]
    result = await manager.read(started["session_id"], cursor=started["next_cursor"], wait_ms=3000, until="absent")
    assert result["status"] == "timed_out"
    await assert_pids_gone(pids, time.monotonic() + 2)
    killed = await manager.kill(started["session_id"])
    assert killed["termination_reason"] == "timeout"


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_kill_escalates_to_sigkill(manager, mode):
    code = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(300)"
    started = await manager.start(command=python_command(code), mode=mode, wait_ms=2000, until=r"\d+")
    before = time.monotonic()
    result = await manager.kill(started["session_id"], grace_ms=100)
    assert result["escalated"]
    assert result["status"] == "signaled"
    assert result["signal"] == "SIGKILL"
    assert time.monotonic() - before >= 0.09
    await assert_pids_gone([started["pid"]], before + 2)


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_kill_reaps_group_after_parent_exit(manager, mode, tmp_path):
    ready = tmp_path / "ready"
    child_code = f"import os,signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); open({str(ready)!r},'w').write(str(os.getpid())); time.sleep(300)"
    parent_code = f"import subprocess,time; from pathlib import Path; subprocess.Popen([{sys.executable!r},'-c',{child_code!r}]);\nwhile not Path({str(ready)!r}).exists(): time.sleep(.01)\nprint('parent exiting',flush=True)"
    started = await manager.start(command=python_command(parent_code), mode=mode, wait_ms=2000, until="never-matches")
    assert started["status"] == "exited"
    child = int(ready.read_text())
    os.kill(child, 0)
    before = time.monotonic()
    result = await manager.kill(started["session_id"])
    assert result["status"] == "exited"
    assert result["exit_code"] == 0
    await assert_pids_gone([started["pid"], child], before + 2)
    assert manager.sessions[started["session_id"]].master_fd is None


async def test_shutdown_all_kills_every_group(manager):
    started = await asyncio.gather(*(manager.start(command=GROUP_COMMAND, mode=mode, wait_ms=2000, until=r"\d+ \d+ \d+") for mode in ("pty", "pipe")))
    pids = [int(part) for result in started for part in (result["output"] or result["stdout"]).split()]
    before = time.monotonic()
    await manager.shutdown_all()
    await assert_pids_gone(pids, before + 2)
    assert all(session.reaper_task.done() for session in manager.sessions.values())
    await manager.shutdown_all()


async def test_kill_idempotence_remove_and_signal_mapping(manager):
    started = await manager.start(command="printf retained", wait_ms=2000, until="never-matches")
    result = await manager.kill(started["session_id"])
    assert result["status"] == "exited" and result["exit_code"] == 0
    assert not result["escalated"]
    assert "retained" in (await manager.read(started["session_id"], cursor=0))["output"]
    removed = await manager.kill(started["session_id"], remove=True)
    assert removed["removed"]
    with pytest.raises(ToolError) as error:
        await manager.list(session_id=started["session_id"])
    assert error.value.code == "SESSION_NOT_FOUND"
    for sig in ("HUP", "INT", "KILL"):
        result = await manager.start(command=python_command("import time; print('ready',flush=True); time.sleep(300)"), mode="pipe", wait_ms=2000, until="ready")
        killed = await manager.kill(result["session_id"], signal=sig)
        assert killed["status"] == "signaled"
        assert killed["signal"] == f"SIG{sig}"
        assert not killed["escalated"]


async def test_retention_gc_and_capacity_release(config):
    assert config.exit_retention_s == 600
    manager = SessionManager(replace(config, exit_retention_s=1, max_sessions=1))
    try:
        first = await manager.start(command="printf retained", wait_ms=1000, until="never-matches")
        second = await manager.start(command="sleep 10", mode="pipe")
        assert (await manager.list())["count"] == 2
        assert "retained" in (await manager.read(first["session_id"], cursor=0))["output"]
        with anyio.fail_after(2):
            while first["session_id"] in manager.sessions:
                await asyncio.sleep(0.02)
        with pytest.raises(ToolError) as error:
            await manager.read(first["session_id"], cursor=0)
        assert error.value.code == "SESSION_NOT_FOUND"
        assert (await manager.list(session_id=second["session_id"]))["sessions"][0]["status"] == "running"
    finally:
        with anyio.fail_after(4, shield=True):
            await manager.shutdown_all()


async def test_spawn_failure_is_retained(manager, monkeypatch):
    def failed_fork():
        raise OSError("Synthetic fork failure")

    monkeypatch.setattr(session_module.pty, "fork", failed_fork)
    with pytest.raises(ToolError) as error:
        await manager.start(command="true")
    assert error.value.code == "START_FAILED"
    assert error.value.status == "failed_to_start"
    session_id = error.value.session_id
    assert (await manager.list(session_id=session_id))["sessions"][0]["status"] == "failed_to_start"
    killed = await manager.kill(session_id, remove=True)
    assert killed["status"] == "failed_to_start" and killed["removed"]


async def test_pty_exec_failure_is_127(manager):
    result = await manager.start(env={"SHELL": "/terminal-mcp-nonexistent-shell"}, wait_ms=2000, until="never-matches")
    assert result["status"] == "exited"
    assert result["exit_code"] == 127


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_natural_exit_closes_resources_and_never_resignals(manager, mode, monkeypatch):
    result = await manager.start(command="printf complete", mode=mode, wait_ms=2000, until="never-matches")
    session = manager.sessions[result["session_id"]]
    with anyio.fail_after(1):
        while not session.group_cleaned:
            await asyncio.sleep(0.01)
    assert session.master_fd is None
    assert session.reaper_task.done()
    assert all(task.done() for task in session.reader_tasks)
    real_killpg = os.killpg
    sent = []

    def observe(pgid, signal):
        if pgid == session.pgid and signal:
            sent.append(signal)
        return real_killpg(pgid, signal)

    monkeypatch.setattr(session_module.os, "killpg", observe)
    await manager.kill(result["session_id"])
    assert sent == []


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_immediate_kill_reaps_child(manager, mode):
    for _ in range(5):
        started = await manager.start(command="exec sleep 300", mode=mode)
        result = await manager.kill(started["session_id"], signal="KILL", remove=True)
        assert result["status"] == "signaled"
        assert result["signal"] == "SIGKILL"
        await assert_pids_gone([started["pid"]], time.monotonic() + 1)


async def test_cancelled_kill_wait_still_cleans_group(manager):
    code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(300)"
    started = await manager.start(command=python_command(code), mode="pipe", wait_ms=2000, until="ready")
    waiter = asyncio.create_task(manager.kill(started["session_id"], grace_ms=100))
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    result = await manager.kill(started["session_id"])
    assert result["escalated"]
    assert result["signal"] == "SIGKILL"
    await assert_pids_gone([started["pid"]], time.monotonic() + 1)


async def test_transient_eperm_still_requires_group_disappearance(manager, monkeypatch):
    started = await manager.start(command="printf ready; sleep 0.1", mode="pipe", wait_ms=2000, until="ready")
    real_killpg = os.killpg
    denied = False

    def transient_permission(pgid, signal):
        nonlocal denied
        if pgid == started["pgid"] and signal and not denied:
            denied = True
            raise PermissionError("Synthetic transient exit race")
        return real_killpg(pgid, signal)

    monkeypatch.setattr(session_module.os, "killpg", transient_permission)
    result = await manager.kill(started["session_id"])
    assert denied
    assert result["status"] == "exited"
    assert not result["escalated"]
    await assert_pids_gone([started["pid"]], time.monotonic() + 1)


async def test_stdio_eof_reaps_all_groups(tmp_path):
    from test_restart import connection, gone

    async with connection(tmp_path) as server:
        started = [await server.call("terminal_start", {
            "command": GROUP_COMMAND, "mode": mode, "wait_ms": 2000, "until": r"\d+ \d+ \d+",
        }) for mode in ("pty", "pipe")]
        pids = [int(part) for row in started for part in (row["output"] or row["stdout"]).split()]
        path, = (server.state / "sessions").glob("*.json")
        before = time.monotonic()
        # Close the actual stdin while the SDK transport is still active. The SDK's
        # fallback process-tree termination cannot make this assertion pass.
        await server.process.stdin.aclose()
        with anyio.fail_after(2):
            assert await server.process.wait() == 0
            await gone(pids, before + 2)
        assert time.monotonic() - before <= 2
        assert not path.exists()


async def test_stdio_two_second_timeout(tmp_path):
    from test_restart import connection, gone

    async with connection(tmp_path) as server:
        started = await server.call("terminal_start", {"command": GROUP_COMMAND, "mode": "pipe", "timeout_s": 2, "wait_ms": 1000, "until": r"\d+ \d+ \d+"})
        pids = [int(part) for part in started["stdout"].split()]
        result = await server.call("terminal_read", {"session_id": started["session_id"], "cursor": started["next_cursor"], "wait_ms": 4000, "until": "(?!)"})
        assert result["status"] == "timed_out"
        await gone(pids, time.monotonic() + 2)


@pytest.mark.parametrize("remove", [False, True])
async def test_kill_waits_for_pending_spawn(manager, monkeypatch, remove):
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._spawn

    async def delayed(session, prepared):
        entered.set()
        await release.wait()
        await original(session, prepared)

    monkeypatch.setattr(manager, "_spawn", delayed)
    starter = asyncio.create_task(manager.start(command="exec sleep 60", mode="pipe"))
    killer = None
    try:
        await entered.wait()
        session, = manager.sessions.values()
        killer = asyncio.create_task(manager.kill(session.session_id, remove=remove))
        await asyncio.sleep(.03)
        assert not killer.done(), "Kill must wait for spawn before final cleanup"
        release.set()
        started, killed = await asyncio.gather(starter, killer)
        assert killed["status"] != "running"
        assert killed["removed"] == remove
        await assert_pids_gone([started["pid"]], time.monotonic() + 2)
        assert (session.session_id not in manager.sessions) == remove
    finally:
        release.set()
        await asyncio.gather(starter, *([killer] if killer else []), return_exceptions=True)
        # Independent fallback cleanup makes RED runs safe even with broken ownership.
        if session.pid is not None:
            try:
                os.kill(session.pid, 9)
            except ProcessLookupError:
                pass
            await assert_pids_gone([session.pid], time.monotonic() + 2)


async def test_list_hides_pending_spawn(manager, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._spawn

    async def delayed(session, prepared):
        entered.set()
        await release.wait()
        await original(session, prepared)

    monkeypatch.setattr(manager, "_spawn", delayed)
    starter = asyncio.create_task(manager.start(command="exec sleep 60", mode="pipe"))
    try:
        await entered.wait()
        assert (await manager.list())["count"] == 0
        session, = manager.sessions.values()
        with pytest.raises(ToolError) as error:
            await manager.list(session.session_id)
        assert error.value.code == "SESSION_NOT_FOUND"
    finally:
        release.set()
        await starter


@pytest.mark.parametrize("phase", ["before_spawn", "after_spawn", "initial_read"])
async def test_cancelled_start_reaps_and_removes_session(manager, monkeypatch, phase):
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._spawn

    async def delayed(session, prepared):
        if phase != "before_spawn":
            await original(session, prepared)
        entered.set()
        await release.wait()
        if phase == "before_spawn":
            await original(session, prepared)

    if phase != "initial_read":
        monkeypatch.setattr(manager, "_spawn", delayed)
    starter = asyncio.create_task(manager.start(command="exec sleep 60", mode="pipe", wait_ms=10000))
    try:
        if phase == "initial_read":
            with anyio.fail_after(2):
                while not manager.sessions or not next(iter(manager.sessions.values())).spawn_task.done():
                    await asyncio.sleep(.01)
        else:
            await entered.wait()
        session, = manager.sessions.values()
        starter.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starter
        await session.spawn_task
        await assert_pids_gone([session.pid], time.monotonic() + 2)
        assert session.session_id not in manager.sessions
    finally:
        release.set()
        await asyncio.gather(starter, return_exceptions=True)


@pytest.mark.parametrize("action", ["kill", "eof", "timeout"])
@pytest.mark.parametrize("jobs", [1, 3])
async def test_stdio_interactive_job_groups_are_all_reaped(tmp_path, action, jobs):
    import re
    from test_restart import connection, gone

    pids = []
    try:
        async with connection(tmp_path, config_text="kill_grace_ms = 100\n") as server:
            started = await server.call("terminal_start", {
                "command": 'PS1="P> " exec /bin/bash --noprofile --norc -i',
                "mode": "pty", "timeout_s": 2 if action == "timeout" else 60,
                "wait_ms": 2000, "until": "P> ",
            })
            pids.append(started["pid"])
            await server.call("terminal_write", {
                "session_id": started["session_id"], "text": "set +H",
                "keys": ["Enter"], "wait_ms": 1000, "until": "P> ",
            })
            background = "; ".join("sleep 60 & echo BG:$!" for _ in range(jobs))
            foreground = python_command("import os,time; print('FG:'+str(os.getpid()),flush=True); time.sleep(60)")
            # Do not exec the foreground job in the interactive shell.
            foreground = foreground.removeprefix("exec ")
            page = await server.call("terminal_write", {
                "session_id": started["session_id"], "text": background + "; " + foreground,
                "keys": ["Enter"], "wait_ms": 2000, "until": r"FG:\d+",
            })
            pids.extend(int(pid) for pid in re.findall(r"(?:BG|FG):(\d+)", page["output"]))
            assert len(pids) == jobs + 2, page["output"]
            assert len({os.getpgid(pid) for pid in pids}) == jobs + 2
            assert all(os.getsid(pid) == started["pid"] for pid in pids)
            before = time.monotonic()
            if action == "kill":
                killed = await server.call("terminal_kill", {"session_id": started["session_id"], "grace_ms": 100})
                assert killed["status"] != "running"
            elif action == "eof":
                await server.process.stdin.aclose()
                with anyio.fail_after(2):
                    assert await server.process.wait() == 0
            else:
                page = await server.call("terminal_read", {
                    "session_id": started["session_id"], "cursor": page["next_cursor"],
                    "wait_ms": 4000, "until": "(?!)",
                })
                assert page["status"] == "timed_out"
                before = time.monotonic()
            await gone(pids, before + 2)
    finally:
        for pid in pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        await assert_pids_gone(pids, time.monotonic() + 2)


@pytest.mark.parametrize("mode", ["pty", "pipe"])
async def test_stdio_term_ignoring_process_escalates(tmp_path, mode):
    from test_restart import connection, gone

    async with connection(tmp_path) as server:
        started = await server.call("terminal_start", {
            "command": python_command("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"),
            "mode": mode, "wait_ms": 2000, "until": "ready",
        })
        before = time.monotonic()
        killed = await server.call("terminal_kill", {"session_id": started["session_id"], "grace_ms": 500})
        assert killed["status"] == "signaled"
        assert killed["signal"] == "SIGKILL" and killed["escalated"]
        assert time.monotonic() - before >= .49
        await gone([started["pid"]], before + 2)


@pytest.mark.parametrize("action", ["shutdown", "watchdog", "cancel_kill"])
async def test_cleanup_coordinates_with_spawn_barrier(manager, monkeypatch, action):
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._spawn

    async def delayed(session, prepared):
        if action == "watchdog":
            await original(session, prepared)
        entered.set()
        await release.wait()
        if action != "watchdog":
            await original(session, prepared)

    monkeypatch.setattr(manager, "_spawn", delayed)
    starter = asyncio.create_task(manager.start(command="exec sleep 60", mode="pipe", timeout_s=1))
    cleanup = None
    try:
        await entered.wait()
        session, = manager.sessions.values()
        if action == "shutdown":
            cleanup = asyncio.create_task(manager.shutdown_all())
        elif action == "cancel_kill":
            cleanup = asyncio.create_task(manager.kill(session.session_id))
            await asyncio.sleep(.02)
            cleanup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cleanup
        else:
            await asyncio.sleep(1.05)
            assert session.termination_task is not None and not session.termination_task.done()
        assert not session.group_cleaned
        release.set()
        started = await starter
        if cleanup is not None:
            await asyncio.gather(cleanup, return_exceptions=True)
        await assert_pids_gone([started["pid"]], time.monotonic() + 2)
        with anyio.fail_after(2):
            await asyncio.shield(session.termination_task)
        if action == "watchdog":
            assert session.status == "timed_out"
        assert session.group_cleaned
    finally:
        release.set()
        await asyncio.gather(starter, *([cleanup] if cleanup else []), return_exceptions=True)


async def test_unknown_membership_keeps_ownership_until_retry(manager, monkeypatch):
    from terminal_mcp.procinfo import ProcessProbeError

    started = await manager.start(command="exec sleep 60", mode="pipe")
    session = manager.sessions[started["session_id"]]
    with monkeypatch.context() as scoped:
        def unknown(sid):
            raise ProcessProbeError("Synthetic incomplete membership scan")

        scoped.setattr(session_module, "session_process_groups", unknown)
        scoped.setattr(session_module.os, "killpg", lambda *args: pytest.fail("Unknown groups must not be signaled"))
        with pytest.raises(ProcessProbeError):
            await manager.kill(session.session_id, remove=True)
        assert not session.group_cleaned
        assert session.session_id in manager.sessions
        os.kill(session.pid, 0)
    killed = await manager.kill(session.session_id, remove=True)
    assert killed["status"] == "signaled"
    await assert_pids_gone([session.pid], time.monotonic() + 2)


async def test_parent_side_spawn_failure_reaps_without_self_deadlock(manager, monkeypatch):
    def fail(fd, blocking):
        raise OSError("Synthetic PTY parent setup failure")

    monkeypatch.setattr(session_module.os, "set_blocking", fail)
    with anyio.fail_after(2):
        with pytest.raises(ToolError) as error:
            await manager.start(command="exec sleep 60", mode="pty")
    assert error.value.code == "START_FAILED"
    session, = manager.sessions.values()
    assert session.status == "failed_to_start" and session.group_cleaned
    assert session.reaper_task.done()
    await assert_pids_gone([session.pid], time.monotonic() + 2)
