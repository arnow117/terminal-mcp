"""Native process probes distinguish absence from inaccessible identity."""

import errno
import os
import subprocess
import sys

import pytest

from terminal_mcp.procinfo import ProcessProbeError, process_identity, process_rss


def test_self_identity_and_rss():
    identity = process_identity(os.getpid())
    assert identity.pid == os.getpid()
    assert identity.pgid == os.getpgrp()
    assert identity.start_time
    assert process_identity(os.getpid()) == identity
    assert process_rss(os.getpid()) > 0


def test_child_and_exited_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        identity = process_identity(child.pid)
        assert identity.pid == identity.pgid == child.pid
        assert identity.start_time
        assert process_rss(child.pid) > 0
    finally:
        child.kill()
        child.wait(timeout=2)
    assert process_identity(child.pid) is None
    assert process_rss(child.pid) is None


def test_system_process_never_returns_wrong_identity():
    try:
        identity = process_identity(1)
    except ProcessProbeError:
        return
    assert identity is not None
    assert identity.pid == 1
    assert identity.pgid >= 0 and identity.start_time


@pytest.mark.parametrize("pid", [0, -1, True])
def test_invalid_pid_is_not_absence(pid):
    with pytest.raises(ProcessProbeError):
        process_identity(pid)
    with pytest.raises(ProcessProbeError):
        process_rss(pid)


def test_linux_stat_parses_last_parenthesis(tmp_path, monkeypatch):
    import terminal_mcp.procinfo as module

    # Field 5 is pgrp; field 22 is starttime. The process name contains ')'.
    stat = "42 (name ) with spaces) S 1 42 " + "0 " * 16 + "98765 0 0"
    (tmp_path / "42").mkdir()
    (tmp_path / "42/stat").write_text(stat)
    (tmp_path / "42/statm").write_text("200 11 0")
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text("boot-test\n")
    monkeypatch.setattr(module, "_PROC", tmp_path)
    monkeypatch.setattr(module.sys, "platform", "linux")
    identity = process_identity(42)
    assert (identity.pid, identity.pgid, identity.start_time) == (42, 42, "boot-test:98765")
    assert process_rss(42) == 11 * os.sysconf("SC_PAGE_SIZE")
    assert process_identity(43) is None
    (tmp_path / "42/stat").write_text("malformed")
    with pytest.raises(ProcessProbeError):
        process_identity(42)


def test_darwin_probe_errno_and_short_reads(monkeypatch):
    import terminal_mcp.procinfo as module
    import ctypes

    def fake(error, size):
        def probe(*args):
            ctypes.set_errno(error)
            return size
        return probe

    monkeypatch.setattr(module.sys, "platform", "darwin")
    for error in (errno.EPERM, errno.EIO, 0):
        monkeypatch.setattr(module, "_pidinfo", fake(error, 0))
        with pytest.raises(ProcessProbeError):
            process_identity(42)
        with pytest.raises(ProcessProbeError):
            process_rss(42)
    monkeypatch.setattr(module, "_pidinfo", fake(errno.ESRCH, 0))
    assert process_identity(42) is None
    assert process_rss(42) is None
    monkeypatch.setattr(module, "_pidinfo", fake(0, 12))
    with pytest.raises(ProcessProbeError):
        process_identity(42)


def test_darwin_layout_and_wrong_pid_are_rejected(monkeypatch):
    import ctypes
    import terminal_mcp.procinfo as module

    assert ctypes.sizeof(module._BSDInfo) == 136
    assert ctypes.sizeof(module._TaskInfo) == 96
    assert module._TaskInfo.pti_resident_size.offset == 8

    def wrong_pid(pid, flavor, arg, destination, size):
        record = ctypes.cast(destination, ctypes.POINTER(module._BSDInfo)).contents
        record.pbi_pid = pid + 1
        return size

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "_pidinfo", wrong_pid)
    with pytest.raises(ProcessProbeError):
        process_identity(42)
