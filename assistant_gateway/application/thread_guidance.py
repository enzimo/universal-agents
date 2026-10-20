"""Guidance for a thread's running request: hold it, send it, settle it.

A thread (one session conversation) runs one request at a time, and a later
message in a busy thread is guidance for that request. Which request runs is
durable state (``RequestRecord.run_state``), so guidance reaches its request
after the person's reply wait timed out, after a gateway restart and while
the request waits for a credential form.

Each guidance message is saved before it is sent: its request row and a held
copy (``HeldGuidanceRecord``) are written with the person's message. Sending
names the request's own task and correlation id, never an earlier task of the
conversation, and uses the guidance's correlation id as the idempotency key,
so sending it again is a duplicate the team lead ignores. The lead's answer
settles the held copy:

- accepted: the lead saved it with the request's task, which applies it at
  its next safe point (``custom_agents.task_steering_runtime``);
- the lead does not hold the request's task yet (it waits behind other work),
  or no answer came in time, or no lead is reachable: the copy stays held and
  is sent again when the lead reports its state (it does when a turn starts
  or ends and on every presence heartbeat), when a lead becomes available,
  and at least every ``retry_seconds``;
- the request ended first, or the lead refused it: the person is told, and
  the message is not used. Guidance never becomes a new request: the person
  starts new work themselves (New Chat, ``/new``, or a message after the
  request finished).

A late answer, after the sending attempt stopped waiting, settles the held
copy the same way instead of appearing in the chat.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.local_responses import GatewayLocalResponseFactory
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayClock, GatewayEventOutput
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.domain.request_state import (
    GUIDANCE_STATUS_REQUEST_FINISHED,
    REQUEST_KIND_TASK_STEERING,
    REQUEST_STATUS_WAITING_FOR_STEERING,
    RUN_STATE_RUNNING,
    HeldGuidanceRecord,
    RequestRecord,
)
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import (
    RequesterType,
    RoutingType,
    TaskSteeringStatus,
    TaskSteeringStatusCode,
    TaskSteeringType,
)

GUIDANCE_MESSAGE_TYPE = "task_steering"

NOT_STARTED_TEXT = (
    "Your request in this thread has not started yet. "
    "This message will be passed to it when it starts."
)
UNCONFIRMED_TEXT = (
    "Your request in this thread is still running. This message will be passed to it."
)
NO_LEAD_TEXT = (
    "The team lead is not available right now. "
    "This message will be passed to your running request when it is back."
)
REQUEST_FINISHED_TEXT = (
    "Your request in this thread finished before this message reached it, "
    "so the message was not used. Send it again to start a new request."
)


class GuidanceDelivery(StrEnum):
    """How one attempt to send held guidance ended."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    REQUEST_FINISHED = "request_finished"
    NOT_STARTED = "not_started"
    UNCONFIRMED = "unconfirmed"
    NO_LEAD = "no_lead"


# Outcomes that keep the copy held for another attempt, with the reply then.
_HELD_REPLIES = {
    GuidanceDelivery.NOT_STARTED: NOT_STARTED_TEXT,
    GuidanceDelivery.UNCONFIRMED: UNCONFIRMED_TEXT,
    GuidanceDelivery.NO_LEAD: NO_LEAD_TEXT,
}


@dataclass(frozen=True)
class GatewayThreadGuidanceSettings:
    """Stable routing and timing for guidance delivery."""

    response_route: str
    # How long one attempt waits for the lead's answer before it keeps the
    # message held; the answer may still arrive later.
    status_timeout_seconds: float = 5.0
    retry_seconds: float = 15.0


@dataclass(frozen=True)
class _Attempt:
    outcome: GuidanceDelivery
    status_envelope: MessageEnvelope | None = None
    target_task_id: str | None = None


class GatewayThreadGuidance:
    """Hold, send and settle guidance for each thread's running request."""

    def __init__(
        self,
        *,
        settings: GatewayThreadGuidanceSettings,
        requests: GatewayRequestService,
        sessions: SessionRepository,
        manager: GatewayManagerClient,
        request_context: GatewayRequestContextService,
        local_responses: GatewayLocalResponseFactory,
        render_envelope: Callable[[MessageEnvelope], str],
        event_output: GatewayEventOutput,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._requests = requests
        self._sessions = sessions
        self._manager = manager
        self._request_context = request_context
        self._local_responses = local_responses
        self._render_envelope = render_envelope
        self._event_output = event_output
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._wake = asyncio.Event()
        # One sending attempt per guidance at a time in this process; a second
        # would register the same reply correlation. Repeats across processes
        # are harmless duplicates for the lead.
        self._in_flight: set[str] = set()

    # Building held guidance -------------------------------------------------

    def held_record(
        self,
        *,
        correlation_id: str,
        running: RequestRecord,
        text: str,
        attachments: tuple[UserTaskAttachment, ...],
        replied_message: RepliedMessage | None,
    ) -> HeldGuidanceRecord:
        """The held copy of one message for ``running``; the caller saves it with the message."""

        steering = TaskSteering(
            steering_type=(
                TaskSteeringType.INTERRUPT_RESPONSE
                if running.status == REQUEST_STATUS_WAITING_FOR_STEERING
                else TaskSteeringType.PROACTIVE_GUIDANCE
            ),
            conversation_id=running.conversation_id,
            guidance_text=text,
            attachments=list(attachments),
            replied_message=replied_message,
            idempotency_key=correlation_id,
        )
        return HeldGuidanceRecord(
            correlation_id=correlation_id,
            target_correlation_id=running.correlation_id,
            session_key=running.session_key,
            conversation_id=running.conversation_id,
            steering_json=steering.model_dump_json(),
            created_at=self._clock.now_utc().isoformat(),
        )

    # Sending ----------------------------------------------------------------

    async def deliver(self, correlation_id: str) -> GatewayResponse:
        """Send a message just saved as guidance; the reply tells the person what happened.

        Older guidance still held for the same request is sent first, so the
        lead receives a thread's messages in the order they were written.
        """

        held = await asyncio.to_thread(self._requests.get_held_guidance, correlation_id)
        if held is None:
            raise RuntimeError(f"Guidance {correlation_id} was not saved before sending.")
        for earlier in await self._earlier_held(held):
            await self._send_and_settle(earlier, notify=True)
        attempt = await self._send_and_settle(held, notify=False)
        return self._reply(held, attempt)

    async def _earlier_held(self, held: HeldGuidanceRecord) -> list[HeldGuidanceRecord]:
        records = await asyncio.to_thread(self._requests.held_guidance)
        earlier: list[HeldGuidanceRecord] = []
        for record in records:
            if record.correlation_id == held.correlation_id:
                break
            if record.target_correlation_id == held.target_correlation_id:
                earlier.append(record)
        return earlier

    async def retry_held(self) -> None:
        """Send every held message again, oldest first, and settle what it can."""

        for held in await asyncio.to_thread(self._requests.held_guidance):
            if held.correlation_id not in self._in_flight:
                await self._send_and_settle(held, notify=True)

    async def _send_and_settle(self, held: HeldGuidanceRecord, *, notify: bool) -> _Attempt:
        attempt = await self._attempt(held)
        await self._settle(held, attempt, notify=notify)
        return attempt

    async def _attempt(self, held: HeldGuidanceRecord) -> _Attempt:
        if held.correlation_id in self._in_flight:
            return _Attempt(GuidanceDelivery.UNCONFIRMED)
        target = await asyncio.to_thread(self._requests.get, held.target_correlation_id)
        if target is None or target.run_state != RUN_STATE_RUNNING:
            return _Attempt(GuidanceDelivery.REQUEST_FINISHED)
        manager_id = self._manager.manager_id
        if not manager_id:
            return _Attempt(GuidanceDelivery.NO_LEAD, target_task_id=target.active_task_id)
        envelope = await asyncio.to_thread(self._envelope, held, target, manager_id)
        if envelope is None:
            return _Attempt(GuidanceDelivery.REQUEST_FINISHED)
        self._in_flight.add(held.correlation_id)
        try:
            status_envelope = await self._publish_and_wait(held, envelope, target)
        finally:
            self._in_flight.discard(held.correlation_id)
        if status_envelope is None:
            return _Attempt(GuidanceDelivery.UNCONFIRMED, target_task_id=target.active_task_id)
        return self._classify(status_envelope, manager_id, target_task_id=target.active_task_id)

    async def _publish_and_wait(
        self,
        held: HeldGuidanceRecord,
        envelope: MessageEnvelope,
        target: RequestRecord,
    ) -> MessageEnvelope | None:
        steering = envelope.msg_content
        assert isinstance(steering, TaskSteering)
        self._logger.info(
            "Gateway sending guidance to the thread's running request. "
            "event=gateway_task_steering_publish correlation_id=%s target_correlation_id=%s "
            "target_task_id=%s session_key=%s steering_type=%s attachment_count=%d",
            held.correlation_id,
            target.correlation_id,
            target.active_task_id or "-",
            held.session_key,
            steering.steering_type.value,
            len(steering.attachments),
        )
        try:
            async with self._manager.response_channel(held.correlation_id) as replies:
                await self._manager.publish_operational(envelope)
                return await asyncio.wait_for(
                    replies.get(), timeout=self._settings.status_timeout_seconds
                )
        except TimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 - the copy stays held and is sent again
            self._logger.warning(
                "Guidance could not be sent; it stays held. "
                "event=gateway_held_guidance_send_failed correlation_id=%s error_type=%s",
                held.correlation_id,
                type(exc).__name__,
            )
            return None

    def _envelope(
        self,
        held: HeldGuidanceRecord,
        target: RequestRecord,
        manager_id: str,
    ) -> MessageEnvelope | None:
        """The guidance addressed to the request's own task (storage reads: worker thread)."""

        session = self._sessions.get_session(held.session_key)
        if session is None:
            self._logger.warning(
                "Held guidance belongs to a thread that no longer exists. "
                "event=gateway_held_guidance_session_missing correlation_id=%s session_key=%s",
                held.correlation_id,
                held.session_key,
            )
            return None
        route = GatewayRequestRoute(
            channel_name=session.channel_name,
            user_id=session.user_id,
            chat_id=session.chat_id,
            thread_id=session.thread_id,
            canonical_user_id=session.owner_canonical_user_id,
        )
        presentation = self._request_context.requester_presentation_for_session(session, route)
        steering = TaskSteering.model_validate_json(held.steering_json).model_copy(
            update={
                "target_task_id": target.active_task_id,
                "target_correlation_id": target.correlation_id,
                "target_agent_id": manager_id,
            }
        )
        return MessageEnvelope(
            msg_content=steering,
            msg_meta=MessageMeta(
                from_agent_name=presentation.display_name,
                msg_correlation_id=held.correlation_id,
                conversation_id=held.conversation_id,
                request_timezone=presentation.timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="interactive",
                    route=route,
                    manager_agent_id=manager_id,
                ),
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                response_routing_key=self._settings.response_route,
                agent_signature="",
                idempotency_key=held.correlation_id,
            ),
        )

    def _classify(
        self,
        status_envelope: MessageEnvelope,
        manager_id: str | None,
        *,
        target_task_id: str | None,
    ) -> _Attempt:
        content = status_envelope.msg_content
        if (
            not isinstance(content, TaskSteering)
            or not manager_id
            or status_envelope.msg_header.from_agent_id != manager_id
        ):
            # Only the current lead's answer settles guidance; anything else
            # leaves it held for that lead.
            self._logger.warning(
                "Ignored a guidance answer that is not from the current team lead. "
                "event=gateway_guidance_answer_ignored correlation_id=%s from_agent_id=%s content_type=%s",
                status_envelope.msg_meta.msg_correlation_id if status_envelope.msg_meta else None,
                status_envelope.msg_header.from_agent_id,
                type(content).__name__,
            )
            return _Attempt(GuidanceDelivery.UNCONFIRMED, target_task_id=target_task_id)
        outcome = _lead_answer_outcome(content)
        return _Attempt(outcome, status_envelope=status_envelope, target_task_id=target_task_id)

    # Settling ---------------------------------------------------------------

    async def _settle(self, held: HeldGuidanceRecord, attempt: _Attempt, *, notify: bool) -> None:
        status = _released_status(attempt)
        if status is None:
            self._logger.info(
                "Guidance stays held for its running request. event=gateway_guidance_held "
                "correlation_id=%s target_correlation_id=%s reason=%s",
                held.correlation_id,
                held.target_correlation_id,
                attempt.outcome.value,
            )
            return
        released = await asyncio.to_thread(
            self._requests.release_guidance, held.correlation_id, status=status
        )
        if not released:
            return  # Another answer or process settled it first.
        self._logger.info(
            "Guidance settled. event=gateway_guidance_settled correlation_id=%s "
            "target_correlation_id=%s outcome=%s status=%s",
            held.correlation_id,
            held.target_correlation_id,
            attempt.outcome.value,
            status,
        )
        if notify and attempt.outcome != GuidanceDelivery.ACCEPTED:
            await self._notify(held, self._unused_notice(held, attempt))

    def _unused_notice(self, held: HeldGuidanceRecord, attempt: _Attempt) -> str:
        quoted = _excerpt(TaskSteering.model_validate_json(held.steering_json).guidance_text)
        if attempt.outcome == GuidanceDelivery.REQUEST_FINISHED:
            return (
                f"Your request in this thread finished before your message “{quoted}” "
                "reached it, so the message was not used. Send it again to start a new request."
            )
        reason = self._render_envelope(attempt.status_envelope) if attempt.status_envelope else ""
        return f"Your message “{quoted}” was not passed to your request. {reason}".strip()

    async def _notify(self, held: HeldGuidanceRecord, text: str) -> None:
        session = await asyncio.to_thread(self._sessions.get_session, held.session_key)
        if session is None:
            return
        try:
            await self._event_output.emit(
                GatewayDeliveryEvent(
                    text=text,
                    session_key=session.session_key,
                    channel_name=session.channel_name,
                    user_id=session.user_id,
                    chat_id=session.chat_id,
                    thread_id=session.thread_id,
                    conversation_id=held.conversation_id,
                    scheduled_job_id=None,
                    correlation_id=held.correlation_id,
                    original_message_id=None,
                    message_type=GUIDANCE_MESSAGE_TYPE,
                )
            )
        except Exception:  # noqa: BLE001 - the outcome is already recorded
            self._logger.exception(
                "Could not tell the person their message was not used. "
                "event=gateway_guidance_notice_failed correlation_id=%s session_key=%s",
                held.correlation_id,
                held.session_key,
            )

    def _reply(self, held: HeldGuidanceRecord, attempt: _Attempt) -> GatewayResponse:
        if attempt.status_envelope is not None and attempt.outcome in {
            GuidanceDelivery.ACCEPTED,
            GuidanceDelivery.REFUSED,
        }:
            return GatewayResponse(
                correlation_id=held.correlation_id,
                conversation_id=held.conversation_id,
                task_id=attempt.target_task_id,
                message_type=GUIDANCE_MESSAGE_TYPE,
                text=self._render_envelope(attempt.status_envelope),
                raw_envelope=attempt.status_envelope,
            )
        text = _HELD_REPLIES.get(attempt.outcome, REQUEST_FINISHED_TEXT)
        return self._local_responses.create(
            text=text,
            message_type=GUIDANCE_MESSAGE_TYPE,
            correlation_id=held.correlation_id,
            conversation_id=held.conversation_id,
        )

    # Late answers and retries ---------------------------------------------------

    async def handle_late_status(self, envelope: MessageEnvelope) -> bool:
        """Settle a guidance answer that arrived after its attempt stopped waiting."""

        content = envelope.msg_content
        if not isinstance(content, TaskSteering) or content.steering_type != TaskSteeringType.STATUS:
            return False
        correlation_id = envelope.msg_meta.msg_correlation_id if envelope.msg_meta else None
        if not correlation_id:
            return False
        held = await asyncio.to_thread(self._requests.get_held_guidance, correlation_id)
        if held is None:
            record = await asyncio.to_thread(self._requests.get, correlation_id)
            # A repeated answer to settled guidance is not news for the person.
            return record is not None and record.request_kind == REQUEST_KIND_TASK_STEERING
        target = await asyncio.to_thread(self._requests.get, held.target_correlation_id)
        attempt = self._classify(
            envelope,
            self._manager.manager_id,
            target_task_id=target.active_task_id if target is not None else None,
        )
        await self._settle(held, attempt, notify=True)
        return True

    def wake(self) -> None:
        """Try held guidance again now (a request ended or a lead became available)."""

        self._wake.set()

    def lead_reported(
        self,
        status: AgentStatus,
        reported_at: datetime | None,
        source: str,
    ) -> None:
        """Presence observer: the lead's report may mean it now holds a waiting request."""

        del reported_at, source
        if status.self_id and status.self_id == self._manager.manager_id:
            self._wake.set()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Retry held guidance when woken, and at least every ``retry_seconds``."""

        while not stop_event.is_set():
            try:
                await self.retry_held()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop keeps holding guidance
                self._logger.warning(
                    "Gateway held-guidance loop failed. event=gateway_held_guidance_loop_failed "
                    "error_type=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._settings.retry_seconds)
            except TimeoutError:
                pass
            self._wake.clear()


def _lead_answer_outcome(content: TaskSteering) -> GuidanceDelivery:
    if content.status == TaskSteeringStatus.ACCEPTED:
        return GuidanceDelivery.ACCEPTED
    if content.status == TaskSteeringStatus.STALE:
        if content.status_code == TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND:
            # The lead does not hold the request's task: it waits behind other
            # work, or it ended and left the lead's task list. The request's
            # final reply settles the second case.
            return GuidanceDelivery.NOT_STARTED
        if content.status_code == TaskSteeringStatusCode.TARGET_TASK_TERMINAL:
            return GuidanceDelivery.REQUEST_FINISHED
        return GuidanceDelivery.REFUSED
    if content.status in {TaskSteeringStatus.REJECTED, TaskSteeringStatus.EXPIRED}:
        return GuidanceDelivery.REFUSED
    return GuidanceDelivery.UNCONFIRMED


def _released_status(attempt: _Attempt) -> str | None:
    """The guidance row's final status, or None while the copy stays held."""

    if attempt.outcome == GuidanceDelivery.REQUEST_FINISHED:
        return GUIDANCE_STATUS_REQUEST_FINISHED
    if attempt.outcome in {GuidanceDelivery.ACCEPTED, GuidanceDelivery.REFUSED}:
        content = attempt.status_envelope.msg_content if attempt.status_envelope else None
        assert isinstance(content, TaskSteering)
        return content.status.value
    return None


def _excerpt(text: str | None, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


__all__ = [
    "GUIDANCE_MESSAGE_TYPE",
    "GatewayThreadGuidance",
    "GatewayThreadGuidanceSettings",
    "GuidanceDelivery",
    "NO_LEAD_TEXT",
    "NOT_STARTED_TEXT",
    "REQUEST_FINISHED_TEXT",
    "UNCONFIRMED_TEXT",
]
