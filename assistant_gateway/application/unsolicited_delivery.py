"""Application orchestration for unsolicited gateway delivery."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Protocol

from assistant_gateway.application.delivery_projection import DeliveryPersonMismatch, GatewayDeliveryProjection
from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.pending_questions import (
    GatewayPendingQuestions,
    QuestionDisplay,
)
from assistant_gateway.application.credential_workflow import (
    REQUEST_KINDS_SECURE_CREDENTIAL_RESULTS,
)
from assistant_gateway.application.ports import GatewayClock, GatewayEventOutput
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.delivery import (
    DeliveryDecision,
    GatewayDeliveryPolicy,
    evaluate_delivery,
)
from assistant_gateway.domain.request_state import RequestRecord
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, RequesterType


class GatewayDeliveryPolicyProvider(Protocol):
    """Present the latest operator-selected delivery policy."""

    def current_delivery_policy(self) -> GatewayDeliveryPolicy:
        """Return a validated, immutable delivery-policy snapshot."""


class GatewayDeliveryReceiptPublisher(Protocol):
    """Publish delivery confirmation to the originating manager route."""

    async def send_receipt(
        self,
        *,
        envelope: MessageEnvelope,
        delivery_state: str,
        delivery_via: str,
        delivered_at: str,
    ) -> None:
        """Publish a receipt when the envelope supports acknowledgements."""


@dataclass(frozen=True)
class GatewayUnsolicitedDeliverySettings:
    """Stable values used while deciding unsolicited delivery."""

    requester_timezone: str


@dataclass(frozen=True)
class PreparedGatewayDelivery:
    """Resolved request lineage for one unsolicited delivery."""

    envelope: MessageEnvelope
    correlation_id: str | None
    request_record: RequestRecord | None
    origin_kind: str


class GatewayUnsolicitedDeliveryApplication:
    """Own unsolicited policy, projection, persistence, receipts, and emission."""

    def __init__(
        self,
        *,
        settings: GatewayUnsolicitedDeliverySettings,
        requests: GatewayRequestService,
        questions: GatewayPendingQuestions,
        sessions: SessionRepository,
        projection: GatewayDeliveryProjection,
        presentation: GatewayDeliveryPresentation,
        delivery: GatewayDeliveryService,
        policies: GatewayDeliveryPolicyProvider,
        receipts: GatewayDeliveryReceiptPublisher,
        event_output: GatewayEventOutput,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
        on_request_finished: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._requests = requests
        self._questions = questions
        self._on_request_finished = on_request_finished
        self._sessions = sessions
        self._projection = projection
        self._presentation = presentation
        self._delivery = delivery
        self._policies = policies
        self._receipts = receipts
        self._event_output = event_output
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    def prepare(self, envelope: MessageEnvelope) -> PreparedGatewayDelivery:
        """Resolve request lineage and the delivery origin."""

        msg_meta = envelope.msg_meta
        correlation_id = getattr(msg_meta, "msg_correlation_id", None)
        request_record = self._requests.get(correlation_id) if correlation_id else None
        origin_kind = request_record.request_kind if request_record else "autonomous"
        gateway_context = getattr(msg_meta, "gateway_context", None)
        interactive_continuation = self._is_interactive_continuation(
            envelope,
            gateway_context=gateway_context,
            request_record=request_record,
        )
        if interactive_continuation:
            self._log_interactive_continuation_restored(
                envelope,
                correlation_id=correlation_id,
                origin_kind=origin_kind,
            )
            origin_kind = "interactive"
        return PreparedGatewayDelivery(
            envelope=envelope,
            correlation_id=correlation_id,
            request_record=request_record,
            origin_kind=origin_kind,
        )

    @staticmethod
    def _is_interactive_continuation(
        envelope: MessageEnvelope,
        *,
        gateway_context: object | None,
        request_record: RequestRecord | None,
    ) -> bool:
        """Recognize requester-bound work that resumes after its HTTP wait ends."""

        if getattr(gateway_context, "delivery_mode", None) != "interactive":
            return False
        content = envelope.msg_content
        if isinstance(content, UserNotification):
            return request_record is None
        return bool(
            (
                (
                    isinstance(content, Task)
                    and content.task_type == MessageMetaType.RESPONSE
                )
                or (
                    isinstance(content, TaskQuery)
                    and content.query_type == MessageMetaType.REQUEST
                )
            )
            and getattr(envelope.msg_meta, "requester_type", None)
            == RequesterType.USER_REQUESTER
        )

    def _log_interactive_continuation_restored(
        self,
        envelope: MessageEnvelope,
        *,
        correlation_id: str | None,
        origin_kind: str,
    ) -> None:
        if origin_kind == "interactive":
            return
        self._logger.info(
            "Gateway restored interactive delivery for a requester-bound "
            "continuation. "
            "event=gateway_interactive_continuation_delivery_restored "
            "correlation_id=%s request_kind=%s content_type=%s",
            correlation_id,
            origin_kind,
            type(envelope.msg_content).__name__,
        )

    async def deliver(self, prepared: PreparedGatewayDelivery) -> None:
        """Apply policy and atomically record the resulting delivery outcome."""

        envelope = prepared.envelope
        if not self._presentation.supports_user_delivery(envelope.msg_content):
            self._logger.warning(
                "Dropped an unsupported unsolicited gateway envelope. "
                "event=gateway_unsupported_unsolicited_delivery_dropped "
                "correlation_id=%s conversation_id=%s content_type=%s "
                "from_agent_id=%s",
                prepared.correlation_id,
                getattr(envelope.msg_meta, "conversation_id", None),
                type(envelope.msg_content).__name__,
                envelope.msg_header.from_agent_id,
            )
            return
        rendered_text = self._presentation.render_envelope(envelope)
        finishes_request = self._is_final_reply(envelope.msg_content)
        finished_task_id = envelope.msg_content.task_id if finishes_request and isinstance(envelope.msg_content, Task) else None
        try:
            delivery_event = self._projection.project(
                envelope=envelope,
                rendered_text=rendered_text,
                request_record=prepared.request_record,
            )
        except DeliveryPersonMismatch:
            # Logged by the projection: never deliver to the wrong person. The
            # request still ended; leaving it running would hold the thread.
            if finished_task_id is not None and prepared.correlation_id and (
                prepared.request_record is None or prepared.request_record.matches_task(finished_task_id)
            ):
                self._requests.finish(prepared.correlation_id)
                self._request_finished()
            return
        policy = self._policies.current_delivery_policy()
        decision = evaluate_delivery(
            origin_kind=prepared.origin_kind,
            rendered_text=rendered_text,
            timezone_name=self._settings.requester_timezone,
            delivery_settings=policy,
            now_utc=self._clock.now_utc(),
        )
        decision = self._apply_explicit_urgency(
            envelope=envelope,
            decision=decision,
            policy=policy,
        )
        last_task_id = self._last_task_id(
            envelope,
            request_record=prepared.request_record,
        )
        if decision.action == "deliver_now":
            content = envelope.msg_content
            if (
                isinstance(content, TaskQuery)
                and content.query_type == MessageMetaType.REQUEST
            ):
                shown = self._question_event(
                    content,
                    delivery_event=delivery_event,
                    correlation_id=prepared.correlation_id,
                    rendered_text=rendered_text,
                )
                if shown is None:
                    return
                delivery_event = shown
            await self._event_output.emit(delivery_event)
            delivered_at = self._clock.now_utc().isoformat()
            await self._receipts.send_receipt(
                envelope=envelope,
                delivery_state="delivered",
                delivery_via="immediate",
                delivered_at=delivered_at,
            )
            self._delivery.record_immediate_outcome(
                correlation_id=prepared.correlation_id,
                session_key=delivery_event.session_key,
                conversation_id=delivery_event.conversation_id,
                last_task_id=last_task_id,
                finished_task_id=finished_task_id,
            )
            await self._finish_questions_of(envelope, correlation_id=prepared.correlation_id)
            if finishes_request:
                self._request_finished()
            return
        if decision.available_after is None:
            self._logger.error(
                "Gateway delivery policy returned a deferred decision without a release time. "
                "event=gateway_delivery_policy_invalid correlation_id=%s reason=%s",
                prepared.correlation_id or "-",
                decision.reason,
            )
            raise RuntimeError(
                "Deferred delivery requires an available_after timestamp."
            )
        self._delivery.defer(
            session_key=delivery_event.session_key,
            origin_kind=prepared.origin_kind,
            payload=envelope.model_dump(mode="json"),
            rendered_text=rendered_text,
            available_after=decision.available_after,
            correlation_id=prepared.correlation_id,
            conversation_id=delivery_event.conversation_id,
            last_task_id=last_task_id,
            owner_canonical_user_id=(
                prepared.request_record.owner_canonical_user_id
                if prepared.request_record is not None
                else None
            ),
            finished_task_id=finished_task_id,
        )
        await self._finish_questions_of(envelope, correlation_id=prepared.correlation_id)
        if finishes_request:
            self._request_finished()

    def _question_event(
        self,
        content: TaskQuery,
        *,
        delivery_event: GatewayDeliveryEvent,
        correlation_id: str | None,
        rendered_text: str,
    ) -> GatewayDeliveryEvent | None:
        """Keep an arriving question and return what the chat shows, if anything.

        A timeout or restart removes the response waiter, not the task's need
        for an answer, so the question is stored before it is shown.
        """

        if not correlation_id:
            self._logger.error(
                "Cannot deliver a question without reply correlation. "
                "event=gateway_query_missing_correlation task_query_id=%s session_key=%s",
                content.task_query_id,
                delivery_event.session_key,
            )
            return None
        arrival = self._questions.ask(
            session_key=delivery_event.session_key,
            correlation_id=correlation_id,
            conversation_id=delivery_event.conversation_id,
            query=content,
            rendered_text=rendered_text,
        )
        if arrival.display == QuestionDisplay.QUESTION:
            return delivery_event
        if arrival.display == QuestionDisplay.WAITS_IN_LINE:
            return replace(
                delivery_event,
                text=arrival.text,
                message_type=arrival.message_type,
                message_level="normal",
                attachments=(),
            )
        return None

    async def _finish_questions_of(
        self, envelope: MessageEnvelope, *, correlation_id: str | None
    ) -> None:
        """A task's final reply ends its questions and shows the next one."""

        content = envelope.msg_content
        if isinstance(content, Task) and content.task_type == MessageMetaType.RESPONSE:
            await self._questions.task_finished(
                correlation_id=correlation_id, task_id=content.task_id
            )

    @staticmethod
    def _is_final_reply(content: object) -> bool:
        """A manager ``Task`` response is the request's final reply (also a stop or failure)."""

        return isinstance(content, Task) and content.task_type == MessageMetaType.RESPONSE

    def _request_finished(self) -> None:
        if self._on_request_finished is not None:
            self._on_request_finished()

    @staticmethod
    def _apply_explicit_urgency(
        *,
        envelope: MessageEnvelope,
        decision: DeliveryDecision,
        policy: GatewayDeliveryPolicy,
    ) -> DeliveryDecision:
        content = envelope.msg_content
        if (
            isinstance(content, UserNotification)
            and content.urgency == "urgent"
            and decision.action != "deliver_now"
            and policy.quiet_hours.allow_urgent_delivery
        ):
            return DeliveryDecision(
                action="deliver_now",
                available_after=None,
                reason="urgent_notification",
            )
        return decision

    def _last_task_id(
        self,
        envelope: MessageEnvelope,
        *,
        request_record: RequestRecord | None,
    ) -> str | None:
        content = envelope.msg_content
        if not (
            isinstance(content, Task) and content.task_type == MessageMetaType.RESPONSE
        ):
            return None
        task_id = content.task_id
        if (
            request_record is None
            or request_record.request_kind
            not in REQUEST_KINDS_SECURE_CREDENTIAL_RESULTS
        ):
            return task_id
        conversation_id = getattr(envelope.msg_meta, "conversation_id", None)
        conversation = (
            self._sessions.get_session_conversation(conversation_id)
            if conversation_id
            else None
        )
        if (
            conversation is None
            or not conversation.last_task_id
            or conversation.last_task_id == task_id
        ):
            return task_id
        self._logger.warning(
            "Gateway preserved newer conversation lineage while delivering a late "
            "credential workflow result. "
            "event=gateway_late_credential_result_lineage_preserved "
            "correlation_id=%s conversation_id=%s result_task_id=%s "
            "current_last_task_id=%s request_kind=%s",
            request_record.correlation_id,
            conversation_id,
            task_id,
            conversation.last_task_id,
            request_record.request_kind,
        )
        return None


class GatewayInboundMessageApplication:
    """Own the complete unsolicited manager-message application flow."""

    def __init__(
        self,
        *,
        delivery: GatewayUnsolicitedDeliveryApplication,
        unmatched_credential_handler: Callable[[MessageEnvelope], Awaitable[bool]],
        late_guidance_status_handler: Callable[[MessageEnvelope], Awaitable[bool]],
    ) -> None:
        self._delivery = delivery
        self._unmatched_credential_handler = unmatched_credential_handler
        self._late_guidance_status_handler = late_guidance_status_handler

    async def handle(self, envelope: MessageEnvelope) -> None:
        """Settle late guidance answers and unmatched credential results, otherwise deliver."""

        if await self._late_guidance_status_handler(envelope):
            return
        if await self._unmatched_credential_handler(envelope):
            return
        await self._delivery.deliver(self._delivery.prepare(envelope))
