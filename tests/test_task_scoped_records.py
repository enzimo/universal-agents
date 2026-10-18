"""Task-scoped records have one owner (custom_agents.task_runtime_state.TASK_SCOPED_FIELDS).

Real TaskManager on SQLite. Reproduces the 2026-10-17 finding: remove_task
left resume notes, pause records, tool-call overrides, blockers and Plan and
Objective links of finished tasks in memory and in every later snapshot, and a
takeover attached one snapshot's leftovers to the same task from another.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from custom_agents.task_human_interaction_blockers import (
    get_human_interaction_blocker,
    record_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_agents.task_runtime_state import (
    TASK_SCOPED_FIELDS,
    TASK_STRUCTURE_FIELDS,
    ExecutionRuntimeState,
    ObjectiveRuntimeState,
    PlanRuntimeState,
    TaskRuntimeState,
    WorkflowRuntimeState,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import (
    PlanStepTaskLink,
    RecoverablePauseRecord,
    RecoveryTaskLink,
    RuntimeMailboxEventRecord,
    TaskRuntimeLeaseRecord,
    TaskStateProjectionRecord,
    TurnActionBatchRecord,
    TurnActionOutboxRecord,
    decode_runtime_snapshot,
)
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, ProgressState, RecoverablePauseTrigger
from storage.config import resolve_storage_settings

STATE_CLASSES = {
    "task_state": TaskRuntimeState,
    "plan_state": PlanRuntimeState,
    "objective_state": ObjectiveRuntimeState,
    "execution_state": ExecutionRuntimeState,
}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manager(tmp_path, owner="lead", agent_type="TeamArchitect") -> TaskManager:
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "instance.db")}})
    manager = TaskManager(
        db_path=settings.sqlite_path, logger=logging.getLogger(owner), storage_settings=settings,
        max_queued_top_level_tasks=1000,
    )
    manager.set_owner_identity(owner, owner, "team", agent_type)
    return manager


def _add(manager: TaskManager, task_id: str, *, start: bool = True) -> None:
    assert manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Work {task_id}"),
        MessageHeader(from_agent_id="user"), MessageMeta(conversation_id=f"conv_{task_id}"),
        owner_person_id=None,
    )
    if start:
        manager.task_state.task_queue.remove(task_id)
        manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, _iso())


def _fill_every_field(manager: TaskManager, task_id: str, other: str) -> None:
    """One entry in every task-scoped field for ``task_id`` (links also name ``other``)."""
    ts, ps, os_, es = manager.task_state, manager.plan_state, manager.objective_state, manager.execution_state
    plan_id = f"plan_{task_id}"
    ts.task_to_taskquery_list[task_id] = [
        TaskQuery(task_query_id=f"q_{task_id}", task_id=task_id, query_type=MessageMetaType.REQUEST, message="?")
    ]
    ts.recoverable_pause_records[f"pause_{task_id}"] = RecoverablePauseRecord(
        task_id=task_id, pause_id=f"pause_{task_id}", trigger=RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED.value,
    )
    ts.partial_tool_call_limit_overrides[task_id] = 3
    ts.runtime_resume_directives[task_id] = "Your access request was decided."
    record_human_interaction_blocker(manager, task_id, {"kind": "access_request", "access_request_id": f"r_{task_id}"})
    ps.task_to_plan_id[task_id] = plan_id
    ps.recovery_links[f"rec_{task_id}"] = RecoveryTaskLink(
        original_task_id=task_id, original_plan_id=plan_id, recovery_task_id=f"rec_{task_id}",
        branch_id="b", failure_type="x", failure_summary="y",
    )
    ps.plan_step_task_links[f"step_{task_id}"] = PlanStepTaskLink(
        root_task_id=task_id, plan_id=plan_id, step_id="s1", step_task_id=f"step_{task_id}",
    )
    ps.final_review_attempt_counts[task_id] = 1
    ps.final_review_pending_task_ids.add(task_id)
    os_.task_to_objective_id[task_id] = f"objective_{task_id}"
    os_.plan_to_objective_id[plan_id] = f"objective_{task_id}"
    es.turn_action_batches[f"batch_{task_id}"] = TurnActionBatchRecord(batch_id=f"batch_{task_id}", source_task_id=task_id)
    es.turn_action_outbox[f"act_{task_id}"] = TurnActionOutboxRecord(
        action_id=f"act_{task_id}", batch_id=f"batch_{task_id}", source_task_id=task_id, action_index=0,
        action_type="notify_user", payload_json={},
    )
    es.runtime_leases[f"lease_{task_id}"] = TaskRuntimeLeaseRecord(
        lease_id=f"lease_{task_id}", root_task_id=task_id, source_task_id=task_id, owner_agent_id="lead",
    )
    es.runtime_mailbox_events[f"evt_{task_id}"] = RuntimeMailboxEventRecord(
        event_id=f"evt_{task_id}", root_task_id=other, event_type="task_response", event_class="deferrable",
        status="deferred", related_task_id=task_id,
    )
    es.task_state_projection_records[f"proj_{task_id}"] = TaskStateProjectionRecord(
        projection_id=f"proj_{task_id}", task_id=task_id, from_state="accepted", to_state="in_progress", reason="r",
    )


def _entries_naming(workflow, task_ids: set[str]) -> list[str]:
    found = []
    for scoped in TASK_SCOPED_FIELDS:
        collection = scoped.collection(workflow)
        for key, value in scoped.entries(collection):
            if task_ids & {owner for owner in scoped.owners(key, value, workflow) if owner}:
                found.append(f"{scoped.name}:{key}")
    return found


def _workflow(manager: TaskManager) -> WorkflowRuntimeState:
    return manager.runtime_snapshot_coordinator.state


def _saved_workflow(manager: TaskManager) -> WorkflowRuntimeState | None:
    rows = manager.runtime_state_store.load_task_runtime_by_owner(manager.identity.owner_agent_id)
    if not rows:
        return None
    snapshot = decode_runtime_snapshot(rows[0].runtime_state_json)
    saved = WorkflowRuntimeState(TaskRuntimeState(), PlanRuntimeState(), ObjectiveRuntimeState(), ExecutionRuntimeState())
    for scoped in TASK_SCOPED_FIELDS:
        value = getattr(snapshot, scoped.snapshot_name or scoped.name)
        setattr(getattr(saved, scoped.state), scoped.name, set(value) if isinstance(value, list) else dict(value))
    return saved


def test_every_runtime_state_field_is_classified():
    """A new state field must say whether it belongs to tasks (and to which), or is structure."""
    scoped = {(item.state, item.name) for item in TASK_SCOPED_FIELDS}
    assert not scoped & TASK_STRUCTURE_FIELDS
    declared = {(state, item.name) for state, cls in STATE_CLASSES.items() for item in fields(cls)}
    assert declared == scoped | TASK_STRUCTURE_FIELDS
    for item in TASK_SCOPED_FIELDS:
        assert hasattr(decode_runtime_snapshot('{"schema_version": 3}'), item.snapshot_name or item.name)


@pytest.mark.parametrize("ending", ["complete", "fail", "cancel", "handback_release"])
def test_remove_task_leaves_no_task_scoped_records(tmp_path, ending):
    manager = _manager(tmp_path)
    _add(manager, "keep", start=False)  # keeps the saved snapshot row alive
    _add(manager, "root")
    assert manager.add_subtask(
        "root", Task(task_id="sub", parent_task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="d"),
        MessageHeader(), MessageMeta(),
    )
    _fill_every_field(manager, "root", other="keep")
    _fill_every_field(manager, "sub", other="root")
    _fill_every_field(manager, "keep", other="keep")
    manager._persist_runtime_state()
    now = _iso()
    {
        "complete": lambda: manager.complete_task("root", now),
        "fail": lambda: manager.fail_task("root", "broke", now),
        "cancel": lambda: manager.cancel_task("root", "stopped", now),
        "handback_release": lambda: manager.remove_task("root"),
    }[ending]()

    assert manager.get_task_info("root") is None and manager.get_task_info("sub") is None
    assert _entries_naming(_workflow(manager), {"root", "sub"}) == []
    assert _entries_naming(_saved_workflow(manager), {"root", "sub"}) == []
    # The surviving task keeps all of its own records.
    assert len(_entries_naming(_workflow(manager), {"keep"})) == len(TASK_SCOPED_FIELDS)


def test_snapshot_size_stable_across_finished_tasks(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "keep", start=False)
    sizes = []
    for batch in range(3):
        for index in range(20):
            task_id = f"t{batch}_{index}"
            _add(manager, task_id)
            manager.set_task_plan(task_id, f"Plan for {task_id}")
            manager.create_objective(task_id=task_id, title=task_id, desired_outcome="x", why="y")
            manager.create_recoverable_pause(
                task_id, trigger=RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED, friendly_reason="budget",
            )
            manager.append_runtime_resume_directive(task_id, "note")
            manager.complete_task(task_id, _iso())
        rows = manager.runtime_state_store.load_task_runtime_by_owner("lead")
        sizes.append(len(rows[0].runtime_state_json))
    assert sizes[0] == sizes[1] == sizes[2]
    assert _entries_naming(_workflow(manager), {f"t{b}_{i}" for b in range(3) for i in range(20)}) == []


@pytest.mark.parametrize("handing_back_saves_last", [False, True])
def test_takeover_keeps_task_records_from_custody_source(tmp_path, caplog, handing_back_saves_last):
    """Worker A hands T back (removal without an ending) and keeps waiting task W;
    worker C runs T again; both die and R takes over both snapshots. R must see C's
    records for T only, whatever order the snapshots are read in."""
    first = _manager(tmp_path, "worker_a", "Researcher")
    _add(first, "W")
    first.update_main_task_state("W", ProgressState.WAITING, _iso())
    _add(first, "T")
    first.create_recoverable_pause("T", trigger=RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED, friendly_reason="b")
    first.record_partial_tool_call_limit("T", 2)
    first.append_runtime_resume_directive("T", "A-NOTE")
    first.remove_task("T")
    second = _manager(tmp_path, "worker_c", "Researcher")
    _add(second, "T")
    second.append_runtime_resume_directive("T", "C-NOTE")
    # Leftovers from an earlier release: A's snapshot still has records for T.
    first.task_state.runtime_resume_directives["T"] = "A-NOTE"
    first.task_state.partial_tool_call_limit_overrides["T"] = 2
    (first if handing_back_saves_last else second)._persist_runtime_state()
    (second if handing_back_saves_last else first)._persist_runtime_state()

    replacement = _manager(tmp_path, "worker_r", "Researcher")
    # The order runtime_recovery reads them in: most recently saved first.
    order = [row.owner_agent_id for row in replacement.runtime_state_store.peek_task_runtime_by_role("team", "Researcher")]
    assert order == (["worker_a", "worker_c"] if handing_back_saves_last else ["worker_c", "worker_a"])
    with caplog.at_level(logging.WARNING):
        replacement.restore_runtime_state_for_owner_ids(order)
    assert replacement.task_state.runtime_resume_directives.get("T") == "C-NOTE"
    assert "T" not in replacement.task_state.partial_tool_call_limit_overrides
    assert replacement.count_recoverable_pauses("T") == 0
    dropped = [r for r in caplog.records if getattr(r, "event", None) == "runtime_snapshot_orphans_dropped"]
    assert [r.source_owner_agent_id for r in dropped] == ["worker_a"]
    assert dropped[0].dropped_counts == {"partial_tool_call_limit_overrides": 1, "runtime_resume_directives": 1}


def test_objective_review_after_terminal_does_not_search_repository(tmp_path):
    manager = _manager(tmp_path)
    for ending in ("complete", "cancel"):
        task_id = f"task_{ending}"
        _add(manager, task_id)
        objective = manager.create_objective(task_id=task_id, title="O", desired_outcome="x", why="y")
        with patch.object(
            manager.objective_repository, "get_objective_by_task", side_effect=AssertionError("searched by task id")
        ):
            if ending == "complete":
                manager.complete_task(task_id, _iso())
            else:
                manager.cancel_task(task_id, "stopped", _iso())
        reviewed = manager.get_objective(objective.objective_id)
        events = [e.event_type.value for e in reviewed.event_log if (e.details or {}).get("terminal_task_id") == task_id]
        assert events[:2] == ["review_requested", "review_completed"]
        assert task_id not in manager.objective_state.task_to_objective_id


def test_handback_release_clears_blocker(tmp_path):
    worker = _manager(tmp_path, "worker_a", "Researcher")
    _add(worker, "W")
    _add(worker, "T", start=False)
    record_human_interaction_blocker(worker, "T", {"kind": "access_request", "access_request_id": "r1"})
    worker.remove_task("T")  # lease_loss_wind_down._release_task: no terminal transition
    assert get_human_interaction_blocker(worker, "T") is None
    saved = _saved_workflow(worker)
    assert "T" not in saved.task_state.human_interaction_blockers


def test_restore_leaves_out_delegations_of_a_parent_it_leaves_out_transitively():
    """A snapshot's records of delegated tasks go with their parent, grandchildren too,
    and a record whose parent is missing from every snapshot is left out as well."""
    from custom_agents.runtime_snapshot_codec import SnapshotSource, restore_snapshot_state
    from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord

    def record(task_id):
        return TaskSnapshotRecord(
            task_id=task_id, state=ProgressState.ACCEPTED,
            task=Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="w"),
            header=MessageHeader(message_id=f"m_{task_id}", timestamp="2026-10-17T00:00:00+00:00"),
            meta=MessageMeta(), owner_person_id=None,
        )

    snapshot = RuntimeSnapshotV3(
        schema_version=3,
        tasks=[record(task_id) for task_id in ("P", "C", "G", "K", "O")],
        subtask_to_parent_map={"C": "P", "G": "C", "O": "missing"},
    )
    restored = restore_snapshot_state(
        [SnapshotSource("worker_gone", None, snapshot)], cancelled=set(), plans_enabled=False,
        objectives_enabled=False, logger=logging.getLogger("restore"), taken_elsewhere={"P": "held_by_another_agent"},
    )
    assert sorted(restored.task_state.tasks) == ["K"]
    assert restored.task_state.subtask_to_parent_map == {}


def test_restore_keeps_the_final_review_marker_of_a_restored_task(tmp_path):
    """Regression (2026-10-18 owner review): restore dropped every member of a set
    field, so a final review queued before a restart was no longer marked pending.
    A delegated result that resumed the task before the review turn ran could then
    complete it without the review (task_resume_runtime, subtask_watchdog_runtime,
    task_message_runtime check ``is_final_review_pending``)."""
    manager = _manager(tmp_path)
    for task_id in ("reviewing", "done_elsewhere"):
        assert manager.add_task(
            Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="work"),
            MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None,
        )
    manager.plan_state.final_review_pending_task_ids.update({"reviewing", "not_restored"})
    manager.plan_state.final_review_attempt_counts["reviewing"] = 1
    manager._persist_runtime_state()

    restarted = _manager(tmp_path)
    restarted.restore_runtime_state()

    assert restarted.is_final_review_pending("reviewing")
    assert restarted.plan_state.final_review_attempt_counts == {"reviewing": 1}
    assert restarted.plan_state.final_review_pending_task_ids == {"reviewing"}  # only restored tasks keep one
