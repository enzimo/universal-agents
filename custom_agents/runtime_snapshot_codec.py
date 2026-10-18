"""Map validated persistence contracts into owned runtime state.

Decoding belongs to the versioned contract. This module owns recovery policy:
cancellation wins, a task another agent holds is not adopted, duplicate source
tasks keep the first owner and a task's records come only from the snapshot
whose task record was kept, abandoned dispatch attempts become retryable, and
source order defines queue order.
Neither operation performs I/O or reaches through an agent/manager facade.
"""

from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import logging

from custom_agents.task_records import TaskInfo
from custom_agents.task_runtime_state import (
    TASK_SCOPED_FIELDS,
    ExecutionRuntimeState,
    ObjectiveRuntimeState,
    PlanRuntimeState,
    TaskRuntimeState,
    TaskScopedField,
    WorkflowRuntimeState,
)
from custom_messages.contracts.runtime_snapshot_v3 import (
    RuntimeSnapshotV3,
    TaskSnapshotRecord,
)


# Why recovery left a saved task out (logged as ``reason``).
CANCELLED = "cancelled"
DUPLICATE = "duplicate"
HELD_BY_ANOTHER_AGENT = "held_by_another_agent"
FINISHED_BY_ANOTHER_AGENT = "finished_by_another_agent"
PARENT_LEFT_OUT = "parent_left_out"


@dataclass(frozen=True)
class SnapshotSource:
    owner_agent_id: str
    updated_at: str | None
    snapshot: RuntimeSnapshotV3


@dataclass(frozen=True)
class LeftOutDelegation:
    """A delegated task recovery left out with its parent; its worker should stop.

    Another agent holds or finished the parent and runs it with delegations of
    its own; this one would answer a requester that is gone
    (``orphaned_subtasks.stop_left_out_delegations``).
    """

    parent_task_id: str
    task_info: TaskInfo


def capture_task(task: TaskInfo) -> TaskSnapshotRecord:
    return TaskSnapshotRecord(
        task_id=task.task.task_id, state=task.state,
        task=task.task, header=task.header, meta=task.meta,
        owner_person_id=task.owner_person_id,
    )


def capture_runtime_snapshot(
    state: WorkflowRuntimeState, *, plans_enabled: bool, objectives_enabled: bool,
) -> RuntimeSnapshotV3:
    task, plan, objective, execution = (
        state.task_state, state.plan_state, state.objective_state, state.execution_state,
    )
    return RuntimeSnapshotV3(
        schema_version=3,
        tasks=[capture_task(info) for info in task.tasks.values()],
        task_queue=list(task.task_queue),
        subtask_to_parent_map=task.subtask_to_parent_map,
        task_queries=task.task_to_taskquery_list,
        turn_action_batches=execution.turn_action_batches,
        turn_action_outbox=execution.turn_action_outbox,
        runtime_leases=execution.runtime_leases,
        runtime_mailbox_events=execution.runtime_mailbox_events,
        task_state_projection_records=execution.task_state_projection_records,
        task_to_plan_id=plan.task_to_plan_id if plans_enabled else {},
        task_to_objective_id=objective.task_to_objective_id if objectives_enabled else {},
        plan_to_objective_id=objective.plan_to_objective_id if objectives_enabled else {},
        recovery_links=plan.recovery_links if plans_enabled else {},
        plan_step_task_links=plan.plan_step_task_links if plans_enabled else {},
        recoverable_pause_records=task.recoverable_pause_records,
        partial_tool_call_limit_overrides=task.partial_tool_call_limit_overrides,
        runtime_resume_directives=task.runtime_resume_directives,
        human_interaction_blockers=task.human_interaction_blockers,
        final_review_attempt_counts=plan.final_review_attempt_counts if plans_enabled else {},
        final_review_pending_task_ids=sorted(plan.final_review_pending_task_ids) if plans_enabled else [],
        current_in_progress_task_id=task.current_in_progress_task_id,
        waiting_top_level_task_ids=sorted(task.waiting_top_level_task_ids),
    )


def _restore_tasks(
    sources: Sequence[SnapshotSource], excluded: dict[str, str], logger: logging.Logger,
    left_out: list[LeftOutDelegation],
) -> tuple[dict[str, TaskInfo], dict[str, int]]:
    """Restore task records; return them and, per task, the source index kept.

    ``excluded`` maps task ids recovery must not adopt to the reason
    (cancelled, or held or finished by another agent). Records of tasks the
    snapshot delegated are left out with their parent.
    """
    tasks: dict[str, TaskInfo] = {}
    kept_from: dict[str, int] = {}
    for index, source in enumerate(sources):
        for item in source.snapshot.tasks:
            reason = excluded.get(item.task_id) or (DUPLICATE if item.task_id in tasks else None)
            if reason is not None:
                logger.warning(
                    "Snapshot task excluded by recovery policy",
                    extra={"task_id": item.task_id, "source_owner_agent_id": source.owner_agent_id,
                           "reason": reason},
                )
                continue
            tasks[item.task_id] = TaskInfo.from_snapshot(
                task=item.task, header=item.header, meta=item.meta, state=item.state,
                owner_person_id=item.owner_person_id,
                restored_runtime_updated_at=source.updated_at,
            )
            kept_from[item.task_id] = index
    _leave_out_orphaned_delegations(sources, tasks, kept_from, logger, left_out)
    return tasks, kept_from


def _leave_out_orphaned_delegations(
    sources: Sequence[SnapshotSource], tasks: dict[str, TaskInfo], kept_from: dict[str, int],
    logger: logging.Logger, left_out: list[LeftOutDelegation],
) -> None:
    """Leave out records of delegated tasks whose parent is not restored, transitively.

    A snapshot keeps a record of each task its owner delegated
    (``subtask_to_parent_map``). Without its parent the record would lose its
    parent link and look like a task the restoring agent runs itself: it would
    claim the task's custody (or conflict with the worker that holds it and
    fail the recovery) and never run it (found 2026-10-17).
    """
    changed = True
    while changed:
        changed = False
        for index, source in enumerate(sources):
            for child, parent in source.snapshot.subtask_to_parent_map.items():
                if kept_from.get(child) != index or parent in tasks:
                    continue
                left_out.append(LeftOutDelegation(parent_task_id=parent, task_info=tasks.pop(child)))
                del kept_from[child]
                changed = True
                logger.warning(
                    "Snapshot task excluded by recovery policy",
                    extra={"task_id": child, "source_owner_agent_id": source.owner_agent_id,
                           "reason": PARENT_LEFT_OUT, "parent_task_id": parent},
                )


def _from_source(kept_from: dict[str, int], index: int) -> set[str]:
    return {task_id for task_id, origin in kept_from.items() if origin == index}


def _restore_task_relations(
    state: TaskRuntimeState, sources: Sequence[SnapshotSource], kept_from: dict[str, int],
) -> None:
    """Rebuild task structure from the snapshot whose task record was kept."""
    tasks = state.tasks
    for index, source in enumerate(sources):
        mine = _from_source(kept_from, index)
        snapshot = source.snapshot
        state.subtask_to_parent_map.update({
            child: parent for child, parent in snapshot.subtask_to_parent_map.items()
            if child in mine and parent in tasks
        })
        state.waiting_top_level_task_ids.update(
            task_id for task_id in snapshot.waiting_top_level_task_ids
            if task_id in mine and tasks[task_id].task.parent_task_id is None
        )
    for child, parent in state.subtask_to_parent_map.items():
        state.task_to_subtask_list.setdefault(parent, []).append(tasks[child])
    state.task_queue = deque(dict.fromkeys(
        task_id for index, source in enumerate(sources) for task_id in source.snapshot.task_queue
        if kept_from.get(task_id) == index and task_id not in state.subtask_to_parent_map
    ))


# Recovery policy applied to an entry its task kept: return the value to
# restore, or None to leave it out. Ownership itself is decided only by
# TASK_SCOPED_FIELDS.
RestorePolicy = Callable[[object, object, WorkflowRuntimeState], object | None]


def _non_empty(_key: object, value: object, _state: WorkflowRuntimeState) -> object | None:
    return value or None


def _stripped_note(_key: object, value: object, _state: WorkflowRuntimeState) -> object | None:
    return str(value).strip() or None


def _copied(_key: object, value: object, _state: WorkflowRuntimeState) -> object | None:
    return dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value


def _outbox_with_batch(_key: object, value: object, state: WorkflowRuntimeState) -> object | None:
    return value if getattr(value, "batch_id", None) in state.execution_state.turn_action_batches else None


def _replayable_event(_key: object, value: object, _state: WorkflowRuntimeState) -> object | None:
    return value if getattr(value, "status", None) in {"received", "deferred", "processing"} else None


_RESTORE_POLICY: dict[str, RestorePolicy] = {
    "task_to_taskquery_list": _copied,
    "runtime_resume_directives": _stripped_note,
    "human_interaction_blockers": _copied,
    "task_to_plan_id": _non_empty,
    "task_to_objective_id": _non_empty,
    "plan_to_objective_id": _non_empty,
    "turn_action_outbox": _outbox_with_batch,
    "runtime_mailbox_events": _replayable_event,
}


def _saved_entries(scoped: TaskScopedField, snapshot: RuntimeSnapshotV3) -> Iterable[tuple[object, object]]:
    saved = getattr(snapshot, scoped.snapshot_name or scoped.name)
    if isinstance(saved, dict):
        return list(saved.items())
    return [(member, None) for member in saved]


def _put(collection: object, key: object, value: object) -> None:
    if isinstance(collection, dict):
        collection[key] = value
    elif isinstance(collection, set):
        collection.add(key)


def _restore_entries(
    scoped: TaskScopedField, workflow: WorkflowRuntimeState, snapshot: RuntimeSnapshotV3,
    *, held: set[str], mine: set[str],
) -> int:
    """Copy one source's entries of one field that a task restored from it owns; return how many were dropped."""
    collection = scoped.collection(workflow)
    policy = _RESTORE_POLICY.get(scoped.name)
    dropped = 0
    for key, value in _saved_entries(scoped, snapshot):
        if not scoped.is_kept(key, value, workflow, held=held, active=mine):
            dropped += 1
            continue
        restored = policy(key, value, workflow) if policy is not None else value
        if restored is not None:
            _put(collection, key, restored)
    return dropped


def _restore_scoped_records(
    workflow: WorkflowRuntimeState, sources: Sequence[SnapshotSource], kept_from: dict[str, int],
    *, enabled_states: set[str], logger: logging.Logger,
) -> None:
    """Copy each task-scoped entry from the snapshot whose task record was kept.

    A takeover merges several snapshots that may each hold entries for one
    task id; only the snapshot that supplied the task record decides, and the
    keep rule is the one remove_task uses. Entries no restored task owns are
    dropped and counted, one warning per source.
    """
    origins = [
        ({item.task_id for item in source.snapshot.tasks}, _from_source(kept_from, index))
        for index, source in enumerate(sources)
    ]
    dropped: list[Counter[str]] = [Counter() for _ in sources]
    for scoped in TASK_SCOPED_FIELDS:
        if scoped.state not in enabled_states:
            continue
        for index, source in enumerate(sources):
            held, mine = origins[index]
            dropped[index][scoped.name] += _restore_entries(scoped, workflow, source.snapshot, held=held, mine=mine)
    _warn_dropped_orphans(sources, dropped, logger)


def _warn_dropped_orphans(
    sources: Sequence[SnapshotSource], dropped: list[Counter[str]], logger: logging.Logger,
) -> None:
    for source, counts in zip(sources, dropped):
        counts = +counts  # only fields that dropped something
        if not counts:
            continue
        total = sum(counts.values())
        by_field = dict(sorted(counts.items()))
        logger.warning(
            "Dropped saved task records whose task was not restored from this snapshot. "
            "event=runtime_snapshot_orphans_dropped source_owner_agent_id=%s total=%s counts=%s",
            source.owner_agent_id, total, by_field,
            extra={"event": "runtime_snapshot_orphans_dropped", "source_owner_agent_id": source.owner_agent_id,
                   "dropped_total": total, "dropped_counts": by_field},
        )


def _link_plans_from_task_links(state: PlanRuntimeState, tasks: dict[str, TaskInfo]) -> None:
    for link in state.recovery_links.values():
        if link.recovery_task_id in tasks:
            state.task_to_plan_id[link.recovery_task_id] = link.original_plan_id
        if link.original_task_id in tasks:
            state.task_to_plan_id.setdefault(link.original_task_id, link.original_plan_id)
    for step_link in state.plan_step_task_links.values():
        if step_link.step_task_id in tasks:
            state.task_to_plan_id[step_link.step_task_id] = step_link.plan_id
        if step_link.root_task_id in tasks:
            state.task_to_plan_id.setdefault(step_link.root_task_id, step_link.plan_id)


def _recover_interrupted_operations(state: ExecutionRuntimeState, logger: logging.Logger) -> None:
    for key, record in state.turn_action_outbox.items():
        if record.status != "dispatching":
            continue
        state.turn_action_outbox[key] = record.model_copy(update={
            "status": "retry_scheduled", "recoverability": "automatic_retry",
            "last_error": record.last_error or "Recovered from stale dispatching state after runtime restart.",
        })
        logger.warning("Recovering interrupted outbox dispatch", extra={"action_id": key, "task_id": record.source_task_id})
    for key, event in state.runtime_mailbox_events.items():
        if event.status != "processing":
            continue
        state.runtime_mailbox_events[key] = event.model_copy(update={
            "status": "deferred",
            "last_error": event.last_error or "Recovered from in-progress mailbox processing after restart.",
        })
        logger.warning("Recovering interrupted mailbox processing", extra={"event_id": key, "task_id": event.root_task_id})


def restore_snapshot_state(
    sources: Sequence[SnapshotSource], *, cancelled: set[str], plans_enabled: bool,
    objectives_enabled: bool, logger: logging.Logger, taken_elsewhere: Mapping[str, str] | None = None,
    left_out_delegations: list[LeftOutDelegation] | None = None,
) -> WorkflowRuntimeState:
    """Merge snapshots into one owner's state.

    ``taken_elsewhere`` maps tasks another agent of the team holds or already
    finished (``storage.task_custody_repository``) to the reason; like
    cancelled tasks they are not adopted. Delegated tasks left out with their
    parent are appended to ``left_out_delegations`` when given.
    """
    excluded = dict(taken_elsewhere or {})
    excluded.update({task_id: CANCELLED for task_id in cancelled})
    left_out = left_out_delegations if left_out_delegations is not None else []
    tasks, kept_from = _restore_tasks(sources, excluded, logger, left_out)
    workflow = WorkflowRuntimeState(
        TaskRuntimeState(tasks=tasks), PlanRuntimeState(), ObjectiveRuntimeState(), ExecutionRuntimeState(),
    )
    _restore_task_relations(workflow.task_state, sources, kept_from)
    enabled_states = {"task_state", "execution_state"}
    if plans_enabled:
        enabled_states.add("plan_state")
    if objectives_enabled:
        enabled_states.add("objective_state")
    _restore_scoped_records(workflow, sources, kept_from, enabled_states=enabled_states, logger=logger)
    for task_id in tasks:
        workflow.task_state.task_to_taskquery_list.setdefault(task_id, [])
    if plans_enabled:
        _link_plans_from_task_links(workflow.plan_state, tasks)
    _recover_interrupted_operations(workflow.execution_state, logger)
    return workflow
