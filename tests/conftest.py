"""Shared fixtures: no tests use the user's runtime state directories."""

import signal

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
