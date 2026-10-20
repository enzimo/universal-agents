"""Protocol enum definitions shared by custom message models."""

from __future__ import annotations

from enum import StrEnum


class RequesterType(StrEnum):
    AGENT_REQUESTER  = "agent_req"
    SELF_REQUESTER   = "self_req"
    TIMER_REQUESTER  = "timer_req"
    USER_REQUESTER   = "user_req"


class ScheduleManagementClass(StrEnum):
    """Trusted provenance class controlling schedule mutation authority."""

    USER = "user"
    SYSTEM = "system"


class SystemScheduleKind(StrEnum):
    """Runtime-owned schedule purposes with protected control state."""

    UNCLASSIFIED = "unclassified"
    HEARTBEAT_LAUNCHER = "heartbeat_launcher"
    MEMORY_REVIEW_COREMEM = "memory_review_coremem"
    MEMORY_REVIEW_USERMEM = "memory_review_usermem"
    MEMORY_REVIEW_STALE_CONVERSATIONS = "memory_review_stale_conversations"
    INTERNAL_IMPROVEMENT = "internal_improvement"
    AFTER_ACTION_REPORT_REVIEW = "after_action_report_review"
    # Pickup checks of the release before worker pools; kept only so a schedule
    # left by an upgrade still parses and is dropped when it fires
    # (scheduled_runtime.OBSOLETE_DELEGATION_ASSURANCE_PREFIX).
    DELEGATION_ASSURANCE = "delegation_assurance"
    ISOLATED_RECOVERY_CHECKPOINT = "isolated_recovery_checkpoint"
    PROVIDER_OVERLOAD_RETRY = "provider_overload_retry"
    OBJECTIVE_WORK_SESSION = "objective_work_session"
    # Knowledge curation (KnowledgeCuratorAgent): one run per person, signed for
    # them, and the team run, which works for no person.
    KNOWLEDGE_CURATION_PERSON = "knowledge_curation_person"
    KNOWLEDGE_CURATION_TEAM = "knowledge_curation_team"


class RoutingType(StrEnum):
    ALL_AGENTS_ROUTE = "++<all>++"
    SELF_ROUTE       = "++<self>++"
    NULL_ROUTE       = "++<null>++"


class MessageMetaType(StrEnum):
    REQUEST    = "request"
    RESPONSE   = "response"
    NOOP       = "NO OP"


class TaskSteeringType(StrEnum):
    """Runtime-control message kinds for active task steering."""

    INTERRUPT_REQUEST = "interrupt_request"
    INTERRUPT_RESPONSE = "interrupt_response"
    PROACTIVE_GUIDANCE = "proactive_guidance"
    STATUS = "status"


class TaskSteeringStatus(StrEnum):
    """Controlled state vocabulary for task steering events."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"


class TaskSteeringStatusCode(StrEnum):
    """Machine-readable reason for a task-steering status."""

    TARGET_TASK_NOT_FOUND = "target_task_not_found"
    TARGET_TASK_TERMINAL = "target_task_terminal"


class ProgressState(StrEnum):
    """Controlled vocabulary for long-running task progress."""

    ABANDONED      = "abandoned"
    ACCEPTED       = "accepted"
    CANCELLED      = "cancelled"
    COMPLETED      = "completed"
    FINALIZING_PLAN = "finalizing_plan"
    FAILED         = "failed"
    IN_PROGRESS    = "in_progress"
    NOT_STARTED    = "not_started"
    PARTIAL        = "partial"
    PAUSED         = "paused"
    PREPARING      = "preparing"
    RETRYING       = "retrying"
    UNDEFINED      = "undefined"
    WAITING        = "waiting"


class TaskHistorySource(StrEnum):
    SYSTEM = "system"
    AGENT  = "agent"


class TaskHistoryType(StrEnum):
    STATE_CHANGE  = "state_change"
    TASK_EVENT    = "task_event"
    SUBTASK_EVENT = "subtask_event"
    QUERY_EVENT   = "query_event"
    AGENT_NOTE    = "agent_note"
    PLAN_EVENT    = "plan_event"
    ERROR_EVENT   = "error_event"


class TaskHistoryImportance(StrEnum):
    LOW         = "low"
    NORMAL      = "normal"
    HIGH        = "high"
    CRITICAL    = "critical"


class ExecutionPlanItemStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    DROPPED = "dropped"


class ExecutionUnknownStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class PlanStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class ObjectiveStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    SATISFIED = "satisfied"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    DELETING = "deleting"
    DELETED = "deleted"


class ObjectiveHorizon(StrEnum):
    FINITE = "finite"
    LONG_HORIZON = "long_horizon"
    RECURRING = "recurring"
    STANDING = "standing"


class ObjectivePriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class ObjectiveCompletionPolicy(StrEnum):
    MANAGER_DECIDES = "manager_decides"
    USER_CONFIRMS = "user_confirms"
    SUCCESS_CRITERIA_MET = "success_criteria_met"
    RECURRING_INDEFINITE = "recurring_indefinite"


class ObjectiveCriterionStatus(StrEnum):
    OPEN = "open"
    MET = "met"
    FAILED = "failed"
    DROPPED = "dropped"


class ObjectiveEventType(StrEnum):
    CREATED = "created"
    PROMOTED_FROM_TASK = "promoted_from_task"
    PLAN_LINKED = "plan_linked"
    TASK_LINKED = "task_linked"
    SCHEDULE_LINKED = "schedule_linked"
    HEARTBEAT_LINKED = "heartbeat_linked"
    PROGRESS_UPDATED = "progress_updated"
    STATUS_CHANGED = "status_changed"
    USER_CONFIRMATION_RECORDED = "user_confirmation_recorded"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_COMPLETED = "review_completed"
    WORK_SESSION_POLICY_ENABLED = "work_session_policy_enabled"
    WORK_SESSION_POLICY_DISABLED = "work_session_policy_disabled"
    WORK_SESSION_STARTED = "work_session_started"
    WORK_SESSION_CHECKPOINTED = "work_session_checkpointed"
    WORK_SESSION_COMPLETED = "work_session_completed"


class ObjectiveWorkPolicyStatus(StrEnum):
    """Lifecycle of an explicitly accepted objective work-session policy."""

    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class ObjectiveWorkProfileScope(StrEnum):
    """Lifetime selected for an agent-proposed Objective work profile."""

    OBJECTIVE = "objective"
    REUSABLE = "reusable"


class ObjectiveWorkProfileProposalStatus(StrEnum):
    """One-use lifecycle for an immutable Objective work profile proposal."""

    PENDING = "pending"
    APPLIED = "applied"


class ObjectiveWorkInstalledProfileStatus(StrEnum):
    """Lifecycle of an owner-scoped reusable Objective work profile."""

    ACTIVE = "active"
    REMOVED = "removed"


class ObjectiveWorkProfileSource(StrEnum):
    """Trusted provenance for the profile snapshotted onto an Objective."""

    CONFIGURED = "configured"
    INSTALLED = "installed"
    ONE_TIME = "one_time"


class ObjectiveWorkSessionStatus(StrEnum):
    """Safe-boundary lifecycle for one bounded objective execution window."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    YIELD_REQUESTED = "yield_requested"
    CHECKPOINTING = "checkpointing"
    CHECKPOINTED = "checkpointed"
    OVERRUN = "overrun"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ObjectiveHealthDisposition(StrEnum):
    """Evidence-backed assessment recorded at a work-session checkpoint."""

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    COMPLETE = "complete"


class PlanActionType(StrEnum):
    CHECKPOINT = "checkpoint"
    RECORD_FAILURE = "record_failure"
    START_PLAN_EXECUTION = "start_plan_execution"
    PLAN_STEP_STARTED = "plan_step_started"
    PLAN_STEP_COMPLETED = "plan_step_completed"
    START_INLINE_RECOVERY = "start_inline_recovery"
    START_ISOLATED_RECOVERY = "start_isolated_recovery"
    RECOVERY_RESULT_READY = "recovery_result_ready"
    ASK_USER = "ask_user"
    USER_NOTICE = "user_notice"
    PAUSE_TASK = "pause_task"
    RESUME_TASK = "resume_task"
    ABANDON_BRANCH = "abandon_branch"


class PlanActionStatus(StrEnum):
    RECORDED = "recorded"
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AfterActionReportReviewStatus(StrEnum):
    PENDING_MANAGER_REVIEW = "pending_manager_review"
    IGNORED = "ignored"
    KEPT_IN_TASK_HISTORY = "kept_in_task_history"
    SUMMARIZED_TO_TASK_HISTORY = "summarized_to_task_history"
    PROMOTED_TO_MEMORY = "promoted_to_memory"
    # The lesson became a knowledge page (custom_agents.after_action_report_knowledge).
    SAVED_TO_KNOWLEDGE = "saved_to_knowledge"
    NEEDS_FOLLOWUP = "needs_followup"


class AfterActionReportReviewBatchStatus(StrEnum):
    OPEN = "open"
    FINALIZING = "finalizing"
    CLOSED = "closed"


class PlanStepFailurePolicy(StrEnum):
    RETRY_STEP = "retry_step"
    START_ISOLATED_RECOVERY = "start_isolated_recovery"
    RESUME_ROOT_FOR_REPLAN = "resume_root_for_replan"


class RuntimeBudgetPolicyMode(StrEnum):
    HARD_STOP = "hard_stop"
    USER_GUIDED = "user_guided"


class TaskQueryReason(StrEnum):
    CLARIFICATION = "clarification"
    RECOVERABLE_RUNTIME_PAUSE = "recoverable_runtime_pause"
    RECOVERY_CHECKPOINT = "recovery_checkpoint"


class TaskQueryDecision(StrEnum):
    RESUME = "resume"
    PARTIAL = "partial"
    CANCEL = "cancel"
    RETRY = "retry"
    RETRY_IN_5_MINUTES = "retry_in_5_minutes"
    RETRY_IN_10_MINUTES = "retry_in_10_minutes"


class RecoverablePauseTrigger(StrEnum):
    TOOL_CALL_BUDGET_EXCEEDED = "tool_call_budget_exceeded"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    DELEGATED_SUBTASK_TIMEOUT = "delegated_subtask_timeout"
    SPECIALIST_RUNTIME_BUDGET_PAUSE = "specialist_runtime_budget_pause"
    PROVIDER_OVERLOADED = "provider_overloaded"


class TaskActivityType(StrEnum):
    TASK_STARTED = "task_started"
    PLAN_UPDATED = "plan_updated"
    OBJECTIVE_UPDATED = "objective_updated"
    OBJECTIVE_WORK_SESSION_UPDATED = "objective_work_session_updated"
    TOOL_PHASE = "tool_phase"
    DELEGATED = "delegated"
    WORKER_CLAIMED = "worker_claimed"
    WORKER_RECLAIMED = "worker_reclaimed"
    WAITING_ON_AGENT = "waiting_on_agent"
    WAITING_ON_USER = "waiting_on_user"
    RESUMED = "resumed"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskActivityStage(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    DELEGATING = "delegating"
    WAITING = "waiting"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ControlType(StrEnum):
    """Categorize control commands and status requests."""

    CONTROL_MSG             = "control_msg"
    NOOP                    = "NO OP"
    STATUS_REQUEST_BASIC    = "status_request_basic"
    STATUS_REQUEST_FULL     = "status_request_full"


class ChangeRecordKind(StrEnum):
    """Kinds of stored record whose changes run before a model step's other calls.

    Two of a person's requests can run at once, so one request may change a
    record another request is changing. Every tool operation that changes one
    of these records names its kind in the tool catalog (``record_kind`` and
    ``record_kinds`` in ``ua_definitions/access/tools/functions.yaml``). The
    tool executor (``custom_agents.sdk.strands.changes_first``) runs those calls
    first, one at a time, and stops the step at the first one that does not go
    through.
    """

    MEMORY_FACT = "memory_fact"
    KNOWLEDGE_PAGE = "knowledge_page"
    SCHEDULE = "schedule"
    OBJECTIVE = "objective"
    HEARTBEAT_ITEM = "heartbeat_item"
    FILE = "file"
    # Applying an exact staged plan can change several kinds of record. Its
    # approval must finish before other calls act on the proposed change.
    STAGED_CHANGE = "staged_change"


class ChangeRefusal(StrEnum):
    """Why a store refused a change to a record another request may also change.

    Named once here; stores and tools report these as the reason of a refused
    change (``storage.change_ledger``).
    """

    # The record changed since the writer read it (the version it passed is old).
    CHANGED_SINCE_READ = "changed_since_read"
    # Another of the person's requests changed the record while this request ran.
    CHANGE_CONFLICT = "change_conflict"
    # The writing request was stopped or has ended.
    REQUEST_CLOSED = "request_closed"
    # Settling alone is not allowed, or the record is escalated: a person decides.
    NEEDS_PERSON_DECISION = "needs_person_decision"
    # Another write to the same file or credential is in progress.
    RECORD_BUSY = "record_busy"
