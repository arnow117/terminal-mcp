"""Per-instance atomic snapshots and conservative, locked orphan-group sweeping."""

import asyncio
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import time
from typing import Any, Iterable, Mapping

from .contracts import STATUSES
from .procinfo import ProcessProbeError, process_identity, session_process_groups


def _pid(value: Any) -> bool:
    return type(value) is int and value > 1


def _start_time(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class RegistryRecord:
    session_id: str
    leader_pid: int | None
    pgid: int | None
    leader_start_time: str | None
    started_at: str
    status: str

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("Registry session_id must be a nonempty string")
        if not isinstance(self.started_at, str) or not self.started_at or self.status not in STATUSES:
            raise ValueError("Registry record has invalid timestamp or status")
        if self.leader_pid is not None and not _pid(self.leader_pid):
            raise ValueError("Registry leader_pid must be a positive process ID greater than one")
        if self.pgid is not None and not _pid(self.pgid):
            raise ValueError("Registry pgid must be a positive process group ID greater than one")
        if self.leader_start_time is not None and not _start_time(self.leader_start_time):
            raise ValueError("Registry leader_start_time must be a nonempty process start time")


def owner_is_live(snapshot: Mapping[str, Any], pid: int | None, start_time: str | None) -> bool:
    """Compare an observed owner identity; callers must distinguish failed probes from absence."""
    return (_pid(pid) and _pid(snapshot.get("owner_pid")) and snapshot.get("owner_pid") == pid
            and bool(_start_time(start_time)) and _start_time(snapshot.get("owner_start_time")) == _start_time(start_time))


def record_matches_process(record: RegistryRecord, pid: int | None, pgid: int | None,
                           start_time: str | None) -> bool:
    """Require the entire identity tuple before any caller may signal a stale group."""
    return (_pid(pid) and _pid(pgid) and record.leader_pid == pid and record.pgid == pgid
            and bool(_start_time(start_time)) and _start_time(record.leader_start_time) == _start_time(start_time))


class Registry:
    def __init__(self, state_dir: str | Path, *, owner_pid: int, owner_start_time: str,
                 instance_id: str | None = None):
        if not _pid(owner_pid) or not _start_time(owner_start_time):
            raise ValueError("Registry owner requires a valid PID and process start time")
        instance_id = str(time.time_ns()) if instance_id is None else instance_id
        if not isinstance(instance_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", instance_id) is None:
            raise ValueError("Registry instance identifier must contain only letters, digits, underscores or hyphens")
        self.owner_pid, self.owner_start_time = owner_pid, owner_start_time.strip()
        self.path = Path(state_dir) / "sessions" / f"{owner_pid}-{instance_id}.json"

    def snapshot(self, records: Iterable[RegistryRecord]) -> dict:
        return {"owner_pid": self.owner_pid, "owner_start_time": self.owner_start_time,
                "records": [asdict(record) for record in records]}

    @staticmethod
    def load(path: str | Path) -> dict:
        with Path(path).open(encoding="utf-8") as file:
            snapshot = json.load(file)
        if (not isinstance(snapshot, dict) or set(snapshot) != {"owner_pid", "owner_start_time", "records"}
                or not _pid(snapshot["owner_pid"]) or not _start_time(snapshot["owner_start_time"])
                or not isinstance(snapshot["records"], list)):
            raise ValueError("Invalid registry file model")
        try:
            for record in snapshot["records"]:
                RegistryRecord(**record)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid registry record model") from exc
        return snapshot

    def write(self, records: Iterable[RegistryRecord]) -> None:
        """Publish a private snapshot only after flushing its complete temporary file."""
        data = json.dumps(self.snapshot(records), ensure_ascii=True, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=f".{self.path.stem}-", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    async def sweep(self, accept_records, grace_ms: int) -> None:
        """Transfer processed records durably while holding each stale file's lock.

        Inaccessible or malformed files remain untouched. Preflight all leaders
        before signaling any group, so an unknown leader never causes partial loss.
        """
        for path in sorted(self.path.parent.glob("*.json")):
            if path == self.path:
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Another sweeper may have unlinked this inode before our lock.
                    if path.stat().st_ino != os.fstat(handle.fileno()).st_ino:
                        continue
                    snapshot = self.load(path)
                    owner = process_identity(snapshot["owner_pid"])
                    if owner is not None and owner_is_live(snapshot, owner.pid, owner.start_time):
                        continue
                    records = [RegistryRecord(**row) for row in snapshot["records"]]
                    matching = []
                    for record in records:
                        if record.leader_pid is None or record.status == "lost":
                            continue
                        leader = process_identity(record.leader_pid)
                        if leader is not None and record_matches_process(record, leader.pid, leader.pgid, leader.start_time):
                            matching.append(record)
                    for record in matching:
                        await self._stop_record(record, grace_ms)
                    # The destination write must succeed before the source disappears.
                    accept_records(records)
                    path.unlink()
            except (OSError, ValueError, TypeError):
                # Includes lock contention and ProcessProbeError: never assume absence.
                continue

    @staticmethod
    async def _stop_record(record: RegistryRecord, grace_ms: int) -> None:
        leader = process_identity(record.leader_pid)
        if (leader is None or record.pgid != record.leader_pid
                or not record_matches_process(record, leader.pid, leader.pgid, leader.start_time)):
            # A missing leader at restart gives no trustworthy SID generation.
            # Never rediscover ownership by SID alone, even if members still exist.
            raise ProcessProbeError("Cannot confirm the recorded session leader")

        def members():
            leader = process_identity(record.leader_pid)
            if leader is not None and not record_matches_process(record, leader.pid, leader.pgid, leader.start_time):
                raise ProcessProbeError("The recorded session leader identity changed")
            # Once matched, continuous nonempty membership keeps this SID reserved
            # even if TERM kills the leader. Stop immediately on the first empty scan
            # so a later reused PID/SID can never become a new signaling target.
            return session_process_groups(record.leader_pid)

        def send(groups, sig):
            sent = set()
            for pgid in groups:
                try:
                    os.killpg(pgid, sig)
                    sent.add(pgid)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Unknown is not gone: retry only freshly confirmed members,
                    # and retain the registry file if the bounded cleanup fails.
                    pass
            return sent

        groups = members()
        sent = send(groups, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + grace_ms / 1000
        while groups:
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(.01)
            groups = members()
            sent.update(send(groups - sent, signal.SIGTERM))
        deadline = asyncio.get_running_loop().time() + 1
        killed = set()
        while groups:
            # Refresh membership immediately before escalation as well.
            groups = members()
            if not groups:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise ProcessProbeError("The stale session has not disappeared")
            killed.update(send(groups - killed, signal.SIGKILL))
            await asyncio.sleep(.01)
