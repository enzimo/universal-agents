from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    atomic_write_bytes,
    canonical_json,
    model_digest,
)


class _Plan(BaseModel):
    change_id: str
    plan_digest: str = ""
    values: dict[str, object]


class _Target(BaseModel):
    action: str
    resource_id: str
    optional_scope: str | None = None


def test_canonical_json_and_model_digest_are_order_independent() -> None:
    left = _Plan(change_id="change_1", values={"b": 2, "a": {"y": 1, "x": 0}})
    right = _Plan(change_id="change_1", values={"a": {"x": 0, "y": 1}, "b": 2})

    assert canonical_json(left.model_dump()) == canonical_json(right.model_dump())
    assert model_digest(left) == model_digest(right)
    assert model_digest(
        left.model_copy(update={"plan_digest": "f" * 64})
    ) == model_digest(left)


def test_file_plan_store_uses_operation_owned_directory_and_model(
    tmp_path: Path,
) -> None:
    store = FilePlanStore(tmp_path / "plans", _Plan)
    plan = _Plan(change_id="change_1", values={"safe": True})

    path = store.save(plan)

    assert path == tmp_path / "plans" / "change_1.json"
    assert store.load("change_1") == plan
    assert path.stat().st_mode & 0o222 == 0


def test_atomic_write_bytes_removes_temporary_artifact_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plan.json"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        "custom_agents.governance.authorized_operations.os.replace", fail_replace
    )

    with pytest.raises(OSError, match="disk unavailable"):
        atomic_write_bytes(target, b"{}")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("already_exists", [False, True])
def test_immutable_write_cannot_publish_before_permissions_succeed(tmp_path, monkeypatch, already_exists) -> None:
    target = tmp_path / "receipt.json"
    if already_exists:
        target.write_bytes(b"previous record")
    def fail_chmod(_path, _mode):
        raise OSError("permissions unavailable")
    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(OSError, match="permissions unavailable"):
        atomic_write_bytes(target, b"new record", immutable=True)
    if already_exists:
        assert target.read_bytes() == b"previous record"
    else:
        assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


@dataclass
class _Workflow:
    existing: object | None = None
    decision: object | None = None
    submitted: object | None = None
    approval_decision: object | None = None
    challenge: object | None = None
    default_ttl_seconds: int = 900

    def get_request(self, _request_id: str):
        return self.existing

    def submit_request(self, request, *, actor):
        self.submitted = (request, actor)
        return request

    def get_decision_for_request(self, _request_id: str):
        return self.decision

    def get_decision(self, _decision_id: str):
        return self.approval_decision

    def get_approval_challenge(self, _challenge_id: str):
        return self.challenge

    def get_grant(self, _grant_id: str):
        return None









