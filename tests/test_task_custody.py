"""One team-wide holder per task (storage.task_custody_repository, custom_agents.task_custody).

Real SQLite and, with LIVE_TEST_POSTGRES_DSN, real PostgreSQL for the storage
rules; a real TaskManager for saves, removals, clearing and recovery.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custom_agents import task_custody
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.config import resolve_storage_settings
from storage.runtime_snapshot_layout import canonical_json
from storage.runtime_state_repository import RuntimeRecoveryConflict, create_runtime_state_repository
from storage.task_custody_repository import TaskCustodyConflict, TaskCustodyTakeover


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(*task_ids: str, delegated: dict[str, str] | None = None) -> str:
    def record(task_id: str) -> TaskSnapshotRecord:
        return TaskSnapshotRecord(
            task_id=task_id, state=ProgressState.ACCEPTED,
            task=Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="work"),
            header=MessageHeader(message_id=f"message_{task_id}", timestamp="2026-10-17T00:00:00+00:00"),
            meta=MessageMeta(), owner_person_id=None,
        )

    snapshot = RuntimeSnapshotV3(
        schema_version=3, tasks=[record(task_id) for task_id in task_ids],
        task_queue=[task_id for task_id in task_ids if task_id not in (delegated or {})],
        subtask_to_parent_map=delegated or {},
    )
    return canonical_json(snapshot.model_dump(mode="json"))


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.live)])
def store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL custody")
        config = {"backend": "postgres", "postgres": {"dsn": dsn}}
    else:
        config = {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
    repo = create_runtime_state_repository(resolve_storage_settings(config))
    run = uuid4().hex[:8]
    world = SimpleNamespace(
        repo=repo, team=f"team_{run}", owners=[f"agent_{run}_{n}" for n in range(3)],
        task=lambda name: f"{name}_{run}",
    )
    yield world
    for owner in world.owners:
        repo.clear_task_runtime(owner)


def _save(world, owner, snapshot, *, takeover=None):
    world.repo.save_task_runtime(
        owner_agent_id=owner, owner_agent_name=owner, team_id=world.team,
        owner_named_agent_type="Worker", runtime_state_json=snapshot, custody_takeover=takeover,
    )


def _holder(world, task_id):
    row = world.repo.get_task_custody(world.team, [task_id]).get(task_id)
    return row.holder_agent_id if row else None


def test_a_save_claims_executed_tasks_and_releases_them_when_they_leave(store):
    a, _b, _c = store.owners
    work, child = store.task("work"), store.task("child")
    _save(store, a, _snapshot(work, child, delegated={child: work}))
    assert _holder(store, work) == a
    assert _holder(store, child) is None  # a task it delegated is the worker's to hold
    _save(store, a, _snapshot(store.task("other")))
    assert _holder(store, work) is None
    store.repo.clear_task_runtime(a)
    assert _holder(store, store.task("other")) is None


def test_a_second_holder_is_refused_unless_it_names_the_holder_it_replaces(store):
    a, b, _c = store.owners
    work = store.task("work")
    _save(store, a, _snapshot(work))
    with pytest.raises(TaskCustodyConflict) as refused:
        _save(store, b, _snapshot(work))
    assert refused.value.holder_agent_id == a
    assert store.repo.load_task_runtime_by_owner(b) == []  # the refused save wrote nothing
    with pytest.raises(TaskCustodyConflict):
        _save(store, b, _snapshot(work), takeover=TaskCustodyTakeover(work, "someone_else"))
    _save(store, b, _snapshot(work), takeover=TaskCustodyTakeover(work, a))
    assert _holder(store, work) == b
    # The old holder still has the task saved; its later saves never touch b's row,
    # and removing the task releases only rows it still holds.
    _save(store, a, _snapshot(work, store.task("next")))
    _save(store, a, _snapshot(store.task("next")))
    assert _holder(store, work) == b


def test_recovery_moves_custody_and_refuses_a_task_another_agent_took(store):
    a, b, c = store.owners
    work, held = store.task("work"), store.task("held")
    _save(store, a, _snapshot(work))
    sources = store.repo.load_task_runtime_by_owner(a)
    store.repo.commit_task_runtime_recovery(sources=sources, replacement=replace(sources[0], owner_agent_id=c))
    assert _holder(store, work) == c

    _save(store, b, _snapshot(held))
    _save(store, a, _snapshot(store.task("mine")))
    raced = store.repo.load_task_runtime_by_owner(a)
    with pytest.raises(RuntimeRecoveryConflict):
        # A replacement that still includes a task b holds must not commit.
        store.repo.commit_task_runtime_recovery(
            sources=raced, replacement=replace(raced[0], owner_agent_id=c, runtime_state_json=_snapshot(held))
        )
    assert _holder(store, held) == b


def test_recovery_creates_missing_rows_for_snapshots_saved_before_custody(store):
    a, _b, c = store.owners
    legacy = store.task("legacy")
    store.repo.save_task_runtime(
        owner_agent_id=a, owner_agent_name=a, team_id=None, owner_named_agent_type="Worker",
        runtime_state_json=_snapshot(legacy),
    )  # no team id: no row, as for snapshots saved by earlier releases
    sources = store.repo.load_task_runtime_by_owner(a)
    store.repo.commit_task_runtime_recovery(
        sources=sources, replacement=replace(sources[0], owner_agent_id=c, team_id=store.team)
    )
    assert _holder(store, legacy) == c


# -- TaskManager ----------------------------------------------------------------


def _manager(tmp_path, owner: str) -> TaskManager:
    manager = TaskManager(db_path=str(tmp_path / "instance.db"))
    manager.set_owner_identity(owner, owner, "team", "Worker")
    return manager


def _admit(manager: TaskManager, task_id: str, *, takeover_from: str | None = None) -> bool:
    return manager.add_task(
        Task(task_id=task_id, parent_task_id="parent", task_type=MessageMetaType.REQUEST, problem_statement="w"),
        MessageHeader(from_agent_id="requester"), MessageMeta(), owner_person_id=None,
        custody_takeover_from=takeover_from,
    )


def test_a_refused_admission_leaves_no_task_behind(tmp_path):
    first, second = _manager(tmp_path, "worker_a"), _manager(tmp_path, "worker_b")
    assert _admit(first, "t1")
    with pytest.raises(TaskCustodyConflict):
        _admit(second, "t1")
    assert second.get_task_info("t1") is None and not second.task_state.task_queue
    assert _admit(second, "t1", takeover_from="worker_a")
    holders = second.runtime_state_store.get_task_custody("team", ["t1"])
    assert holders["t1"].holder_agent_id == "worker_b"


def test_handing_a_task_back_releases_its_custody(tmp_path):
    worker = _manager(tmp_path, "worker_a")
    _admit(worker, "t1")
    _admit(worker, "t2")
    worker.remove_task("t1")  # lease-loss wind-down's _release_task
    holders = worker.runtime_state_store.get_task_custody("team", ["t1", "t2"])
    assert set(holders) == {"t2"}
    worker.complete_task("t2", _iso())  # the last task: the snapshot is cleared
    assert worker.runtime_state_store.get_task_custody("team", ["t2"]) == {}


def test_a_takeover_leaves_out_a_task_another_agent_took_over(tmp_path, caplog):
    gone = _manager(tmp_path, "worker_gone")
    _admit(gone, "t1")
    _admit(gone, "t2")
    taker = _manager(tmp_path, "worker_b")
    assert _admit(taker, "t1", takeover_from="worker_gone")  # the redelivered copy
    replacement = _manager(tmp_path, "worker_c")
    with caplog.at_level(logging.WARNING):
        restored = replacement.restore_runtime_state_for_owner_ids(["worker_gone"])
    assert restored["task_count"] == 1
    assert replacement.get_task_info("t1") is None
    assert replacement.get_task_info("t2") is not None
    holders = replacement.runtime_state_store.get_task_custody("team", ["t1", "t2"])
    assert (holders["t1"].holder_agent_id, holders["t2"].holder_agent_id) == ("worker_b", "worker_c")
    assert any(getattr(r, "reason", None) == "held_by_another_agent" for r in caplog.records)


def test_a_takeover_leaves_out_a_task_another_agent_already_finished(tmp_path, caplog):
    gone = _manager(tmp_path, "worker_gone")
    _admit(gone, "t1")
    taker = _manager(tmp_path, "worker_b")
    assert _admit(taker, "t1", takeover_from="worker_gone")
    taker.complete_task("t1", _iso())  # finished and archived; custody released
    replacement = _manager(tmp_path, "worker_c")
    with caplog.at_level(logging.WARNING):
        restored = replacement.restore_runtime_state_for_owner_ids(["worker_gone"])
    assert restored["task_count"] == 0
    assert replacement.get_task_info("t1") is None
    assert any(getattr(r, "reason", None) == "finished_by_another_agent" for r in caplog.records)


# -- admission decision --------------------------------------------------------


def _agent(tmp_path, agent_id="worker_b", holder="worker_a", holder_status=None, launch_gone=None, monkeypatch=None):
    manager = _manager(tmp_path, holder)
    _admit(manager, "t1")
    agent = SimpleNamespace(
        id=agent_id, team_id="team", task_manager=_manager(tmp_path, agent_id),
        _logger=logging.getLogger("custody-test"),
    )
    monkeypatch.setattr(task_custody, "owner_gone_by_launch", lambda _agent, _holder: launch_gone)
    monkeypatch.setattr(task_custody, "lookup_owner_status", lambda _agent, _holder: holder_status)
    monkeypatch.setattr(task_custody, "heartbeat_checks_deferred", lambda _agent: False)
    return agent


def _status(seen_seconds_ago: float, exiting: bool = False) -> AgentStatus:
    return AgentStatus(
        self_id="worker_a", team_id="team", named_agent_type="Worker", name="a", agent_role="worker",
        is_paused=False, is_executing=True, has_execution_capacity=False,
        status_type=MessageMetaType.RESPONSE, is_exiting=exiting,
        last_seen_at=(datetime.now(timezone.utc) - timedelta(seconds=seen_seconds_ago)).isoformat(),
    )


def test_admission_drops_a_copy_while_its_holder_lives(tmp_path, monkeypatch):
    agent = _agent(tmp_path, holder_status=_status(5), monkeypatch=monkeypatch)
    decision = task_custody.decide_admission(agent, "t1", None)
    assert (decision.admit, decision.reason, decision.holder_agent_id) == (False, task_custody.CUSTODY_HELD, "worker_a")
    assert task_custody.decide_admission(agent, "free_task", None).reason == task_custody.CUSTODY_FREE


def test_admission_takes_over_from_a_gone_holder_or_one_that_handed_it_back(tmp_path, monkeypatch):
    live = _agent(tmp_path, holder_status=_status(5), monkeypatch=monkeypatch)
    handed_back = task_custody.decide_admission(live, "t1", "worker_a")
    assert (handed_back.admit, handed_back.takeover_from) == (True, "worker_a")
    for status, launch_gone in ((_status(5, exiting=True), None), (_status(5), True), (None, None), (_status(3600), None)):
        monkeypatch.setattr(task_custody, "owner_gone_by_launch", lambda _a, _h, gone=launch_gone: gone)
        monkeypatch.setattr(task_custody, "lookup_owner_status", lambda _a, _h, s=status: s)
        decision = task_custody.decide_admission(live, "t1", None)
        assert (decision.admit, decision.reason, decision.takeover_from) == (
            True, task_custody.CUSTODY_HOLDER_GONE, "worker_a"
        )
    # A live launch record keeps the holder alive however old its heartbeat.
    monkeypatch.setattr(task_custody, "owner_gone_by_launch", lambda _a, _h: False)
    assert not task_custody.decide_admission(live, "t1", None).admit
