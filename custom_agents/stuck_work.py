"""Tell people when their schedules or tasks are stuck, and when they work again.

Why trusted code: a stuck schedule used to produce only a self-addressed review
task for TeamLead's model. That task carried no signed person and no chat
route, so the model could neither inspect the schedule nor tell anyone, and its
answer was discarded (2026-10-03: three schedules sat blocked for a day while
their owner was never told). Deciding that something is stuck and who to tell
follows fixed rules, so the team lead's own scheduler loop does it here on
every pass, with no model turn.

How it works:

- A source (``custom_agents.stuck_schedules``, ``custom_agents.stuck_tasks``)
  lists what is stuck now, as ``StuckFinding``\\ s: what, since when, why (a
  reason code with a plain-language explanation), whom it affects and the
  chat route to reach them.
- ``report_stuck_work`` keeps one open incident per finding
  (``storage.stuck_work_repository``). Once an incident has lasted
  ``notice_after``, the affected person gets one Inbox notice and one chat
  message; administrators get an Inbox notice when only they can fix it or
  when the work is the team's. When a finding disappears, everyone who was
  told gets a "working again" notice and message, and the incident closes.
- Inbox notices are stored once per notice id (``send_runtime_notice_once``),
  in both authorization modes. Chat messages are ``UserNotification``\\ s sent
  through the gateway like any proactive message; this is the one place
  notices are pushed to chat, by the owner's decision of 2026-10-04 (notices
  are otherwise silent, decision D3).

Every step logs an ``event=stuck_work_*`` line with the subject and incident ids.
A failure to tell someone is logged and retried on the next pass; it never
stops the scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Callable, Protocol

from access_control.people import PERSON_ROLE_ADMIN, effective_roles
from custom_agents.access_runtime import send_runtime_notice_once
from custom_agents.task_context_runtime import people_directory
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.enums import RequesterType
from storage.stuck_work_repository import (
    StuckSubjectKind,
    StuckWorkIncidentV1,
    StuckWorkRepository,
)

logger = logging.getLogger(__name__)

# Notification reasons the gateway sees on the chat messages.
STUCK_NOTICE_REASON = "work_stuck"
RECOVERED_NOTICE_REASON = "work_recovered"
MISSED_RUN_NOTICE_REASON = "schedule_run_missed"


@dataclass(frozen=True)
class ChatRoute:
    """Where a person's chat messages go: the gateway route and their chat."""

    routing_key: str
    gateway_context: GatewayContext


@dataclass(frozen=True)
class StuckMessage:
    """One notice: a short title and the full text (what is wrong, what to do)."""

    title: str
    text: str


@dataclass(frozen=True)
class StuckFinding:
    """Something stuck right now, as a source sees it."""

    subject_kind: StuckSubjectKind
    subject_id: str
    incident_key: str
    reason: str
    since: datetime
    person_id: str | None
    tell_administrators: bool
    message: StuckMessage
    chat_route: ChatRoute | None
    # What administrators read instead, when ``message`` quotes the person's
    # private work (a task's request text); None sends them ``message``.
    administrator_message: StuckMessage | None = None


@dataclass(frozen=True)
class Recovery:
    """What people hear when an incident closes, and where."""

    message: StuckMessage
    chat_route: ChatRoute | None
    administrator_message: StuckMessage | None = None


class StuckWorkSource(Protocol):
    """A kind of work that can get stuck (schedules, tasks)."""

    subject_kind: StuckSubjectKind

    def findings(self, now: datetime) -> list[StuckFinding]:
        """What is stuck now. Runs in the storage lane, so it may read stores."""

    def recovery(self, incident: StuckWorkIncidentV1) -> Recovery:
        """The "working again" notice for a closed incident, and where to send it."""


def _notice_id(incident: StuckWorkIncidentV1, suffix: str) -> str:
    return f"{incident.incident_id}:{suffix}"


def _is_administrator(agent: Any, person_id: str) -> bool:
    """Whether the gateway's copy of the team's people lists the person as an administrator.

    When it cannot be read the person keeps their own copy (a duplicate
    rather than a missed report), and the warning says why.
    """

    try:
        person = people_directory(agent).person(person_id)
    except Exception as exc:
        logger.warning(
            "Could not read the team's people, so an administrator may get a notice twice. "
            "event=stuck_work_people_unreadable person_id=%s error=%s",
            person_id,
            exc,
        )
        return False
    return PERSON_ROLE_ADMIN in effective_roles(person.roles)


def _store_notices(
    agent: Any,
    incident: StuckWorkIncidentV1,
    message: StuckMessage,
    *,
    suffix: str,
    administrator_message: StuckMessage | None = None,
) -> bool:
    """Inbox notices for the person and, when they need to act, administrators.

    Each person gets one notice per report. A schedule's owner is often an
    administrator too, and separate owner and administrator copies piled up
    twice in their Inbox. So when both read the same words, one notice goes
    to the owner and administrators together; when administrators read other
    words (stuck tasks keep a person's request private), an owner who is an
    administrator gets only the administrators' notice.
    """

    tell_administrators = incident.tell_administrators or not incident.person_id
    if incident.person_id and tell_administrators and administrator_message in (None, message):
        return send_runtime_notice_once(
            agent,
            notice_id=_notice_id(incident, f"{suffix}:person_administrators"),
            kind=AccessNoticeKind.WORK_STUCK,
            title=message.title,
            summary=message.text,
            person_id=incident.person_id,
            roles=(PERSON_ROLE_ADMIN,),
        )
    stored = False
    if incident.person_id and not (tell_administrators and _is_administrator(agent, incident.person_id)):
        stored |= send_runtime_notice_once(
            agent,
            notice_id=_notice_id(incident, f"{suffix}:person"),
            kind=AccessNoticeKind.WORK_STUCK,
            title=message.title,
            summary=message.text,
            person_id=incident.person_id,
        )
    if tell_administrators:
        shown = administrator_message or message
        stored |= send_runtime_notice_once(
            agent,
            notice_id=_notice_id(incident, f"{suffix}:administrators"),
            kind=AccessNoticeKind.WORK_STUCK,
            title=shown.title,
            summary=shown.text,
            roles=(PERSON_ROLE_ADMIN,),
        )
    return stored


async def send_chat_notice(
    agent: Any,
    route: ChatRoute | None,
    message: StuckMessage,
    *,
    reason: str,
    correlation_id: str,
    source_task_id: str | None,
) -> bool:
    """Send one notice to a person's chat through the gateway; False without a route."""
    if route is None:
        return False
    # A standalone notice: never a scheduled run's result, and never the answer
    # to the chat's current request.
    context = route.gateway_context.model_copy(
        update={
            "scheduled_job_id": None,
            "schedule_owner_agent_id": None,
            "heartbeat_source_id": None,
            "delivery_mode": "autonomous",
        }
    )
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            text=f"{message.title}\n\n{message.text}",
            urgency="normal",
            reason=reason,
            source_task_id=source_task_id,
            source_agent_id=agent.id,
            source_agent_name=getattr(agent, "name", None),
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            msg_correlation_id=correlation_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=context,
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=route.routing_key,
            response_routing_key=agent.id,
            idempotency_key=correlation_id,
        ),
    )
    await agent.team_comms.send_message(message=envelope, routing_key=route.routing_key)
    return True


async def _tell_stuck(agent: Any, finding: StuckFinding, incident: StuckWorkIncidentV1) -> None:
    await agent.task_manager.operations_runtime.run(
        partial(
            _store_notices,
            agent,
            incident,
            finding.message,
            suffix="stuck",
            administrator_message=finding.administrator_message,
        )
    )
    sent = await send_chat_notice(
        agent,
        finding.chat_route,
        finding.message,
        reason=STUCK_NOTICE_REASON,
        correlation_id=_notice_id(incident, "stuck"),
        source_task_id=finding.subject_id if finding.subject_kind == "task" else None,
    )
    agent._logger.warning(
        "Told people that work is stuck. event=stuck_work_notified kind=%s subject_id=%s "
        "incident_id=%s reason=%s person_id=%s administrators=%s chat=%s since=%s",
        incident.subject_kind,
        incident.subject_id,
        incident.incident_id,
        incident.reason,
        incident.person_id,
        incident.tell_administrators,
        sent,
        incident.first_seen_at_utc.isoformat(),
    )


async def _tell_recovered(agent: Any, source: StuckWorkSource, incident: StuckWorkIncidentV1) -> None:
    recovery = await agent.task_manager.operations_runtime.run(
        partial(source.recovery, incident)
    )
    await agent.task_manager.operations_runtime.run(
        partial(
            _store_notices,
            agent,
            incident,
            recovery.message,
            suffix="recovered",
            administrator_message=recovery.administrator_message,
        )
    )
    sent = await send_chat_notice(
        agent,
        recovery.chat_route,
        recovery.message,
        reason=RECOVERED_NOTICE_REASON,
        correlation_id=_notice_id(incident, "recovered"),
        source_task_id=incident.subject_id if incident.subject_kind == "task" else None,
    )
    agent._logger.info(
        "Told people that stuck work is working again. event=stuck_work_recovered kind=%s "
        "subject_id=%s incident_id=%s person_id=%s chat=%s",
        incident.subject_kind,
        incident.subject_id,
        incident.incident_id,
        incident.person_id,
        sent,
    )


def _incident(agent: Any, finding: StuckFinding) -> StuckWorkIncidentV1:
    return StuckWorkIncidentV1(
        team_id=agent.team_id,
        subject_kind=finding.subject_kind,
        subject_id=finding.subject_id,
        incident_key=finding.incident_key,
        reason=finding.reason,
        person_id=finding.person_id,
        tell_administrators=finding.tell_administrators,
        first_seen_at_utc=finding.since,
    )


async def report_stuck_work(
    agent: Any,
    source: StuckWorkSource,
    *,
    notice_after: timedelta,
    now: datetime,
    repository_factory: Callable[[Any], StuckWorkRepository] | None = None,
) -> None:
    """Record what ``source`` finds stuck, tell people about it, and about recovery."""

    runtime = agent.task_manager.operations_runtime
    repository = (repository_factory or StuckWorkRepository)(agent.task_manager.storage_settings)
    findings = await runtime.run(partial(source.findings, now))
    open_incidents = {
        incident.incident_id: incident
        for incident in await runtime.run(
            repository.open_incidents, agent.team_id, source.subject_kind
        )
    }
    current: set[str] = set()
    for finding in findings:
        try:
            incident = await runtime.run(repository.record_seen, _incident(agent, finding))
            current.add(incident.incident_id)
            if incident.notified_at_utc is not None or now - incident.first_seen_at_utc < notice_after:
                continue
            # Tell with the current reason, then remember that it was told.
            incident = incident.model_copy(
                update={
                    "reason": finding.reason,
                    "person_id": finding.person_id,
                    "tell_administrators": finding.tell_administrators,
                }
            )
            await _tell_stuck(agent, finding, incident)
            await runtime.run(repository.mark_notified, incident, now)
        except Exception:
            agent._logger.exception(
                "Could not report stuck work; retrying on the next pass. "
                "event=stuck_work_report_failed kind=%s subject_id=%s",
                finding.subject_kind,
                finding.subject_id,
            )
    for incident_id, incident in open_incidents.items():
        if incident_id in current:
            continue
        try:
            if incident.notified_at_utc is not None:
                await _tell_recovered(agent, source, incident)
            await runtime.run(repository.close, incident)
        except Exception:
            agent._logger.exception(
                "Could not report recovered work; retrying on the next pass. "
                "event=stuck_work_recovery_failed kind=%s subject_id=%s incident_id=%s",
                incident.subject_kind,
                incident.subject_id,
                incident_id,
            )
