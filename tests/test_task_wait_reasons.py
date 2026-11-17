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
from custom_agents.task_message_runtime import SECURE_CREDENTIAL_RESULT_EVENT
from custom_agents.task_steering_runtime import INTERRUPT_REQUEST_EVENT, INTERRUPT_WAITS_ON_REQUESTER_KEY
from custom_agents.task_wait_reasons import (
    IDLE_STATES,
    PLAN_STORE,
    RUNNING_STATES,
    SETUP_RUN_STORE,
    TERMINAL_STATES,
    OutsideRecords,
    SavedWork,
    UnreadableWaitRecord,
    WaitReason,
    last_started_at,
    saved_wait_reason,
    wait_reason,
)
from custom_agents.tool_setup_runs import RUN_FINISHED, RUN_STOPPED, RUN_WAITING, ToolSetupRunV1
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import (
    BATCH_WAITING_EXTERNAL_RESULT,
    PlanStepTaskLink,
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
from custom_messages.system_schedules import PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX

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
    "event being handled": (WaitReason.PENDING_EVENT, lambda: work(record(), runtime_mailbox_events=event("processing"))),
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
    "outside result after an earlier form's result": (
        WaitReason.OUTSIDE_RESULT,
        lambda: work(
            record(history=[entry(1.2, event=SECURE_CREDENTIAL_RESULT_EVENT)]),
            turn_action_batches=dict([batch("b1", BATCH_WAITING_EXTERNAL_RESULT, 1.5)]),
        ),
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
    # The batch keeps its waiting status after the wait ends (review F1).
    "an outside result that has arrived": lambda: work(
        record(history=[entry(1.8, event=SECURE_CREDENTIAL_RESULT_EVENT)]),
        turn_action_batches=dict([batch("b1", BATCH_WAITING_EXTERNAL_RESULT, 1.5)]),
    ),
    "an outside result after which the task ran again": lambda: work(
        record(states=[(ProgressState.IN_PROGRESS, at(1)), (W, at(1.6)), (ProgressState.IN_PROGRESS, at(3)), (W, at(4))]),
        turn_action_batches=dict([batch("b1", BATCH_WAITING_EXTERNAL_RESULT, 1.5)]),
    ),
    # A received record logs an event handled when it arrived (review F2).
    "a received record of an event handled when it arrived": lambda: work(
        record(), runtime_mailbox_events=event("received")
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


def test_a_plan_steps_root_is_its_plan_root() -> None:
    """Leases and held events are keyed by the plan root (review C07)."""
    step_link = {"step": PlanStepTaskLink(root_task_id="root", plan_id="plan", step_id="s1", step_task_id="step")}
    leased = work(record("root"), record("step", ProgressState.ACCEPTED), plan_step_task_links=step_link,
                  runtime_leases=lease(root="root"))
    assert leased.root_of("step") == "root"
    assert saved_wait_reason(leased, leased.tasks["step"]) is WaitReason.RUNNING_ACTIONS
    held = work(record("root"), record("step", ProgressState.ACCEPTED), plan_step_task_links=step_link,
                runtime_mailbox_events={"e1": RuntimeMailboxEventRecord(
                    event_id="e1", root_task_id="root", event_type="task_response", event_class="deferrable",
                    status="deferred")})
    assert saved_wait_reason(held, held.tasks["step"]) is WaitReason.PENDING_EVENT


def test_a_recovery_tasks_root_is_the_task_it_recovers() -> None:
    saved = work(record("t"), record("rec", ProgressState.ACCEPTED), recovery_links=recovery(),
                 runtime_leases=lease(root="t"))
    assert saved.root_of("rec") == "t"
    assert saved_wait_reason(saved, saved.tasks["rec"]) is WaitReason.RUNNING_ACTIONS


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


# -- waits recorded in team stores (OutsideRecords) ---------------------------------------


class Readers:
    """Fake store readers that count their calls."""

    def __init__(self, *, schedules=(), runs=None, plans=None, failing=None):
        self.calls = {"schedules": 0, "runs": 0, "plans": 0}
        self.schedules, self.runs, self.plans, self.failing = list(schedules), runs or {}, plans or {}, failing

    def records(self, waiting_access=()) -> OutsideRecords:
        return OutsideRecords(
            waiting_access_task_ids=frozenset(waiting_access),
            schedule_rows=self.schedule_rows,
            setup_runs_for_task=self.setup_runs,
            plan=self.plan,
        )

    def schedule_rows(self):
        self.calls["schedules"] += 1
        if self.failing == "schedules":
            raise RuntimeError("database is locked")
        return self.schedules

    def setup_runs(self, task_id):
        self.calls["runs"] += 1
        if self.failing == "runs":
            raise ValueError("damaged row")
        return self.runs.get(task_id, [])

    def plan(self, plan_id, scope):
        self.calls["plans"] += 1
        if self.failing == "plans":
            raise ValueError("damaged row")
        assert scope == "manager:team:TeamArchitect"
        return self.plans.get(plan_id)


SCOPE = "manager:team:TeamArchitect"


def schedule(name: str, *, enabled: int = 1, remaining: int | None = 1) -> dict[str, Any]:
    return {"schedule_name": name, "enabled": enabled, "remaining_count": remaining}


def run(status: str) -> ToolSetupRunV1:
    return ToolSetupRunV1(
        run_id="run-1", team_id="team", person_id="jordan", task_id="t", agent_id="lead",
        agent_type="TeamArchitect", tool_ids=("http:registrar",), status=status,
    )


RETRY = PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX


@pytest.mark.parametrize(
    ("reason", "readers", "waiting_access", "sections"),
    [
        (WaitReason.ACCESS_REQUEST, Readers(), {"t"}, {}),
        (WaitReason.RETRY_TIMER, Readers(schedules=[schedule(f"{RETRY}t:pause-1")]), (), {}),
        (WaitReason.SETUP_RUN, Readers(runs={"t": [run(RUN_WAITING)]}), (), {}),
        (
            WaitReason.PLAN_EXECUTION,
            Readers(plans={"plan-1": _plan(ExecutionPlanItemStatus.PLANNED, active="t")}),
            (),
            {"task_to_plan_id": {"t": "plan-1"}},
        ),
    ],
)
def test_each_store_recorded_wait_is_found(reason, readers, waiting_access, sections) -> None:
    saved = work(record(), **sections)
    assert wait_reason(saved, saved.tasks["t"], readers.records(waiting_access), plan_scope=SCOPE) is reason


@pytest.mark.parametrize(
    "readers",
    [
        pytest.param(Readers(schedules=[schedule(f"{RETRY}t2:pause-1")]), id="another task's retry timer"),
        pytest.param(Readers(schedules=[schedule(f"{RETRY}t:pause-1", enabled=0)]), id="a turned-off timer"),
        pytest.param(Readers(schedules=[schedule(f"{RETRY}t:pause-1", remaining=0)]), id="a timer with no runs left"),
        pytest.param(Readers(schedules=[schedule("t:pause-1")]), id="a schedule of another kind"),
        pytest.param(Readers(runs={"t": [run(RUN_FINISHED), run(RUN_STOPPED)]}), id="ended setup runs"),
        pytest.param(Readers(plans={"plan-1": _plan(ExecutionPlanItemStatus.COMPLETED, active="t")}), id="a finished plan"),
    ],
)
def test_store_records_that_no_longer_hold_the_task_explain_nothing(readers) -> None:
    saved = work(record(), task_to_plan_id={"t": "plan-1"})
    assert wait_reason(saved, saved.tasks["t"], readers.records(), plan_scope=SCOPE) is None


def test_a_plan_step_is_not_excused_by_its_running_plan() -> None:
    """Its own wake is its queue entry; excusing it would hide a plan that cannot go on."""
    readers = Readers(plans={"plan-1": _plan(ExecutionPlanItemStatus.IN_PROGRESS, active="t")})
    step_link = {"step": PlanStepTaskLink(root_task_id="t", plan_id="plan-1", step_id="s1", step_task_id="step")}
    saved = work(record(), record("step", ProgressState.ACCEPTED), plan_step_task_links=step_link,
                 task_to_plan_id={"t": "plan-1", "step": "plan-1"})
    outside = readers.records()
    assert wait_reason(saved, saved.tasks["t"], outside, plan_scope=SCOPE) is WaitReason.PLAN_EXECUTION
    assert wait_reason(saved, saved.tasks["step"], outside, plan_scope=SCOPE) is None


def test_stores_are_read_only_when_saved_work_explains_nothing_and_schedules_once() -> None:
    readers = Readers()
    outside = readers.records()
    queued = work(record(state=ProgressState.ACCEPTED), task_queue=["t"])
    assert wait_reason(queued, queued.tasks["t"], outside, plan_scope=SCOPE) is WaitReason.QUEUED
    assert readers.calls == {"schedules": 0, "runs": 0, "plans": 0}

    stranded = work(record("a"), record("b"))
    for task_id in ("a", "b"):
        assert wait_reason(stranded, stranded.tasks[task_id], outside, plan_scope=SCOPE) is None
    assert readers.calls == {"schedules": 1, "runs": 2, "plans": 0}


def test_a_running_plan_never_makes_the_pass_read_every_schedule() -> None:
    """Review C10: a plan's root task waits here on every pass while its steps run."""
    readers = Readers(plans={"plan-1": _plan(ExecutionPlanItemStatus.IN_PROGRESS, active="t")})
    saved = work(record(), task_to_plan_id={"t": "plan-1"})
    assert wait_reason(saved, saved.tasks["t"], readers.records(), plan_scope=SCOPE) is WaitReason.PLAN_EXECUTION
    assert readers.calls == {"schedules": 0, "runs": 0, "plans": 1}


@pytest.mark.parametrize(("failing", "store"), [("runs", SETUP_RUN_STORE), ("plans", PLAN_STORE)])
def test_an_unreadable_row_about_one_task_names_its_store(failing, store) -> None:
    saved = work(record(), task_to_plan_id={"t": "plan-1"})
    with pytest.raises(UnreadableWaitRecord) as raised:
        wait_reason(saved, saved.tasks["t"], Readers(failing=failing).records(), plan_scope=SCOPE)
    assert (raised.value.store, raised.value.task_id) == (store, "t")
    assert isinstance(raised.value.__cause__, ValueError)


def test_an_unreadable_team_wide_list_fails_the_whole_check() -> None:
    saved = work(record())
    with pytest.raises(RuntimeError):
        wait_reason(saved, saved.tasks["t"], Readers(failing="schedules").records(), plan_scope=SCOPE)
