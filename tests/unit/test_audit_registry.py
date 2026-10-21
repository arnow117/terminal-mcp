import json
import os

import pytest

from terminal_mcp.audit import Audit
from terminal_mcp.registry import Registry, RegistryRecord, owner_is_live, record_matches_process


def test_audit_drops_unapproved_nested_fields(tmp_path):
    path = tmp_path / "audit.jsonl"
    Audit(path).log({
        "tool": "terminal_write", "parameter_summary": {"text": "PRIVATE", "keys": ["Enter"], "env": {"X": "PRIVATE"}},
        "policy": {"decision": "deny", "rule_id": "rm-root", "stdout": "PRIVATE"},
        "result": {"status": "exited", "exit_code": 0, "env": {"X": "PRIVATE"}},
        "unexpected": "PRIVATE",
    })
    record = json.loads(path.read_text())
    assert "PRIVATE" not in path.read_text()
    assert record["policy"] == {"decision": "deny", "rule_id": "rm-root"}
    assert record["result"]["exit_code"] == 0
    assert record["parameter_summary"]["keys"] == ["Enter"]


def test_audit_short_write_fails_without_a_second_write(tmp_path, monkeypatch):
    calls = []

    def short_write(fd, data):
        calls.append(data)
        return len(data) - 1

    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(OSError, match="Incomplete audit write"):
        Audit(tmp_path / "audit.jsonl").log({"tool": "terminal_list"})
    assert len(calls) == 1


def test_registry_failure_is_safe_and_private(tmp_path, monkeypatch):
    registry = Registry(tmp_path, owner_pid=123, owner_lstart=" start ", instance_id="one")
    registry.write([])
    previous = registry.path.read_bytes()
    assert registry.path.stat().st_mode & 0o777 == 0o600
    assert registry.path.parent.stat().st_mode & 0o777 == 0o700

    def fail(fd):
        raise OSError("Simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        registry.write([])
    assert registry.path.read_bytes() == previous
    assert list(registry.path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize("snapshot", [{}, {"owner_pid": 1, "owner_lstart": "", "records": []}, {"owner_pid": True, "owner_lstart": "start", "records": []}, {"owner_pid": 123, "owner_lstart": "start", "records": [{}]}])
def test_registry_load_rejects_malformed_records(tmp_path, snapshot):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(snapshot))
    with pytest.raises(ValueError):
        Registry.load(path)


def test_matching_never_accepts_empty_identity_or_process_group_zero():
    assert not owner_is_live({"owner_pid": 123, "owner_lstart": ""}, 123, "")
    record = RegistryRecord("s", 123, 123, "start", "2026-10-21T10:00:00Z", "running")
    assert record_matches_process(record, 123, 123, " start\n")
    assert not record_matches_process(record, 123, 0, "start")
    assert not record_matches_process(record, True, 123, "start")


def test_registry_instance_identifier_cannot_escape_directory(tmp_path):
    with pytest.raises(ValueError):
        Registry(tmp_path, owner_pid=123, owner_lstart="start", instance_id="../../escape")
