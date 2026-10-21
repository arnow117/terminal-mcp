"""Shared fixtures: no tests use the user's runtime state directories."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import signal
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))


@pytest.fixture(autouse=True)
def test_deadline():
    def expired(signum, frame):
        raise TimeoutError("Test exceeded the 15 second deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def config(tmp_path):
    from terminal_mcp.config import load_config

    return load_config(
        config_path=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        startup_cwd=tmp_path,
    )


@pytest.fixture
def stdio_connection(tmp_path):
    """Launch the installed entry point with only temporary config and state."""
    counter = 0

    @asynccontextmanager
    async def connect(config_text=""):
        nonlocal counter
        counter += 1
        runtime = tmp_path / f"server-{counter}"
        runtime.mkdir()
        config_path = runtime / "config.toml"
        config_path.write_text(config_text, encoding="utf-8")
        params = StdioServerParameters(
            command=str(Path(sys.executable).parent / "terminal-mcp"),
            args=["--config", str(config_path), "--state-dir", str(runtime / "state"),
                  "--allowed-root", str(tmp_path)],
            cwd=tmp_path,
            env={"HOME": os.environ["HOME"], "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONUNBUFFERED": "1", "TMPDIR": "/tmp"},
        )
        with (runtime / "stderr.log").open("w", encoding="utf-8") as errlog:
            with anyio.fail_after(12):
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        with anyio.fail_after(4):
                            await client.initialize()
                        try:
                            yield client
                        finally:
                            with anyio.fail_after(4, shield=True):
                                result = await client.call_tool("terminal_list", {})
                                if not result.is_error:
                                    for session in result.structured_content["sessions"]:
                                        await client.call_tool("terminal_kill", {
                                            "session_id": session["session_id"],
                                            "signal": "KILL", "remove": True,
                                        })

    return connect


@pytest.fixture
async def mcp_client(stdio_connection):
    async with stdio_connection() as client:
        yield client
