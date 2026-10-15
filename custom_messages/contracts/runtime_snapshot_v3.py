"""Version 3 snapshot records. Validation never invents ids or timestamps.

These are persistence contracts, independent of mutable task-owner collections.
Transport envelopes and UI rendering have separate owners.
"""

from typing import Annotated, Any, Dict, Literal, Optional, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator

from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import ProgressState


class SnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSnapshotRecord(SnapshotRecord):
    task_id: str
    state: ProgressState
    task: Task
    header: MessageHeader
    meta: MessageMeta
    # The person the task verifiably works for, decided at admission
    # (``custom_agents.task_records.TaskInfo``); null for work for nobody.
    # Required so a restored task never re-derives it from ``meta``.
    owner_person_id: Optional[str]

    @model_validator(mode="after")
    def consistent_task_identity(self) -> Self:
        if self.task_id != self.task.task_id:
            raise ValueError("Snapshot task_id must match the embedded task identity")
        return self


class RecoveryTaskLink(SnapshotRecord):
    """Runtime-owned correlation for an isolated recovery task."""

    original_task_id: str
    original_plan_id: str
    recovery_task_id: str
    branch_id: str
    failure_type: str
    failure_summary: str
    affected_step_id: Optional[str] = None
    affected_subtask_id: Optional[str] = None
    checkpoint_schedule_id: Optional[str] = None
    merge_status: str = "pending"
    created_at: str = ""
    merged_at: Optional[str] = None


class PlanStepTaskLink(SnapshotRecord):
    """Runtime-owned correlation between an internal manager task and a PlanStep."""

    root_task_id: str
    plan_id: str
    step_id: str
    step_task_id: str
    status: str = "active"
    created_at: str = ""
    completed_at: Optional[str] = None


class RecoverablePauseRecord(SnapshotRecord):
    """Runtime-owned state for a user-guided recoverable budget pause."""

    task_id: str
    pause_id: str
    trigger: str
    status: str = "pending"
    created_at: str = ""
    task_query_id: Optional[str] = None
    affected_subtask_id: Optional[str] = None
    affected_agent_id: Optional[str] = None
    affected_named_agent_type: Optional[str] = None
    friendly_reason: str = ""
    base_tool_call_limit: Optional[int] = None
    partial_tool_call_limit: Optional[int] = None
    pause_count_for_task: int = 1
    user_decision: Optional[str] = None
    user_decision_text: Optional[str] = None




class TaskRuntimeLeaseRecord(SnapshotRecord):
    """Runtime-owned concurrency lease for one active task-tree batch."""

    lease_id: str
    root_task_id: str
    source_task_id: str
    owner_agent_id: str
    epoch: int = 1
    batch_id: Optional[str] = None
    status: Literal[
        "active",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "stale",
        "waiting_external_result",
    ] = "active"
    current_action_index: int = 0
    interrupt_requested: bool = False
    interrupt_reason: Optional[str] = None
    interrupt_event_id: Optional[str] = None
    started_at: str = ""
    updated_at: str = ""
    expires_at: Optional[str] = None


class RuntimeMailboxEventRecord(SnapshotRecord):
    """Durable inbound event recorded before it mutates a leased task tree."""

    event_id: str
    root_task_id: str
    event_type: str
    event_class: Literal["advisory", "preemptive", "recovery", "deferrable"]
    status: Literal[
        "received",
        "deferred",
        "processing",
        "processed",
        "rejected",
        "failed",
        "cancelled",
        "stale",
    ] = "received"
    priority: int = 50
    idempotency_key: str = ""
    local_task_id: Optional[str] = None
    related_task_id: Optional[str] = None
    message_id: Optional[str] = None
    correlation_id: Optional[str] = None
    schedule_id: Optional[str] = None
    payload_json: Dict[str, Any] = Field(default_factory=dict)
    header_json: Dict[str, Any] = Field(default_factory=dict)
    meta_json: Dict[str, Any] = Field(default_factory=dict)
    blocked_by_lease_id: Optional[str] = None
    received_at: str = ""
    updated_at: str = ""
    processed_at: Optional[str] = None
    last_error: Optional[str] = None


class TaskStateProjectionRecord(SnapshotRecord):
    """Audit record for coordinator-owned semantic task-state projection."""

    projection_id: str
    task_id: str
    from_state: str
    to_state: str
    reason: str
    source: str = "coordinator"
    batch_id: Optional[str] = None
    event_id: Optional[str] = None
    created_at: str = ""


class TurnActionBatchRecord(SnapshotRecord):
    """Runtime-owned durable state for one ordered set of LLM-requested actions."""

    batch_id: str
    source_task_id: str
    status: Literal[
        "prepared",
        "executing",
        "completed",
        "retrying",
        "needs_model_recovery",
        "isolated_recovery_started",
        "failed",
        "waiting_external_result",
    ] = "prepared"
    plan_id: Optional[str] = None
    plan_step_id: Optional[str] = None
    current_action_index: int = 0
    inline_recovery_attempt_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class TurnActionOutboxRecord(SnapshotRecord):
    """Runtime-owned durable state for one action inside a turn-action batch."""

    action_id: str
    batch_id: str
    source_task_id: str
    action_index: int
    action_type: str
    payload_json: Dict[str, Any]
    phase: Literal[
        "validate",
        "local_commit",
        "publish",
        "advisory_capacity",
        "post_publish_finalize",
        "complete",
    ] = "validate"
    recoverability: Literal[
        "automatic_retry", "model_recovery", "isolated_recovery", "terminal"
    ] = "automatic_retry"
    status: Literal[
        "pending",
        "prepared",
        "dispatching",
        "dispatched",
        "completed",
        "blocked",
        "retry_scheduled",
        "failed_recoverable",
        "failed_terminal",
    ] = "pending"
    idempotency_key: str = ""
    target_route: Optional[str] = None
    message_id: Optional[str] = None
    side_effects: Dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 0
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    failure_report: Optional[Dict[str, Any]] = None
    created_at: str = ""
    updated_at: str = ""


PositiveCount = Annotated[int, Field(ge=1)]
NonnegativeCount = Annotated[int, Field(ge=0)]


class RuntimeSnapshotV3(SnapshotRecord):
    """Complete durable owner state; older versions and unknown fields are rejected.

    Empty collections are valid. Cancellation and orphan filtering are recovery
    policy decisions applied only after this whole document validates.
    """

    schema_version: Literal[3]
    tasks: list[TaskSnapshotRecord] = Field(default_factory=list)
    task_queue: list[str] = Field(default_factory=list)
    subtask_to_parent_map: dict[str, str] = Field(default_factory=dict)
    task_queries: dict[str, list[TaskQuery]] = Field(default_factory=dict)
    turn_action_batches: dict[str, TurnActionBatchRecord] = Field(default_factory=dict)
    turn_action_outbox: dict[str, TurnActionOutboxRecord] = Field(default_factory=dict)
    runtime_leases: dict[str, TaskRuntimeLeaseRecord] = Field(default_factory=dict)
    runtime_mailbox_events: dict[str, RuntimeMailboxEventRecord] = Field(
        default_factory=dict
    )
    task_state_projection_records: dict[str, TaskStateProjectionRecord] = Field(
        default_factory=dict
    )
    task_to_plan_id: dict[str, str] = Field(default_factory=dict)
    task_to_objective_id: dict[str, str] = Field(default_factory=dict)
    plan_to_objective_id: dict[str, str] = Field(default_factory=dict)
    recovery_links: dict[str, RecoveryTaskLink] = Field(default_factory=dict)
    plan_step_task_links: dict[str, PlanStepTaskLink] = Field(default_factory=dict)
    recoverable_pause_records: dict[str, RecoverablePauseRecord] = Field(
        default_factory=dict
    )
    partial_tool_call_limit_overrides: dict[str, PositiveCount] = Field(
        default_factory=dict
    )
    runtime_resume_directives: dict[str, str] = Field(default_factory=dict)
    human_interaction_blockers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    final_review_attempt_counts: dict[str, NonnegativeCount] = Field(
        default_factory=dict
    )
    final_review_pending_task_ids: list[str] = Field(default_factory=list)
    current_in_progress_task_id: Optional[str] = None
    waiting_top_level_task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_record_identities(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Duplicate task identity within a snapshot")
        for records, identity_field in (
            (self.turn_action_batches, "batch_id"),
            (self.turn_action_outbox, "action_id"),
            (self.runtime_leases, "lease_id"),
            (self.runtime_mailbox_events, "event_id"),
            (self.task_state_projection_records, "projection_id"),
            (self.recovery_links, "recovery_task_id"),
            (self.plan_step_task_links, "step_task_id"),
            (self.recoverable_pause_records, "pause_id"),
        ):
            for key, record in records.items():
                if not key or key != getattr(record, identity_field):
                    raise ValueError(
                        f"Snapshot map key does not match {identity_field}: {key!r}"
                    )
        return self


def decode_runtime_snapshot(payload: str) -> RuntimeSnapshotV3:
    """Validate the entire snapshot before any custody or runtime state changes."""
    return RuntimeSnapshotV3.model_validate_json(payload)
