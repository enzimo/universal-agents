"""Map validated persistence contracts into owned runtime state.

Decoding belongs to the versioned contract. This module owns recovery policy:
cancellation wins, duplicate source tasks keep the first owner, abandoned
dispatch attempts become retryable, and source order defines queue order.
Neither operation performs I/O or reaches through an agent/manager facade.
"""

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import logging
from typing import TypeVar

from custom_agents.task_records import TaskInfo
from custom_agents.task_runtime_state import (
    ExecutionRuntimeState,
    ObjectiveRuntimeState,
    PlanRuntimeState,
    TaskRuntimeState,
    WorkflowRuntimeState,
)
from custom_messages.contracts.runtime_snapshot_v3 import (
    RuntimeSnapshotV3,
    SnapshotRecord,
    TaskSnapshotRecord,
)


@dataclass(frozen=True)
class SnapshotSource:
    owner_agent_id: str
    updated_at: str | None
    snapshot: RuntimeSnapshotV3


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


Record = TypeVar("Record", bound=SnapshotRecord)


def _records(
    snapshots: Sequence[RuntimeSnapshotV3],
    select: Callable[[RuntimeSnapshotV3], dict[str, Record]],
    retain: Callable[[Record], bool],
) -> dict[str, Record]:
    return {
        key: value for snapshot in snapshots for key, value in select(snapshot).items()
        if retain(value)
    }


def _restore_tasks(
    sources: Sequence[SnapshotSource], cancelled: set[str], logger: logging.Logger,
) -> dict[str, TaskInfo]:
    tasks: dict[str, TaskInfo] = {}
    for source in sources:
        for item in source.snapshot.tasks:
            if item.task_id in cancelled or item.task_id in tasks:
                logger.warning(
                    "Snapshot task excluded by recovery policy",
                    extra={"task_id": item.task_id, "source_owner_agent_id": source.owner_agent_id,
                           "reason": "cancelled" if item.task_id in cancelled else "duplicate"},
                )
                continue
            tasks[item.task_id] = TaskInfo.from_snapshot(
                task=item.task, header=item.header, meta=item.meta, state=item.state,
                owner_person_id=item.owner_person_id,
                restored_runtime_updated_at=source.updated_at,
            )
    return tasks


def _restore_task_relations(state: TaskRuntimeState, snapshots: Sequence[RuntimeSnapshotV3]) -> None:
    tasks = state.tasks
    for snapshot in snapshots:
        for task_id, queries in snapshot.task_queries.items():
            if task_id in tasks:
                state.task_to_taskquery_list.setdefault(task_id, []).extend(queries)
        state.subtask_to_parent_map.update({
            child: parent for child, parent in snapshot.subtask_to_parent_map.items()
            if child in tasks and parent in tasks
        })
        state.waiting_top_level_task_ids.update(
            task_id for task_id in snapshot.waiting_top_level_task_ids
            if task_id in tasks and tasks[task_id].task.parent_task_id is None
        )
    for child, parent in state.subtask_to_parent_map.items():
        state.task_to_subtask_list.setdefault(parent, []).append(tasks[child])
    for task_id in tasks:
        state.task_to_taskquery_list.setdefault(task_id, [])
    state.task_queue = deque(dict.fromkeys(
        task_id for snapshot in snapshots for task_id in snapshot.task_queue
        if task_id in tasks and task_id not in state.subtask_to_parent_map
    ))


def _restore_task_controls(state: TaskRuntimeState, snapshots: Sequence[RuntimeSnapshotV3]) -> None:
    tasks = state.tasks
    state.recoverable_pause_records = _records(
        snapshots, lambda s: s.recoverable_pause_records, lambda r: r.task_id in tasks,
    )
    for snapshot in snapshots:
        state.partial_tool_call_limit_overrides.update({
            key: value for key, value in snapshot.partial_tool_call_limit_overrides.items() if key in tasks
        })
        state.runtime_resume_directives.update({
            key: value.strip() for key, value in snapshot.runtime_resume_directives.items()
            if key in tasks and value.strip()
        })
        state.human_interaction_blockers.update({
            key: dict(value) for key, value in snapshot.human_interaction_blockers.items() if key in tasks
        })


def _restore_execution(
    snapshots: Sequence[RuntimeSnapshotV3], tasks: dict[str, TaskInfo], logger: logging.Logger,
) -> ExecutionRuntimeState:
    state = ExecutionRuntimeState()
    state.turn_action_batches = _records(snapshots, lambda s: s.turn_action_batches, lambda r: r.source_task_id in tasks)
    state.turn_action_outbox = _records(
        snapshots, lambda s: s.turn_action_outbox,
        lambda r: r.source_task_id in tasks and r.batch_id in state.turn_action_batches,
    )
    state.runtime_leases = _records(
        snapshots, lambda s: s.runtime_leases,
        lambda r: r.root_task_id in tasks and r.source_task_id in tasks,
    )
    state.runtime_mailbox_events = _records(
        snapshots, lambda s: s.runtime_mailbox_events,
        lambda r: r.root_task_id in tasks and r.status in {"received", "deferred", "processing"},
    )
    state.task_state_projection_records = _records(
        snapshots, lambda s: s.task_state_projection_records, lambda r: r.task_id in tasks,
    )
    _recover_interrupted_operations(state, logger)
    return state


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


def _restore_plans(snapshots: Sequence[RuntimeSnapshotV3], tasks: dict[str, TaskInfo]) -> PlanRuntimeState:
    state = PlanRuntimeState()
    for snapshot in snapshots:
        state.task_to_plan_id.update({key: value for key, value in snapshot.task_to_plan_id.items() if key in tasks and value})
        state.final_review_attempt_counts.update({key: value for key, value in snapshot.final_review_attempt_counts.items() if key in tasks})
        state.final_review_pending_task_ids.update(key for key in snapshot.final_review_pending_task_ids if key in tasks)
    state.recovery_links = _records(
        snapshots, lambda s: s.recovery_links,
        lambda r: r.original_task_id in tasks or r.recovery_task_id in tasks,
    )
    state.plan_step_task_links = _records(
        snapshots, lambda s: s.plan_step_task_links,
        lambda r: r.root_task_id in tasks or r.step_task_id in tasks,
    )
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
    return state


def _restore_objectives(snapshots: Iterable[RuntimeSnapshotV3], tasks: dict[str, TaskInfo]) -> ObjectiveRuntimeState:
    state = ObjectiveRuntimeState()
    for snapshot in snapshots:
        state.task_to_objective_id.update({key: value for key, value in snapshot.task_to_objective_id.items() if key in tasks and value})
        state.plan_to_objective_id.update({key: value for key, value in snapshot.plan_to_objective_id.items() if key and value})
    return state


def restore_snapshot_state(
    sources: Sequence[SnapshotSource], *, cancelled: set[str], plans_enabled: bool,
    objectives_enabled: bool, logger: logging.Logger,
) -> WorkflowRuntimeState:
    snapshots = [source.snapshot for source in sources]
    task = TaskRuntimeState(tasks=_restore_tasks(sources, cancelled, logger))
    _restore_task_relations(task, snapshots)
    _restore_task_controls(task, snapshots)
    return WorkflowRuntimeState(
        task,
        _restore_plans(snapshots, task.tasks) if plans_enabled else PlanRuntimeState(),
        _restore_objectives(snapshots, task.tasks) if objectives_enabled else ObjectiveRuntimeState(),
        _restore_execution(snapshots, task.tasks, logger),
    )
