"""Explicit mutable state owned by task and workflow components."""

from __future__ import annotations

import logging
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



@dataclass(frozen=True)
class TurnStartNotes:
    """What a turn read for itself at its start: its next-turn note and partial tool-call limit.

    They are released when the turn's decision is recorded: in the same task
    lane step and the same snapshot save as the decision's outbox batch
    (``create_turn_action_batch``), or, for a result saved outside the outbox
    (a budget pause, a work-session yield), when the turn returns. A turn that
    pauses for an approval hands them to its interrupted session, and the
    resumed turn's batch releases them. A cancel, failover or failure before
    the decision is recorded keeps them for the task's next turn.

    Why at the decision and not later: a repeated delegation is not
    deduplicated across turns. Each turn's batch gets a new id, a delegation's
    idempotency key falls back to the batch id and action index, and receivers
    deduplicate only by task id or message id. A note kept after its decision
    was recorded would make a later turn act on it again.
    """

    task_id: str
    resume_directive: Optional[str] = None
    partial_tool_call_limit: Optional[int] = None

    @classmethod
    def read(
        cls, task_id: Optional[str], resume_directive: Optional[str], partial_tool_call_limit: Optional[int]
    ) -> Optional["TurnStartNotes"]:
        """What a turn read for its task, or None when it read nothing to release."""
        if not task_id or (not resume_directive and partial_tool_call_limit is None):
            return None
        return cls(task_id, resume_directive, partial_tool_call_limit)


def release_turn_start_notes(
    state: TaskRuntimeState, notes: TurnStartNotes, logger: logging.Logger
) -> bool:
    """Remove what a turn read, keeping what was added since; True when the state changed.

    Notes are only ever added after the existing text
    (``TaskManager.append_runtime_resume_directive``), so the note a turn read
    is the start of the stored note. A partial limit is removed only if no
    newer one replaced it.
    """
    changed = False
    applied = notes.resume_directive
    current = state.runtime_resume_directives.get(notes.task_id)
    if applied and current is not None:
        if current == applied:
            del state.runtime_resume_directives[notes.task_id]
            changed = True
        elif current.startswith(f"{applied}\n\n"):
            state.runtime_resume_directives[notes.task_id] = current[len(applied) + 2 :]
            changed = True
        else:
            logger.warning(
                "The note for a task's next turn changed during the turn in an unexpected way; "
                "it is kept for the next turn. event=resume_note_kept_after_turn task_id=%s",
                notes.task_id,
            )
    limit = notes.partial_tool_call_limit
    if limit is not None and state.partial_tool_call_limit_overrides.get(notes.task_id) == limit:
        del state.partial_tool_call_limit_overrides[notes.task_id]
        changed = True
    return changed

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
