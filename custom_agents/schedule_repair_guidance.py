"""Scheduled-worker block reasons, the repair each one needs, and what people are told.

Every reason the scheduler records is defined here once. Two tables read it:

- ``repair_instructions`` for TeamLead (``inspect_scheduled_worker``), in tool terms;
- ``person_explanation`` for the notice a stuck schedule's owner or the
  administrators get (``custom_agents.stuck_schedules``), in plain words: what
  is wrong and what to do. ``administrators_act`` says whether only an
  administrator can fix it, so the notice for a person's own schedule also
  reaches administrators.

The table has no runtime dependencies.
"""

from __future__ import annotations

from custom_messages.contracts.schedule_worker_v1 import (
    AUTOMATIC_WORKER_CAPACITY_LIMIT,
    TEAM_WORKER_CAPACITY_LIMIT,
    WORKER_LAUNCH_RATE_LIMIT,
)

AGENT_DEFINITION_CHANGED = "agent_definition_changed_review_required"
SCHEDULE_EXECUTION_CHANGED = "schedule_execution_changed_review_required"
# The schedule changed between the admission check and the start itself.
SCHEDULE_LAUNCH_INTENT_CHANGED = "schedule_launch_intent_changed_before_start"
KNOWN_CAPABILITY_BLOCKER = "known_capability_blocker"
WORKER_POLICY_EXPIRED = "worker_policy_expired"
LAUNCH_LIVENESS_UNKNOWN = "launch_liveness_unknown"
WORKER_READY_BUT_UNCLAIMED = "worker_ready_but_occurrence_unclaimed"
WORKER_LAUNCH_ATTEMPTS_EXHAUSTED = "worker_launch_attempts_exhausted"
WORKER_LAUNCH_UNAVAILABLE = "worker_launch_unavailable"
EXISTING_WORKER_NOT_CLAIMING = "existing_worker_not_claiming"
WORKER_RECONCILIATION_FAILED = "worker_reconciliation_failed"
AGENT_TYPE_DISALLOWED = "agent_type_disallowed"
AGENT_TYPE_INELIGIBLE = "agent_type_ineligible_for_automatic_launch"
AGENT_DEFINITION_UNAVAILABLE = "agent_definition_unavailable"
# Not a worker block: a schedule with no worker policy is overdue and no
# standing worker took it (custom_agents.stuck_schedules).
SCHEDULE_OVERDUE_UNCLAIMED = "schedule_overdue_unclaimed"

# Reasons whose repair is re-saving the policy with configure_scheduled_worker.
RESAVE_REPAIR_REASONS = frozenset(
    {AGENT_DEFINITION_CHANGED, SCHEDULE_EXECUTION_CHANGED, WORKER_POLICY_EXPIRED}
)

_ELIGIBILITY_REPAIR = (
    "Follow eligibility.next_steps. Explicit operator denies and the automatic "
    "start switch stay operator-owned; do not bypass them."
)

_GENERIC_ACCESS_REPAIR = (
    "When a tool call is refused, call request_access with the tool_id and level "
    "the refusal names; the run waits until TeamLead or a person gives it. A new "
    "grant is usable on the next model call, with no restart. For a tool the "
    "catalog lacks or a missing account link, use a manager_escalation action "
    "naming this schedule and the tool. A notice or a message grants nothing; "
    "retry the call once access is given."
)

_REPAIR_BY_REASON = {
    AGENT_DEFINITION_CHANGED: (
        "Someone changed the team-written definition this schedule starts. "
        "definition.changes lists what changed since the policy was saved. Show "
        "the owner those changes and ask whether the schedule should run with the "
        "new definition. After the owner agrees in their conversation, call "
        "configure_scheduled_worker with the values in suggested_refresh; leave "
        "worker_policy_json empty to keep the saved startup settings, then run "
        "prepare_scheduled_job_access to check the new definition's access. "
        "Code releases never cause this block."
    ),
    SCHEDULE_EXECUTION_CHANGED: (
        "The schedule's task, owner, target or delivery changed after its worker "
        "policy was saved. Confirm with the owner that the change was intended, "
        "then call configure_scheduled_worker with the values in suggested_refresh "
        "(leave worker_policy_json empty to keep the saved startup settings)."
    ),
    WORKER_POLICY_EXPIRED: (
        "The worker policy's expiry passed. If the owner wants the schedule to "
        "keep running, re-save it with configure_scheduled_worker and a new "
        "expires_at_utc (or none) in worker_policy_json."
    ),
    KNOWN_CAPABILITY_BLOCKER: (
        "A declared requirement is blocked. Read readiness.checks for the failed "
        "requirement and its reason. For missing access, call "
        "authorize_scheduled_job_access: it gives the worker type what TeamLead "
        "may hand on and names the rest, which a person gives (or the worker asks "
        "for with request_access). requirement_not_a_catalog_tool means the saved "
        "requirement names something that is not a tool catalog id, such as a "
        "removed tool group (builtin:web_research): show the owner, and after they "
        "agree call configure_scheduled_worker with requirements_json naming "
        "catalog tool ids and operations (find_tools lists them). "
        "tool_not_available_to_worker_type means the worker type's default_tools "
        "lack the tool (authorization off); mcp_server_not_for_worker_type means the "
        "installed MCP server's record does not name the worker type (authorization off; "
        "with it on, the worker type needs access to the server's mcp: tool). Then inspect again."
    ),
    LAUNCH_LIVENESS_UNKNOWN: (
        "The previous start's process could not be checked, so the scheduler will "
        "not start a duplicate. It clears by itself once the process is proven gone: "
        "after the runtime restarts, a start from the earlier run is settled on the "
        "next scan (runtime_hosting.process_run). If it stays, the start belongs to "
        "a process started without the supervisor; report the launch id to the "
        "operator. Do not start another worker manually for this occurrence."
    ),
    SCHEDULE_LAUNCH_INTENT_CHANGED: (
        "The schedule changed while its worker was being started. Inspect again: "
        "if the block is now schedule_execution_changed_review_required, follow "
        "that repair; otherwise the next scan starts it."
    ),
    AUTOMATIC_WORKER_CAPACITY_LIMIT: (
        "The operator's limit on automatically started workers is reached. The "
        "start is retried when a worker finishes. If it lasts, look for a stuck "
        "worker of this type; raising agent.schedule_workers limits is operator-owned."
    ),
    TEAM_WORKER_CAPACITY_LIMIT: (
        "The team's worker limit is reached. The start is retried when a worker "
        "finishes; raising the limit is operator-owned."
    ),
    WORKER_LAUNCH_RATE_LIMIT: (
        "Too many workers of this type started in the last hour. The start is "
        "retried once the hourly limit allows."
    ),
    WORKER_READY_BUT_UNCLAIMED: (
        "A worker started but did not take the occurrence within the admission "
        "window. Inspect that worker's queue and recent errors before retrying."
    ),
    WORKER_LAUNCH_ATTEMPTS_EXHAUSTED: (
        "Every start attempt for this occurrence failed. Read launch_attempt and "
        "launch for the failure reason and fix its cause. The next occurrence "
        "tries again."
    ),
    WORKER_LAUNCH_UNAVAILABLE: (
        "The launcher refused the start. Read launch_attempt.reason and repair "
        "that condition."
    ),
    EXISTING_WORKER_NOT_CLAIMING: (
        "A live worker of the target type exists but is not claiming the "
        "occurrence. Check whether it is busy or stuck. Setting launch_when_busy "
        "lets the scheduler start another worker when the existing one is busy."
    ),
    WORKER_RECONCILIATION_FAILED: (
        "The scheduler hit an internal error for this schedule. Report it to the "
        "operator with the schedule id; the runtime log has the details."
    ),
    AGENT_TYPE_DISALLOWED: _ELIGIBILITY_REPAIR,
    AGENT_TYPE_INELIGIBLE: _ELIGIBILITY_REPAIR,
    AGENT_DEFINITION_UNAVAILABLE: (
        "The target agent type is no longer registered. Retarget the schedule to "
        "a registered specialist from get_available_agent_types."
    ),
}


_ASK_ASSISTANT = "Ask your assistant to review this schedule; it can show you the details."
_ADMIN_LOG = "An administrator should check the runtime log for this schedule."

_PERSON_EXPLANATION_BY_REASON = {
    AGENT_DEFINITION_CHANGED: (
        "Someone on the team changed the worker this schedule uses, so it is "
        "waiting for you to confirm it should keep running with the new version. "
        + _ASK_ASSISTANT
    ),
    SCHEDULE_EXECUTION_CHANGED: (
        "The schedule's task, owner or delivery was changed outside a request of "
        "yours, so it is waiting for you to confirm the change. " + _ASK_ASSISTANT
    ),
    SCHEDULE_LAUNCH_INTENT_CHANGED: (
        "The schedule changed while its worker was starting, so the run did not "
        "start. " + _ASK_ASSISTANT
    ),
    WORKER_POLICY_EXPIRED: (
        "The permission to start a worker for this schedule has expired. Ask your "
        "assistant to renew it if you want the schedule to keep running."
    ),
    KNOWN_CAPABILITY_BLOCKER: (
        "The worker for this schedule lacks access it needs. Ask your assistant "
        "which access is missing; it can ask for it on your behalf."
    ),
    LAUNCH_LIVENESS_UNKNOWN: (
        "A worker started for this schedule earlier could not be confirmed as "
        "stopped, so the system will not start a second one. This normally clears "
        "by itself after the next restart. " + _ADMIN_LOG
    ),
    WORKER_READY_BUT_UNCLAIMED: (
        "A worker started for this schedule but did not pick up the run. " + _ADMIN_LOG
    ),
    WORKER_LAUNCH_ATTEMPTS_EXHAUSTED: (
        "Every attempt to start a worker for this run failed. " + _ADMIN_LOG
    ),
    WORKER_LAUNCH_UNAVAILABLE: (
        "The system refused to start a worker for this schedule. " + _ADMIN_LOG
    ),
    EXISTING_WORKER_NOT_CLAIMING: (
        "A worker for this schedule is running but is not picking up the run; it "
        "may be busy or stuck. " + _ADMIN_LOG
    ),
    WORKER_RECONCILIATION_FAILED: (
        "The scheduler hit an internal error for this schedule. " + _ADMIN_LOG
    ),
    AGENT_TYPE_DISALLOWED: (
        "An administrator has blocked the kind of worker this schedule uses. Ask an "
        "administrator, or ask your assistant to move the schedule to another worker."
    ),
    AGENT_TYPE_INELIGIBLE: (
        "The kind of worker this schedule uses is not allowed to start "
        "automatically. " + _ASK_ASSISTANT
    ),
    AGENT_DEFINITION_UNAVAILABLE: (
        "The kind of worker this schedule uses no longer exists. Ask your assistant "
        "to move the schedule to another worker."
    ),
    AUTOMATIC_WORKER_CAPACITY_LIMIT: (
        "Too many automatically started workers are running, so this schedule's "
        "worker cannot start yet. It starts when one finishes. If this lasts, an "
        "administrator should look for a stuck worker."
    ),
    TEAM_WORKER_CAPACITY_LIMIT: (
        "The team's limit on running workers is reached, so this schedule's worker "
        "cannot start yet. It starts when one finishes. If this lasts, an "
        "administrator should look for a stuck worker."
    ),
    WORKER_LAUNCH_RATE_LIMIT: (
        "Too many workers of this kind started in the last hour. The schedule "
        "starts again once the hourly limit allows."
    ),
    SCHEDULE_OVERDUE_UNCLAIMED: (
        "The schedule is overdue and no worker has picked it up. " + _ADMIN_LOG
    ),
}

# Reasons only an administrator can fix (runtime or operator settings).
_ADMINISTRATOR_REASONS = frozenset(
    {
        LAUNCH_LIVENESS_UNKNOWN,
        WORKER_READY_BUT_UNCLAIMED,
        WORKER_LAUNCH_ATTEMPTS_EXHAUSTED,
        WORKER_LAUNCH_UNAVAILABLE,
        EXISTING_WORKER_NOT_CLAIMING,
        WORKER_RECONCILIATION_FAILED,
        AUTOMATIC_WORKER_CAPACITY_LIMIT,
        TEAM_WORKER_CAPACITY_LIMIT,
        SCHEDULE_OVERDUE_UNCLAIMED,
    }
)


def repair_instructions(reason: str | None) -> str:
    """Instructions for one block reason; access repair when nothing is blocked."""
    return _REPAIR_BY_REASON.get(reason or "", _GENERIC_ACCESS_REPAIR)


def person_explanation(reason: str | None) -> str:
    """What a person is told about a stuck schedule: what is wrong and what to do."""
    return _PERSON_EXPLANATION_BY_REASON.get(
        reason or "",
        "The schedule is not running and the system could not tell why. " + _ADMIN_LOG,
    )


def administrators_act(reason: str | None) -> bool:
    """True when only an administrator can fix this block."""
    return (reason or "") not in _PERSON_EXPLANATION_BY_REASON or reason in _ADMINISTRATOR_REASONS
