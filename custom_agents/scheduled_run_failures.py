"""Deliver failure notices saved atomically before scheduled tasks are archived.

The lead verifies the original identity for historical attribution, never for
authorization. Inbox retries use the same notice id; chat retries use the same
gateway idempotency key. The outbox is acknowledged only after both succeed.
"""

from __future__ import annotations

from functools import partial
from hashlib import sha256
from typing import Any

from access_control.identity import ISSUER_SCHEDULER, IdentityRejected
from access_control.people import PERSON_ROLE_ADMIN
from custom_agents.access_runtime import send_runtime_notice_once
from custom_agents.stuck_tasks import StuckTasks, stuck_task_settings
from custom_agents.stuck_work import StuckMessage, send_chat_notice
from custom_agents.task_context_runtime import context_verifier
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.enums import RequesterType
from storage.scheduled_run_failure_repository import ScheduledRunFailureRepository, ScheduledRunFailureV1

SCHEDULE_RUN_FAILED = "schedule_run_failed"


def _identity(meta: Any, team_id: str) -> tuple[str, str | None] | None:
    """Schedule and person from the original signature, even after expiry."""
    gateway = meta.gateway_context
    if gateway is None or meta.requester_type != RequesterType.TIMER_REQUESTER:
        return None
    verifier = context_verifier()
    try:
        if gateway.schedule_run is not None and gateway.task_context is None:
            permit = verifier.verify_permit(gateway.schedule_run.permit, team_id=team_id, check_expiry=False)
            return permit.schedule_id, permit.person_id
        if gateway.task_context is not None and gateway.schedule_run is None:
            context = verifier.verify(gateway.task_context, team_id=team_id, check_expiry=False)
            if context.issuer == ISSUER_SCHEDULER and context.schedule_id:
                return context.schedule_id, context.person_id
    except (IdentityRejected, ValueError):
        return None
    return None


async def report_scheduled_run_failures(agent: Any) -> None:
    """Send one Inbox/chat report per failed top-level scheduled run, in both access modes."""
    if not getattr(agent, "is_team_lead", False):
        return
    try:
        await _report(agent)
    except Exception:
        agent._logger.exception(
            "Could not check failed scheduled runs; retrying next pass. "
            "event=scheduled_run_failure_scan_failed team_id=%s", agent.team_id,
        )


async def _report(agent: Any) -> None:
    runtime = agent.task_manager.operations_runtime
    repository = ScheduledRunFailureRepository(agent.task_manager.storage_settings)
    for failure in await runtime.run(repository.pending, agent.team_id):
        try:
            identity = await runtime.run(_identity, failure.meta, agent.team_id)
            if identity is None:
                agent._logger.warning(
                    "A failed timer has no verified scheduled identity; no personal notice was sent. "
                    "event=scheduled_run_failure_identity_refused task_id=%s", failure.task_id,
                )
            else:
                await _tell(agent, failure, *identity)
            await runtime.run(repository.reported, agent.team_id, failure.task_id)
        except Exception:
            agent._logger.exception(
                "Could not report a failed scheduled run; retrying next pass. "
                "event=scheduled_run_failure_notice_failed task_id=%s", failure.task_id,
            )


async def _tell(agent: Any, failure: ScheduledRunFailureV1, schedule_id: str, person_id: str | None) -> None:
    message = StuckMessage(
        title="A scheduled run failed",
        text=(f"Schedule {schedule_id} started, but its run {failure.task_id} failed before completion. "
              "Its result is not confirmed. Ask your assistant to inspect the task history before retrying it."),
    )
    notice_id = "scheduled_failure:" + sha256(f"{agent.team_id}\x1f{failure.task_id}".encode()).hexdigest()
    await agent.task_manager.operations_runtime.run(partial(
        send_runtime_notice_once, agent, notice_id=notice_id, kind=AccessNoticeKind.WORK_STUCK,
        title=message.title, summary=message.text, person_id=person_id,
        roles=() if person_id else (PERSON_ROLE_ADMIN,), raise_on_error=True,
    ))
    source = StuckTasks(agent, stuck_task_settings(agent))
    # The manager and the executing worker are agent routes, not chats.
    agent_ids = {agent.id, failure.agent_id}
    route = (
        await agent.task_manager.operations_runtime.run(
            source.chat_route, failure.header, failure.meta, person_id, agent_ids,
        ) if person_id else None
    )
    await send_chat_notice(
        agent, route, message, reason=SCHEDULE_RUN_FAILED,
        correlation_id=notice_id, source_task_id=failure.task_id,
    )
    agent._logger.warning(
        "Reported a failed scheduled run. event=scheduled_run_failure_notified "
        "schedule_id=%s task_id=%s person_id=%s", schedule_id, failure.task_id, person_id,
    )
