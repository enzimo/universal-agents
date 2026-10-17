"""Explicit mutable state owned by task and workflow components.

``TASK_SCOPED_FIELDS`` (end of this module) is the one owner of task-scoped
records: it names, for every task-scoped field, the tasks each entry belongs
to. ``TaskManager.remove_task`` forgets a removed task's entries through it
(``forget_task_records``) and restore (``runtime_snapshot_codec``) keeps a
saved entry through the same rule, so removal and restore cannot drift apart.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any, Dict, List, Optional

from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink, RecoverablePauseRecord, RecoveryTaskLink, RuntimeMailboxEventRecord, TaskRuntimeLeaseRecord, TaskStateProjectionRecord, TurnActionBatchRecord, TurnActionOutboxRecord
from custom_agents.task_records import TaskInfo
from custom_messages.contracts.task_v1 import TaskQuery


@dataclass
class TaskRuntimeState:
    tasks: Dict[str, TaskInfo] = field(default_factory=dict)
    subtask_to_parent_map: Dict[str, str] = field(default_factory=dict)
    task_to_subtask_list: Dict[str, List[TaskInfo]] = field(default_factory=dict)
    task_to_taskquery_list: Dict[str, List[TaskQuery]] = field(default_factory=dict)
    task_queue: deque[str] = field(default_factory=deque)
    current_in_progress_task_id: Optional[str] = None
    waiting_top_level_task_ids: set[str] = field(default_factory=set)
    recoverable_pause_records: Dict[str, RecoverablePauseRecord] = field(
        default_factory=dict
    )
    partial_tool_call_limit_overrides: Dict[str, int] = field(default_factory=dict)
    runtime_resume_directives: Dict[str, str] = field(default_factory=dict)
    human_interaction_blockers: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class PlanRuntimeState:
    task_to_plan_id: Dict[str, str] = field(default_factory=dict)
    recovery_links: Dict[str, RecoveryTaskLink] = field(default_factory=dict)
    plan_step_task_links: Dict[str, PlanStepTaskLink] = field(default_factory=dict)
    final_review_attempt_counts: Dict[str, int] = field(default_factory=dict)
    final_review_pending_task_ids: set[str] = field(default_factory=set)


@dataclass
class ObjectiveRuntimeState:
    task_to_objective_id: Dict[str, str] = field(default_factory=dict)
    plan_to_objective_id: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExecutionRuntimeState:
    turn_action_batches: Dict[str, TurnActionBatchRecord] = field(default_factory=dict)
    turn_action_outbox: Dict[str, TurnActionOutboxRecord] = field(default_factory=dict)
    runtime_leases: Dict[str, TaskRuntimeLeaseRecord] = field(default_factory=dict)
    runtime_mailbox_events: Dict[str, RuntimeMailboxEventRecord] = field(
        default_factory=dict
    )
    task_state_projection_records: Dict[str, TaskStateProjectionRecord] = field(
        default_factory=dict
    )


@dataclass
class RuntimeOwnerIdentity:
    owner_agent_id: Optional[str] = None
    owner_agent_name: Optional[str] = None
    team_id: Optional[str] = None
    owner_named_agent_type: Optional[str] = None


@dataclass(frozen=True)
class WorkflowRuntimeState:
    """The snapshot owner coordinates these records as one persistence unit."""

    task_state: TaskRuntimeState
    plan_state: PlanRuntimeState
    objective_state: ObjectiveRuntimeState
    execution_state: ExecutionRuntimeState

    def checkpoint(self) -> WorkflowRuntimeState:
        """Save field references before decoding replaces whole state collections."""
        return WorkflowRuntimeState(
            replace(self.task_state),
            replace(self.plan_state),
            replace(self.objective_state),
            replace(self.execution_state),
        )

    def restore(self, checkpoint: WorkflowRuntimeState) -> None:
        """Restore only these declared records, never arbitrary host attributes."""
        for name in ("task_state", "plan_state", "objective_state", "execution_state"):
            target, source = getattr(self, name), getattr(checkpoint, name)
            for item in fields(source):
                setattr(target, item.name, getattr(source, item.name))


# -- task-scoped records --------------------------------------------------------
#
# Why one table (2026-10-17): remove_task used to clear only some task-keyed
# maps. Resume notes, pause records, tool-call overrides, blockers and Plan and
# Objective links of finished tasks stayed in memory and in every later
# snapshot, growing with each finished task until a restart's restore dropped
# them. A takeover could also attach one snapshot's leftover note to the same
# task restored from another snapshot. Removal and restore each had their own
# idea of which entries belong to a task; this table is now the only one.


class KeepRule(StrEnum):
    """When an entry that names several tasks stays."""

    # Links between two tasks (a recovery task and the task it recovers, a
    # PlanStep task and its root) stay while either side is still active.
    WHILE_ANY = "while_any"
    # Records of one running task tree (leases, deferred mailbox events) go as
    # soon as any task they name leaves.
    WHILE_ALL = "while_all"


# (key, value, whole state) -> the ids of the tasks the entry belongs to.
EntryOwners = Callable[[Any, Any, "WorkflowRuntimeState"], tuple[Optional[str], ...]]


def _key_owner(key: Any, _value: Any, _state: "WorkflowRuntimeState") -> tuple[Optional[str], ...]:
    return (key,)


def _record_owners(*attributes: str) -> EntryOwners:
    def owners(_key: Any, record: Any, _state: "WorkflowRuntimeState") -> tuple[Optional[str], ...]:
        return tuple(getattr(record, attribute, None) for attribute in attributes)

    return owners


def _plan_owners(plan_id: Any, _value: Any, state: "WorkflowRuntimeState") -> tuple[Optional[str], ...]:
    # A plan -> Objective entry is a cache of a fact the Objective record also
    # holds; it is needed only while a task linked to that plan is active.
    return tuple(task_id for task_id, linked in state.plan_state.task_to_plan_id.items() if linked == plan_id)


@dataclass(frozen=True)
class TaskScopedField:
    """One task-scoped field and the tasks each of its entries belongs to."""

    state: str
    name: str
    owners: EntryOwners
    keep: KeepRule = KeepRule.WHILE_ANY
    # The RuntimeSnapshotV3 field holding the saved entries, when named differently.
    snapshot_name: Optional[str] = None

    def collection(self, workflow: "WorkflowRuntimeState") -> Any:
        return getattr(getattr(workflow, self.state), self.name)

    def entries(self, collection: Any) -> Iterator[tuple[Any, Any]]:
        """(key, value) pairs; a set of task ids has its members as keys."""
        if isinstance(collection, dict):
            yield from list(collection.items())
        else:
            yield from ((member, None) for member in list(collection))

    def is_kept(
        self,
        key: Any,
        value: Any,
        workflow: "WorkflowRuntimeState",
        *,
        held: Collection[str],
        active: Collection[str],
    ) -> bool:
        """Apply the keep rule to the entry's tasks among those the owner holds.

        ``held`` is every task the owner holds (before a removal, or in the
        saved snapshot); tasks outside it (another agent's) do not decide.
        """
        owners = [owner for owner in self.owners(key, value, workflow) if owner and owner in held]
        if not owners:
            return False
        if self.keep is KeepRule.WHILE_ALL:
            return all(owner in active for owner in owners)
        return any(owner in active for owner in owners)


# Order matters for restore: plan links before the plan -> Objective cache,
# and turn-action batches before their outbox records.
TASK_SCOPED_FIELDS: tuple[TaskScopedField, ...] = (
    TaskScopedField("task_state", "task_to_taskquery_list", _key_owner, snapshot_name="task_queries"),
    TaskScopedField("task_state", "recoverable_pause_records", _record_owners("task_id")),
    TaskScopedField("task_state", "partial_tool_call_limit_overrides", _key_owner),
    TaskScopedField("task_state", "runtime_resume_directives", _key_owner),
    TaskScopedField("task_state", "human_interaction_blockers", _key_owner),
    TaskScopedField("plan_state", "task_to_plan_id", _key_owner),
    TaskScopedField("plan_state", "recovery_links", _record_owners("original_task_id", "recovery_task_id")),
    TaskScopedField("plan_state", "plan_step_task_links", _record_owners("root_task_id", "step_task_id")),
    TaskScopedField("plan_state", "final_review_attempt_counts", _key_owner),
    TaskScopedField("plan_state", "final_review_pending_task_ids", _key_owner),
    TaskScopedField("objective_state", "task_to_objective_id", _key_owner),
    TaskScopedField("objective_state", "plan_to_objective_id", _plan_owners),
    TaskScopedField("execution_state", "turn_action_batches", _record_owners("source_task_id")),
    TaskScopedField("execution_state", "turn_action_outbox", _record_owners("source_task_id")),
    TaskScopedField(
        "execution_state", "runtime_leases", _record_owners("root_task_id", "source_task_id"),
        keep=KeepRule.WHILE_ALL,
    ),
    TaskScopedField(
        "execution_state", "runtime_mailbox_events",
        _record_owners("root_task_id", "local_task_id", "related_task_id"), keep=KeepRule.WHILE_ALL,
    ),
    TaskScopedField("execution_state", "task_state_projection_records", _record_owners("task_id")),
)

# The task records themselves and their structure, kept by remove_task and
# runtime_snapshot_codec._restore_task_relations directly: these say which
# tasks exist and how they relate, rather than belonging to one task.
TASK_STRUCTURE_FIELDS: frozenset[tuple[str, str]] = frozenset({
    ("task_state", "tasks"),
    ("task_state", "subtask_to_parent_map"),
    ("task_state", "task_to_subtask_list"),
    ("task_state", "task_queue"),
    ("task_state", "current_in_progress_task_id"),
    ("task_state", "waiting_top_level_task_ids"),
})


def forget_task_records(workflow: WorkflowRuntimeState, task_id: str) -> dict[str, int]:
    """Drop the task-scoped entries that no longer belong to an active task.

    Call before the task leaves ``task_state.tasks``. Only entries naming
    ``task_id`` are decided. Returns how many entries each field dropped.
    """
    held = set(workflow.task_state.tasks) | {task_id}
    active = held - {task_id}
    doomed: list[tuple[TaskScopedField, Any]] = []
    for scoped in TASK_SCOPED_FIELDS:
        collection = scoped.collection(workflow)
        for key, value in scoped.entries(collection):
            if task_id not in scoped.owners(key, value, workflow):
                continue
            if not scoped.is_kept(key, value, workflow, held=held, active=active):
                doomed.append((scoped, key))
    # Decide everything before deleting: the plan -> Objective cache reads the
    # task's plan link to find its owners.
    dropped: dict[str, int] = {}
    for scoped, key in doomed:
        collection = scoped.collection(workflow)
        if isinstance(collection, dict):
            collection.pop(key, None)
        else:
            collection.discard(key)
        dropped[scoped.name] = dropped.get(scoped.name, 0) + 1
    return dropped


def cache_task_objective(
    task_state: TaskRuntimeState, objective_state: ObjectiveRuntimeState, task_id: str, objective_id: str,
) -> None:
    """Remember a task's Objective only while this agent holds the task.

    Writers that walk an Objective's or plan's related tasks see finished tasks
    too; caching those recreated links forget_task_records had dropped.
    """
    if task_id in task_state.tasks:
        objective_state.task_to_objective_id[task_id] = objective_id


def cache_plan_objective(
    plan_state: PlanRuntimeState, objective_state: ObjectiveRuntimeState, plan_id: str, objective_id: str,
) -> None:
    """Remember a plan's Objective only while a task this agent holds follows the plan."""
    if plan_id in plan_state.task_to_plan_id.values():
        objective_state.plan_to_objective_id[plan_id] = objective_id
