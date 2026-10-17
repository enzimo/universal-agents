"""schedule v1 contracts, independent of runtime hosts and SDKs."""

from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_messages.enums import ScheduleManagementClass
from custom_messages.enums import SystemScheduleKind
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

# The catalog change kind (ua_definitions/access/tools, ``changes:``) for
# changes to schedules the runtime manages. Agents stage them and a person
# approves each exact change (custom_agents.system_schedule_changes); in the
# gateway, the people who could approve one may make it directly.
SYSTEM_SCHEDULE_CHANGE_KIND = "system_schedule"
# ManagerScheduleCommand.message when the person may not change a schedule the
# runtime manages (custom_agents.manager_control_runtime decides it with the
# command's verified signed person).
SYSTEM_SCHEDULE_CHANGE_NOT_ALLOWED = "system_schedule_change_not_allowed"
# ManagerScheduleCommand.message when the schedule changed after it was listed
# (expected_record_digest), checked in the store's write transaction.
SCHEDULE_CHANGED_SINCE_LISTED = "schedule_changed"
# ManagerScheduleCommand.message when the store refused a change to a schedule
# the runtime manages for lack of an exact permit (team, owner or state mismatch).
SYSTEM_SCHEDULE_CHANGE_REFUSED = "system_schedule_change_refused"


class AutomationEnvelopeSnapshot(BaseModel):
    """
    Durable structured snapshot of the envelope that triggered an automation hook.

    The snapshot intentionally stores the original content as raw structured JSON
    so automation follow-ups stay replayable without recursively nesting the full
    MessageEnvelope union.
    """

    message_class: Literal["AutomationEnvelopeSnapshot"] = Field(
        default="AutomationEnvelopeSnapshot",
        description="Automation Envelope Snapshot",
    )
    msg_header: Optional[MessageHeader] = Field(
        default=None,
        description="Original envelope header when available.",
    )
    msg_meta: Optional[MessageMeta] = Field(
        default=None,
        description="Original envelope metadata when available.",
    )
    msg_content: Dict[str, Any] = Field(
        default_factory=dict,
        description="Original structured message content serialized as JSON.",
    )


class AutomationDeliveryPreferenceSnapshot(BaseModel):
    """Stable delivery-policy snapshot attached to an automation event."""

    message_class: Literal["AutomationDeliveryPreferenceSnapshot"] = Field(
        default="AutomationDeliveryPreferenceSnapshot",
        description="Automation Delivery Preference Snapshot",
    )
    preferred_channel: str = Field(
        default="", description="Configured preferred delivery channel."
    )
    preferred_output_chat_id: str = Field(
        default="", description="Configured preferred output chat target."
    )
    preferred_channel_thread_id: str = Field(
        default="", description="Configured preferred thread/topic target."
    )
    summarize_deferred: bool = Field(
        default=True, description="Whether deferred deliveries should be summarized."
    )
    digest_delivery_hour_local: int = Field(
        default=8, description="Configured local digest release hour."
    )
    urgent_keywords: List[str] = Field(
        default_factory=list, description="Keywords that may bypass quiet hours."
    )


class AutomationQuietHoursWindow(BaseModel):
    """Quiet-hours policy snapshot attached to an automation event."""

    message_class: Literal["AutomationQuietHoursWindow"] = Field(
        default="AutomationQuietHoursWindow",
        description="Automation Quiet Hours Window",
    )
    timezone_name: Optional[str] = Field(
        default=None,
        description="Requester-local timezone for this quiet-hours policy.",
    )
    enabled: bool = Field(default=False, description="Whether quiet hours are enabled.")
    start_hour_local: Optional[int] = Field(
        default=None, description="Quiet-hours start hour in requester-local time."
    )
    end_hour_local: Optional[int] = Field(
        default=None, description="Quiet-hours end hour in requester-local time."
    )
    allow_urgent_delivery: bool = Field(
        default=True, description="Whether urgent content may bypass quiet hours."
    )
    currently_within_quiet_hours: Optional[bool] = Field(
        default=None,
        description="Whether the snapshot was taken during quiet hours.",
    )


class AutomationMessage(BaseModel):
    """
    Gateway-originated automation follow-up carrying the full source snapshot.

    This is normalized into a synthetic top-level Task by the manager runtime so
    it can reuse ordinary task history, conversation, delivery, and receipt flow.
    """

    automation_id: str = Field(
        description="Stable automation identifier for this event lineage."
    )
    message_class: Literal["AutomationMessage"] = Field(
        default="AutomationMessage", description="Automation Message"
    )
    hook_name: str = Field(
        description="Name of the automation hook that triggered this message."
    )
    original_envelope_snapshot: AutomationEnvelopeSnapshot = Field(
        description="Snapshot of the original structured envelope that led to this automation event."
    )
    rendered_original_user_text: str = Field(
        description="Rendered original user-facing text associated with the triggering envelope."
    )
    delivery_preference_snapshot: AutomationDeliveryPreferenceSnapshot = Field(
        description="Structured delivery-preference snapshot captured when the automation fired."
    )
    requester_local_time: str = Field(
        description="Requester-local timestamp when the automation snapshot was created."
    )
    quiet_hours_window: AutomationQuietHoursWindow = Field(
        description="Quiet-hours policy snapshot captured when the automation fired."
    )
    suggested_later_time: Optional[str] = Field(
        default=None,
        description="Optional suggested requester-local or UTC time for later follow-up.",
    )
    allowed_decisions: List[str] = Field(
        default_factory=list,
        description="Explicit list of decisions the automation handler may take.",
    )
    decision_instructions: str = Field(
        description="Explicit instruction block describing the allowed decision surface."
    )

    def get_id(self) -> str:
        return self.automation_id


ScheduleNotifyMode = Literal["on_change", "always", "never"]


class ScheduleMemoryPolicy(BaseModel):
    """Durable notification and evidence rules for a recurring scheduled task."""

    notify: ScheduleNotifyMode = Field(
        default="on_change",
        description="Notification mode for successful runs: on_change, always, or never.",
    )
    notify_on_failure: bool = Field(
        default=False,
        description="Whether failed or inconclusive runs should be user-visible.",
    )
    evidence_required: str = Field(
        default="none",
        description="Task-defined evidence requirement such as none, fresh_source, multiple_sources, or authoritative_source.",
    )
    custom: Dict[str, Any] = Field(
        default_factory=dict,
        description="Flexible task-specific policy rules that the platform does not interpret globally.",
    )


class ScheduleMemoryState(BaseModel):
    """Compact current known state for a recurring scheduled task."""

    summary: str = Field(
        default="",
        description="Concise human-readable summary of the latest known state.",
    )
    confidence: str = Field(
        default="unknown",
        description="Task-defined confidence label for the current state.",
    )
    last_observed_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp when the state was last checked.",
    )
    last_changed_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp when a material state change was last recorded.",
    )
    domain: Dict[str, Any] = Field(
        default_factory=dict,
        description="Flexible compact domain state for this schedule kind.",
    )


class ScheduleMemoryLedger(BaseModel):
    """Runtime-managed bounded execution history for a recurring scheduled task."""

    total_runs: int = Field(
        default=0,
        ge=0,
        description="Total scheduled runs recorded in this memory envelope.",
    )
    total_notifications: int = Field(
        default=0,
        ge=0,
        description="Total user notifications recorded for this schedule.",
    )
    last_run_at: Optional[str] = Field(
        default=None, description="ISO 8601 UTC timestamp of the latest recorded run."
    )
    last_success_at: Optional[str] = Field(
        default=None, description="ISO 8601 UTC timestamp of the latest successful run."
    )
    last_notification_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp of the latest recorded notification.",
    )
    consecutive_failures: int = Field(
        default=0,
        ge=0,
        description="Number of consecutive failed or inconclusive runs.",
    )
    recent_runs: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Bounded newest-last list of recent run records.",
    )
    recent_notifications: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Bounded newest-last list of recent user-visible notification records.",
    )


class ScheduleTaskMemory(BaseModel):
    """
    Compact persistent memory envelope for recurring scheduled tasks.

    The envelope is intentionally strict at the top level while policy.custom
    and state.domain remain flexible for arbitrary recurring-task domains.
    """

    schema_version: int = Field(
        default=1, description="Version of the schedule memory envelope contract."
    )
    schedule_kind: str = Field(
        default="generic_recurring_task",
        description="Task-defined schedule category used to guide future runs.",
    )
    policy: ScheduleMemoryPolicy = Field(default_factory=ScheduleMemoryPolicy)
    state: ScheduleMemoryState = Field(default_factory=ScheduleMemoryState)
    ledger: ScheduleMemoryLedger = Field(default_factory=ScheduleMemoryLedger)
    domain_schema_hint: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional task-authored hint describing expected keys in state.domain.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp when this schedule memory envelope was created.",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp when this schedule memory envelope was last updated.",
    )


class ScheduledMessage(BaseModel):
    """
    A scheduled message that fires on this agent at a specified time, once or on a recurring basis.

    Schedules are persisted through the configured schedule repository backend
    so they survive agent restarts.
    When a schedule fires, its embedded Task payload is processed by the agent's LLM.

    Scheduling modes (mutually exclusive):
        - interval_ms: Fixed-interval repeat in milliseconds.
        - cron_expression: Cron string (e.g. "*/5 * * * *") for complex schedules,
          read in ``cron_timezone`` ("0 9 * * *" is 9:00 in that zone, across
          daylight saving changes).

    Attributes:
        schedule_id (str): Unique identifier for this schedule.
        payload (Task): The Task to execute when the schedule fires.
        interval_ms (Optional[int]): Fixed-interval repeat in milliseconds.
        cron_expression (Optional[str]): Cron expression for complex scheduling.
        repeat_count (Optional[int]): Number of times to fire. None = infinite, 1 = once.
        next_fire_at (Optional[str]): ISO 8601 UTC timestamp of next scheduled fire.
        created_at (str): ISO 8601 UTC timestamp of when this schedule was created.
        enabled (bool): Whether this schedule is active. Can be paused without deleting.
    """

    schedule_id: str = Field(
        default_factory=lambda: new_id("sched"),
        description="Unique identifier for this schedule.",
    )
    message_class: Literal["ScheduledMessage"] = Field(
        default="ScheduledMessage", description="Scheduled Message"
    )
    schedule_name: Optional[str] = Field(
        default=None, description="Optional human-readable label for this schedule."
    )
    management_class: ScheduleManagementClass = Field(
        default=ScheduleManagementClass.SYSTEM,
        description=(
            "Trusted mutation-authority class. Missing provenance fails safe as system-managed; "
            "user-facing creation paths must set this to user explicitly."
        ),
    )
    system_schedule_kind: Optional[SystemScheduleKind] = Field(
        default=None,
        description=(
            "Known runtime purpose for a system-managed schedule. None on a system schedule "
            "means conservatively protected unclassified infrastructure."
        ),
    )
    objective_id: Optional[str] = Field(
        default=None,
        description="Optional manager-owned Objective id this schedule advances.",
    )
    claim_team_id: Optional[str] = Field(
        default=None,
        description="Optional team id whose agents may claim this schedule when it fires.",
    )
    claim_named_agent_type: Optional[str] = Field(
        default=None,
        description="Optional named agent type that may claim this schedule when it fires.",
    )
    claim_agent_id: Optional[str] = Field(
        default=None,
        description="Optional exact agent id that may claim this schedule when it fires. Prefer claim_named_agent_type for same-type failover.",
    )
    install_mode: Literal["replace", "ensure", "refresh"] = Field(
        default="replace",
        description=(
            "Persistence behavior when the schedule id already exists. 'replace' updates the row; "
            "'ensure' inserts only when absent and preserves all persisted runtime state otherwise; "
            "'refresh' (trusted runtime producers only, for system-managed schedules) creates nothing "
            "and replaces only the person and session fields of an existing schedule's gateway "
            "context, keeping pause, enabled state, cadence and next fire time."
        ),
    )
    payload: Task = Field(description="The Task to execute when the schedule fires.")
    payload_header: Optional[MessageHeader] = Field(
        default=None,
        description="Optional header to reuse when the payload task fires.",
    )
    payload_meta: Optional[MessageMeta] = Field(
        default=None,
        description="Optional metadata to reuse when the payload task fires.",
    )
    interval_ms: Optional[int] = Field(
        default=None,
        description="Fixed-interval repeat in milliseconds. Mutually exclusive with cron_expression.",
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description="Cron expression for complex scheduling (e.g. '*/5 * * * *'). Mutually exclusive with interval_ms.",
    )
    cron_timezone: Optional[str] = Field(
        default=None,
        description=(
            "IANA time zone the cron expression is read in, such as 'America/Chicago'. "
            "The store fills it when a cron schedule is added without one: the owner's "
            "time zone from the schedule's request metadata, else the instance time zone."
        ),
    )
    repeat_count: Optional[int] = Field(
        default=1, description="Number of times to fire. None = infinite, 1 = once."
    )
    next_fire_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp of next scheduled fire. Computed automatically.",
    )
    paused_until: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp until which this schedule is temporarily paused.",
    )
    expires_at_utc: Optional[str] = Field(
        default=None,
        description="Explicit ISO 8601 UTC expiration timestamp. Expired schedules are pruned and not fired.",
    )
    schedule_memory: Optional[ScheduleTaskMemory] = Field(
        default=None,
        description="Optional compact persistent policy/state/ledger envelope for recurring scheduled tasks.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp of when this schedule was created.",
    )
    enabled: bool = Field(default=True, description="Whether this schedule is active.")

    @model_validator(mode="after")
    def _validate_cron_timezone(self) -> "ScheduledMessage":
        if self.cron_timezone is None:
            return self
        if not self.cron_expression:
            raise ValueError("cron_timezone applies only to a schedule with a cron_expression")
        try:
            ZoneInfo(self.cron_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"cron_timezone must be an IANA time zone such as America/Chicago, not {self.cron_timezone!r}"
            ) from exc
        return self

    @model_validator(mode="after")
    def _validate_management_provenance(self) -> "ScheduledMessage":
        if (
            self.management_class == ScheduleManagementClass.USER
            and self.system_schedule_kind is not None
        ):
            raise ValueError(
                "user-managed schedules cannot declare a system schedule kind"
            )
        return self

    @property
    def effective_system_schedule_kind(self) -> Optional[SystemScheduleKind]:
        if self.management_class != ScheduleManagementClass.SYSTEM:
            return None
        return self.system_schedule_kind or SystemScheduleKind.UNCLASSIFIED

    def get_id(self) -> str:
        return self.schedule_id


# What runs change by themselves: a repeating schedule's next run time and the
# content of the run notes (state and ledger). A person's controls are bound to
# the schedule they saw without these, so a schedule that runs often can still
# be paused from the page that listed it.
SCHEDULE_RUN_BOOKKEEPING_FIELDS = frozenset({"next_fire_at", "schedule_memory"})


def schedule_listing_digest(schedule: ScheduledMessage) -> str:
    """The digest a person's schedule controls bind to (``expected_record_digest``).

    Covers what the person sees and decides on: the task, timing, controls,
    owner and routing, the run-notes settings (kind and notification policy)
    and, for a one-time schedule (no interval or cron), its run time, which
    runs never move (they delete the schedule). Leaves out a repeating
    schedule's next run time and the run notes' content. The gateway lists
    schedules with it and the manager's store checks it in the write
    transaction (``custom_agents.schedule_store``).
    """

    repeating = schedule.interval_ms is not None or bool(schedule.cron_expression)
    excluded = set(SCHEDULE_RUN_BOOKKEEPING_FIELDS) if repeating else {"schedule_memory"}
    document = schedule.model_dump(mode="json", exclude=excluded)
    # No notes yet reads as the default settings: a run that writes the first
    # notes does not change what the person saw.
    memory = schedule.schedule_memory or ScheduleTaskMemory()
    document["run_notes_settings"] = {"schedule_kind": memory.schedule_kind, "policy": memory.policy.model_dump(mode="json")}
    return contract_sha256(document)


class ScheduleWorkerAssignmentV1(BaseModel):
    """Manager-projected assignment, separate from canonical schedule mutation data.

    Automatic creation describes saved default or TeamLead startup permission,
    not proof that a worker is running or has tool access.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_type: str | None
    agent_id: str | None
    automatic_creation: Literal["not_configured", "enabled", "disabled", "default_policy"]
    review_agent_type: str | None = None
    upgrade_required: bool = False
    desired_agent_type: str | None = None
    reassignment_pending: bool = False
    # Set while the schedule is stuck (custom_agents.stuck_schedules.not_running):
    # the missed due time, and what is wrong and what to do, in plain words.
    not_running_since: str | None = None
    not_running_explanation: str | None = None


class ManagerScheduleCommand(BaseModel):
    """Durable gateway-to-manager schedule query or mutation."""

    message_class: Literal["ManagerScheduleCommand"] = "ManagerScheduleCommand"
    command_id: str = Field(default_factory=lambda: new_id("mgr_sched"))
    command_type: MessageMetaType = MessageMetaType.REQUEST
    action: Literal["list", "pause", "resume", "disable", "remove"]
    schedule_id: Optional[str] = None
    session_key: Optional[str] = None
    paused_until: Optional[str] = None
    expected_record_digest: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    success: Optional[bool] = None
    schedules: List[ScheduledMessage] = Field(default_factory=list)
    worker_assignments: Dict[str, ScheduleWorkerAssignmentV1] = Field(default_factory=dict)
    message: str = ""

    def get_id(self) -> str:
        return self.command_id
