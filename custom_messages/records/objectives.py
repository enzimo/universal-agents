"""objectives contracts, independent of runtime hosts and SDKs."""

from custom_messages.enums import ObjectiveCompletionPolicy
from custom_messages.enums import ObjectiveCriterionStatus
from custom_messages.enums import ObjectiveEventType
from custom_messages.enums import ObjectiveHealthDisposition
from custom_messages.enums import ObjectiveHorizon
from custom_messages.enums import ObjectivePriority
from custom_messages.enums import ObjectiveStatus
from custom_messages.enums import ObjectiveWorkInstalledProfileStatus
from custom_messages.enums import ObjectiveWorkPolicyStatus
from custom_messages.enums import ObjectiveWorkProfileProposalStatus
from custom_messages.enums import ObjectiveWorkProfileScope
from custom_messages.enums import ObjectiveWorkProfileSource
from custom_messages.enums import ObjectiveWorkSessionStatus
from datetime import datetime
from datetime import timezone
from id_generation import new_id
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

# ``cadence_timezone`` value: the cadence runs in the time zone of the person who
# owns the Objective (the request time zone of the task that installed it),
# else the instance time zone. Any other value is an IANA time zone.
OWNER_CADENCE_TIMEZONE = "owner"


def _cadence_timezone(value: str) -> str:
    text = str(value or "").strip()
    if text.lower() == OWNER_CADENCE_TIMEZONE:
        return OWNER_CADENCE_TIMEZONE
    try:
        return ZoneInfo(text).key
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"cadence_timezone must be 'owner' or an IANA time zone such as America/Chicago, not {text!r}"
        ) from exc


class ObjectiveSuccessCriterion(BaseModel):
    """One measurable criterion used to decide whether an Objective is satisfied."""

    criterion_id: str = Field(
        default_factory=lambda: new_id("obj_criterion"),
        description="Stable identifier for this objective success criterion.",
    )
    message_class: Literal["ObjectiveSuccessCriterion"] = Field(
        default="ObjectiveSuccessCriterion",
        description="Objective Success Criterion",
    )
    description: str = Field(description="Concrete condition that should become true.")
    status: ObjectiveCriterionStatus = Field(
        default=ObjectiveCriterionStatus.OPEN,
        description="Current criterion lifecycle status.",
    )
    evidence_summary: Optional[str] = Field(
        default=None,
        description="Short evidence summary explaining why the current status is valid.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the criterion was last updated.",
    )


class ObjectiveEventRecord(BaseModel):
    """Append-style event describing Objective lifecycle and linkage changes."""

    event_id: str = Field(
        default_factory=lambda: new_id("objective_event"),
        description="Stable identifier for this objective event.",
    )
    message_class: Literal["ObjectiveEventRecord"] = Field(
        default="ObjectiveEventRecord",
        description="Objective Event Record",
    )
    event_type: ObjectiveEventType = Field(
        description="Structured objective event type."
    )
    summary: str = Field(description="Concise description of the event.")
    task_id: Optional[str] = Field(
        default=None, description="Related manager-local task id, when applicable."
    )
    plan_id: Optional[str] = Field(
        default=None, description="Related manager-local plan id, when applicable."
    )
    schedule_id: Optional[str] = Field(
        default=None, description="Related schedule id, when applicable."
    )
    heartbeat_item_id: Optional[str] = Field(
        default=None, description="Related heartbeat item id, when applicable."
    )
    user_visible: bool = Field(
        default=False,
        description="Whether this event is suitable for user-facing status.",
    )
    details: Dict[str, Any] = Field(
        default_factory=dict, description="Additional structured event details."
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the event was recorded.",
    )


class ObjectiveScheduleLink(BaseModel):
    """Link from an Objective to a manager-owned scheduled job."""

    schedule_id: str = Field(description="Linked ScheduledMessage id.")
    managed_by_objective: bool = Field(
        default=True,
        description="Whether Objective lifecycle changes should affect this schedule.",
    )
    disabled_by_objective_pause: bool = Field(
        default=False,
        description="Whether the schedule was disabled by an Objective pause/terminal transition.",
    )
    linked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the schedule was linked.",
    )


class ObjectiveHeartbeatLink(BaseModel):
    """Link from an Objective to a canonical heartbeat item."""

    heartbeat_item_id: str = Field(description="Linked heartbeat item id.")
    managed_by_objective: bool = Field(
        default=True,
        description="Whether Objective lifecycle changes should affect this heartbeat item.",
    )
    disabled_by_objective_pause: bool = Field(
        default=False,
        description="Whether the heartbeat item was disabled by an Objective pause/terminal transition.",
    )
    linked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the heartbeat item was linked.",
    )


class ObjectiveRecord(BaseModel):
    """Manager-owned durable desired outcome or standing responsibility."""

    objective_id: str = Field(
        default_factory=lambda: new_id("objective_id"),
        description="Stable manager-local identifier for this durable objective.",
    )
    message_class: Literal["Objective"] = Field(
        default="Objective", description="Objective"
    )
    title: str = Field(description="Short user-visible objective title.")
    desired_outcome: str = Field(
        description="Durable description of the outcome being pursued or maintained."
    )
    why: str = Field(
        description="Rationale for pursuing or maintaining this objective."
    )
    status: ObjectiveStatus = Field(
        default=ObjectiveStatus.ACTIVE,
        description="Current objective lifecycle status.",
    )
    horizon: ObjectiveHorizon = Field(description="Objective time horizon.")
    priority: ObjectivePriority = Field(
        default=ObjectivePriority.NORMAL, description="Objective priority."
    )
    completion_policy: ObjectiveCompletionPolicy = Field(
        default=ObjectiveCompletionPolicy.MANAGER_DECIDES,
        description="Policy used to decide whether the objective is satisfied.",
    )
    success_criteria: List[ObjectiveSuccessCriterion] = Field(
        default_factory=list,
        description="Criteria used to decide whether the objective is satisfied.",
    )
    constraints: List[str] = Field(
        default_factory=list, description="Durable constraints that shape execution."
    )
    non_goals: List[str] = Field(
        default_factory=list, description="Explicitly excluded outcomes."
    )
    progress_summary: Optional[str] = Field(
        default=None, description="Current compact progress summary."
    )
    current_focus: Optional[str] = Field(
        default=None, description="Current execution or monitoring focus."
    )
    owner_agent_id: Optional[str] = Field(
        default=None, description="Agent id that owns this objective."
    )
    owner_agent_name: Optional[str] = Field(
        default=None, description="Human-readable owner agent name."
    )
    owner_canonical_user_id: Optional[str] = Field(
        default=None,
        description="Authenticated user that owns this objective.",
    )
    team_id: Optional[str] = Field(
        default=None, description="Team associated with the objective."
    )
    owner_named_agent_type: Optional[str] = Field(
        default=None, description="Named agent type that owns this objective."
    )
    root_conversation_id: Optional[str] = Field(
        default=None, description="Primary durable conversation id, when any."
    )
    created_by_task_id: Optional[str] = Field(
        default=None, description="Task id that created this objective."
    )
    promoted_from_task_id: Optional[str] = Field(
        default=None, description="Task id from which this objective was promoted."
    )
    current_plan_id: Optional[str] = Field(
        default=None, description="Current plan attempt for this objective."
    )
    plan_ids: List[str] = Field(
        default_factory=list, description="Plan ids associated with this objective."
    )
    related_task_ids: List[str] = Field(
        default_factory=list, description="Task ids associated with this objective."
    )
    recovery_task_ids: List[str] = Field(
        default_factory=list,
        description="Recovery task ids associated with this objective.",
    )
    schedule_links: List[ObjectiveScheduleLink] = Field(
        default_factory=list,
        description="Schedules associated with this objective.",
    )
    heartbeat_links: List[ObjectiveHeartbeatLink] = Field(
        default_factory=list,
        description="Heartbeat items associated with this objective.",
    )
    event_log: List[ObjectiveEventRecord] = Field(
        default_factory=list,
        description="Recent append-style objective lifecycle and linkage events.",
    )
    requires_user_confirmation: bool = Field(
        default=False,
        description="Whether user confirmation is required before marking satisfied.",
    )
    user_confirmation_summary: Optional[str] = Field(
        default=None, description="Summary of user confirmation."
    )
    satisfied_by_task_id: Optional[str] = Field(
        default=None, description="Task id whose result satisfied the objective."
    )
    satisfied_at: Optional[str] = Field(
        default=None, description="Timestamp when the objective was satisfied."
    )
    deletion_operation_id: Optional[str] = Field(
        default=None,
        description="Idempotency key for a recoverable Objective deletion.",
    )
    deletion_requested_at: Optional[str] = Field(
        default=None,
        description="Timestamp when the owner requested recoverable deletion.",
    )
    deleted_at: Optional[str] = Field(
        default=None,
        description="Timestamp when the recoverable Objective tombstone completed.",
    )
    deletion_summary: Optional[str] = Field(
        default=None,
        description="Last durable result of the Objective deletion state machine.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the objective was first created.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the objective was last updated.",
    )


class ObjectiveWorkBudget(BaseModel):
    """Immutable budget snapshot for one objective work session."""

    message_class: Literal["ObjectiveWorkBudget"] = "ObjectiveWorkBudget"
    model_config = ConfigDict(extra="forbid")
    active_seconds: int = Field(gt=0)
    checkpoint_reserve_seconds: int = Field(ge=0)
    checkpoint_reserve_turns: int = Field(default=0, ge=0)
    checkpoint_reserve_tokens: int = Field(default=0, ge=0)
    checkpoint_reserve_tool_calls: int = Field(default=0, ge=0)
    turns: int = Field(gt=0)
    total_tokens: int = Field(gt=0)
    tool_calls: int = Field(gt=0)


class ObjectiveWorkProfile(BaseModel):
    """Reusable cadence and budget contract for bounded Objective execution."""

    message_class: Literal["ObjectiveWorkProfile"] = "ObjectiveWorkProfile"
    model_config = ConfigDict(extra="forbid")
    cadence_cron: str
    cadence_timezone: str = OWNER_CADENCE_TIMEZONE
    budget: ObjectiveWorkBudget

    @model_validator(mode="after")
    def _normalize_cadence_timezone(self) -> "ObjectiveWorkProfile":
        self.cadence_timezone = _cadence_timezone(self.cadence_timezone)
        return self


class ObjectiveWorkProfileProposal(BaseModel):
    """Immutable, owner-scoped proposal awaiting exact user acceptance."""

    schema_version: Literal[1] = 1
    proposal_id: str = Field(default_factory=lambda: new_id("objwork_prop"))
    message_class: Literal["ObjectiveWorkProfileProposal"] = (
        "ObjectiveWorkProfileProposal"
    )
    owner_agent_id: str
    owner_canonical_user_id: str
    source_task_id: str
    scope: ObjectiveWorkProfileScope
    objective_id: Optional[str] = None
    profile_name: str
    profile: ObjectiveWorkProfile
    rationale: str
    replace_existing: bool = False
    replaces_installed_profile_id: Optional[str] = None
    replaces_installed_profile_digest: Optional[str] = None
    expected_policy_state_digest: Optional[str] = None
    proposal_digest: str
    status: ObjectiveWorkProfileProposalStatus = (
        ObjectiveWorkProfileProposalStatus.PENDING
    )
    created_at: str
    expires_at: str
    applied_at: Optional[str] = None
    applied_by_task_id: Optional[str] = None
    acceptance_summary: Optional[str] = None
    installed_profile_id: Optional[str] = None
    applied_policy_id: Optional[str] = None

    @model_validator(mode="after")
    def _validate_scope_target(self) -> "ObjectiveWorkProfileProposal":
        if self.scope == ObjectiveWorkProfileScope.OBJECTIVE and not self.objective_id:
            raise ValueError("objective-scoped profile proposals require objective_id")
        if self.scope == ObjectiveWorkProfileScope.REUSABLE and self.objective_id:
            raise ValueError("reusable profile proposals must not bind an objective_id")
        replacement_fields = (
            self.replaces_installed_profile_id,
            self.replaces_installed_profile_digest,
        )
        if self.replace_existing and not all(replacement_fields):
            raise ValueError(
                "replacement proposals require the installed profile id and digest"
            )
        if not self.replace_existing and any(replacement_fields):
            raise ValueError(
                "non-replacement proposals cannot bind installed profile state"
            )
        if self.scope == ObjectiveWorkProfileScope.OBJECTIVE and any(
            replacement_fields
        ):
            raise ValueError(
                "objective-scoped proposals cannot bind installed profile state"
            )
        if (
            self.scope == ObjectiveWorkProfileScope.REUSABLE
            and self.expected_policy_state_digest
        ):
            raise ValueError("reusable proposals cannot bind an Objective policy state")
        return self


class ObjectiveWorkInstalledProfile(BaseModel):
    """Owner-scoped reusable profile installed from one accepted proposal."""

    schema_version: Literal[1] = 1
    installed_profile_id: str = Field(default_factory=lambda: new_id("objwork_prof"))
    message_class: Literal["ObjectiveWorkInstalledProfile"] = (
        "ObjectiveWorkInstalledProfile"
    )
    owner_agent_id: str
    owner_canonical_user_id: str
    profile_name: str
    profile: ObjectiveWorkProfile
    source_proposal_id: str
    proposal_digest: str
    installed_by_task_id: str
    acceptance_summary: str
    status: ObjectiveWorkInstalledProfileStatus = (
        ObjectiveWorkInstalledProfileStatus.ACTIVE
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    removed_at: Optional[str] = None
    removed_by_task_id: Optional[str] = None
    removal_summary: Optional[str] = None

    @model_validator(mode="after")
    def _validate_removal_provenance(self) -> "ObjectiveWorkInstalledProfile":
        removal_fields = (
            self.removed_at,
            self.removed_by_task_id,
            self.removal_summary,
        )
        if self.status == ObjectiveWorkInstalledProfileStatus.REMOVED and not all(
            removal_fields
        ):
            raise ValueError("removed profiles require complete removal provenance")
        if self.status == ObjectiveWorkInstalledProfileStatus.ACTIVE and any(
            removal_fields
        ):
            raise ValueError("active profiles cannot contain removal provenance")
        return self


class ObjectiveWorkPolicy(BaseModel):
    """Explicitly accepted, durable policy for time-boxing one objective."""

    policy_id: str = Field(default_factory=lambda: new_id("objwork_policy"))
    message_class: Literal["ObjectiveWorkPolicy"] = "ObjectiveWorkPolicy"
    objective_id: str
    owner_agent_id: str
    profile_name: str
    profile_source: ObjectiveWorkProfileSource = ObjectiveWorkProfileSource.CONFIGURED
    profile_digest: Optional[str] = None
    owner_canonical_user_id: Optional[str] = None
    installed_profile_id: Optional[str] = None
    profile_proposal_id: Optional[str] = None
    status: ObjectiveWorkPolicyStatus = ObjectiveWorkPolicyStatus.ACTIVE
    disabled_by_objective_control: bool = False
    cadence_cron: str
    cadence_timezone: str = OWNER_CADENCE_TIMEZONE
    required_skills: List[str] = Field(default_factory=list)
    budget: ObjectiveWorkBudget
    accepted_by_task_id: str
    acceptance_summary: str
    schedule_id: Optional[str] = None
    next_run_at: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @model_validator(mode="after")
    def _normalize_cadence_timezone(self) -> "ObjectiveWorkPolicy":
        self.cadence_timezone = _cadence_timezone(self.cadence_timezone)
        return self


class ObjectiveWorkSession(BaseModel):
    """Durable execution window whose limits are checked only at safe boundaries."""

    session_id: str = Field(default_factory=lambda: new_id("objwork_session"))
    message_class: Literal["ObjectiveWorkSession"] = "ObjectiveWorkSession"
    policy_id: str
    objective_id: str
    owner_agent_id: str
    task_id: str
    status: ObjectiveWorkSessionStatus = ObjectiveWorkSessionStatus.SCHEDULED
    budget: ObjectiveWorkBudget
    required_skills: List[str] = Field(default_factory=list)
    activated_skills: List[str] = Field(default_factory=list)
    started_at: Optional[str] = None
    safe_deadline_at: Optional[str] = None
    hard_deadline_at: Optional[str] = None
    ended_at: Optional[str] = None
    active_seconds_used: float = Field(default=0.0, ge=0)
    turns_used: int = Field(default=0, ge=0)
    input_tokens_used: int = Field(default=0, ge=0)
    output_tokens_used: int = Field(default=0, ge=0)
    total_tokens_used: int = Field(default=0, ge=0)
    cache_read_input_tokens_used: int = Field(default=0, ge=0)
    cache_write_input_tokens_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    yield_reason: Optional[str] = None
    checkpoint_id: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ObjectiveWorkCheckpoint(BaseModel):
    """OODA-shaped continuation record written before a bounded session yields."""

    checkpoint_id: str = Field(default_factory=lambda: new_id("objwork_ckpt"))
    message_class: Literal["ObjectiveWorkCheckpoint"] = "ObjectiveWorkCheckpoint"
    session_id: str
    policy_id: str
    objective_id: str
    owner_agent_id: str
    health: ObjectiveHealthDisposition
    observe: str
    orient: str
    decide: str
    act: str
    evidence_delta: List[str] = Field(default_factory=list)
    completed_subgoals: List[str] = Field(default_factory=list)
    next_subgoal: Optional[str] = None
    blockers: List[str] = Field(default_factory=list)
    in_flight_effects: List[str] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)
    degraded: bool = False
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
