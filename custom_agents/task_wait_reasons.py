"""Why a task in an agent's saved work is not running, read from that work alone.

The stuck-task check (``custom_agents.stuck_tasks``) reports a task that is not
running and has nothing that could continue it (``task_waiting_on_nothing``).
That is only safe if every legitimate reason to wait is known, so this module
is the one list of them. Each reason below names the saved record it reads;
the rule matches the runtime's own rule for the same wait, cited in brackets.

Read from the agent's saved work (``RuntimeSnapshotV3``):

- ``QUEUED``: the task is in the agent's queue (``task_queue``). A final
  review waits this way: ``begin_final_review_if_needed`` queues the task as
  PARTIAL at the front.
- ``RUNNING_ACTIONS``: an action batch holds the task tree's runtime lease
  (``get_active_runtime_lease``).
- ``PENDING_EVENT``: a reply or a fired retry was held for the task tree while
  a batch ran and is still to be handled (``runtime_mailbox_events``; the same
  statuses recovery replays).
- ``DELEGATED_WORK``: a task this one delegated has not ended
  (``has_pending_subtasks``).
- ``QUESTION``: the task's latest TaskQuery asks and is unanswered
  (``is_waiting_on_task_query``): a question to the person, or a worker's
  question to its parent.
- ``RECOVERABLE_PAUSE``: a pause still waits for the person's choice
  (``get_pending_recoverable_pause``).
- ``PERSON_DECISION``: a human interaction blocker of any kind: an access,
  change or proposal request, or a ``set_up_tools`` run
  (``task_human_interaction_blockers``). Every interrupt the code raises today
  records one, so a paused turn's live session, which only its own process
  holds in memory, needs no saved record of its own.
- ``OUTSIDE_RESULT``: the task's latest action batch stopped to wait for a
  secure credential form or signup automation (the outbox's external-wait
  barrier, ``BATCH_WAITING_EXTERNAL_RESULT``). Earlier batches do not count.
- ``REQUESTER_ANSWER``: the turn paused on an interrupt it sent to its
  requester (the chat), after the task last started
  (``INTERRUPT_REQUEST_EVENT`` with ``INTERRUPT_WAITS_ON_REQUESTER_KEY``). No
  interrupt raised today does this; access and setup-run interrupts wait
  through their blocker instead, so their history entry never counts here.
- ``ISOLATED_RECOVERY``: a recovery task started for this one is still open and
  not merged back (``recovery_links``).
- ``QUEUE_PAUSE``: a person paused the task in the queue
  (``runtime_task_controls.is_explicit_queue_pause``).

Read from team stores, only for a task with no reason above (``OutsideRecords``):

- ``ACCESS_REQUEST``: an access, change or proposal request for the task still
  waits in the access store.
- ``RETRY_TIMER``: an enabled one-time schedule with runs left named
  ``provider_overload_retry:<task id>:<marker>``: a person's "retry in 5 or 10
  minutes" choice, or a worker's wait after a model provider refused calls.
- ``SETUP_RUN``: a ``set_up_tools`` run for the task still waits
  (``ToolSetupRunRepository``).
- ``PLAN_EXECUTION``: the task is its plan's active execution with a step
  planned or in progress (``plan_execution_active``). Plans live in the plan
  store, and only the team lead has them. A plan step task is not excused by
  its plan: its own wake is its queue entry, and excusing it would hide a plan
  that cannot go on.

A new kind of wait must save a record that one of these rules can find, or
this list must learn it; otherwise the stuck-task check reports a task that is
waiting legitimately. Messages still in transit (the deferred inbox, NATS) need
no rule: each answers a wait whose record above stays saved until the message
is handled.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from custom_agents.agent_liveness import parse_utc
from custom_agents.runtime_snapshot_codec import REPLAYABLE_EVENT_STATUSES
from custom_agents.runtime_task_controls import is_explicit_queue_pause
from custom_agents.task_manager_plans import plan_execution_active
from custom_agents.task_steering_runtime import (
    INTERRUPT_REQUEST_EVENT,
    INTERRUPT_WAITS_ON_REQUESTER_KEY,
)
from custom_agents.tool_setup_runs import RUN_WAITING, ToolSetupRunV1
from custom_messages.contracts.runtime_snapshot_v3 import (
    BATCH_WAITING_EXTERNAL_RESULT,
    RuntimeSnapshotV3,
    TaskSnapshotRecord,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_messages.records.plans import PlanRecord
from custom_messages.system_schedules import PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX

# The runtime's state classes (TaskManager.is_terminal_state and
# is_executing_state); tests keep them equal. A running state means a turn runs
# or is about to, so only the other states can wait on nothing.
TERMINAL_STATES = frozenset(
    {ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.CANCELLED, ProgressState.ABANDONED}
)
RUNNING_STATES = frozenset(
    {ProgressState.FINALIZING_PLAN, ProgressState.IN_PROGRESS, ProgressState.PREPARING, ProgressState.RETRYING}
)
IDLE_STATES = frozenset(set(ProgressState) - TERMINAL_STATES - RUNNING_STATES)

# Saved statuses these rules read (TaskRuntimeLeaseRecord, RecoverablePauseRecord,
# RecoveryTaskLink), as the runtime writes them.
ACTIVE_LEASE = "active"
PENDING_PAUSE = "pending"
UNMERGED_RECOVERY = "pending"


class WaitReason(StrEnum):
    """A legitimate reason a task is not running (module docstring)."""

    QUEUED = "queued"
    RUNNING_ACTIONS = "running_actions"
    PENDING_EVENT = "pending_event"
    DELEGATED_WORK = "delegated_work"
    QUESTION = "question"
    RECOVERABLE_PAUSE = "recoverable_pause"
    PERSON_DECISION = "person_decision"
    OUTSIDE_RESULT = "outside_result"
    REQUESTER_ANSWER = "requester_answer"
    ISOLATED_RECOVERY = "isolated_recovery"
    QUEUE_PAUSE = "queue_pause"
    ACCESS_REQUEST = "access_request"
    RETRY_TIMER = "retry_timer"
    SETUP_RUN = "setup_run"
    PLAN_EXECUTION = "plan_execution"


# Stores a per-task read can fail in (``UnreadableWaitRecord.store``).
SETUP_RUN_STORE = "tool_setup_runs"
PLAN_STORE = "plans"


def last_started_at(task: Task) -> datetime | None:
    """When the task last moved into a running state, or None if it never ran."""
    started = [parse_utc(moment) for state, moment in task.task_state or [] if state in RUNNING_STATES]
    known = [moment for moment in started if moment is not None]
    return max(known) if known else None


# A batch saved without a time sorts before every dated one.
_UNDATED = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SavedWork:
    """One agent's saved work with the records the rules read indexed once.

    Built per check, so one pass is linear in the size of the snapshot rather
    than scanning every map for every task.
    """

    snapshot: RuntimeSnapshotV3
    tasks: Mapping[str, TaskSnapshotRecord]
    queued: frozenset[str]
    parents_with_open_delegations: frozenset[str]
    tasks_with_pending_pause: frozenset[str]
    latest_batch_status: Mapping[str, str]
    roots_with_active_lease: frozenset[str]
    roots_with_pending_event: frozenset[str]
    tasks_in_isolated_recovery: frozenset[str]

    @classmethod
    def from_snapshot(cls, snapshot: RuntimeSnapshotV3) -> SavedWork:
        tasks = {record.task_id: record for record in snapshot.tasks}

        def is_open(task_id: str) -> bool:
            record = tasks.get(task_id)
            return record is not None and record.state not in TERMINAL_STATES

        latest: dict[str, tuple[datetime, str]] = {}
        for batch in snapshot.turn_action_batches.values():
            moment = parse_utc(batch.created_at) or _UNDATED
            known = latest.get(batch.source_task_id)
            # Ties keep the batch saved later (the snapshot keeps insertion order).
            if known is None or moment >= known[0]:
                latest[batch.source_task_id] = (moment, batch.status)
        return cls(
            snapshot=snapshot,
            tasks=tasks,
            queued=frozenset(snapshot.task_queue),
            parents_with_open_delegations=frozenset(
                parent for child, parent in snapshot.subtask_to_parent_map.items() if is_open(child)
            ),
            tasks_with_pending_pause=frozenset(
                pause.task_id for pause in snapshot.recoverable_pause_records.values() if pause.status == PENDING_PAUSE
            ),
            latest_batch_status={task_id: status for task_id, (_moment, status) in latest.items()},
            roots_with_active_lease=frozenset(
                lease.root_task_id for lease in snapshot.runtime_leases.values() if lease.status == ACTIVE_LEASE
            ),
            roots_with_pending_event=frozenset(
                event.root_task_id
                for event in snapshot.runtime_mailbox_events.values()
                if event.status in REPLAYABLE_EVENT_STATUSES
            ),
            tasks_in_isolated_recovery=frozenset(
                link.original_task_id
                for link in snapshot.recovery_links.values()
                if link.merge_status == UNMERGED_RECOVERY and is_open(link.recovery_task_id)
            ),
        )

    def runs_itself(self, task_id: str) -> bool:
        """Whether the agent runs the task, rather than keeping a record of work it delegated."""
        return task_id in self.tasks and task_id not in self.snapshot.subtask_to_parent_map

    def idle_tasks(self) -> list[TaskSnapshotRecord]:
        """The tasks the agent runs itself that are neither running nor finished."""
        return [
            record
            for record in self.snapshot.tasks
            if record.state in IDLE_STATES and self.runs_itself(record.task_id)
        ]

    def root_of(self, task_id: str) -> str:
        """The task tree's root, by the walk of ``resolve_root_task_id``.

        The runtime keys leases and held events by this root. A plan step
        belongs to its plan's root task and a recovery task to the task it
        recovers, so those links come before the parent link.
        """
        seen: set[str] = set()
        current = task_id
        while current not in seen:
            seen.add(current)
            linked = self._linked_execution_parent(current)
            if linked is not None:
                current = linked
                continue
            record = self.tasks.get(current)
            if record is None:
                return current
            parent = self.snapshot.subtask_to_parent_map.get(current) or record.task.parent_task_id
            if not parent or parent not in self.tasks:
                return current
            current = parent
        return current

    def _linked_execution_parent(self, task_id: str) -> str | None:
        """The plan root of a plan step, or the original of a recovery task (``_linked_execution_parent_id``)."""
        step = self.snapshot.plan_step_task_links.get(task_id)
        if step is not None and step.root_task_id != task_id:
            return step.root_task_id
        recovery = self.snapshot.recovery_links.get(task_id)
        if recovery is not None and recovery.original_task_id != task_id:
            return recovery.original_task_id
        return None


def _waits_on_requester(task: Task) -> bool:
    """An interrupt sent to the requester after the task last started (``REQUESTER_ANSWER``)."""
    started = last_started_at(task)
    for entry in task.execution_history or []:
        details = entry.details or {}
        if details.get("event") != INTERRUPT_REQUEST_EVENT or details.get(INTERRUPT_WAITS_ON_REQUESTER_KEY) is not True:
            continue
        asked = parse_utc(entry.timestamp)
        if asked is not None and (started is None or asked > started):
            return True
    return False


def _asks_unanswered_question(work: SavedWork, task_id: str) -> bool:
    queries = work.snapshot.task_queries.get(task_id) or []
    return bool(queries) and queries[-1].query_type == MessageMetaType.REQUEST


def saved_wait_reason(work: SavedWork, record: TaskSnapshotRecord) -> WaitReason | None:
    """The first reason in the agent's saved work why ``record`` legitimately waits, or None."""
    task_id = record.task_id
    root = work.root_of(task_id)
    checks: tuple[tuple[WaitReason, bool], ...] = (
        (WaitReason.QUEUED, task_id in work.queued),
        (WaitReason.RUNNING_ACTIONS, root in work.roots_with_active_lease),
        (WaitReason.PENDING_EVENT, root in work.roots_with_pending_event),
        (WaitReason.DELEGATED_WORK, task_id in work.parents_with_open_delegations),
        (WaitReason.RECOVERABLE_PAUSE, task_id in work.tasks_with_pending_pause),
        (WaitReason.PERSON_DECISION, task_id in work.snapshot.human_interaction_blockers),
        (WaitReason.OUTSIDE_RESULT, work.latest_batch_status.get(task_id) == BATCH_WAITING_EXTERNAL_RESULT),
        (WaitReason.ISOLATED_RECOVERY, task_id in work.tasks_in_isolated_recovery),
    )
    for reason, holds in checks:
        if holds:
            return reason
    # These read the task's own history and queries, so they run last.
    if _asks_unanswered_question(work, task_id):
        return WaitReason.QUESTION
    if _waits_on_requester(record.task):
        return WaitReason.REQUESTER_ANSWER
    if is_explicit_queue_pause(record.task):
        return WaitReason.QUEUE_PAUSE
    return None


class UnreadableWaitRecord(Exception):
    """A store row about one task could not be read, so whether it waits is unknown for now."""

    def __init__(self, store: str, task_id: str) -> None:
        super().__init__(f"The {store} record of task {task_id} could not be read.")
        self.store = store
        self.task_id = task_id


@dataclass
class OutsideRecords:
    """Waits recorded in team stores rather than in an agent's saved work (module docstring).

    The caller injects the readers, so this module does no I/O. The team-wide
    lists (waiting access requests, schedules) are read at most once per pass,
    and their errors propagate, as the check's other list reads do. A row about
    one task that cannot be read raises ``UnreadableWaitRecord``: one bad row
    must not stop the check for every other task.
    """

    waiting_access_task_ids: frozenset[str]
    schedule_rows: Callable[[], Iterable[Mapping[str, Any]]]
    setup_runs_for_task: Callable[[str], Iterable[ToolSetupRunV1]]
    plan: Callable[[str, str], PlanRecord | None]
    _retry_names: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    def has_retry_timer(self, task_id: str) -> bool:
        # The colon ends the task id, so one id never matches a longer one.
        prefix = f"{PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX}{task_id}:"
        return any(name.startswith(prefix) for name in self._retry_timer_names())

    def _retry_timer_names(self) -> tuple[str, ...]:
        if self._retry_names is None:
            names = (str(row.get("schedule_name") or "") for row in self.schedule_rows() if _will_fire(row))
            self._retry_names = tuple(
                name for name in names if name.startswith(PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX)
            )
        return self._retry_names

    def reason(self, work: SavedWork, record: TaskSnapshotRecord, *, plan_scope: str | None) -> WaitReason | None:
        """The first store-recorded reason ``record`` waits; ``plan_scope`` is the owner's plan scope."""
        task_id = record.task_id
        if task_id in self.waiting_access_task_ids:
            return WaitReason.ACCESS_REQUEST
        if self.has_retry_timer(task_id):
            return WaitReason.RETRY_TIMER
        if self._setup_run_waits(task_id):
            return WaitReason.SETUP_RUN
        plan_id = work.snapshot.task_to_plan_id.get(task_id)
        if plan_id and plan_scope and plan_execution_active(self._plan(plan_id, plan_scope, task_id), task_id):
            return WaitReason.PLAN_EXECUTION
        return None

    def _setup_run_waits(self, task_id: str) -> bool:
        try:
            return any(run.status == RUN_WAITING for run in self.setup_runs_for_task(task_id))
        except Exception as exc:  # a bad row (another version, damaged JSON) or a failed read
            raise UnreadableWaitRecord(SETUP_RUN_STORE, task_id) from exc

    def _plan(self, plan_id: str, plan_scope: str, task_id: str) -> PlanRecord | None:
        try:
            return self.plan(plan_id, plan_scope)
        except Exception as exc:  # a bad row (another version, damaged JSON) or a failed read
            raise UnreadableWaitRecord(PLAN_STORE, task_id) from exc


def _will_fire(row: Mapping[str, Any]) -> bool:
    """An enabled schedule with runs left (the filter of ``ScheduleRepository.get_all_enabled``)."""
    remaining = row.get("remaining_count")
    return bool(row.get("enabled")) and (remaining is None or int(remaining) > 0)


def wait_reason(
    work: SavedWork, record: TaskSnapshotRecord, outside: OutsideRecords, *, plan_scope: str | None
) -> WaitReason | None:
    """Why ``record`` legitimately waits: saved work first, then team stores; None when nothing explains it.

    Raises ``UnreadableWaitRecord`` when a store row about the task cannot be read.
    """
    return saved_wait_reason(work, record) or outside.reason(work, record, plan_scope=plan_scope)


__all__ = [
    "IDLE_STATES",
    "PLAN_STORE",
    "RUNNING_STATES",
    "SETUP_RUN_STORE",
    "TERMINAL_STATES",
    "OutsideRecords",
    "SavedWork",
    "UnreadableWaitRecord",
    "WaitReason",
    "last_started_at",
    "saved_wait_reason",
    "wait_reason",
]
