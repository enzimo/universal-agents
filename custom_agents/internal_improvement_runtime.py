"""Manager-owned recurring maintenance for InternalImprovementAgent workers.

Each occurrence first removes old scratch files and saved tool outputs past
their retention periods in trusted manager code
(``prune_scratch_for_maintenance``, ``remove_old_outputs_for_maintenance``),
then dispatches the agent's review with the counts in its instructions, which
the run records, so administrators see in the maintenance schedule's runs
that retention runs and what it deleted. The clean-ups run before the check
that a maintenance worker is available, so a worker that cannot start never
skips them, and once per occurrence: the scheduler retries an occurrence
whose worker did not start every 30 seconds, and the clean-ups are not
repeated for it. The saved output cleanup also writes a record of each run
(``custom_agents.output_retention.write_run_record``), from which the Status
page warns when no run succeeded for two days.

The clean-ups run only with this schedule. With
``agent.schedule_maintenance.internal_improvement_review_interval_seconds``
at 0 or less it is never installed, so they never run; the manager says so
in its log when it starts (``note_trusted_cleanups_at_start``).

A maintenance run is system work (access plan 7c): the scheduler signs it for
no person (``custom_agents.task_context_runtime.sign_system_context``; without
the key, the run carries the schedule's permit for no person,
``custom_agents.schedule_permits.sign_system_run``), and
InternalImprovementAgent is a built-in system agent (``access_role: system``),
so the run may use its role's standard tools on the shared ``team/`` and
``agents/`` folders and record its own schedule's run, and reaches nothing of
any person's: people's folders and schedules are private. So trusted manager
code does what needs everyone's records before each run and tells the run
only counts: it prunes scratch folders (``prune_scratch_for_maintenance``),
deletes saved tool outputs past their period (``remove_old_outputs_for_maintenance``),
ends the work of disabled and removed people and purges held data past its
retention time (``custom_agents.person_departure_runtime``), and checks every
schedule for ones nobody needs (``custom_agents.stale_schedules``, which tells
owners and administrators through the Inbox). The run reports risky files to
TeamArchitect instead of changing them; TeamArchitect's task for that
escalation works for this schedule's stored owner, the first user
(``task_context_runtime.sign_system_escalation``), so findings reach a person.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Mapping, Optional

from custom_agents.conversation_runtime import start_new_conversation
from custom_agents.output_retention import (
    OutputRetentionResult,
    ensure_run_record,
    grace_notice,
    remove_old_outputs_and_record,
)
from custom_agents.scratch_retention import prune_scratch_folders
from python_env_lifecycle import prune_request_environments
from custom_agents.schedule_permits import sign_system_run
from default_values import AGENT_DEFAULTS, TOOL_POLICY_DEFAULTS
from custom_messages.contracts.runtime_v1 import (
    INTERNAL_IMPROVEMENT_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduleMemoryPolicy, ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, RoutingType, ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import (
    INTERNAL_IMPROVEMENT_DELIVERY_MODE,
    INTERNAL_IMPROVEMENT_SCHEDULE_NAME_PREFIX,
)
from id_generation import (
    INTERNAL_IMPROVEMENT_ID_PREFIX,
    INTERNAL_IMPROVEMENT_LAUNCH_ID_PREFIX,
    INTERNAL_IMPROVEMENT_REVIEW_ID_PREFIX,
    operational_id,
)

logger = logging.getLogger(__name__)

# The schedule's name prefix and its runs' delivery mode come from the system
# schedule table (custom_messages.system_schedules), which the gateway reads too.
INTERNAL_IMPROVEMENT_SCHEDULE_KIND = "internal_improvement_maintenance"

MEMORY_REVIEW_CLEANUP_INSTRUCTION = """Inspect abandoned memory-review claims with inspect_memory_review_cleanup. These are inactive review records, not recurring memory schedules. A claim expiry does not establish whether memory was changed. Keep the outcome unknown unless authoritative evidence establishes it. Use maintain_memory_review_jobs(operation='archive') for the exact expired records after reviewing the metadata; archive preserves their saved content. Purge only already archived records after their retention period, with the same tool and operation='purge'. Never replay an expired review, declare it completed, edit the database with shell, or delete a recurring/system timer to clear this backlog. Both tools are among your fixed duties and need no grant; if a call is still refused with access_denied (for example an administrator's block), do not ask again: report the tool, the reason and the IDs to TeamArchitect. Record counts and exact IDs in the run result; do not copy private review content into messages."""

INTERNAL_IMPROVEMENT_TASK_INSTRUCTION = """Perform one bounded internal-maintenance review for this team.

This run is system work: it works for no person. You may use the shared team/ and agents/ folders (agents/ is your default folder), sandboxed shell and Python there, the memory-review cleanup tools, model lookup, and this schedule's own record. People's folders and people's schedules are private and out of reach in this run; do not try to open them.

Start with this schedule's own record: its compact memory is below, and list_scheduled_jobs and get_schedule_telemetry show only this schedule in this run. Then inspect generated artifacts in team/ and agents/ only where the available evidence identifies a concrete maintenance need. Prefer small, reversible improvements and concise evidence.

Never remove, pause or change a schedule. Trusted code checks every schedule for ones nobody needs before this run and tells their owners and administrators itself (the counts are below); do not look for or report stale schedules.

For files and artifacts in team/ and agents/, do not delete source-controlled, user-authored, or ambiguous content. You may repair or remove only clearly generated, disposable stale artifacts when the evidence and existing workspace rules make that safe. Otherwise report the candidate and recommended action to TeamArchitect.

Record the run with record_scheduled_run, including a compact state summary and evidence. If nothing changed and no material risk needs review, finish with task_response and suppress_outbound_dispatch=true. If you changed anything or found an actionable risk, send TeamArchitect a manager_escalation with escalation_type=worker_result and end the same action batch with a suppressed terminal task response; TeamArchitect does not reply to a report. If you need a decision, send a manager_escalation with escalation_type=needs_user_input, wait for TeamArchitect's reply, then finish with a suppressed terminal task response. TeamArchitect takes your findings to the person this maintenance schedule belongs to. Do not contact the user directly."""


def internal_improvement_schedule_id(team_id: str) -> str:
    """Return the stable singleton schedule id for a team's maintenance launcher."""

    digest = hashlib.sha256(str(team_id or "NO_TEAM").encode("utf-8")).hexdigest()[:16]
    return operational_id(INTERNAL_IMPROVEMENT_REVIEW_ID_PREFIX, digest)


def internal_improvement_schedule_name(team_id: str) -> str:
    return f"{INTERNAL_IMPROVEMENT_SCHEDULE_NAME_PREFIX}{team_id or 'NO_TEAM'}"


def is_internal_improvement_schedule_name(schedule_name: Optional[str]) -> bool:
    return str(schedule_name or "").startswith(INTERNAL_IMPROVEMENT_SCHEDULE_NAME_PREFIX)


def configured_review_interval_seconds(schedule_maintenance: Optional[Mapping[str, Any]]) -> float:
    """The maintenance cadence from ``agent.schedule_maintenance`` (zero or less turns it off).

    Shared by the manager, which installs the schedule, and the gateway, which
    refreshes it. A value that is not a number falls back to the default, logged.
    """

    default = AGENT_DEFAULTS.schedule_maintenance.value["internal_improvement_review_interval_seconds"]
    raw_value = dict(schedule_maintenance or {}).get("internal_improvement_review_interval_seconds", default)
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid internal improvement review interval %r; using default %s seconds. "
            "event=internal_improvement_schedule_interval_invalid",
            raw_value,
            default,
        )
        return float(default)


def _review_interval_seconds(agent: Any) -> float:
    return configured_review_interval_seconds(getattr(agent, "schedule_maintenance", None))


def build_internal_improvement_schedule(agent: Any) -> Optional[ScheduledMessage]:
    """Build an ensure-only request for the team's manager-owned launcher."""

    interval_seconds = _review_interval_seconds(agent)
    if interval_seconds <= 0:
        agent._logger.info(
            "[%s] Internal improvement schedule installation is disabled by interval=%s. "
            "event=internal_improvement_schedule_disabled",
            agent.id,
            interval_seconds,
        )
        return None

    team_id = str(getattr(agent, "team_id", None) or "NO_TEAM")
    schedule_id = internal_improvement_schedule_id(team_id)
    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=internal_improvement_schedule_name(team_id),
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
        install_mode="ensure",
        payload=Task(
            task_id=operational_id(
                INTERNAL_IMPROVEMENT_LAUNCH_ID_PREFIX,
                hashlib.sha256(team_id.encode("utf-8")).hexdigest()[:12],
            ),
            task_type=MessageMetaType.REQUEST,
            problem_statement=INTERNAL_IMPROVEMENT_TASK_INSTRUCTION,
        ),
        payload_header=MessageHeader(
            from_agent_id=getattr(agent, "id", None),
            to_routing_path=team_id,
            response_routing_key=getattr(agent, "id", None),
            agent_signature="",
        ),
        payload_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            to_named_agent_type=INTERNAL_IMPROVEMENT_AGENT_TYPE,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=False,
        ),
        interval_ms=max(1, int(interval_seconds * 1000)),
        repeat_count=None,
        schedule_memory=ScheduleTaskMemory(
            schedule_kind=INTERNAL_IMPROVEMENT_SCHEDULE_KIND,
            policy=ScheduleMemoryPolicy(
                notify="never",
                notify_on_failure=True,
                evidence_required="local_source_of_truth",
                custom={
                    # Trusted code handles stale schedules before each run (module docstring).
                    "manager_review_for_material_findings": True,
                },
            ),
            domain_schema_hint={
                "workspace_findings": "list[string]",
            },
        ),
    )


async def publish_internal_improvement_schedule_ensure(agent: Any) -> bool:
    """Ask TeamArchitect to install the singleton schedule without replacement."""

    if getattr(agent, "named_agent_type", None) != INTERNAL_IMPROVEMENT_AGENT_TYPE:
        return False
    scheduled_message = build_internal_improvement_schedule(agent)
    if scheduled_message is None:
        return False

    team_id = str(getattr(agent, "team_id", None) or "NO_TEAM")
    envelope = MessageEnvelope(
        msg_content=scheduled_message,
        msg_meta=scheduled_message.payload_meta.model_copy(deep=True),
        msg_header=MessageHeader(
            from_agent_id=getattr(agent, "id", None),
            to_routing_path=team_id,
            response_routing_key=getattr(agent, "id", None),
            agent_signature="",
            idempotency_key=(
                f"internal_improvement_schedule_ensure:{team_id}:{getattr(agent, 'id', 'unknown')}"
            ),
        ),
    )
    try:
        await agent.team_comms.send_message(message=envelope, routing_key=team_id)
    except Exception:
        agent._logger.exception(
            "[%s] Failed to request the manager-owned internal improvement schedule. "
            "event=internal_improvement_schedule_ensure_publish_failed schedule_id=%s team_id=%s",
            agent.id,
            scheduled_message.schedule_id,
            team_id,
        )
        return False

    agent._logger.info(
        "[%s] Requested ensure-only installation of internal improvement schedule %s for team %s. "
        "event=internal_improvement_schedule_ensure_published",
        agent.id,
        scheduled_message.schedule_id,
        team_id,
    )
    return True


def prepare_internal_improvement_schedule_for_manager(
    agent: Any,
    scheduled_message: ScheduledMessage,
) -> Optional[ScheduledMessage]:
    """Validate and normalize an InternalImprovement schedule as manager-owned."""

    if not is_internal_improvement_schedule_name(scheduled_message.schedule_name):
        return scheduled_message
    if (
        not getattr(agent, "is_team_lead", False)
        or getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE
    ):
        agent._logger.warning(
            "[%s] Rejected internal improvement schedule %s because only TeamArchitect may own it. "
            "event=internal_improvement_schedule_owner_rejected",
            agent.id,
            scheduled_message.schedule_id,
        )
        return None

    team_id = str(getattr(agent, "team_id", None) or "NO_TEAM")
    expected_id = internal_improvement_schedule_id(team_id)
    expected_name = internal_improvement_schedule_name(team_id)
    schedule_memory = scheduled_message.schedule_memory
    payload_meta = scheduled_message.payload_meta
    if (
        scheduled_message.schedule_id != expected_id
        or scheduled_message.schedule_name != expected_name
        or scheduled_message.install_mode != "ensure"
        or scheduled_message.interval_ms is None
        or scheduled_message.interval_ms <= 0
        or scheduled_message.cron_expression is not None
        or scheduled_message.repeat_count is not None
        or schedule_memory is None
        or schedule_memory.schedule_kind != INTERNAL_IMPROVEMENT_SCHEDULE_KIND
        or payload_meta is None
        or payload_meta.to_named_agent_type != INTERNAL_IMPROVEMENT_AGENT_TYPE
    ):
        agent._logger.warning(
            "[%s] Rejected malformed internal improvement schedule request. "
            "event=internal_improvement_schedule_invalid received_id=%s expected_id=%s "
            "received_name=%s expected_name=%s install_mode=%s",
            agent.id,
            scheduled_message.schedule_id,
            expected_id,
            scheduled_message.schedule_name,
            expected_name,
            scheduled_message.install_mode,
        )
        return None

    normalized = scheduled_message.model_copy(deep=True)
    bootstrap = dict(getattr(agent, "bootstrap_config", {}) or {})
    canonical_user_id = str(bootstrap.get("canonical_user_id") or "").strip() or None
    # The schedule records the person who set up the instance only because a
    # worker launch names the person it was started for; its runs still work
    # for no person (sign_system_context in enqueue_internal_improvement_job).
    # This context is only for a new row: installs here are ensure-only, so a
    # stored schedule, and the chat session the gateway refreshes on it
    # (install mode refresh, custom_agents.schedule_context_refresh), is never
    # replaced. Escalations take the owner's current roles from the gateway's
    # copy of the team's people (access_control.people_directory), so a
    # schedule created here after the gateway's last refresh works at once.
    if canonical_user_id is None:
        agent._logger.warning(
            "[%s] Installing internal improvement schedule %s without a registered bootstrap owner; "
            "the timer cannot start an InternalImprovementAgent when none is running. "
            "event=internal_improvement_schedule_owner_context_missing",
            agent.id,
            normalized.schedule_id,
        )

    normalized.claim_team_id = team_id
    normalized.claim_named_agent_type = getattr(agent, "named_agent_type", None)
    normalized.claim_agent_id = None
    normalized.payload_header = MessageHeader(
        from_agent_id=getattr(agent, "id", None),
        to_routing_path=RoutingType.SELF_ROUTE.value,
        response_routing_key=getattr(agent, "id", None),
        agent_signature="",
    )
    normalized.payload_meta = MessageMeta(
        from_agent_name=getattr(agent, "name", None),
        to_named_agent_type=INTERNAL_IMPROVEMENT_AGENT_TYPE,
        requester_type=RequesterType.TIMER_REQUESTER,
        is_response_requested=False,
        request_timezone=(
            str(bootstrap.get("timezone") or "").strip()
            or getattr(agent, "default_request_timezone", None)
        ),
        gateway_context=GatewayContext(
            gateway_instance_name=str(bootstrap.get("instance_name") or "").strip() or None,
            canonical_user_id=canonical_user_id,
            delivery_mode=INTERNAL_IMPROVEMENT_DELIVERY_MODE,
            scheduled_job_id=normalized.schedule_id,
            schedule_owner_agent_id=getattr(agent, "id", None),
        ),
    )
    return normalized


async def enqueue_internal_improvement_job(
    agent: Any,
    schedule_id: str,
    scheduled_message: ScheduledMessage,
) -> bool:
    """Ensure one worker is live and dispatch one idempotent maintenance occurrence."""

    if not getattr(agent, "is_team_lead", False):
        agent._logger.warning(
            "[%s] Refusing to dispatch manager-owned internal improvement schedule %s from a non-lead.",
            agent.id,
            schedule_id,
        )
        return False

    routing_key = (getattr(agent, "named_agent_type_queue_map", {}) or {}).get(
        INTERNAL_IMPROVEMENT_AGENT_TYPE
    ) or f"{agent.team_id}_{INTERNAL_IMPROVEMENT_AGENT_TYPE}"
    occurrence_key = hashlib.sha256(
        f"{schedule_id}:{scheduled_message.next_fire_at or 'unknown'}".encode("utf-8")
    ).hexdigest()[:20]
    # Before the worker check, so a worker that cannot start never skips them.
    scratch_sentence, outputs_sentence = await trusted_cleanups_for_occurrence(agent, schedule_id, occurrence_key)
    if not await ensure_maintenance_worker(agent, scheduled_message, occurrence_key):
        agent._logger.warning(
            "[%s] Could not verify or start an InternalImprovementAgent for schedule %s. "
            "occurrence_key=%s routing_key=%s",
            agent.id,
            schedule_id,
            occurrence_key,
            routing_key,
        )
        return False

    python_sentence = await prune_python_envs_for_maintenance(agent, schedule_id)
    schedules_sentence = await check_schedules_for_maintenance(agent, schedule_id)
    task = scheduled_message.payload.model_copy(deep=True)
    occurrence_id = operational_id(INTERNAL_IMPROVEMENT_ID_PREFIX, occurrence_key)
    task.task_id = occurrence_id
    task.parent_task_id = None
    memory_json = (
        scheduled_message.schedule_memory.model_dump_json(indent=2)
        if scheduled_message.schedule_memory is not None
        else "null"
    )
    task.problem_statement = (
        f"{str(task.problem_statement or '').strip()}\n\n"
        f"{MEMORY_REVIEW_CLEANUP_INSTRUCTION}\n\n"
        f"{scratch_sentence}\n\n"
        f"{python_sentence}\n\n"
        f"{outputs_sentence}\n\n"
        f"{schedules_sentence}\n\n"
        f"Canonical schedule_id: {schedule_id}\n"
        "Current compact schedule memory:\n"
        f"```json\n{memory_json}\n```"
    )

    meta = (
        scheduled_message.payload_meta.model_copy(deep=True)
        if scheduled_message.payload_meta is not None
        else MessageMeta()
    )
    meta.from_agent_name = getattr(agent, "name", None)
    meta.to_named_agent_type = INTERNAL_IMPROVEMENT_AGENT_TYPE
    meta.requester_type = RequesterType.TIMER_REQUESTER
    meta.is_response_requested = False
    meta.msg_correlation_id = occurrence_id
    # System work for no person starts its own conversation (conversation_runtime).
    start_new_conversation(meta)
    if meta.gateway_context is None:
        meta.gateway_context = GatewayContext()
    meta.gateway_context.delivery_mode = INTERNAL_IMPROVEMENT_DELIVERY_MODE
    meta.gateway_context.scheduled_job_id = schedule_id
    meta.gateway_context.schedule_owner_agent_id = getattr(agent, "id", None)
    # System work for no person, naming this schedule: signed by the scheduler
    # here, or else carrying the schedule's permit for no person.
    await asyncio.to_thread(sign_system_run, agent, meta, schedule_id=schedule_id)

    envelope = MessageEnvelope(
        msg_content=task,
        msg_meta=meta,
        msg_header=MessageHeader(
            from_agent_id=getattr(agent, "id", None),
            to_routing_path=routing_key,
            response_routing_key=getattr(agent, "id", None),
            agent_signature="",
            idempotency_key=f"internal_improvement_occurrence:{occurrence_key}",
        ),
    )
    try:
        await agent.team_comms.send_message(message=envelope, routing_key=routing_key)
    except Exception:
        agent._logger.exception(
            "[%s] Failed to dispatch internal improvement occurrence %s for schedule %s. "
            "event=internal_improvement_dispatch_failed",
            agent.id,
            occurrence_key,
            schedule_id,
        )
        return False

    agent._logger.info(
        "[%s] Dispatched internal improvement occurrence %s for schedule %s to %s. "
        "event=internal_improvement_dispatched",
        agent.id,
        occurrence_key,
        schedule_id,
        routing_key,
    )
    return True


_CLEANUPS_DONE_ATTRIBUTE = "_maintenance_cleanups_done"


async def trusted_cleanups_for_occurrence(agent: Any, schedule_id: str, occurrence_key: str) -> tuple[str, str]:
    """Scratch and saved output clean-ups, once per occurrence; their sentences for the run's instructions.

    A retried occurrence (its worker did not start) gets the sentences of
    the clean-ups that already ran for it.
    """

    done = getattr(agent, _CLEANUPS_DONE_ATTRIBUTE, None)
    if isinstance(done, tuple) and len(done) == 3 and done[0] == occurrence_key:
        return done[1], done[2]
    scratch_sentence = await prune_scratch_for_maintenance(agent, schedule_id)
    outputs_sentence = await remove_old_outputs_for_maintenance(agent, schedule_id)
    setattr(agent, _CLEANUPS_DONE_ATTRIBUTE, (occurrence_key, scratch_sentence, outputs_sentence))
    return scratch_sentence, outputs_sentence


def note_trusted_cleanups_at_start(agent: Any) -> None:
    """At manager start: start the saved output cleanup's record, and log when the clean-ups can never run."""

    from custom_tools.workspace_scope import current_instance_layout

    layout = current_instance_layout(agent)
    if layout is not None:
        try:
            ensure_run_record(
                layout.output_retention_record_path,
                retention_days=int(getattr(agent, "output_retention_days", 0) or 0),
            )
        except OSError:
            agent._logger.exception(
                "[%s] The record of the saved tool output cleanup could not be started. "
                "event=output_retention_record_failed",
                agent.id,
            )
    scratch_days = int(getattr(agent, "scratch_retention_days", 0) or 0)
    output_days = int(getattr(agent, "output_retention_days", 0) or 0)
    if _review_interval_seconds(agent) <= 0 and (scratch_days > 0 or output_days > 0):
        agent._logger.warning(
            "[%s] The internal-improvement maintenance schedule is turned off, so old scratch files and saved "
            "tool outputs are never deleted. event=retention_cleanup_off reason=maintenance_schedule_off "
            "scratch_retention_days=%s output_retention_days=%s",
            agent.id,
            scratch_days,
            output_days,
        )


async def prune_scratch_for_maintenance(agent: Any, schedule_id: str) -> str:
    """Delete old files from every scratch/ folder; returns a sentence for the run's instructions.

    Trusted manager code does this before InternalImprovementAgent's review
    because the review is system work for no person and cannot open people's
    folders (decision D6). See ``custom_agents.scratch_retention`` for the rules.
    """

    from custom_tools.workspace_scope import current_instance_layout

    retention_days = int(
        getattr(agent, "scratch_retention_days", TOOL_POLICY_DEFAULTS.scratch_retention_days.value)
    )
    layout = current_instance_layout(agent)
    if layout is None:
        agent._logger.warning(
            "[%s] Scratch cleanup did not run because this runtime has no instance folder layout. "
            "event=scratch_prune_skipped reason=no_instance_layout schedule_id=%s",
            agent.id,
            schedule_id,
        )
        return "Scratch cleanup did not run because this runtime has no instance folder layout; report it."
    try:
        result = await asyncio.to_thread(
            prune_scratch_folders, layout.workspaces_dir, retention_days=retention_days
        )
    except Exception:
        agent._logger.exception(
            "[%s] Scratch cleanup failed. event=scratch_prune_failed schedule_id=%s workspaces_dir=%s",
            agent.id,
            schedule_id,
            layout.workspaces_dir,
        )
        return (
            "Scratch cleanup failed before this review (manager log event=scratch_prune_failed); "
            "report it to TeamArchitect."
        )
    return result.instruction_sentence()


async def prune_python_envs_for_maintenance(agent: Any, schedule_id: str) -> str:
    """Keep private workspace cleanup in trusted code; send only counts to the review."""

    from custom_tools.workspace_scope import current_instance_layout

    layout = current_instance_layout(agent)
    if layout is None:
        agent._logger.warning(
            "Python environment cleanup needs an instance layout. event=python_env_prune_failed schedule_id=%s",
            schedule_id,
        )
        return "Python environment cleanup did not run because this runtime has no instance folder layout; report it."
    try:
        removed = await asyncio.to_thread(prune_request_environments, layout)
    except Exception:
        agent._logger.exception("Python environment cleanup failed. event=python_env_prune_failed schedule_id=%s", schedule_id)
        return "Python environment cleanup failed before this review; report event=python_env_prune_failed to TeamArchitect."
    return f"Trusted maintenance removed {removed} request-default Python environments unused for 7 days. Named environments were kept."


async def remove_old_outputs_for_maintenance(agent: Any, schedule_id: str) -> str:
    """Delete saved tool outputs past ``tool_policy.output_retention_days``; returns a sentence for the run's instructions.

    Trusted manager code, for the same reason as scratch cleanup: the outputs
    are in ``state/``, ``dbs/`` and people's folders, which the review cannot
    open. See ``custom_agents.output_retention`` for the rules.
    """

    from custom_tools.workspace_scope import current_instance_layout

    retention_days = int(
        getattr(agent, "output_retention_days", TOOL_POLICY_DEFAULTS.output_retention_days.value)
    )
    layout = current_instance_layout(agent)
    if layout is None:
        agent._logger.warning(
            "[%s] Saved tool output cleanup did not run because this runtime has no instance folder layout. "
            "event=output_retention_skipped reason=no_instance_layout schedule_id=%s",
            agent.id,
            schedule_id,
        )
        return "Saved tool output cleanup did not run because this runtime has no instance folder layout; report it."
    try:
        result = await asyncio.to_thread(
            remove_old_outputs_and_record,
            layout,
            retention_days=retention_days,
            conversation_records=getattr(agent, "_conversation_store", None),
        )
    except Exception:
        agent._logger.exception(
            "[%s] Saved tool output cleanup failed. event=output_retention_failed schedule_id=%s",
            agent.id,
            schedule_id,
        )
        return (
            "Saved tool output cleanup failed before this review (manager log event=output_retention_failed); "
            "report it to TeamArchitect."
        )
    if result.dry_run:
        await asyncio.to_thread(tell_administrators_of_grace_period, agent, result)
    return result.instruction_sentence()


def tell_administrators_of_grace_period(agent: Any, result: OutputRetentionResult) -> bool:
    """One Inbox notice to administrators per grace period, with the first dry run's counts.

    The Status page shows the latest counts for the whole grace period
    (``custom_agents.status_findings``); this notice is the one message
    administrators get, so they can change the period before anything goes.
    """

    from access_control.people import PERSON_ROLE_ADMIN
    from custom_agents.access_runtime import send_runtime_notice_once
    from custom_messages.contracts.access_v2 import AccessNoticeKind

    title, summary = grace_notice(result)
    return send_runtime_notice_once(
        agent,
        notice_id=f"output_retention_grace:{int(result.active_since or 0)}",
        kind=AccessNoticeKind.SAVED_OUTPUT_RETENTION,
        roles=(PERSON_ROLE_ADMIN,),
        title=title,
        summary=summary,
    )


async def check_schedules_for_maintenance(agent: Any, schedule_id: str) -> str:
    """End departed people's work, then check every schedule; returns a sentence of counts for the run's instructions.

    Trusted manager code, for the same reason as scratch cleanup
    (``custom_agents.stale_schedules.check_schedules``).
    """

    from custom_agents.stale_schedules import check_schedules

    try:
        return await check_schedules(agent)
    except Exception:
        agent._logger.exception(
            "[%s] The stale-schedule check failed. event=stale_schedule_check_failed schedule_id=%s",
            agent.id,
            schedule_id,
        )
        return (
            "The stale-schedule check failed before this review (manager log event=stale_schedule_check_failed); "
            "report it to TeamArchitect."
        )


async def ensure_maintenance_worker(agent: Any, schedule: ScheduledMessage, occurrence_key: str) -> bool:
    """Use the timer's trusted owner when the manager has no current chat task."""
    from custom_agents.agent_instantiation_service import AgentInstantiationService
    from custom_agents.schedule_store import owner_canonical_user_id_for_schedule

    if await agent._probe_live_named_agent_type(INTERNAL_IMPROVEMENT_AGENT_TYPE) is not None:
        return True
    if (
        not agent.is_team_lead
        or schedule.schedule_id != internal_improvement_schedule_id(agent.team_id)
        or schedule.claim_team_id != agent.team_id
        or schedule.system_schedule_kind != SystemScheduleKind.INTERNAL_IMPROVEMENT
    ):
        return False
    launcher = AgentInstantiationService(agent)
    result = launcher.launch(
        agent_type=INTERNAL_IMPROVEMENT_AGENT_TYPE,
        owner_id=owner_canonical_user_id_for_schedule(schedule) or "",
        request_id=f"internal_maintenance:{occurrence_key}", source="delegation",
        idle_ttl_seconds=launcher.default_idle_ttl(INTERNAL_IMPROVEMENT_AGENT_TYPE),
    )
    if result.status not in {"ready", "starting", "waiting"}:
        agent._logger.warning(
            "Maintenance worker could not start. event=internal_improvement_launch_blocked schedule_id=%s reason=%s",
            schedule.schedule_id, result.reason,
        )
        return False
    return await agent._probe_live_named_agent_type(INTERNAL_IMPROVEMENT_AGENT_TYPE) is not None
