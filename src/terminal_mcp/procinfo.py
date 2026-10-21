"""Native Unix process probes: absence is distinct from an unconfirmed identity."""

import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import sys


class ProcessProbeError(OSError):
    """The operating system did not provide a complete, trustworthy process record."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    start_time: str


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid", "pbi_uid",
        "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid", "pbi_svgid", "rfu_1",
    )] + [("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid")
    ] + [("pbi_nice", ctypes.c_int32), ("pbi_start_tvsec", ctypes.c_uint64),
         ("pbi_start_tvusec", ctypes.c_uint64)]


class _TaskInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "pti_virtual_size", "pti_resident_size", "pti_total_user", "pti_total_system",
        "pti_threads_user", "pti_threads_system",
    )] + [(name, ctypes.c_int32) for name in (
        "pti_policy", "pti_faults", "pti_pageins", "pti_cow_faults", "pti_messages_sent",
        "pti_messages_received", "pti_syscalls_mach", "pti_syscalls_unix", "pti_csw",
        "pti_threadnum", "pti_numrunning", "pti_priority",
    )]


_PROC = Path("/proc")
_pidinfo = None
_listallpids = None
if sys.platform == "darwin":
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _pidinfo = _libproc.proc_pidinfo
    _pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    _pidinfo.restype = ctypes.c_int
    _listallpids = _libproc.proc_listallpids
    _listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _listallpids.restype = ctypes.c_int


def _validate_pid(pid: int) -> None:
    if type(pid) is not int or not 0 < pid <= 2147483647:
        raise ProcessProbeError("Process ID must be a positive signed 32-bit integer")


def _darwin_info(pid: int, flavor: int, record_type):
    if _pidinfo is None:
        raise ProcessProbeError("Native process information is unavailable")
    record = record_type()
    ctypes.set_errno(0)
    count = _pidinfo(pid, flavor, 0, ctypes.byref(record), ctypes.sizeof(record))
    if count == ctypes.sizeof(record):
        return record
    error = ctypes.get_errno()
    if count <= 0 and error == errno.ESRCH:
        return None
    raise ProcessProbeError(error or errno.EIO, "Cannot confirm native process information")


def process_identity(pid: int) -> ProcessIdentity | None:
    """Return identity, None for confirmed absence, or raise ProcessProbeError."""
    _validate_pid(pid)
    if sys.platform == "darwin":
        record = _darwin_info(pid, 3, _BSDInfo)
        if record is None:
            return None
        if record.pbi_pid != pid or record.pbi_start_tvusec >= 1_000_000:
            raise ProcessProbeError("Native process identity did not match the requested PID")
        return ProcessIdentity(pid, record.pbi_pgid, f"{record.pbi_start_tvsec}.{record.pbi_start_tvusec:06d}")
    if sys.platform.startswith("linux"):
        try:
            raw = (_PROC / str(pid) / "stat").read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProcessProbeError("Cannot read process identity") from exc
        try:
            fields = raw[raw.rindex(")") + 1:].split()
            observed_pid = int(raw.split("(", 1)[0].strip())
            pgid, ticks = int(fields[2]), int(fields[19])
            boot = (_PROC / "sys/kernel/random/boot_id").read_text().strip()
            if observed_pid != pid or pgid < 0 or ticks < 0 or not boot:
                raise ValueError("Invalid native identity fields")
            return ProcessIdentity(pid, pgid, f"{boot}:{ticks}")
        except (OSError, ValueError, IndexError) as exc:
            raise ProcessProbeError("Cannot confirm process identity") from exc
    raise ProcessProbeError("Only macOS and Linux process probes are supported")


def session_process_groups(sid: int) -> set[int]:
    """Return all confirmed member PGIDs, including groups with an exited leader.

    An empty set means confirmed absence; an incomplete scan raises instead.
    Callers must establish SID ownership, and stop using it once it disappears:
    a SID cannot be reused while members remain, but can be reused afterwards.
    """
    _validate_pid(sid)
    groups = set()
    try:
        if sys.platform == "darwin":
            if _listallpids is None:
                raise ProcessProbeError("Native process enumeration is unavailable")
            for _ in range(3):
                count = _listallpids(None, 0)
                if count <= 0:
                    raise ProcessProbeError("Cannot size the native process list")
                pids = (ctypes.c_int * (count + 64))()
                count = _listallpids(pids, ctypes.sizeof(pids))
                if 0 < count < len(pids):
                    break
            else:
                raise ProcessProbeError("Cannot obtain a complete native process list")
            for pid in pids[:count]:
                if pid <= 0:
                    continue
                try:
                    if os.getsid(pid) == sid:
                        pgid = os.getpgid(pid)
                        # Recheck after getpgid to reject a PID-reuse race.
                        if pgid > 0 and os.getsid(pid) == sid:
                            groups.add(pgid)
                except ProcessLookupError:
                    continue
        elif sys.platform.startswith("linux"):
            for path in _PROC.iterdir():
                if not path.name.isdecimal():
                    continue
                try:
                    raw = (path / "stat").read_text()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                # comm may contain spaces and parentheses; fields after its last
                # ')' start with state, ppid, pgrp, session (stat fields 3..6).
                fields = raw[raw.rindex(")") + 1:].split()
                pgid, observed_sid = int(fields[2]), int(fields[3])
                if observed_sid == sid and pgid > 0:
                    groups.add(pgid)
        else:
            raise ProcessProbeError("Only macOS and Linux process probes are supported")
    except (OSError, ValueError, IndexError) as exc:
        raise ProcessProbeError("Cannot confirm all session process groups") from exc
    return groups


def process_rss(pid: int) -> int | None:
    """Return resident bytes without launching any helper processes."""
    _validate_pid(pid)
    if sys.platform == "darwin":
        record = _darwin_info(pid, 4, _TaskInfo)
        return None if record is None else record.pti_resident_size
    if sys.platform.startswith("linux"):
        try:
            fields = (_PROC / str(pid) / "statm").read_text().split()
            pages = int(fields[1])
            if pages < 0:
                raise ValueError("Invalid resident page count")
            return pages * os.sysconf("SC_PAGE_SIZE")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError) as exc:
            raise ProcessProbeError("Cannot confirm resident memory") from exc
    raise ProcessProbeError("Only macOS and Linux process probes are supported")
