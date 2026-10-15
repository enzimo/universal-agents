"""plans contracts, independent of runtime hosts and SDKs."""

from custom_messages.contracts.task_v1 import ExecutionPlanItem
from custom_messages.contracts.task_v1 import ExecutionUnknown
from custom_messages.enums import ExecutionPlanItemStatus
from custom_messages.enums import PlanActionStatus
from custom_messages.enums import PlanActionType
from custom_messages.enums import PlanStatus
from custom_messages.enums import PlanStepFailurePolicy
from datetime import datetime
from datetime import timezone
from id_generation import new_id
from pydantic import BaseModel
from pydantic import Field
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional


class PlanActionRecord(BaseModel):
    """
    Manager-owned recovery or orchestration action recorded against a Plan.
    """

    action_id: str = Field(
        default_factory=lambda: new_id("plan_action"),
        description="Stable identifier for this manager-local plan action.",
    )
    message_class: Literal["PlanActionRecord"] = Field(
        default="PlanActionRecord",
        description="Plan Action Record",
    )
    action_type: PlanActionType = Field(
        description="Structured type of recovery or orchestration action.",
    )
    status: PlanActionStatus = Field(
        default=PlanActionStatus.RECORDED,
        description="Current status of this plan action.",
    )
    summary: str = Field(description="Concise description of the action.")
    reason: Optional[str] = Field(
        default=None,
        description="Why the action was needed.",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Manager-local task id where the action was recorded.",
    )
    related_task_id: Optional[str] = Field(
        default=None,
        description="Related manager-local task or subtask id, when applicable.",
    )
    recovery_task_id: Optional[str] = Field(
        default=None,
        description="Future or existing recovery task id associated with this action.",
    )
    plan_step_id: Optional[str] = Field(
        default=None,
        description="Execution-plan step this action concerns, when applicable.",
    )
    branch_id: Optional[str] = Field(
        default=None,
        description="Recovery or troubleshooting branch identifier.",
    )
    user_visible: bool = Field(
        default=False,
        description="Whether this action represents a user-visible divergence from the happy path.",
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured details for runtime or future recovery tooling.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the action was recorded.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the action was last updated.",
    )


class PlanStepRecord(BaseModel):
    """
    Runtime-owned executable step inside a manager Plan.

    The LLM supplies semantic title/objective/rationale text. Step identifiers,
    ordering, dependency identifiers, and task links are generated and managed by
    the runtime.
    """

    step_id: str = Field(
        default_factory=lambda: new_id("plan_step"),
        description="Stable runtime-owned identifier for this executable plan step.",
    )
    message_class: Literal["PlanStep"] = Field(
        default="PlanStep", description="Plan Step"
    )
    title: str = Field(description="Short semantic title for the step.")
    objective: str = Field(description="Concrete outcome this step should produce.")
    rationale: Optional[str] = Field(
        default=None, description="Why this step is useful for the plan."
    )
    status: ExecutionPlanItemStatus = Field(
        default=ExecutionPlanItemStatus.PLANNED,
        description="Current runtime execution status for the plan step.",
    )
    status_detail: Optional[str] = Field(
        default=None, description="Short detail about the current status."
    )
    result_summary: Optional[str] = Field(
        default=None, description="Compact outcome summary from the step task."
    )
    order_index: int = Field(
        default=0, description="Runtime-owned sequential order for this step."
    )
    depends_on_step_ids: List[str] = Field(
        default_factory=list,
        description="Runtime-owned dependency step identifiers.",
    )
    step_task_ids: List[str] = Field(
        default_factory=list,
        description="Runtime-owned internal task ids that attempted this step.",
    )
    current_step_task_id: Optional[str] = Field(
        default=None,
        description="Currently active internal task id for this step, when any.",
    )
    attempt_count: int = Field(
        default=0,
        description="Runtime-owned count of internal task attempts started for this step.",
    )
    max_attempts: int = Field(
        default=2,
        description="Maximum internal task attempts before the runtime resumes the root for replanning.",
    )
    failure_type: Optional[str] = Field(
        default=None,
        description="Runtime-classified latest failure type for this step.",
    )
    failure_summary: Optional[str] = Field(
        default=None,
        description="Compact latest failure summary for this step.",
    )
    last_failure_policy: Optional[PlanStepFailurePolicy] = Field(
        default=None,
        description="Runtime-selected policy applied to the latest failed attempt.",
    )
    final_response_step: bool = Field(
        default=False,
        description="Whether this step is intended to prepare the final user-facing response.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the step was first recorded.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the step was last updated.",
    )


class PlanRecord(BaseModel):
    """
    Manager-owned first-class durable orchestration state for related local task records.
    """

    plan_id: str = Field(
        default_factory=lambda: new_id("plan_id"),
        description="Stable manager-local identifier for this durable plan.",
    )
    message_class: Literal["Plan"] = Field(default="Plan", description="Plan")
    root_task_id: Optional[str] = Field(
        default=None,
        description="Creation-provenance task id that started this plan lineage; it does not own Objective-linked plan lifecycle.",
    )
    root_conversation_id: Optional[str] = Field(
        default=None,
        description="Durable conversation id associated with the root task, when available.",
    )
    owner_agent_id: Optional[str] = Field(
        default=None, description="Agent id that owns this local plan record."
    )
    owner_agent_name: Optional[str] = Field(
        default=None, description="Human-readable owner agent name."
    )
    team_id: Optional[str] = Field(
        default=None, description="Team associated with the plan."
    )
    owner_named_agent_type: Optional[str] = Field(
        default=None, description="Named agent type that owns the plan."
    )
    status: PlanStatus = Field(
        default=PlanStatus.ACTIVE, description="Current lifecycle status for the plan."
    )
    active_execution_task_id: Optional[str] = Field(
        default=None,
        description="Current manager task anchoring runtime-owned plan-step execution, when any.",
    )
    objective_id: Optional[str] = Field(
        default=None,
        description="Manager-local Objective id this plan belongs to, when promoted.",
    )
    objective_title: Optional[str] = Field(
        default=None, description="Snapshot of the linked Objective title for listings."
    )
    goal_summary: Optional[str] = Field(
        default=None, description="Durable summary of the objective."
    )
    plan_summary: Optional[str] = Field(
        default=None, description="High-level current plan summary."
    )
    current_focus: Optional[str] = Field(
        default=None, description="Current execution focus."
    )
    plan_items: List[ExecutionPlanItem] = Field(
        default_factory=list,
        description="Ordered durable plan items mirrored from the active execution digest.",
    )
    plan_steps: List[PlanStepRecord] = Field(
        default_factory=list,
        description="Ordered runtime-owned executable plan steps for manager outer-loop execution.",
    )
    unknowns: List[ExecutionUnknown] = Field(
        default_factory=list,
        description="Tracked unknowns mirrored from the active execution digest.",
    )
    related_task_ids: List[str] = Field(
        default_factory=list,
        description="Task ids known to be associated with this plan.",
    )
    recovery_task_ids: List[str] = Field(
        default_factory=list,
        description="Task ids used for explicit recovery or troubleshooting branches.",
    )
    plan_actions: List[PlanActionRecord] = Field(
        default_factory=list,
        description="Manager-owned recovery and orchestration actions recorded for this plan.",
    )
    source_task_id: Optional[str] = Field(
        default=None,
        description="Most recent task id that materialized this plan record.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the plan was first created.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the plan was last updated.",
    )
