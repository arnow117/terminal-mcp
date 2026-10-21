"""Restart tests own every server, process group, and temporary runtime directory."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import mcp.client.stdio as transport
import pytest

from terminal_mcp.procinfo import ProcessProbeError, process_identity
from terminal_mcp.registry import Registry, RegistryRecord
from terminal_mcp.session import SessionManager
from test_mcp_stdio import call

pytestmark = pytest.mark.anyio
_CREATE_PROCESS = transport._create_platform_compatible_process


def identity(pid):
    value = process_identity(pid)
    return (value.pid, value.pgid, value.start_time) if value is not None else None


async def gone(pids, deadline):
    while any(identity(pid) is not None for pid in pids):
        assert time.monotonic() < deadline, f"Owned processes survived cleanup: {pids}"
        await anyio.sleep(.02)


@asynccontextmanager
async def connection(tmp_path, *, state=None, config_text="", env=None):
    """Use the official transport, but expose its owned process for EOF/SIGKILL tests."""
    runtime = tmp_path / str(uuid4())
    runtime.mkdir()
    config = runtime / "config.toml"
    config.write_text(config_text)
    state = state or runtime / "state"
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "terminal-mcp"),
        args=["--config", str(config), "--state-dir", str(state), "--allowed-root", str(tmp_path)],
        cwd=tmp_path,
        env={"HOME": os.environ["HOME"], "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
    )
    owned = []
    captured = []

    async def spawn(**kwargs):
        process = await _CREATE_PROCESS(**kwargs)
        captured.append(process)
        return process

    try:
        with (runtime / "stderr.log").open("w") as errlog, patch.object(transport, "_create_platform_compatible_process", side_effect=spawn):
            with anyio.fail_after(12):
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()

                        async def invoke(tool, arguments=None, **kwargs):
                            result = await call(client, tool, arguments, **kwargs)
                            if tool == "terminal_start" and "pid" in result:
                                owned.append(result["pgid"])
                            return result

                        yield SimpleNamespace(client=client, call=invoke, process=captured[-1], state=state, config=config)
    finally:
        # Failing assertions must never leave test-owned sessions behind.
        with anyio.fail_after(3, shield=True):
            for pgid in owned:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # Cleanup and acceptance probes use only test-owned PIDs.
            while owned:
                for pid in owned[:]:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        owned.remove(pid)
                if owned:
                    await anyio.sleep(.02)


async def test_restart_sweeps_matching_pipe_group_and_lists_lost(tmp_path):
    state = tmp_path / "shared"
    async with connection(tmp_path, state=state) as first:
        started = await first.call("terminal_start", {
            "command": "sleep 60 & a=$!; sleep 60 & b=$!; printf '%s %s %s\\n' $$ \"$a\" \"$b\"; wait",
            "mode": "pipe", "wait_ms": 2000, "until": r"\d+ \d+ \d+",
        })
        pids = [int(part) for part in started["stdout"].split()]
        files = list((state / "sessions").glob("*.json"))
        assert len(files) == 1
        old_file = files[0]
        snapshot = json.loads(old_file.read_text())
        assert snapshot["owner_pid"] == first.process.pid
        record = snapshot["records"][0]
        assert record["leader_start_time"] == identity(started["pid"])[2]
        first.process.kill()
        await first.process.wait()
        assert identity(started["pid"]) is not None
        async with connection(tmp_path, state=state) as second:
            await gone(pids, time.monotonic() + 2)
            assert not old_file.exists()
            result = await second.call("terminal_list")
            assert result["count"] == 1
            assert result["sessions"][0]["session_id"] == started["session_id"]
            assert result["sessions"][0]["status"] == "lost"
            for tool, args in (("terminal_write", {"text": "x"}), ("terminal_kill", {})):
                await second.call(tool, {"session_id": started["session_id"], **args}, error="SESSION_LOST")
            assert (await second.call("terminal_list", {"include_exited": False}))["count"] == 0


async def test_live_instance_sessions_are_not_swept(tmp_path):
    state = tmp_path / "shared"
    async with connection(tmp_path, state=state) as first:
        started = await first.call("terminal_start", {"command": "exec cat", "mode": "pipe"})
        files = list((state / "sessions").glob("*.json"))
        assert len(files) == 1
        previous = files[0].read_bytes()
        async with connection(tmp_path, state=state) as second:
            assert (await second.call("terminal_list"))["count"] == 0
            assert files[0].read_bytes() == previous
            assert len(list((state / "sessions").glob("*.json"))) == 2
            result = await first.call("terminal_write", {"session_id": started["session_id"], "text": "alive\n", "wait_ms": 1000})
            assert result["stdout"] == "alive\n" and result["status"] == "running"
    assert not list((state / "sessions").glob("*.json"))


async def test_registry_tracks_every_state_and_removal(tmp_path):
    async with connection(tmp_path, config_text="exit_retention_s = 1\n") as server:
        started = await server.call("terminal_start", {"command": "exec sleep 60", "mode": "pipe"})
        path, = (server.state / "sessions").glob("*.json")

        def records():
            return {row["session_id"]: row for row in json.loads(path.read_text())["records"]}

        assert records()[started["session_id"]]["status"] == "running"
        await server.call("terminal_kill", {"session_id": started["session_id"]})
        assert records()[started["session_id"]]["status"] == "signaled"
        await server.call("terminal_kill", {"session_id": started["session_id"], "remove": True})
        assert not records()
        failed = await server.call("terminal_start", {"mode": "pipe", "env": {"SHELL": str(tmp_path / "absent")}}, error="START_FAILED")
        assert records()[failed["session_id"]]["status"] == "failed_to_start"
        exited = await server.call("terminal_start", {"command": "true", "wait_ms": 1000, "until": "(?!)"})
        assert records()[exited["session_id"]]["status"] == "exited"
        with anyio.fail_after(2):
            while records():
                await anyio.sleep(.02)


@pytest.mark.parametrize("mismatch", ["pid", "pgid", "start_time"])
async def test_sweep_never_signals_mismatched_identity(config, mismatch):
    process = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
    try:
        pid, pgid, start_time = identity(process.pid)
        record = RegistryRecord(str(uuid4()), pid if mismatch != "pid" else 99999999,
                                pgid if mismatch != "pgid" else pgid + 1,
                                start_time if mismatch != "start_time" else "different start", "2026-10-21T00:00:00Z", "running")
        stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused owner", instance_id="stale")
        stale.write([record])
        manager = SessionManager(config)
        try:
            await manager.initialize()
            assert process.returncode is None
            assert (await manager.list())["sessions"][0]["status"] == "lost"
            assert not stale.path.exists()
        finally:
            await manager.shutdown_all()
        assert process.returncode is None
    finally:
        process.kill()
        await process.wait()


async def test_sweep_skips_locked_and_unprobeable_owners(config, monkeypatch):
    import terminal_mcp.registry as module

    stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="different start", instance_id="locked")
    stale.write([])
    manager = SessionManager(config)
    try:
        with stale.path.open() as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await manager.initialize()
            assert stale.path.exists()
    finally:
        await manager.shutdown_all()
    manager = SessionManager(config)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(module, "process_identity", lambda pid: (_ for _ in ()).throw(ProcessProbeError("Synthetic probe failure")))
            await manager.initialize()
        assert stale.path.exists()
    finally:
        await manager.shutdown_all()


async def test_lost_records_expire_without_signals(config):
    stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused", instance_id="lost")
    sid = str(uuid4())
    stale.write([RegistryRecord(sid, None, None, None, "2026-10-21T00:00:00Z", "exited")])
    assert config.exit_retention_s == 600
    manager = SessionManager(replace(config, exit_retention_s=1))
    try:
        await manager.initialize()
        assert (await manager.list())["sessions"][0]["status"] == "lost"
        with anyio.fail_after(2):
            while (await manager.list())["count"]:
                await anyio.sleep(.02)
        assert json.loads(manager.registry.path.read_text())["records"] == []
    finally:
        await manager.shutdown_all()


async def test_unprobeable_leader_keeps_file_without_signals(config, monkeypatch):
    import terminal_mcp.registry as module

    child = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
    manager = SessionManager(config)
    try:
        observed = process_identity(child.pid)
        stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused", instance_id="unknown")
        stale.write([RegistryRecord(str(uuid4()), child.pid, child.pid, observed.start_time,
                                    "2026-10-21T00:00:00Z", "running")])
        original = stale.path.read_bytes()

        def probe(pid):
            if pid == child.pid:
                raise ProcessProbeError("Synthetic inaccessible leader")
            return process_identity(pid)

        with monkeypatch.context() as scoped:
            scoped.setattr(module, "process_identity", probe)
            scoped.setattr(module.os, "killpg", lambda *args: pytest.fail("Unknown identity must not be signaled"))
            await manager.initialize()
            assert stale.path.read_bytes() == original
            assert (await manager.list())["count"] == 0
        assert child.returncode is None
    finally:
        await manager.shutdown_all()
        child.kill()
        await child.wait()


async def test_concurrent_sweep_imports_record_once(config):
    stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused", instance_id="race")
    stale.write([RegistryRecord(str(uuid4()), None, None, None, "2026-10-21T00:00:00Z", "exited")])
    managers = [SessionManager(config), SessionManager(config)]
    try:
        await asyncio.gather(*(manager.initialize() for manager in managers))
        counts = [(await manager.list())["count"] for manager in managers]
        assert sum(counts) == 1
        assert not stale.path.exists()
    finally:
        await asyncio.gather(*(manager.shutdown_all() for manager in managers))


async def test_confirmed_gone_leader_is_lost_without_signals(config, monkeypatch):
    import terminal_mcp.registry as module

    child = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await child.wait()
    stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused", instance_id="gone")
    sid = str(uuid4())
    stale.write([RegistryRecord(sid, child.pid, child.pid, "old-start", "2026-10-21T00:00:00Z", "running")])
    manager = SessionManager(config)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(module.os, "killpg", lambda *args: pytest.fail("A confirmed-gone leader must not be signaled"))
            await manager.initialize()
            assert (await manager.list(session_id=sid))["sessions"][0]["status"] == "lost"
            await manager.shutdown_all()
        assert not stale.path.exists()
    finally:
        await manager.shutdown_all()


async def test_restart_sweep_escalates_with_fresh_identity_checks(config, monkeypatch):
    import terminal_mcp.registry as module

    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
        start_new_session=True, stdout=asyncio.subprocess.PIPE,
    )
    manager = SessionManager(replace(config, kill_grace_ms=100))
    try:
        with anyio.fail_after(2):
            assert await child.stdout.readline() == b"ready\n"
        observed = process_identity(child.pid)
        stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused", instance_id="escalate")
        stale.write([RegistryRecord(str(uuid4()), child.pid, child.pid, observed.start_time,
                                    "2026-10-21T00:00:00Z", "running")])
        actual_killpg = os.killpg
        sent = []

        def checked_signal(pgid, sig):
            if sig:
                assert pgid == child.pid
                assert process_identity(child.pid) == observed
                sent.append(sig)
            return actual_killpg(pgid, sig)

        with monkeypatch.context() as scoped:
            scoped.setattr(module.os, "killpg", checked_signal)
            before = time.monotonic()
            await manager.initialize()
        assert .09 <= time.monotonic() - before < 2
        assert sent == [signal.SIGTERM, signal.SIGKILL]
        assert process_identity(child.pid) is None
        assert not stale.path.exists()
        assert (await manager.list())["sessions"][0]["status"] == "lost"
    finally:
        await manager.shutdown_all()
        if child.returncode is None:
            child.kill()
        await child.wait()


async def test_sweep_waits_through_transient_group_eperm(config, monkeypatch):
    import terminal_mcp.registry as module

    child = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
    manager = SessionManager(config)
    try:
        observed = process_identity(child.pid)
        stale = Registry(config.state_dir, owner_pid=os.getpid(), owner_start_time="reused", instance_id="transient")
        stale.write([RegistryRecord(str(uuid4()), child.pid, child.pid, observed.start_time,
                                    "2026-10-21T00:00:00Z", "running")])
        actual_killpg = os.killpg
        denied = False

        def transient_group(pgid, sig):
            nonlocal denied
            if pgid == child.pid and sig == 0 and not denied:
                denied = True
                raise PermissionError("Synthetic exiting group permission race")
            return actual_killpg(pgid, sig)

        with monkeypatch.context() as scoped:
            scoped.setattr(module.os, "killpg", transient_group)
            await manager.initialize()
        assert denied
        assert not stale.path.exists()
        assert process_identity(child.pid) is None
        assert (await manager.list())["sessions"][0]["status"] == "lost"
    finally:
        await manager.shutdown_all()
        if child.returncode is None:
            child.kill()
        await child.wait()
