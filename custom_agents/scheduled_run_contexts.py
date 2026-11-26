"""Give a scheduled run its protocol 1 identity: standing schedule permit first, else ``run_context.sign`` on ``svc.scheduler``.

No model-running process (neither the team lead nor any worker) holds a signing key
(Decision 58). Both the lead and workers follow the same permit-first order:

1. **Standing permit first (``schedule_permits.attach_run_permit``):**
   Read the scheduler-signed protocol 1 permit stored on the schedule row
   (``run_permit_json``), verify its signature, ``scheduler`` role, binding,
   schedule id and person, and wrap it in ``ScheduleRunIdentityV1`` with a fresh
   ``schedule_run_<hex>`` id per run. This keeps ``team_services`` off the hot path
   of normal runs.
2. **``run_context.sign`` on ``svc.scheduler`` when no usable permit exists:**
   When the schedule row has no usable permit yet (or when a heartbeat item needs a
   per-item token), send ``run_context.sign`` to ``svc.scheduler`` signed with this
   agent's protocol 1 session. ``team_services.scheduler`` verifies the live claim
   against ``verified.frame.issuer.agent`` (refusing others as ``claim_not_held``),
   checks schedule state and owner standing/vouching, and returns a fresh
   scheduler-signed protocol 1 root-task ``AuthorityToken``.
3. **Claimless ``scheduled_message`` mailbox replays (C02):**
   When ``from_runtime_mailbox=True`` and ``claim_token is None``, the run uses the
   schedule's standing permit (logged as ``event=scheduled_run_mailbox_replay_permit``).
   Without a usable permit it logs ``event=scheduled_run_mailbox_replay_unpermitted``
   and returns ``False`` rather than starting unsigned.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from access_control.authority_verifier import IdentityRejected
from access_control.people_directory import PersonNow
from custom_agents.agent_requests import AgentUnreachable, ask_scheduler
from custom_agents.schedule_permits import attach_run_permit
from custom_agents.task_context_runtime import context_verifier
from custom_messages.contracts.routing_v1 import GatewayContext, PERSON_GATEWAY_FIELDS
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.wire.authority_bindings import AuthorityTokenRecord, SCHEDULER_ISSUER_ROLE
from id_generation import new_id

logger = logging.getLogger(__name__)

# How long a claiming agent waits for svc.scheduler's run_context.sign answer.
REPLY_TIMEOUT_SECONDS = 20.0

# Why an agent did not start a run, besides the scheduler's refusal outcomes.
NOT_STARTED_NO_CLAIM = "no_claim"
NOT_STARTED_NO_ANSWER = "no_answer"
NOT_STARTED_REQUEST_NOT_SENT = "request_not_sent"
NOT_STARTED_REPLY_UNVERIFIABLE = "reply_unverifiable"
NOT_STARTED_REPLY_MISMATCH = "reply_mismatch"
OUTCOME_SCHEDULE_STOPPED = "schedule_stopped"


@dataclass(frozen=True)
class ScheduledRun:
    """One scheduled run that needs a protocol 1 identity: the claimed schedule and target."""

    schedule: ScheduledMessage  # the claimed schedule as loaded from its row
    claim_token: str | None  # None when replayed without a claim
    scheduled_job_id: str  # the schedule id the token names; a heartbeat item's id for its runs
    owner: PersonNow | None  # run_owner for the run's meta (None for system work)
    heartbeat_item_id: str | None = None
    from_runtime_mailbox: bool = False


def _reply_timeout(agent: Any) -> float:
    if getattr(agent, "_worker_launch", None) is None:
        return REPLY_TIMEOUT_SECONDS
    from custom_agents.agent_instantiation_service import current_worker_limits

    return min(REPLY_TIMEOUT_SECONDS, current_worker_limits(agent).lease_seconds / 3)


def _verify_signed_reply(
    agent: Any,
    reply: dict[str, Any],
    *,
    expected_schedule_id: str,
    expected_person_id: str | None,
) -> AuthorityTokenRecord | str:
    """Parse and cryptographically verify the authority token from ``run_context.sign.reply``."""

    raw_authority = reply.get("authority")
    if not isinstance(raw_authority, dict):
        return NOT_STARTED_REPLY_UNVERIFIABLE
    try:
        token = AuthorityTokenRecord.model_validate(raw_authority)
        verified = context_verifier(agent).verify_root_task(
            token,
            team_id=str(agent.team_id),
            require_issuer=SCHEDULER_ISSUER_ROLE,
            require_active_person=False,
        )
    except (IdentityRejected, ValueError):
        return NOT_STARTED_REPLY_UNVERIFIABLE
    expected_system_work = expected_person_id is None
    if (
        verified.schedule_id != expected_schedule_id
        or verified.person_id != expected_person_id
        or bool(reply.get("system_work")) != expected_system_work
    ):
        return NOT_STARTED_REPLY_MISMATCH
    return token


async def _ask_scheduler_for_run(
    agent: Any,
    *,
    schedule_id: str,
    claim_token: str | None,
    scheduled_job_id: str,
    conversation_id: str | None,
    expected_person_id: str | None,
    heartbeat_item_id: str | None = None,
) -> AuthorityTokenRecord | str:
    """Ask ``svc.scheduler`` for a fresh per-run authority token; returns the token or a refusal reason."""

    if not claim_token:
        return NOT_STARTED_NO_CLAIM
    from custom_transport.protocol1.live import RequestRefused, ServiceUnavailable

    body: dict[str, Any] = {
        "request_id": new_id("run_context"),
        "schedule_id": schedule_id,
        "claim_token": claim_token,
        "scheduled_job_id": scheduled_job_id,
        "conversation_id": str(conversation_id or f"conv_{schedule_id}"),
    }
    if heartbeat_item_id:
        body["heartbeat_item_id"] = heartbeat_item_id
    try:
        reply = await ask_scheduler(agent, "run_context.sign", body, timeout=_reply_timeout(agent))
    except ServiceUnavailable:
        return NOT_STARTED_NO_ANSWER
    except (AgentUnreachable, RequestRefused) as exc:
        logger.warning(
            "Could not ask svc.scheduler to sign a scheduled run. "
            "event=scheduled_run_context_request_failed schedule_id=%s agent_id=%s error=%s",
            schedule_id,
            getattr(agent, "id", None),
            exc,
        )
        return NOT_STARTED_REQUEST_NOT_SENT
    except Exception:
        logger.warning(
            "Could not ask svc.scheduler to sign a scheduled run. "
            "event=scheduled_run_context_request_failed schedule_id=%s agent_id=%s",
            schedule_id,
            getattr(agent, "id", None),
            exc_info=True,
        )
        return NOT_STARTED_REQUEST_NOT_SENT

    outcome = str(reply.get("outcome") or "")
    if outcome != "ok":
        return outcome or NOT_STARTED_REPLY_UNVERIFIABLE
    return _verify_signed_reply(
        agent,
        reply,
        expected_schedule_id=scheduled_job_id,
        expected_person_id=expected_person_id,
    )


async def sign_scheduled_run(agent: Any, meta: Any, run: ScheduledRun) -> bool:
    """Put the run's protocol 1 identity in ``meta``; False means do not start the run (logged).

    Follows permit-first order for both the team lead and workers:
    1. Try the schedule row's standing permit (``attach_run_permit``).
    2. If none is usable, ask ``svc.scheduler`` with ``run_context.sign``.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        gateway_context = GatewayContext()
        meta.gateway_context = gateway_context
    person_id = run.owner.person_id if run.owner is not None else None

    if await asyncio.to_thread(
        attach_run_permit, agent, meta, schedule_id=run.schedule.schedule_id, person_id=person_id
    ):
        if run.from_runtime_mailbox and not run.claim_token:
            logger.info(
                "Replayed a deferred scheduled run using its schedule's standing permit. "
                "event=scheduled_run_mailbox_replay_permit schedule_id=%s scheduled_job_id=%s agent_id=%s",
                run.schedule.schedule_id,
                run.scheduled_job_id,
                getattr(agent, "id", None),
            )
        return True

    if run.from_runtime_mailbox and not run.claim_token:
        logger.warning(
            "Skipped a deferred scheduled run replay because the schedule has no usable permit and no claim. "
            "event=scheduled_run_mailbox_replay_unpermitted schedule_id=%s scheduled_job_id=%s agent_id=%s",
            run.schedule.schedule_id,
            run.scheduled_job_id,
            getattr(agent, "id", None),
        )
        return False

    signed = await _ask_scheduler_for_run(
        agent,
        schedule_id=run.schedule.schedule_id,
        claim_token=run.claim_token,
        scheduled_job_id=run.scheduled_job_id,
        conversation_id=getattr(meta, "conversation_id", None),
        expected_person_id=person_id,
        heartbeat_item_id=run.heartbeat_item_id,
    )
    if signed == OUTCOME_SCHEDULE_STOPPED:
        logger.info(
            "A claimed schedule was paused, turned off or ended before its run started; the run is skipped "
            "and the claim released. event=scheduled_run_skipped_schedule_stopped agent_id=%s schedule_id=%s "
            "scheduled_job_id=%s reason=scheduler_refused",
            getattr(agent, "id", None),
            run.schedule.schedule_id,
            run.scheduled_job_id,
        )
        return False
    if isinstance(signed, str):
        logger.warning(
            "A scheduled run was not started because svc.scheduler did not sign its authority token; "
            "the claim is released so the schedule is retried later. event=scheduled_run_not_started_unsigned "
            "schedule_id=%s scheduled_job_id=%s agent_id=%s reason=%s",
            run.schedule.schedule_id,
            run.scheduled_job_id,
            getattr(agent, "id", None),
            signed,
        )
        return False
    gateway_context.schedule_run = None
    gateway_context.task_context = signed
    return True


async def sign_system_run_async(
    agent: Any,
    meta: Any,
    *,
    schedule_id: str,
    claim_token: str | None = None,
) -> bool:
    """Give a system-work run (no person) its scheduler-signed identity: standing permit first, else ``svc.scheduler``.

    Clears all person/session fields on ``meta.gateway_context`` and returns False without
    dispatching if neither a standing permit nor a ``run_context.sign`` token can be obtained.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None:
        gateway_context = GatewayContext()
        meta.gateway_context = gateway_context
    for name in PERSON_GATEWAY_FIELDS:
        setattr(gateway_context, name, None)
    gateway_context.task_context = None
    gateway_context.schedule_run = None

    if await asyncio.to_thread(attach_run_permit, agent, meta, schedule_id=schedule_id, person_id=None):
        return True
    signed = await _ask_scheduler_for_run(
        agent,
        schedule_id=schedule_id,
        claim_token=claim_token,
        scheduled_job_id=schedule_id,
        conversation_id=getattr(meta, "conversation_id", None),
        expected_person_id=None,
    )
    if isinstance(signed, str):
        logger.warning(
            "A scheduled system-work run could not be signed; it waits for the next try. "
            "event=system_work_run_unsigned agent_id=%s schedule_id=%s reason=%s",
            getattr(agent, "id", None),
            schedule_id,
            signed,
        )
        return False
    gateway_context.schedule_run = None
    gateway_context.task_context = signed
    return True


__all__ = [
    "NOT_STARTED_NO_ANSWER",
    "NOT_STARTED_NO_CLAIM",
    "NOT_STARTED_REPLY_MISMATCH",
    "NOT_STARTED_REPLY_UNVERIFIABLE",
    "NOT_STARTED_REQUEST_NOT_SENT",
    "OUTCOME_SCHEDULE_STOPPED",
    "REPLY_TIMEOUT_SECONDS",
    "ScheduledRun",
    "sign_scheduled_run",
    "sign_system_run_async",
]
