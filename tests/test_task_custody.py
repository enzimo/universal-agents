"""One team-wide holder per task (storage.task_custody_repository, custom_agents.task_custody).

Real SQLite and, with LIVE_TEST_POSTGRES_DSN, real PostgreSQL for the storage
rules; a real TaskManager for saves, removals, clearing and recovery.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from custom_agents import task_custody
from custom_agents.agent_liveness import LivenessPurpose
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.config import resolve_storage_settings
from storage.runtime_snapshot_layout import canonical_json
from storage.runtime_custody import RuntimeRecoveryConflict
from storage.runtime_state_repository import create_runtime_state_repository
from storage.task_custody_repository import (
    TaskCustodyConflict, TaskCustodyReleased, TaskCustodyTakenOver, TaskCustodyTakeover,
)


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


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL custody")
        config = {"backend": "postgres", "postgres": {"dsn": dsn}}
    else:
        config = {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
    settings = resolve_storage_settings(config)
    repo = create_runtime_state_repository(settings)
    run = uuid4().hex[:8]
    world = SimpleNamespace(
        repo=repo, settings=settings, team=f"team_{run}", owners=[f"agent_{run}_{n}" for n in range(3)],
        task=lambda name: f"{name}_{run}",
    )
    yield world
    for owner in world.owners:
        repo.clear_task_runtime(owner)


def _save(world, owner, snapshot, *, takeover=None):
    world.repo.save_task_runtime(
        owner_agent_id=owner, owner_agent_name=owner, team_id=world.team,
        owner_named_agent_type="Worker", runtime_state_json=snapshot, custody_takeover=takeover,
        expect_existing=False,
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
    # Removing the task from the old holder's saved work releases only rows it still holds.
    _save(store, a, _snapshot(store.task("next")))
    assert _holder(store, work) == b


def test_a_save_that_still_lists_a_task_another_agent_took_over_is_refused(store):
    """Regression (2026-10-17 review round 2): an admission takeover moves only the
    custody row. A holder wrongly judged gone kept saving and running the task, so it
    ran twice. Its save is refused before anything is written, naming the new holder."""
    a, b, _c = store.owners
    work, other = store.task("work"), store.task("other")
    _save(store, a, _snapshot(work, other))
    _save(store, b, _snapshot(work), takeover=TaskCustodyTakeover(work, a))
    before = store.repo.load_task_runtime_by_owner(a)[0].runtime_state_json
    with pytest.raises(TaskCustodyTakenOver) as refused:
        _save(store, a, _snapshot(work, other, store.task("new")))
    assert refused.value.holders == {work: b}
    assert store.repo.load_task_runtime_by_owner(a)[0].runtime_state_json == before
    assert _holder(store, store.task("new")) is None  # the refused save claimed nothing
    _save(store, a, _snapshot(other, store.task("new")))  # without the taken task it saves
    assert (_holder(store, work), _holder(store, other)) == (b, a)


def test_a_save_refused_for_a_taken_task_neither_consumes_nor_loses_its_deferred_message(store):
    """With main's exact-message hand-off (2026-10-18 merge): a deferred request's
    admission save that the taken-over check refuses rolls back before the message
    is checked or consumed. The coordinator's next save, without the taken task,
    consumes it once."""
    import json

    a, b, _c = store.owners
    work, other, request = store.task("work"), store.task("other"), store.task("request")
    _save(store, a, _snapshot(work, other))
    _save(store, b, _snapshot(work), takeover=TaskCustodyTakeover(work, a))
    envelope = {"msg_header": {"message_id": request}, "msg_content": {"task_id": request}}
    store.repo.add_deferred_message(
        owner_agent_id=a, owner_agent_name=a, team_id=store.team, owner_named_agent_type="Worker",
        message_id=request, envelope_json=json.dumps(envelope), first=False, expect_existing=False,
    )

    def deferred_ids():
        rows = store.repo.load_deferred_messages_by_owner(a)
        return [item["msg_header"]["message_id"] for row in rows for item in json.loads(row.messages_json)]

    def admit(*task_ids):
        store.repo.save_task_runtime(
            owner_agent_id=a, owner_agent_name=a, team_id=store.team, owner_named_agent_type="Worker",
            runtime_state_json=_snapshot(*task_ids), expect_existing=True,
            deferred_message_id=request, consume_deferred_message=True,
        )

    with pytest.raises(TaskCustodyTakenOver):
        admit(work, other, request)
    assert deferred_ids() == [request]
    assert _holder(store, request) is None
    admit(other, request)  # the coordinator dropped the taken task
    assert deferred_ids() == []
    assert (_holder(store, request), _holder(store, work)) == (a, b)


def test_a_record_of_a_task_this_owner_delegated_is_not_its_custody(store):
    """A requester keeps a record of each task it delegated; the worker holds it.
    When the record leaves the delegation map before it leaves the snapshot, the
    worker's row does not make the requester's save look taken over."""
    a, b, _c = store.owners
    parent, child = store.task("parent"), store.task("child")
    _save(store, a, _snapshot(parent, child, delegated={child: parent}))
    _save(store, b, _snapshot(child))
    _save(store, a, _snapshot(parent, child))
    assert (_holder(store, parent), _holder(store, child)) == (a, b)


def test_a_takeover_never_claims_a_task_its_holder_finished_before_releasing_it(store):
    """Regression (2026-10-17 review round 3): a holder judged gone archived the task
    after the admission's archive read but before its own save released custody. The
    compare-and-set still found its row and the task ran twice. The takeover now reads
    the archive inside its transaction and refuses."""
    from storage.task_archive_repository import create_task_archive_repository

    a, b, _c = store.owners
    work = store.task("work")
    _save(store, a, _snapshot(work))
    create_task_archive_repository(store.settings).save_completed_task(
        archive_id=f"archive_{work}", task_id=work, owner_agent_id=a, owner_agent_name=a, team_id=store.team,
        task_json=Task(task_id=work, task_type=MessageMetaType.REQUEST, problem_statement="w").model_dump_json(),
        status="completed",
    )  # finished; a's save that releases custody has not happened yet
    with pytest.raises(TaskCustodyReleased):
        _save(store, b, _snapshot(work), takeover=TaskCustodyTakeover(work, a))
    assert _holder(store, work) == a
    assert store.repo.load_task_runtime_by_owner(b) == []


def test_a_takeover_never_claims_a_task_its_holder_released_meanwhile(store):
    """Regression (2026-10-17 review round 2): a takeover against a holder judged gone
    inserted a fresh row when that holder had finished and released the task between
    the admission's reads and its save, so the task ran again."""
    a, b, _c = store.owners
    work = store.task("work")
    _save(store, a, _snapshot(work))
    _save(store, a, _snapshot(store.task("next")))  # a finished the task: its row is released
    with pytest.raises(TaskCustodyReleased) as refused:
        _save(store, b, _snapshot(work), takeover=TaskCustodyTakeover(work, a))
    assert (refused.value.task_id, refused.value.holder_agent_id) == (work, a)
    assert _holder(store, work) is None
    assert store.repo.load_task_runtime_by_owner(b) == []


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
        runtime_state_json=_snapshot(legacy), expect_existing=False,
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


def test_a_holder_wrongly_judged_gone_drops_the_task_at_its_next_save(tmp_path, caplog):
    """Regression (2026-10-17 review round 2): after another worker took a task over,
    the old holder kept it in memory and in its saves and ran it too. Its next save
    drops the task (no ending, no answer), saves the rest and tells its listener."""
    old_holder, taker = _manager(tmp_path, "worker_a"), _manager(tmp_path, "worker_b")
    _admit(old_holder, "taken")
    _admit(old_holder, "kept")
    _delegate_child(old_holder, "taken", "taken_child")
    released: list[tuple[str, ...]] = []
    old_holder.set_custody_lost_listener(released.append)
    assert _admit(taker, "taken", takeover_from="worker_a")

    with caplog.at_level(logging.WARNING):
        old_holder.update_main_task_state("kept", ProgressState.IN_PROGRESS, _iso())

    assert released == [("taken",)]
    assert old_holder.get_task_info("taken") is None and old_holder.get_task_info("taken_child") is None
    assert old_holder.get_task_info("kept").state == ProgressState.IN_PROGRESS
    saved = old_holder.runtime_state_store.load_task_runtime_by_owner("worker_a")[0].runtime_state_json
    assert "kept" in saved and '"taken"' not in saved
    holders = old_holder.runtime_state_store.get_task_custody("team", ["taken", "kept"])
    assert (holders["taken"].holder_agent_id, holders["kept"].holder_agent_id) == ("worker_b", "worker_a")
    assert old_holder.get_task_from_db("taken") is None  # not ended, so not archived
    assert any("event=task_custody_taken_over" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_released_task_stops_its_delegated_work_but_is_not_cancelled(tmp_path, caplog):
    """Regression (2026-10-17 review round 3): worker 1, alive but judged gone, ran T,
    which had escalated C to TeamArchitect; worker 2 took T over and starts it again.
    Worker 1 dropped T at its next save but C kept running with no barrier or stop.
    Now C gets its own cancellation record and a stop; T itself is not cancelled."""
    from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
    from custom_agents.orphaned_subtasks import register_parent_end_stops

    old_holder = _manager(tmp_path, "worker_1")
    _admit(old_holder, "T")
    _delegate_child(old_holder, "T", "C")
    agent = SimpleNamespace(
        id="worker_1", team_id="team", task_manager=old_holder, _request_subtask_stop=AsyncMock(return_value=True),
    )
    record_publication(agent, subtask_id="C", parent_task_id="T", named_agent_type="TeamArchitect",
                       target_agent_name=None, publish_sequence=5)
    delegated_tasks(agent).record_claim(team_id="team", task_id="C", worker_agent_id="lead",
                                        worker_agent_name="lead", claimed_at=datetime.now(timezone.utc))
    register_parent_end_stops(agent)
    taker = _manager(tmp_path, "worker_2")
    assert _admit(taker, "T", takeover_from="worker_1")

    with caplog.at_level(logging.WARNING):
        old_holder._persist_runtime_state()  # any save of the old holder
        for _ in range(5):
            await asyncio.sleep(0)

    assert old_holder.get_task_info("T") is None and old_holder.get_task_info("C") is None
    assert old_holder.is_execution_cancelled("C")
    assert not taker.is_execution_cancelled("T")
    stop = agent._request_subtask_stop.await_args.kwargs
    assert (stop["parent_task_id"], stop["subtask_info"].task.task_id) == ("T", "C")
    assert "event=released_task_subtasks_stopping" in caplog.text


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


def _delegate_child(manager: TaskManager, parent: str, child: str) -> None:
    assert manager.add_subtask(
        parent,
        Task(task_id=child, parent_task_id=parent, task_type=MessageMetaType.REQUEST, problem_statement="c"),
        MessageHeader(from_agent_id=manager.identity.owner_agent_id), MessageMeta(to_named_agent_type="Other"),
    ) is not None


@pytest.mark.parametrize("child_held_by_its_worker", [True, False])
def test_a_takeover_leaves_out_the_delegations_of_a_parent_it_leaves_out(tmp_path, child_held_by_its_worker):
    """Regression (2026-10-17 review): A ran P and delegated C; another worker took P
    over. Recovering A's snapshot left P out but restored A's record of C without its
    parent, so C looked like a task the replacement runs: recovery failed when C's
    worker held it, or the replacement claimed C and never ran it."""
    gone = _manager(tmp_path, "worker_gone")
    _admit(gone, "P")
    _delegate_child(gone, "P", "C")
    if child_held_by_its_worker:
        assert _admit(_manager(tmp_path, "worker_2"), "C")
    assert _admit(_manager(tmp_path, "worker_3"), "P", takeover_from="worker_gone")
    replacement = _manager(tmp_path, "worker_r")
    restored = replacement.restore_runtime_state_for_owner_ids(["worker_gone"])
    assert restored["task_count"] == 0
    assert replacement.get_task_info("C") is None
    holders = replacement.runtime_state_store.get_task_custody("team", ["P", "C"])
    assert holders["P"].holder_agent_id == "worker_3"
    if child_held_by_its_worker:
        assert holders["C"].holder_agent_id == "worker_2"
    else:
        assert "C" not in holders
    assert replacement.runtime_state_store.load_task_runtime_by_owner("worker_gone") == []


# -- startup recovery -----------------------------------------------------------


def _starting_agent(manager: TaskManager) -> SimpleNamespace:
    return SimpleNamespace(
        id=manager.identity.owner_agent_id, team_id="team", named_agent_type="Worker",
        same_role_runtime_takeover_enabled=True, task_manager=manager, _logger=logging.getLogger("t"),
        deferred_inbox=SimpleNamespace(
            stored_team_id=AsyncMock(return_value=None), restore_role=AsyncMock(return_value=False),
            restore=AsyncMock(return_value=False),
        ),
        _recovery_retry_deadline_seconds=lambda: None,
    )


@pytest.mark.asyncio
async def test_startup_reads_saved_work_again_after_a_custody_conflict(tmp_path, monkeypatch, caplog):
    """Regression (2026-10-17 review round 2): a task claimed by a redelivered copy
    while a starting agent adopted a gone agent's work made recovery fail with
    RuntimeRecoveryConflict, which startup did not handle, so the agent never
    started. It now reads again once and leaves the contested task out."""
    from custom_agents import runtime_recovery

    gone = _manager(tmp_path, "worker_gone")
    _admit(gone, "contested")
    _admit(gone, "free")
    starting = _manager(tmp_path, "worker_new")
    monkeypatch.setattr(
        runtime_recovery, "gone_owner_ids",
        AsyncMock(side_effect=lambda _agent, ids, **_kw: [owner for owner in ids if owner == "worker_gone"]),
    )
    store = starting.runtime_state_store
    real_commit = store.commit_task_runtime_recovery
    attempts: list[int] = []

    def commit_after_a_copy_is_admitted(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            assert _admit(_manager(tmp_path, "worker_copy"), "contested", takeover_from="worker_gone")
        return real_commit(**kwargs)

    monkeypatch.setattr(store, "commit_task_runtime_recovery", commit_after_a_copy_is_admitted)
    with caplog.at_level(logging.WARNING):
        await runtime_recovery.recover_runtime_after_restart(_starting_agent(starting))

    assert len(attempts) == 2
    assert starting.get_task_info("free") is not None and starting.get_task_info("contested") is None
    holders = store.get_task_custody("team", ["contested", "free"])
    assert (holders["contested"].holder_agent_id, holders["free"].holder_agent_id) == ("worker_copy", "worker_new")
    assert "event=runtime_recovery_conflict_retrying" in caplog.text


@pytest.mark.asyncio
async def test_startup_stops_the_delegated_work_of_a_task_it_leaves_out(tmp_path, monkeypatch, caplog):
    """Follow-up to the 2026-10-17 review: recovery left out a gone agent's delegated
    tasks together with a parent another worker took over, but their worker kept
    running them and answered the gone requester. They now get a barrier and a stop;
    the parent the other worker runs is untouched."""
    from custom_agents import runtime_recovery

    gone = _manager(tmp_path, "worker_gone")
    _admit(gone, "P")
    _delegate_child(gone, "P", "C")
    taker = _manager(tmp_path, "worker_3")
    assert _admit(taker, "P", takeover_from="worker_gone")
    starting = _manager(tmp_path, "worker_new")
    monkeypatch.setattr(
        runtime_recovery, "gone_owner_ids",
        AsyncMock(side_effect=lambda _agent, ids, **_kw: [owner for owner in ids if owner == "worker_gone"]),
    )
    agent = _starting_agent(starting)
    agent._request_subtask_stop = AsyncMock(return_value=True)

    with caplog.at_level(logging.WARNING):
        await runtime_recovery.recover_runtime_after_restart(agent)

    stop = agent._request_subtask_stop.await_args.kwargs
    assert (stop["parent_task_id"], stop["subtask_info"].task.task_id) == ("P", "C")
    assert starting.is_execution_cancelled("C")
    assert not taker.is_execution_cancelled("P")
    assert "event=left_out_delegation_stopping" in caplog.text


@pytest.mark.asyncio
async def test_startup_skips_gone_agents_work_that_conflicts_twice(tmp_path, monkeypatch, caplog):
    """A takeover of gone agents' work that conflicts on both reads starts without that
    work: it stays saved with its holder, and the lead's replacement pass adopts it if
    that holder is gone; a startup agent that stopped here would stop the runtime."""
    from custom_agents import runtime_recovery

    gone = _manager(tmp_path, "worker_gone")
    _admit(gone, "t1")
    starting = _manager(tmp_path, "worker_new")
    monkeypatch.setattr(
        runtime_recovery, "gone_owner_ids",
        AsyncMock(side_effect=lambda _agent, ids, **_kw: [owner for owner in ids if owner == "worker_gone"]),
    )

    def always_changed(**_kwargs):
        raise RuntimeRecoveryConflict("Runtime recovery source or destination changed.")

    monkeypatch.setattr(starting.runtime_state_store, "commit_task_runtime_recovery", always_changed)
    with caplog.at_level(logging.WARNING):
        await runtime_recovery.recover_runtime_after_restart(_starting_agent(starting))

    lost = [r for r in caplog.records if "event=role_takeover_lost_race" in r.getMessage()]
    assert len(lost) == 1 and lost[0].levelno == logging.ERROR
    assert "agent_id=worker_new team_id=team owner_agent_ids=['worker_gone']" in lost[0].getMessage()
    assert starting.get_task_info("t1") is None  # nothing adopted
    assert gone.runtime_state_store.load_task_runtime_by_owner("worker_gone") != []


@pytest.mark.asyncio
async def test_startup_stops_when_its_own_saved_work_conflicts_twice(tmp_path, monkeypatch, caplog):
    """Starting without the agent's own saved work could leave it unrun or let its first
    save replace it, so a second conflict there stops the start with a clear error."""
    from custom_agents import runtime_recovery

    starting = _manager(tmp_path, "worker_new")
    _admit(starting, "mine")
    restarted = _manager(tmp_path, "worker_new")
    monkeypatch.setattr(
        runtime_recovery, "gone_owner_ids", AsyncMock(return_value=[]),
    )

    def always_changed(**_kwargs):
        raise RuntimeRecoveryConflict("Runtime recovery source or destination changed.")

    monkeypatch.setattr(restarted.runtime_state_store, "commit_task_runtime_recovery", always_changed)
    with caplog.at_level(logging.WARNING), pytest.raises(runtime_recovery.RuntimeRecoveryStartFailed) as failed:
        await runtime_recovery.recover_runtime_after_restart(_starting_agent(restarted))
    assert (failed.value.agent_id, failed.value.team_id, failed.value.named_agent_type) == ("worker_new", "team", "Worker")
    assert "event=runtime_recovery_conflict_retrying" in caplog.text
    assert "event=runtime_recovery_conflict_start_failed" in caplog.text
    assert restarted.get_task_info("mine") is None
    assert restarted.runtime_state_store.load_task_runtime_by_owner("worker_new") != []


# -- admission decision --------------------------------------------------------


def _agent(tmp_path, agent_id="worker_b", holder="worker_a", holder_gone=False, monkeypatch=None):
    manager = _manager(tmp_path, holder)
    _admit(manager, "t1")
    agent = SimpleNamespace(
        id=agent_id, team_id="team", task_manager=_manager(tmp_path, agent_id),
        _logger=logging.getLogger("custody-test"),
    )
    _liveness(monkeypatch, holder_gone)
    return agent


def _liveness(monkeypatch, gone: bool) -> list:
    """The team's owner-liveness rule (agent_liveness, tested there) decides; custody asks it."""
    asked: list = []

    def decide(_agent, holder, *, purpose, now=None, **_kwargs):
        asked.append((holder, purpose))
        return SimpleNamespace(gone=gone)

    monkeypatch.setattr(task_custody, "owner_liveness", decide)
    return asked


def test_admission_drops_a_copy_while_its_holder_lives(tmp_path, monkeypatch):
    agent = _agent(tmp_path, holder_gone=False, monkeypatch=monkeypatch)
    decision = task_custody.decide_admission(agent, "t1", None)
    assert (decision.admit, decision.reason, decision.holder_agent_id) == (False, task_custody.CUSTODY_HELD, "worker_a")
    assert task_custody.decide_admission(agent, "free_task", None).reason == task_custody.CUSTODY_FREE


def test_admission_takes_over_from_a_gone_holder_or_one_that_handed_it_back(tmp_path, monkeypatch):
    live = _agent(tmp_path, holder_gone=False, monkeypatch=monkeypatch)
    handed_back = task_custody.decide_admission(live, "t1", "worker_a")
    assert (handed_back.admit, handed_back.takeover_from) == (True, "worker_a")
    asked = _liveness(monkeypatch, True)
    decision = task_custody.decide_admission(live, "t1", None)
    assert (decision.admit, decision.reason, decision.takeover_from) == (
        True, task_custody.CUSTODY_HOLDER_GONE, "worker_a"
    )
    assert asked == [("worker_a", LivenessPurpose.CUSTODY_ADMISSION)]


@pytest.mark.asyncio
async def test_a_copy_of_a_task_its_live_holder_finishes_during_admission_is_dropped(tmp_path, monkeypatch):
    """Regression (2026-10-17 review): admission read the archive before custody. A live
    holder that archived and released the task right after the archive read made the
    copy look free. Custody is read first now (a holder archives before it releases),
    so the copy is dropped while the holder still holds it."""
    from custom_agents import task_message_runtime

    holder = _manager(tmp_path, "worker_a")
    task = Task(task_id="t1", parent_task_id="p", task_type=MessageMetaType.REQUEST, problem_statement="w")
    assert holder.add_task(task, MessageHeader(from_agent_id="r"), MessageMeta(), owner_person_id=None)
    taker = _manager(tmp_path, "worker_b")
    monkeypatch.setattr(task_custody, "holder_gone", lambda _agent, _holder, now=None: False)
    archive_read = taker.get_task_from_db

    def read_then_holder_finishes(task_id, **kwargs):
        found = archive_read(task_id, **kwargs)
        holder.complete_task(task_id, _iso())  # archives, then its save releases custody
        return found

    taker.get_task_from_db = read_then_holder_finishes
    agent = SimpleNamespace(id="worker_b", team_id="team", task_manager=taker, _logger=logging.getLogger("t"))
    decision = await task_message_runtime._custody_for_new_task(
        agent, task, MessageHeader(message_id="m2"), MessageMeta(), "queue"
    )
    assert decision is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder_ends_task", "reason", "admitted"),
    [
        ("finishes", task_custody.CUSTODY_HOLDER_GONE, None),  # finished: the copy is a duplicate
        ("hands_back", task_custody.CUSTODY_HOLDER_GONE, None),  # the lead re-sends its own copy
        ("hands_back", task_custody.CUSTODY_HANDED_BACK, True),  # this is that re-sent copy
    ],
)
async def test_a_copy_whose_holder_released_it_before_the_takeover_is_never_claimed_afresh(
    tmp_path, caplog, holder_ends_task, reason, admitted
):
    """Regression (2026-10-17 review round 2): a holder judged gone finished the task
    after both admission reads; the takeover then inserted a fresh row and ran the
    task again. Now the compare-and-set fails, the archive is read again, and only
    the lead's re-sent copy after a handback is admitted."""
    from custom_agents import task_message_runtime

    holder, taker = _manager(tmp_path, "worker_a"), _manager(tmp_path, "worker_b")
    _admit(holder, "t1")
    _admit(holder, "t2")  # keeps the holder's snapshot (and its row for t2) alive
    if holder_ends_task == "finishes":
        holder.complete_task("t1", _iso())
    else:
        holder.remove_task("t1")  # lease-loss handback
    agent = SimpleNamespace(id="worker_b", team_id="team", task_manager=taker, _logger=logging.getLogger("t"))
    custody = task_custody.CustodyDecision(True, reason, "worker_a")

    async def admit(takeover_from):
        return _admit(taker, "t1", takeover_from=takeover_from)

    with pytest.raises(TaskCustodyReleased) as released:
        await admit(custody.takeover_from)
    with caplog.at_level(logging.WARNING):
        outcome = await task_message_runtime._admit_after_holder_released(
            agent, custody, released.value, admit, MessageHeader(message_id="m"), "queue"
        )
    assert outcome is admitted
    holders = taker.runtime_state_store.get_task_custody("team", ["t1"])
    assert (holders["t1"].holder_agent_id if holders else None) == ("worker_b" if admitted else None)
    expected_event = (
        "duplicate_archived_delivery_ignored" if holder_ends_task == "finishes"
        else None if admitted else "released_by_holder_meanwhile"
    )
    if expected_event:
        assert any(expected_event in record.getMessage() for record in caplog.records)


@pytest.mark.live
@pytest.mark.postgres
def test_postgres_recovery_never_overwrites_a_claim_made_meanwhile(monkeypatch):
    """Regression (2026-10-17 review): recovering a snapshot saved before custody
    existed (no row) upserted the replacement's row unconditionally. On PostgreSQL a
    copy admitted concurrently claimed the task first, the upsert overwrote that
    claim, and both agents held the task. The update now applies only to rows the
    sources or the replacement hold, and anything else is a recovery conflict."""
    import threading

    from storage import task_custody_repository as custody

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL custody")
    config = {"backend": "postgres", "postgres": {"dsn": dsn}}
    repo = create_runtime_state_repository(resolve_storage_settings(config))
    other = create_runtime_state_repository(resolve_storage_settings(config))
    run = uuid4().hex[:8]
    team, gone, taker, replacement, task = (f"{name}_{run}" for name in ("team", "gone", "taker", "repl", "task"))
    repo.save_task_runtime(
        owner_agent_id=gone, owner_agent_name=gone, team_id=None, owner_named_agent_type="Worker",
        runtime_state_json=_snapshot(task), expect_existing=False,
    )
    sources = repo.load_task_runtime_by_owner(gone)
    real_now = custody._now
    armed = {"on": True}
    admitted: list[str] = []

    def admit_copy() -> None:
        other.save_task_runtime(
            owner_agent_id=taker, owner_agent_name=taker, team_id=team, owner_named_agent_type="Worker",
            runtime_state_json=_snapshot(task), expect_existing=False,
        )
        admitted.append(taker)

    def now_with_a_copy_admitted_first() -> str:
        if armed["on"]:
            armed["on"] = False
            racer = threading.Thread(target=admit_copy)
            racer.start()
            racer.join(timeout=10)
        return real_now()

    monkeypatch.setattr(custody, "_now", now_with_a_copy_admitted_first)
    try:
        with pytest.raises(RuntimeRecoveryConflict):
            repo.commit_task_runtime_recovery(
                sources=sources, replacement=replace(sources[0], owner_agent_id=replacement, team_id=team)
            )
        assert admitted == [taker]
        assert repo.get_task_custody(team, [task])[task].holder_agent_id == taker
        assert repo.load_task_runtime_by_owner(replacement) == []
        assert repo.load_task_runtime_by_owner(gone) != []
    finally:
        for owner in (gone, taker, replacement):
            repo.clear_task_runtime(owner)


def test_a_missing_archive_table_counts_as_no_finished_task_and_is_logged_once(caplog, monkeypatch):
    """A store used on its own has no finished-task archive; a running team always does,
    so the takeover's archive check logs the missing table once instead of passing silently."""
    import sqlite3

    import storage.task_custody_repository as custody_repository
    from storage.sql_dialect import SQLITE

    monkeypatch.setattr(custody_repository, "_MISSING_ARCHIVE_LOGGED", False)
    conn = sqlite3.connect(":memory:")
    with caplog.at_level("WARNING", logger=custody_repository.__name__):
        for task_id in ("t1", "t2"):
            assert not custody_repository.finished_by_team(conn, SQLITE, team_id="team", task_id=task_id)
    warnings = [record for record in caplog.records if "task_custody_archive_missing" in record.getMessage()]
    assert len(warnings) == 1
