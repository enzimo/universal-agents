"""Application-owned terminal-response orchestration.

Guidance for a running request is sent by ``thread_guidance``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from assistant_gateway.application.credential_handoff import ToolSetupHandoffs
from assistant_gateway.application.credential_workflow import (
    GatewayCredentialWorkflow,
)
from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.pending_questions import (
    GatewayPendingQuestions,
    QuestionDisplay,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.domain.request_state import REQUEST_STATUS_WAITING_FOR_STEERING
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.control_v1 import AgentStatus, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import (
    ToolSetupRequestV1,
    SecureCredentialCollectionRequest,
    SecureCredentialFieldSpec,
    SignupAutomationRequest,
)
from custom_messages.contracts.delivery_v1 import UserNotification, UserNotificationAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import MessageMetaType, RequesterType, RoutingType
from custom_messages.presentation.task_query import render_task_query_text
from custom_messages.enums import TaskSteeringType


QueryHandler = Callable[[TaskQuery], Awaitable[str] | str]
ReplyToTaskQuery = Callable[..., Awaitable[None]]
RenderEnvelope = Callable[[MessageEnvelope], str]


class ResolveAttachments(Protocol):
    def __call__(
        self, content: object, *, person_id: str | None
    ) -> tuple[UserNotificationAttachment, ...]: ...


ManagerUnavailableText = Callable[[], str]


def _task_steering_coordination_status(content: TaskSteering) -> str:
    if content.steering_type == TaskSteeringType.INTERRUPT_REQUEST:
        return REQUEST_STATUS_WAITING_FOR_STEERING
    return "steering_status"


@dataclass(frozen=True)
class GatewayResponseCoordinatorSettings:
    """Stable identity and timeout settings for response coordination."""

    gateway_instance_name: str
    user_display_name: str
    response_route: str
    request_timezone: str
    request_timeout_seconds: float


class GatewayResponseCoordinator:
    """Own correlated response interpretation for one waiting request."""

    def __init__(
        self,
        *,
        settings: GatewayResponseCoordinatorSettings,
        manager_client: GatewayManagerClient,
        request_service: GatewayRequestService,
        session_repository: SessionRepository,
        credential_workflow: GatewayCredentialWorkflow,
        credential_handoffs: ToolSetupHandoffs,
        questions: GatewayPendingQuestions,
        reply_to_task_query: ReplyToTaskQuery,
        render_envelope: RenderEnvelope,
        resolve_attachments: ResolveAttachments,
        manager_unavailable_text: ManagerUnavailableText,
        on_request_finished: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._manager_client = manager_client
        self._request_service = request_service
        self._session_repository = session_repository
        self._credential_workflow = credential_workflow
        self._credential_handoffs = credential_handoffs
        self._questions = questions
        self._reply_to_task_query = reply_to_task_query
        # Guidance still held for a request is settled when it ends (thread_guidance).
        self._on_request_finished = on_request_finished
        self._render_envelope = render_envelope
        self._resolve_attachments = resolve_attachments
        self._manager_unavailable_text = manager_unavailable_text
        self._logger = logger or logging.getLogger(__name__)

    async def await_terminal_response(
        self,
        *,
        correlation_id: str,
        session_key: str,
        query_handler: QueryHandler | None,
    ) -> GatewayResponse:
        queue = self._manager_client.get_response_channel(correlation_id)
        timeout_seconds = self._settings.request_timeout_seconds
        timeout_label = f"{timeout_seconds:g}"
        while True:
            try:
                envelope = await asyncio.wait_for(
                    queue.get(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                self._request_service.update_status(
                    correlation_id,
                    "timed_out",
                )
                raise TimeoutError(
                    "Timed out after "
                    f"{timeout_label}s waiting for a terminal manager response."
                ) from exc
            content = envelope.msg_content
            conversation_id = (
                envelope.msg_meta.conversation_id if envelope.msg_meta else None
            )

            if (
                isinstance(content, Task)
                and content.task_type == MessageMetaType.RESPONSE
            ):
                manager_agent_id = (
                    envelope.msg_header.from_agent_id or self._manager_client.manager_id
                )
                self._request_service.transition_with_session_lineage(
                    correlation_id=correlation_id,
                    status="completed",
                    session_key=session_key,
                    conversation_id=conversation_id,
                    last_task_id=content.task_id,
                    manager_agent_id=manager_agent_id,
                    finishes_request=True,
                )
                await self._questions.task_finished(
                    correlation_id=correlation_id, task_id=content.task_id
                )
                if self._on_request_finished is not None:
                    self._on_request_finished()
                return self._response(
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    task_id=content.task_id,
                    message_type="task",
                    envelope=envelope,
                )

            if (
                isinstance(content, TaskQuery)
                and content.query_type == MessageMetaType.REQUEST
            ):
                rendered_query = render_task_query_text(content)
                if query_handler is None:
                    arrival = self._questions.ask(
                        session_key=session_key,
                        correlation_id=correlation_id,
                        conversation_id=conversation_id,
                        query=content,
                        rendered_text=rendered_query,
                    )
                    if arrival.display == QuestionDisplay.NOTHING:
                        continue  # a repeat of an ended question; keep waiting
                    return GatewayResponse(
                        correlation_id=correlation_id,
                        conversation_id=conversation_id,
                        task_id=content.parent_task_id,
                        message_type=arrival.message_type,
                        text=arrival.text,
                        raw_envelope=envelope,
                        attachments=(
                            self._attachments_for(content, correlation_id)
                            if arrival.display == QuestionDisplay.QUESTION
                            else ()
                        ),
                    )
                response_text = query_handler(content)
                if inspect.isawaitable(response_text):
                    response_text = await response_text
                await self._reply_to_task_query(
                    content,
                    correlation_id=correlation_id,
                    response_text=str(response_text),
                    session_key=session_key,
                    conversation_id=conversation_id,
                )
                continue

            if isinstance(content, TaskSteering):
                status = _task_steering_coordination_status(content)
                if content.target_task_id:
                    self._request_service.update_coordination(
                        correlation_id,
                        status=status,
                        active_task_id=content.target_task_id,
                    )
                else:
                    self._request_service.update_status(correlation_id, status)
                return self._response(
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    task_id=content.target_task_id,
                    message_type="task_steering",
                    envelope=envelope,
                )

            if (
                isinstance(content, SecureCredentialCollectionRequest)
                and content.request_type == MessageMetaType.REQUEST
            ):
                return await self._credential_workflow.start_collection(
                    request_content=content,
                    request_envelope=envelope,
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    session_key=session_key,
                )

            if (
                isinstance(content, ToolSetupRequestV1)
                and content.request_type == MessageMetaType.REQUEST
            ):
                return await self._credential_workflow.tool_setups.start(
                    request_content=content,
                    request_envelope=envelope,
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    session_key=session_key,
                )

            if (
                isinstance(content, SignupAutomationRequest)
                and content.request_type == MessageMetaType.REQUEST
            ):
                await self._credential_workflow.execute_approved_signup_probe(
                    content,
                    session_key=session_key,
                    conversation_id=conversation_id,
                )
                collection_request = self._manual_signup_collection_request(content)
                return await self._credential_workflow.start_collection(
                    request_content=collection_request,
                    request_envelope=envelope,
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    session_key=session_key,
                )

            if isinstance(content, UserNotification):
                return self._response(
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    task_id=getattr(content, "source_task_id", None),
                    message_type=GatewayDeliveryPresentation.message_type(content),
                    envelope=envelope,
                )

            if isinstance(content, AgentStatus):
                return self._response(
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    task_id=None,
                    message_type="status",
                    envelope=envelope,
                )

            if isinstance(content, SystemStatusMessage):
                return GatewayResponse(
                    correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    task_id=None,
                    message_type="system_status",
                    text=content.system_status,
                    raw_envelope=envelope,
                    attachments=self._attachments_for(content, correlation_id),
                )

    async def handle_unmatched_credential_request(
        self, envelope: MessageEnvelope
    ) -> bool:
        """Show late Website setup pages; fail unreachable requests closed."""

        if await self._credential_handoffs.try_present(envelope):
            return True

        content = envelope.msg_content
        if not isinstance(
            content,
            (
                SecureCredentialCollectionRequest,
                ToolSetupRequestV1,
                SignupAutomationRequest,
            ),
        ):
            return False
        meta = envelope.msg_meta
        correlation_id = str(getattr(meta, "msg_correlation_id", None) or "").strip()
        conversation_id = getattr(meta, "conversation_id", None)
        gateway_context = getattr(meta, "gateway_context", None)
        request_record = (
            self._request_service.get(correlation_id) if correlation_id else None
        )
        session_key = str(
            (getattr(gateway_context, "session_key", None) if gateway_context else None)
            or (request_record.session_key if request_record is not None else None)
            or ""
        ).strip()
        if not session_key and conversation_id:
            session = self._session_repository.get_session_by_conversation_id(
                conversation_id
            )
            session_key = session.session_key if session is not None else ""
        if not correlation_id or not session_key:
            self._logger.error(
                "Unmatched credential request lacked durable gateway lineage. "
                "event=gateway_unmatched_credential_request_lineage_missing "
                "correlation_id=%s conversation_id=%s content_type=%s",
                correlation_id or "-",
                conversation_id or "-",
                type(content).__name__,
            )
            return True
        self._logger.warning(
            "Failing an unmatched credential request instead of presenting an "
            "unusable form prompt. event=gateway_unmatched_credential_request_failed_closed "
            "correlation_id=%s conversation_id=%s session_key=%s content_type=%s",
            correlation_id,
            conversation_id or "-",
            session_key,
            type(content).__name__,
        )
        if isinstance(content, SignupAutomationRequest):
            collection_request = self._manual_signup_collection_request(content)
            await self._credential_workflow.fail_unmatched_collection_request(
                request_content=collection_request,
                request_envelope=envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
            )
        elif isinstance(content, SecureCredentialCollectionRequest):
            await self._credential_workflow.fail_unmatched_collection_request(
                request_content=content,
                request_envelope=envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
            )
        else:
            await self._credential_workflow.tool_setups.fail_unmatched(
                request_content=content,
                request_envelope=envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
            )
        return True

    @staticmethod
    def _manual_signup_collection_request(
        content: SignupAutomationRequest,
    ) -> SecureCredentialCollectionRequest:
        return SecureCredentialCollectionRequest(
            credential_request_id=content.signup_request_id,
            parent_task_id=content.parent_task_id,
            service_name=content.service_name,
            signup_url=content.signup_url,
            credential_key=content.credential_key,
            display_name=f"{content.service_name} Login",
            purpose="Account created for approved agent signup workflow.",
            store_target=content.store_target,
            flow="manual_signup",
            fields=[
                SecureCredentialFieldSpec(
                    name="username",
                    kind="username",
                    visibility="metadata_visible",
                    value_source="user_or_agent_supplied_metadata",
                    value=content.desired_username,
                ),
                SecureCredentialFieldSpec(
                    name=content.password_field_name,
                    kind="password",
                    visibility="secret",
                    value_source="user_input",
                ),
            ],
            automation_steps=content.automation_steps,
            user_approved=content.user_approved,
        )

    async def queue_manager_unavailable(self, correlation_id: str) -> None:
        if not self._manager_client.has_pending_response(correlation_id):
            return
        request_record = self._request_service.mark_manager_unavailable(correlation_id)
        await self._manager_client.queue_response(
            correlation_id,
            MessageEnvelope(
                msg_content=SystemStatusMessage(
                    system_status=self._manager_unavailable_text()
                ),
                msg_meta=MessageMeta(
                    from_agent_name=(f"{self._settings.gateway_instance_name}_gateway"),
                    msg_correlation_id=correlation_id,
                    conversation_id=(
                        request_record.conversation_id if request_record else None
                    ),
                    requester_type=RequesterType.AGENT_REQUESTER,
                    is_response_requested=False,
                ),
                msg_header=MessageHeader(
                    from_agent_id=self._settings.response_route,
                    to_routing_path=RoutingType.NULL_ROUTE,
                    agent_signature="",
                ),
            ),
        )

    def _response(
        self,
        *,
        correlation_id: str,
        conversation_id: str | None,
        task_id: str | None,
        message_type: str,
        envelope: MessageEnvelope,
    ) -> GatewayResponse:
        return GatewayResponse(
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            task_id=task_id,
            message_type=message_type,
            text=self._render_envelope(envelope),
            raw_envelope=envelope,
            attachments=self._attachments_for(envelope.msg_content, correlation_id),
        )

    def _attachments_for(
        self, content: object, correlation_id: str
    ) -> tuple[UserNotificationAttachment, ...]:
        """Attachments from the folders of the person who made this request.

        The person comes from the gateway's own request record, not from
        metadata in the manager's reply.
        """

        record = self._request_service.get(correlation_id)
        person_id = record.owner_canonical_user_id if record is not None else None
        return self._resolve_attachments(content, person_id=person_id)
