"""Which saved records explain why a task is not running (custom_agents.task_wait_reasons).

Each legitimate wait is found from the saved record the runtime writes for it,
and a record that no longer holds the task (finished, answered, superseded,
merged, or from an earlier pause) does not count. The stuck-task check reports
a task with no reason (tests/test_stuck_tasks.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from custom_agents.runtime_task_controls import QUEUE_PAUSE_EVENT
from custom_agents.task_manager import TaskManager
from custom_agents.task_manager_plans import plan_execution_active
from custom_agents.task_steering_runtime import INTERRUPT_REQUEST_EVENT, INTERRUPT_WAITS_ON_REQUESTER_KEY
from custom_agents.task_wait_reasons import (
    IDLE_STATES,
    RUNNING_STATES,
    TERMINAL_STATES,
    SavedWork,
    WaitReason,
    last_started_at,
    saved_wait_reason,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import (
    BATCH_WAITING_EXTERNAL_RESULT,
    RecoverablePauseRecord,
    RecoveryTaskLink,
    RuntimeMailboxEventRecord,
    RuntimeSnapshotV3,
    TaskRuntimeLeaseRecord,
    TaskSnapshotRecord,
    TurnActionBatchRecord,
)
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskQuery
from custom_messages.enums import ExecutionPlanItemStatus, MessageMetaType, ProgressState
from custom_messages.records.plans import PlanRecord, PlanStepRecord

START = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
W = ProgressState.WAITING


def at(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


def record(task_id: str = "t", state: ProgressState = W, *, parent: str | None = None, states=None, history=()) -> TaskSnapshotRecord:
    """A task that ran at minute 1 and moved to ``state`` at minute 2."""
    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Check two registrars",
        parent_task_id=parent,
        task_state=states or [(ProgressState.ACCEPTED, at(0)), (ProgressState.IN_PROGRESS, at(1)), (state, at(2))],
    )
    task.execution_history = list(history)
    return TaskSnapshotRecord(
        task_id=task_id, state=state, task=task, header=MessageHeader(), meta=MessageMeta(), owner_person_id=None
    )


def entry(minutes: float, **details: Any) -> TaskHistoryEntry:
    return TaskHistoryEntry(summary="event", timestamp=at(minutes), details=details)


def work(*records: TaskSnapshotRecord, **sections: Any) -> SavedWork:
    return SavedWork.from_snapshot(RuntimeSnapshotV3(schema_version=3, tasks=list(records), **sections))


def interrupt(minutes: float, *, waits_on_requester: bool) -> TaskHistoryEntry:
    return entry(minutes, event=INTERRUPT_REQUEST_EVENT, **{INTERRUPT_WAITS_ON_REQUESTER_KEY: waits_on_requester})


def lease(status: str = "active", root: str = "t") -> dict[str, TaskRuntimeLeaseRecord]:
    return {"l1": TaskRuntimeLeaseRecord(lease_id="l1", root_task_id=root, source_task_id=root, owner_agent_id="lead", status=status)}


def event(status: str) -> dict[str, RuntimeMailboxEventRecord]:
    return {
        "e1": RuntimeMailboxEventRecord(
            event_id="e1", root_task_id="t", event_type="task_response", event_class="deferrable", status=status
        )
    }


def query(*types: MessageMetaType) -> dict[str, list[TaskQuery]]:
    return {"t": [TaskQuery(query_type=kind, message="Which registrar?", parent_task_id="t") for kind in types]}


def pause(status: str) -> dict[str, RecoverablePauseRecord]:
    return {"p1": RecoverablePauseRecord(task_id="t", pause_id="p1", trigger="delegated_subtask_timeout", status=status)}


def batch(batch_id: str, status: str, minutes: float) -> tuple[str, TurnActionBatchRecord]:
    return batch_id, TurnActionBatchRecord(batch_id=batch_id, source_task_id="t", status=status, created_at=at(minutes))


def recovery(merge_status: str = "pending") -> dict[str, RecoveryTaskLink]:
    return {
        "rec": RecoveryTaskLink(
            original_task_id="t", original_plan_id="plan", recovery_task_id="rec", branch_id="b",
            failure_type="tool_error", failure_summary="A tool failed.", merge_status=merge_status,
        )
    }


def queue_paused_at(paused_at: str) -> TaskSnapshotRecord:
    paused = [(ProgressState.ACCEPTED, at(0)), (ProgressState.PAUSED, at(2))]
    return record(state=ProgressState.PAUSED, states=paused, history=[entry(2, event=QUEUE_PAUSE_EVENT, paused_at=paused_at)])


WAITS: dict[str, tuple[WaitReason, Callable[[], SavedWork]]] = {
    "queued": (WaitReason.QUEUED, lambda: work(record(state=ProgressState.ACCEPTED), task_queue=["t"])),
    "running batch": (WaitReason.RUNNING_ACTIONS, lambda: work(record(), runtime_leases=lease())),
    "pending event": (WaitReason.PENDING_EVENT, lambda: work(record(), runtime_mailbox_events=event("deferred"))),
    "delegated work": (
        WaitReason.DELEGATED_WORK,
        lambda: work(record(), record("sub", ProgressState.IN_PROGRESS, parent="t"), subtask_to_parent_map={"sub": "t"}),
    ),
    "question": (WaitReason.QUESTION, lambda: work(record(), task_queries=query(MessageMetaType.REQUEST))),
    "recoverable pause": (
        WaitReason.RECOVERABLE_PAUSE, lambda: work(record(state=ProgressState.PAUSED), recoverable_pause_records=pause("pending"))
    ),
    "access request": (
        WaitReason.PERSON_DECISION,
        lambda: work(record(), human_interaction_blockers={"t": {"kind": "access_request", "access_request_id": "req-1"}}),
    ),
    "setup run": (
        WaitReason.PERSON_DECISION,
        lambda: work(record(), human_interaction_blockers={"t": {"kind": "tool_setup_run", "run_id": "run-1"}}),
    ),
    "outside result": (
        WaitReason.OUTSIDE_RESULT,
        lambda: work(record(), turn_action_batches=dict([batch("b1", BATCH_WAITING_EXTERNAL_RESULT, 1.5)])),
    ),
    "requester answer": (
        WaitReason.REQUESTER_ANSWER, lambda: work(record(history=[interrupt(1.5, waits_on_requester=True)]))
    ),
    "isolated recovery": (
        WaitReason.ISOLATED_RECOVERY,
        lambda: work(record(), record("rec", ProgressState.IN_PROGRESS), recovery_links=recovery()),
    ),
    "queue pause": (WaitReason.QUEUE_PAUSE, lambda: work(queue_paused_at(at(2)))),
}


@pytest.mark.parametrize("name", list(WAITS))
def test_each_saved_wait_is_found(name: str) -> None:
    reason, build = WAITS[name]
    saved = build()
    assert saved_wait_reason(saved, saved.tasks["t"]) is reason


ENDED: dict[str, Callable[[], SavedWork]] = {
    "a finished delegation": lambda: work(
        record(), record("sub", ProgressState.COMPLETED, parent="t"), subtask_to_parent_map={"sub": "t"}
    ),
    "an answered question": lambda: work(record(), task_queries=query(MessageMetaType.REQUEST, MessageMetaType.RESPONSE)),
    "a decided pause": lambda: work(record(state=ProgressState.PAUSED), recoverable_pause_records=pause("retry_scheduled")),
    "an outside result followed by a later batch": lambda: work(
        record(), turn_action_batches=dict([batch("b1", BATCH_WAITING_EXTERNAL_RESULT, 1.2), batch("b2", "completed", 1.6)])
    ),
    # An access or setup-run interrupt waits through its blocker; once that is
    # cleared its history entry explains nothing (review C01).
    "an access or setup-run interrupt whose blocker is cleared": lambda: work(
        record(history=[interrupt(1.5, waits_on_requester=False)])
    ),
    "a requester interrupt answered before the task ran again": lambda: work(
        record(
            states=[(ProgressState.IN_PROGRESS, at(1)), (W, at(1.5)), (ProgressState.IN_PROGRESS, at(3)), (W, at(4))],
            history=[interrupt(1.4, waits_on_requester=True)],
        )
    ),
    "a merged recovery": lambda: work(record(), record("rec", ProgressState.IN_PROGRESS), recovery_links=recovery("resumed")),
    "a finished recovery task": lambda: work(record(), record("rec", ProgressState.COMPLETED), recovery_links=recovery()),
    "a queue pause marker from an earlier pause": lambda: work(queue_paused_at(at(0.5))),
    "a released lease": lambda: work(record(), runtime_leases=lease("completed")),
    "a handled event": lambda: work(record(), runtime_mailbox_events=event("processed")),
    "another task's queue entry": lambda: work(record(state=ProgressState.ACCEPTED), task_queue=["other"]),
}


@pytest.mark.parametrize("name", list(ENDED))
def test_records_that_no_longer_hold_the_task_explain_nothing(name: str) -> None:
    saved = ENDED[name]()
    assert saved_wait_reason(saved, saved.tasks["t"]) is None


def test_the_incidents_stranded_task_has_no_reason() -> None:
    """Queued for the workflow, then set WAITING, which removed its queue entry."""
    states = [
        (ProgressState.ACCEPTED, at(0)), (ProgressState.IN_PROGRESS, at(1)), (W, at(2)),
        (ProgressState.IN_PROGRESS, at(3)), (ProgressState.ACCEPTED, at(4)), (W, at(4.1)),
    ]
    stranded = record(states=states, history=[interrupt(1.9, waits_on_requester=False)])
    saved = work(
        stranded,
        turn_action_batches=dict([batch("b1", BATCH_WAITING_EXTERNAL_RESULT, 1.5), batch("b2", "completed", 3.5)]),
        runtime_leases=lease("completed"),
        recoverable_pause_records=pause("resolved"),
    )
    assert saved_wait_reason(saved, stranded) is None


def test_a_reason_on_the_tree_root_holds_its_other_tasks() -> None:
    saved = work(record("root"), record("t", parent="root"), runtime_leases=lease(root="root"))
    assert saved.root_of("t") == "root"
    assert saved_wait_reason(saved, saved.tasks["t"]) is WaitReason.RUNNING_ACTIONS


def test_root_walk_stops_at_a_parent_loop() -> None:
    saved = work(record("a", parent="b"), record("b", parent="a"))
    assert saved.root_of("a") in {"a", "b"}


def test_only_idle_tasks_the_agent_runs_itself_are_checked() -> None:
    saved = work(
        record("waiting"),
        record("delegated", W, parent="waiting"),
        record("running", ProgressState.IN_PROGRESS),
        record("done", ProgressState.COMPLETED),
        record("accepted", ProgressState.ACCEPTED),
        subtask_to_parent_map={"delegated": "waiting"},
    )
    assert [item.task_id for item in saved.idle_tasks()] == ["waiting", "accepted"]


def test_state_classes_match_the_task_manager() -> None:
    assert TERMINAL_STATES == {state for state in ProgressState if TaskManager.is_terminal_state(state)}
    assert RUNNING_STATES == {state for state in ProgressState if TaskManager.is_executing_state(state)}
    assert IDLE_STATES == set(ProgressState) - TERMINAL_STATES - RUNNING_STATES


def test_last_start_is_the_latest_move_into_a_running_state() -> None:
    assert last_started_at(record().task) == START + timedelta(minutes=1)
    assert last_started_at(record(states=[(ProgressState.ACCEPTED, at(0))]).task) is None


def _plan(*statuses: ExecutionPlanItemStatus, active: str | None = "root") -> PlanRecord:
    steps = [PlanStepRecord(title=f"Step {index}", objective="Do it", status=status) for index, status in enumerate(statuses)]
    return PlanRecord(root_task_id="root", active_execution_task_id=active, plan_steps=steps)


@pytest.mark.parametrize(
    ("plan", "running"),
    [
        (None, False),
        (_plan(), False),
        (_plan(ExecutionPlanItemStatus.PLANNED, active="other"), False),
        (_plan(ExecutionPlanItemStatus.COMPLETED), False),
        (_plan(ExecutionPlanItemStatus.COMPLETED, ExecutionPlanItemStatus.PLANNED), True),
        (_plan(ExecutionPlanItemStatus.IN_PROGRESS), True),
    ],
)
def test_plan_execution_rule(plan: PlanRecord | None, running: bool) -> None:
    assert plan_execution_active(plan, "root") is running
