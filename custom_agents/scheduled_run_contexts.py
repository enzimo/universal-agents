"""Give a scheduled run its identity: signed here, from its schedule's permit, or by the manager.

Every scheduled run must say whom it works for, or the access checks refuse
every tool call of the run: a task context signed by the scheduler
(``access_control.identity``, issuer ``scheduler``) for the schedule's owner,
or the schedule's manager-signed permit (``custom_agents.schedule_permits``).
Only the manager process holds the scheduler's signing key
(``docker/secret_env_vars.yaml`` gives ``ACCESS_SCHEDULER_SIGNING_KEY`` to the
manager alone), so the key never reaches a process that runs agent-directed
work on its own.

Scheduled workers claim their own due schedules from the shared database.
Where the claiming agent runs decides what the run carries:

- An agent inside the manager process (the team lead, and workers the
  manager starts on demand as threads) signs locally
  (``task_context_runtime.sign_scheduled_context``); nothing is sent.
- A worker running as its own process (started from team composition) has no
  key. Normally it gives the run the permit stored on the schedule row
  (``schedule_permits.attach_run_permit``) and sends nothing.
- Only when the row has no usable permit (the manager has not looked at a new
  schedule yet, or issued none) does the worker send the manager a
  ``ScheduledRunContextRequestV1`` with its claim on the Core NATS subject
  ``<prefix>.lead.run_context`` and wait, at most ``REPLY_TIMEOUT_SECONDS``,
  for the ``ScheduledRunContextReplyV1`` on its own subject
  ``<prefix>.agent.<id>.run_context``.

The manager signs only for a claim the asking agent holds: the schedule row's
``claimed_by_agent_id`` and ``claim_token`` must match the request, the claim
must not have expired, and the request's sender must be the agent named in
it. The owner comes only from the schedule row (``run_owner``), never from the
request, and the usual rules apply (``task_context_runtime``): current roles
only when the owner is vouched for, and a disabled or removed owner's
schedule does not run. The worker checks the answer's signature and that it
names the expected schedule and person before it uses it.

When the manager refuses, does not answer in time, or the request cannot be
sent, the worker does not start the run. It says so in a warning log, and
the caller releases the claim so the schedule is retried later
(``scheduled_runtime._fire_claimed_schedule``). A run the runtime replays
later without a claim (a deferred fire) cannot be vouched for by the manager,
so a worker without the key does not start it either.

Both subjects are Core NATS, outside every JetStream stream: a request that
nobody answers now is not worth keeping, because the worker has already
given up on it and the schedule will be claimed again.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

from access_control.identity import ISSUER_SCHEDULER, ContextSigner, IdentityRejected
from access_control.people_directory import PersonNow
from custom_agents.heartbeat_store import HeartbeatStore, is_heartbeat_launcher
from custom_agents.schedule_management import is_system_managed_schedule
from custom_agents.schedule_permits import attach_run_permit
from custom_agents.task_context_runtime import (
    context_verifier,
    inactive_owner,
    issue_scheduled_context,
    run_owner,
    scheduler_contexts_verified,
    scheduler_signer,
    sign_scheduled_context,
)
from custom_messages.contracts.access_v2 import SignedTaskContextV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.scheduled_run_context_v1 import (
    ScheduledRunContextRefusal,
    ScheduledRunContextReplyV1,
    ScheduledRunContextRequestV1,
)
from custom_transport.base import InboundDelivery, SubscriptionDelivery, TransportPublishOptions
from id_generation import new_id

logger = logging.getLogger(__name__)

# How long a worker waits for the manager's answer. Well below the schedule
# claim's lifetime (schedule_store.DEFAULT_SCHEDULE_CLAIM_TTL_SECONDS, 120
# seconds), so the claim is still held when the run starts; a worker with a
# launch lease waits at most a third of the lease, like its idle waits.
REPLY_TIMEOUT_SECONDS = 20.0

# Why a worker did not start a run, besides the manager's refusal reasons.
NOT_STARTED_NO_CLAIM = "no_claim"
NOT_STARTED_NO_ANSWER = "no_answer"
NOT_STARTED_REQUEST_NOT_SENT = "request_not_sent"
NOT_STARTED_REPLY_UNVERIFIABLE = "reply_unverifiable"
NOT_STARTED_REPLY_MISMATCH = "reply_mismatch"

# Requests this process sent and still waits for: request id -> (asking agent id, answer).
_WAITING: dict[str, tuple[str, asyncio.Future[ScheduledRunContextReplyV1]]] = {}


@dataclass(frozen=True)
class ScheduledRun:
    """One run that needs a signed context: the claimed schedule and what the context names."""

    schedule: ScheduledMessage  # the claimed schedule as loaded from its row
    claim_token: str | None  # None when the run is replayed without a claim
    scheduled_job_id: str  # the schedule id the context names; a heartbeat item's id for its runs
    owner: PersonNow | None  # run_owner for the run's meta
    heartbeat_item_id: str | None = None


# -- the claiming agent ---------------------------------------------------------------


def asks_manager(agent: Any) -> bool:
    """Whether this agent must ask the manager to sign its scheduled runs.

    True for a worker whose process trusts a scheduler key but holds none.
    The team lead is the manager and always signs itself; a process that
    trusts no scheduler key (tests, a team without task-context keys) keeps
    the local path, which logs that it has no key.
    """

    return (
        not getattr(agent, "is_team_lead", False)
        and scheduler_signer() is None
        and scheduler_contexts_verified()
    )


async def sign_scheduled_run(agent: Any, meta: Any, run: ScheduledRun) -> bool:
    """Put the run's identity in ``meta``; False means do not start the run (logged).

    Signs locally where the key is (module docstring), else uses the
    schedule's permit, else asks the manager.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    if gateway_context is None or not asks_manager(agent):
        sign_scheduled_context(
            meta,
            team_id=agent.team_id,
            schedule_id=run.scheduled_job_id,
            owner=run.owner,
            system_managed=is_system_managed_schedule(run.schedule),
        )
        return True
    person_id = run.owner.person_id if run.owner is not None else None
    if await asyncio.to_thread(
        attach_run_permit, agent, meta, schedule_id=run.schedule.schedule_id, person_id=person_id
    ):
        return True
    signed = await _manager_signed_context(agent, meta, run)
    if isinstance(signed, str):
        logger.warning(
            "A scheduled run was not started because the manager did not sign its task context; the claim is "
            "released so the schedule is retried later. event=scheduled_run_not_started_unsigned "
            "schedule_id=%s scheduled_job_id=%s agent_id=%s reason=%s",
            run.schedule.schedule_id,
            run.scheduled_job_id,
            agent.id,
            signed,
        )
        return False
    gateway_context.schedule_run = None
    gateway_context.task_context = signed
    return True


async def _manager_signed_context(agent: Any, meta: Any, run: ScheduledRun) -> SignedTaskContextV1 | str:
    """The manager's verified signed context for the run, or why there is none."""

    if not run.claim_token:
        return NOT_STARTED_NO_CLAIM
    request = ScheduledRunContextRequestV1(
        request_id=new_id("run_context"),
        team_id=agent.team_id,
        agent_id=agent.id,
        schedule_id=run.schedule.schedule_id,
        claim_token=run.claim_token,
        scheduled_job_id=run.scheduled_job_id,
        conversation_id=getattr(meta, "conversation_id", None),
        heartbeat_item_id=run.heartbeat_item_id,
    )
    reply = await _ask_manager(agent, request)
    if isinstance(reply, str):
        return reply
    if reply.refusal is not None:
        return reply.refusal.value
    assert reply.task_context is not None  # the reply contract carries one or the other
    return _reply_problem(agent, reply.task_context, run) or reply.task_context


def _reply_timeout(agent: Any) -> float:
    if getattr(agent, "_worker_launch", None) is None:
        return REPLY_TIMEOUT_SECONDS
    from custom_agents.agent_instantiation_service import current_worker_limits

    return min(REPLY_TIMEOUT_SECONDS, current_worker_limits(agent).lease_seconds / 3)


async def _ask_manager(agent: Any, request: ScheduledRunContextRequestV1) -> ScheduledRunContextReplyV1 | str:
    """Send the request and wait for the matching answer; a reason string when there is none."""

    answer: asyncio.Future[ScheduledRunContextReplyV1] = asyncio.get_running_loop().create_future()
    _WAITING[request.request_id] = (agent.id, answer)
    try:
        try:
            adapter = agent._ensure_nats_transport_adapter()
            await adapter.publish_subject(
                subject=adapter.mapper.run_context_request_subject(),
                envelope=_envelope(request, sender=agent.id, to=agent.team_id),
                options=TransportPublishOptions(durable=False),
            )
        except Exception:
            logger.warning(
                "Could not send the manager a request to sign a scheduled run. "
                "event=scheduled_run_context_request_failed schedule_id=%s agent_id=%s request_id=%s",
                request.schedule_id,
                agent.id,
                request.request_id,
                exc_info=True,
            )
            return NOT_STARTED_REQUEST_NOT_SENT
        try:
            return await asyncio.wait_for(answer, timeout=_reply_timeout(agent))
        except asyncio.TimeoutError:
            return NOT_STARTED_NO_ANSWER
    finally:
        _WAITING.pop(request.request_id, None)


def _reply_problem(agent: Any, signed: SignedTaskContextV1, run: ScheduledRun) -> str | None:
    """Why the answer's context cannot be used for this run, or None."""

    try:
        context = context_verifier().verify(signed, team_id=agent.team_id)
    except (IdentityRejected, ValueError):
        return NOT_STARTED_REPLY_UNVERIFIABLE
    expected_person = run.owner.person_id if run.owner is not None else None
    if (
        context.issuer != ISSUER_SCHEDULER
        or context.schedule_id != run.scheduled_job_id
        or context.person_id != expected_person
    ):
        return NOT_STARTED_REPLY_MISMATCH
    return None


async def _on_reply(agent: Any, delivery: InboundDelivery) -> None:
    """Hand the manager's answer to the waiting request; late or unknown answers are dropped (logged)."""

    reply = delivery.envelope.msg_content
    if not isinstance(reply, ScheduledRunContextReplyV1):
        logger.warning(
            "Ignored a message that is not a scheduled run context answer. "
            "event=scheduled_run_context_reply_unexpected agent_id=%s content_type=%s",
            agent.id,
            type(reply).__name__,
        )
        return
    waiting = _WAITING.get(reply.request_id)
    if waiting is None or waiting[0] != agent.id or reply.agent_id != agent.id:
        logger.info(
            "Ignored an answer about a scheduled run context that nobody here waits for (it may have come "
            "after the wait ended). event=scheduled_run_context_reply_unmatched agent_id=%s request_id=%s "
            "schedule_id=%s",
            agent.id,
            reply.request_id,
            reply.schedule_id,
        )
        return
    if not waiting[1].done():
        waiting[1].set_result(reply)


# -- the manager ----------------------------------------------------------------------


async def answer_run_context_request(
    agent: Any,
    request: ScheduledRunContextRequestV1,
    *,
    sender: str | None,
    signer: ContextSigner | None = None,
) -> ScheduledRunContextReplyV1:
    """The manager's answer to one request (module docstring); every refusal is logged.

    ``signer`` defaults to this process's scheduler signer.
    """

    signer = signer or scheduler_signer()
    try:
        outcome = await asyncio.to_thread(_signed_or_refusal, agent, request, sender, signer)
    except Exception:
        logger.exception(
            "Could not check a request to sign a scheduled run. event=scheduled_run_context_check_failed "
            "schedule_id=%s agent_id=%s request_id=%s",
            request.schedule_id,
            request.agent_id,
            request.request_id,
        )
        outcome = ScheduledRunContextRefusal.CHECK_FAILED
    _log_outcome(request, sender, outcome)
    signed = outcome if isinstance(outcome, SignedTaskContextV1) else None
    return ScheduledRunContextReplyV1(
        request_id=request.request_id,
        team_id=request.team_id,
        agent_id=request.agent_id,
        schedule_id=request.schedule_id,
        task_context=signed,
        refusal=None if signed is not None else outcome,
    )


def _log_outcome(
    request: ScheduledRunContextRequestV1,
    sender: str | None,
    outcome: SignedTaskContextV1 | ScheduledRunContextRefusal,
) -> None:
    if isinstance(outcome, SignedTaskContextV1):
        logger.info(
            "Signed a scheduled run's task context for the agent that claimed it. "
            "event=scheduled_run_context_signed schedule_id=%s scheduled_job_id=%s agent_id=%s person_id=%s roles=%s",
            request.schedule_id,
            outcome.context.schedule_id,
            request.agent_id,
            outcome.context.person_id,
            ",".join(outcome.context.person_roles),
        )
        return
    logger.warning(
        "Refused to sign a scheduled run's task context. event=scheduled_run_context_refused "
        "schedule_id=%s scheduled_job_id=%s agent_id=%s sender=%s request_id=%s reason=%s",
        request.schedule_id,
        request.scheduled_job_id,
        request.agent_id,
        sender,
        request.request_id,
        outcome.value,
    )


def _request_refusal(
    agent: Any, request: ScheduledRunContextRequestV1, sender: str | None, signer: ContextSigner | None
) -> ScheduledRunContextRefusal | None:
    if signer is None:
        return ScheduledRunContextRefusal.SIGNER_MISSING
    if request.team_id != agent.team_id:
        return ScheduledRunContextRefusal.WRONG_TEAM
    if not sender or sender != request.agent_id:
        return ScheduledRunContextRefusal.SENDER_MISMATCH
    return None


def _signed_or_refusal(
    agent: Any, request: ScheduledRunContextRequestV1, sender: str | None, signer: ContextSigner | None
) -> SignedTaskContextV1 | ScheduledRunContextRefusal:
    """Check the request against the schedule row and sign for its owner (module docstring)."""

    refusal = _request_refusal(agent, request, sender, signer)
    if refusal is not None:
        return refusal
    assert signer is not None  # _request_refusal refuses without one
    store = agent._schedule_store
    schedule = store.get_scheduled_message(request.schedule_id)
    if schedule is None:
        return ScheduledRunContextRefusal.SCHEDULE_NOT_FOUND
    if not store.holds_claim(request.schedule_id, claim_token=request.claim_token, claimer_agent_id=request.agent_id):
        return ScheduledRunContextRefusal.CLAIM_NOT_HELD
    target = _run_target(agent, schedule, request)
    if isinstance(target, ScheduledRunContextRefusal):
        return target
    scheduled_job_id, conversation_id = target
    # The owner comes from the authoritative schedule row (applied on load).
    owner = run_owner(agent, schedule.payload_meta)
    if inactive_owner(owner, schedule_id=scheduled_job_id):
        return ScheduledRunContextRefusal.OWNER_INACTIVE
    return issue_scheduled_context(
        signer,
        getattr(schedule.payload_meta, "gateway_context", None),
        team_id=agent.team_id,
        schedule_id=scheduled_job_id,
        conversation_id=conversation_id,
        owner=owner,
        system_managed=is_system_managed_schedule(schedule),
    )


def _run_target(
    agent: Any, schedule: ScheduledMessage, request: ScheduledRunContextRequestV1
) -> tuple[str, str | None] | ScheduledRunContextRefusal:
    """The schedule id and conversation the context names, as the schedule row defines them."""

    if request.heartbeat_item_id:
        return _heartbeat_target(agent, schedule, request)
    gateway_context = getattr(schedule.payload_meta, "gateway_context", None)
    scheduled_job_id = getattr(gateway_context, "scheduled_job_id", None) or schedule.schedule_id
    if request.scheduled_job_id != scheduled_job_id:
        return ScheduledRunContextRefusal.SCHEDULED_JOB_MISMATCH
    # The run's conversation is the one the worker opened for it; the context
    # records it for reference and grants nothing by it.
    return scheduled_job_id, request.conversation_id


def _heartbeat_target(
    agent: Any, schedule: ScheduledMessage, request: ScheduledRunContextRequestV1
) -> tuple[str, str | None] | ScheduledRunContextRefusal:
    """One enabled item of the claimed heartbeat schedule's source, and its conversation."""

    if not is_heartbeat_launcher(schedule):
        return ScheduledRunContextRefusal.HEARTBEAT_ITEM_NOT_FOUND
    # is_heartbeat_launcher checked that the schedule names a source.
    source_id = getattr(getattr(schedule.payload_meta, "gateway_context", None), "heartbeat_source_id", None)
    store = HeartbeatStore(
        db_path=agent.task_manager._db_path,
        logger_=logger,
        storage_settings=agent.task_manager.storage_settings,
    )
    item = store.get_item(str(request.heartbeat_item_id))
    if item is None or not item.enabled or item.source_id != source_id:
        return ScheduledRunContextRefusal.HEARTBEAT_ITEM_NOT_FOUND
    if request.scheduled_job_id != item.heartbeat_item_id:
        return ScheduledRunContextRefusal.SCHEDULED_JOB_MISMATCH
    return item.heartbeat_item_id, item.conversation_id


async def _on_request(agent: Any, delivery: InboundDelivery) -> None:
    """Answer one request on the asking agent's own subject; failures are logged, never raised."""

    envelope = delivery.envelope
    request = envelope.msg_content
    if not isinstance(request, ScheduledRunContextRequestV1):
        logger.warning(
            "Ignored a message that is not a scheduled run context request. "
            "event=scheduled_run_context_request_unexpected agent_id=%s content_type=%s",
            agent.id,
            type(request).__name__,
        )
        return
    reply = await answer_run_context_request(agent, request, sender=envelope.msg_header.from_agent_id)
    try:
        adapter = agent._ensure_nats_transport_adapter()
        await adapter.publish_subject(
            subject=adapter.mapper.run_context_reply_subject(request.agent_id),
            envelope=_envelope(reply, sender=agent.id, to=request.agent_id),
            options=TransportPublishOptions(durable=False),
        )
    except Exception:
        logger.warning(
            "Could not send the answer about a scheduled run context; the worker will retry the schedule. "
            "event=scheduled_run_context_reply_failed schedule_id=%s agent_id=%s request_id=%s",
            request.schedule_id,
            request.agent_id,
            request.request_id,
            exc_info=True,
        )


# -- transport ------------------------------------------------------------------------


def _envelope(content: Any, *, sender: str, to: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=content,
        msg_header=MessageHeader(from_agent_id=sender, to_routing_path=to, agent_signature=""),
        msg_meta=MessageMeta(is_response_requested=False),
    )


async def subscribe_run_context_routes(agent: Any, adapter: Any, mapper: Any) -> None:
    """The team lead answers requests; a worker that asks listens for its answers.

    ``mapper`` is the agent's subject mapper (``NatsSubjectMapper``).
    """

    if getattr(agent, "is_team_lead", False):
        await adapter.subscribe_subject(
            name=f"{agent.id}:run_context_requests",
            subject=mapper.run_context_request_subject(),
            handler=partial(_on_request, agent),
            # One lead answers even while a replacement lead starts up.
            core_queue_group=mapper.durable_name("run_context_signers"),
            logical_route=agent.team_id,
            delivery=SubscriptionDelivery.CORE,
        )
        logger.info("Ready to sign scheduled runs for workers. event=scheduled_run_context_route_ready agent_id=%s", agent.id)
    elif asks_manager(agent):
        await adapter.subscribe_subject(
            name=f"{agent.id}:run_context_replies",
            subject=mapper.run_context_reply_subject(agent.id),
            handler=partial(_on_reply, agent),
            logical_route=agent.id,
            delivery=SubscriptionDelivery.CORE,
        )
        logger.info(
            "This worker has no scheduler key and asks the manager to sign its scheduled runs. "
            "event=scheduled_run_context_replies_ready agent_id=%s",
            agent.id,
        )


__all__ = [
    "REPLY_TIMEOUT_SECONDS",
    "ScheduledRun",
    "answer_run_context_request",
    "asks_manager",
    "sign_scheduled_run",
    "subscribe_run_context_routes",
]
