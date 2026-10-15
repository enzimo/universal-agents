"""Explicit mutable state owned by task and workflow components."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, fields, replace
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
