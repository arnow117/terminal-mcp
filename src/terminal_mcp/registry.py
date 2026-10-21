"""Per-instance registry snapshots; process probing, locks and sweeping belong to lifecycle wiring."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterable, Mapping

from .contracts import STATUSES


def _pid(value: Any) -> bool:
    return type(value) is int and value > 1


def _lstart(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class RegistryRecord:
    session_id: str
    leader_pid: int | None
    pgid: int | None
    leader_lstart: str | None
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
        if self.leader_lstart is not None and not _lstart(self.leader_lstart):
            raise ValueError("Registry leader_lstart must be a nonempty process start time")


def owner_is_live(snapshot: Mapping[str, Any], pid: int | None, lstart: str | None) -> bool:
    """Compare an observed owner identity; callers must distinguish failed probes from absence."""
    return (_pid(pid) and _pid(snapshot.get("owner_pid")) and snapshot.get("owner_pid") == pid
            and bool(_lstart(lstart)) and _lstart(snapshot.get("owner_lstart")) == _lstart(lstart))


def record_matches_process(record: RegistryRecord, pid: int | None, pgid: int | None,
                           lstart: str | None) -> bool:
    """Require the entire identity tuple before any caller may signal a stale group."""
    return (_pid(pid) and _pid(pgid) and record.leader_pid == pid and record.pgid == pgid
            and bool(_lstart(lstart)) and _lstart(record.leader_lstart) == _lstart(lstart))


class Registry:
    def __init__(self, state_dir: str | Path, *, owner_pid: int, owner_lstart: str,
                 instance_id: str | None = None):
        if not _pid(owner_pid) or not _lstart(owner_lstart):
            raise ValueError("Registry owner requires a valid PID and process start time")
        instance_id = str(time.time_ns()) if instance_id is None else instance_id
        if not isinstance(instance_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", instance_id) is None:
            raise ValueError("Registry instance identifier must contain only letters, digits, underscores or hyphens")
        self.owner_pid, self.owner_lstart = owner_pid, owner_lstart.strip()
        self.path = Path(state_dir) / "sessions" / f"{owner_pid}-{instance_id}.json"

    def snapshot(self, records: Iterable[RegistryRecord]) -> dict:
        return {"owner_pid": self.owner_pid, "owner_lstart": self.owner_lstart,
                "records": [asdict(record) for record in records]}

    @staticmethod
    def load(path: str | Path) -> dict:
        with Path(path).open(encoding="utf-8") as file:
            snapshot = json.load(file)
        if (not isinstance(snapshot, dict) or set(snapshot) != {"owner_pid", "owner_lstart", "records"}
                or not _pid(snapshot["owner_pid"]) or not _lstart(snapshot["owner_lstart"])
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
