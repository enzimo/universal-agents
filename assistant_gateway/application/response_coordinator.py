"""Application-owned terminal-response and task-steering orchestration."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from assistant_gateway.application.credential_handoff import ToolSetupHandoffs
from assistant_gateway.application.credential_workflow import (
    GatewayCredentialWorkflow,
)
from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.domain.request_state import PendingQueryRecord
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.control_v1 import AgentStatus, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import (
    ToolSetupRequestV1,
    SecureCredentialCollectionRequest,
    SecureCredentialFieldSpec,
    SignupAutomationRequest,
)
from custom_messages.contracts.delivery_v1 import UserNotification, UserNotificationAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import MessageMetaType, RequesterType, RoutingType
from custom_messages.presentation.task_query import render_task_query_text
from custom_messages.enums import TaskSteeringStatus, TaskSteeringType


QueryHandler = Callable[[TaskQuery], Awaitable[str] | str]
ReplyToTaskQuery = Callable[..., Awaitable[None]]
ResolvePendingQuery = Callable[
    [str],
    Awaitable[PendingQueryRecord | None],
]
RenderEnvelope = Callable[[MessageEnvelope], str]


class ResolveAttachments(Protocol):
    def __call__(
        self, content: object, *, person_id: str | None
    ) -> tuple[UserNotificationAttachment, ...]: ...


ManagerUnavailableText = Callable[[], str]


def _task_steering_coordination_status(content: TaskSteering) -> str:
    if content.steering_type == TaskSteeringType.INTERRUPT_REQUEST:
        return "waiting_for_steering"
    return "steering_status"


@dataclass(frozen=True)
class GatewayResponseCoordinatorSettings:
    """Stable identity and timeout settings for response coordination."""

    gateway_instance_name: str
    user_display_name: str
    response_route: str
    request_timezone: str
    request_timeout_seconds: float
    steering_status_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class GatewayTaskSteeringRequest:
    """Resolved input for one mid-turn task-steering operation."""

    text: str
    session_key: str
    conversation_id: str | None
    gateway_context: GatewayContext
    requester_display_name: str
    request_timezone: str
    target_task_id: str | None = None
    target_correlation_id: str | None = None
    interrupt_responses: tuple[dict[str, Any], ...] = ()
    steering_type: TaskSteeringType | None = None
    attachments: tuple[UserTaskAttachment, ...] = ()
    correlation_id: str | None = None
    request_already_recorded: bool = False
    replied_message: RepliedMessage | None = None


class GatewayResponseCoordinator:
    """Own correlated response interpretation and task-steering publication."""

    def __init__(
        self,
        *,
        settings: GatewayResponseCoordinatorSettings,
        manager_client: GatewayManagerClient,
        request_service: GatewayRequestService,
        session_repository: SessionRepository,
        id_generator: GatewayIdGenerator,
        credential_workflow: GatewayCredentialWorkflow,
        credential_handoffs: ToolSetupHandoffs,
        resolve_pending_query: ResolvePendingQuery,
        reply_to_task_query: ReplyToTaskQuery,
        render_envelope: RenderEnvelope,
        resolve_attachments: ResolveAttachments,
        manager_unavailable_text: ManagerUnavailableText,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._manager_client = manager_client
        self._request_service = request_service
        self._session_repository = session_repository
        self._id_generator = id_generator
        self._credential_workflow = credential_workflow
        self._credential_handoffs = credential_handoffs
        self._resolve_pending_query = resolve_pending_query
        self._reply_to_task_query = reply_to_task_query
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
                )
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
                    self._request_service.upsert_pending_query(
                        session_key=session_key,
                        correlation_id=correlation_id,
                        task_query_id=content.task_query_id,
                        parent_task_id=content.parent_task_id,
                        conversation_id=conversation_id,
                        query_text=rendered_query,
                        query_payload_json=content.model_dump_json(),
                    )
                    return GatewayResponse(
                        correlation_id=correlation_id,
                        conversation_id=conversation_id,
                        task_id=content.parent_task_id,
                        message_type="task_query",
                        text=rendered_query,
                        raw_envelope=envelope,
                        attachments=self._attachments_for(content, correlation_id),
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

    async def send_task_steering(
        self,
        request: GatewayTaskSteeringRequest,
    ) -> GatewayResponse:
        manager_id = self._manager_client.manager_id
        if manager_id is None:
            correlation_id = self._steering_correlation_id(request)
            self._record_steering_request_if_needed(
                request=request,
                correlation_id=correlation_id,
                target_task_id=request.target_task_id,
            )
            self._manager_client.register_response_channel(correlation_id)
            try:
                await self.queue_manager_unavailable(correlation_id)
                return await self.await_terminal_response(
                    correlation_id=correlation_id,
                    session_key=request.session_key,
                    query_handler=None,
                )
            finally:
                self._manager_client.unregister_response_channel(correlation_id)

        target_task_id = request.target_task_id
        target_correlation_id = request.target_correlation_id
        if not target_task_id and not target_correlation_id:
            target_task_id, target_correlation_id = await self.resolve_target(
                session_key=request.session_key,
                conversation_id=request.conversation_id,
            )
        if not target_task_id and not target_correlation_id:
            raise ValueError("No active task is available for mid-turn steering.")

        correlation_id = self._steering_correlation_id(request)
        request_queue = self._manager_client.register_response_channel(correlation_id)
        if request.steering_type is not None:
            resolved_steering_type = request.steering_type
        elif request.interrupt_responses:
            resolved_steering_type = TaskSteeringType.INTERRUPT_RESPONSE
        else:
            resolved_steering_type = TaskSteeringType.PROACTIVE_GUIDANCE
        steering = TaskSteering(
            steering_type=resolved_steering_type,
            target_task_id=target_task_id,
            target_correlation_id=target_correlation_id,
            conversation_id=request.conversation_id,
            target_agent_id=manager_id,
            guidance_text=request.text,
            attachments=list(request.attachments),
            replied_message=request.replied_message,
            interrupt_responses=list(request.interrupt_responses),
            idempotency_key=correlation_id,
        )
        envelope = MessageEnvelope(
            msg_content=steering,
            msg_meta=MessageMeta(
                from_agent_name=request.requester_display_name,
                msg_correlation_id=correlation_id,
                conversation_id=request.conversation_id,
                request_timezone=request.request_timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=request.gateway_context,
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                response_routing_key=self._settings.response_route,
                agent_signature="",
                idempotency_key=correlation_id,
            ),
        )
        self._record_steering_request_if_needed(
            request=request,
            correlation_id=correlation_id,
            target_task_id=target_task_id,
        )
        self._logger.info(
            "Gateway publishing task steering. "
            "event=gateway_task_steering_publish correlation_id=%s "
            "conversation_id=%s session_key=%s target_task_id=%s "
            "target_correlation_id=%s steering_type=%s attachment_count=%d",
            correlation_id,
            request.conversation_id,
            request.session_key,
            target_task_id or "-",
            target_correlation_id or "-",
            resolved_steering_type.value,
            len(request.attachments),
        )
        try:
            await self._manager_client.publish_operational(envelope)
            try:
                status_envelope = await asyncio.wait_for(
                    request_queue.get(),
                    timeout=self._settings.steering_status_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return GatewayResponse(
                    correlation_id=correlation_id,
                    conversation_id=request.conversation_id,
                    task_id=target_task_id,
                    message_type="task_steering",
                    text="Mid-turn guidance was sent to the active task.",
                    raw_envelope=envelope,
                )
            content = status_envelope.msg_content
            if isinstance(content, TaskSteering) and content.status in {
                TaskSteeringStatus.REJECTED,
                TaskSteeringStatus.STALE,
                TaskSteeringStatus.EXPIRED,
            }:
                self._request_service.update_status(
                    correlation_id,
                    content.status.value,
                )
            else:
                self._request_service.update_status(
                    correlation_id,
                    "accepted",
                )
            return GatewayResponse(
                correlation_id=correlation_id,
                conversation_id=request.conversation_id,
                task_id=target_task_id,
                message_type="task_steering",
                text=self._render_envelope(status_envelope),
                raw_envelope=status_envelope,
            )
        finally:
            self._manager_client.unregister_response_channel(correlation_id)

    def _record_steering_request_if_needed(
        self,
        *,
        request: GatewayTaskSteeringRequest,
        correlation_id: str,
        target_task_id: str | None,
    ) -> None:
        if request.request_already_recorded:
            return
        self._request_service.record(
            correlation_id=correlation_id,
            session_key=request.session_key,
            request_kind="task_steering",
            request_text=request.text,
            conversation_id=request.conversation_id,
            followup_to_task_id=target_task_id,
            status="submitted",
        )

    def _steering_correlation_id(self, request: GatewayTaskSteeringRequest) -> str:
        return request.correlation_id or self._id_generator.new_correlation_id()

    async def resolve_target(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> tuple[str | None, str | None]:
        pending_query = await self._resolve_pending_query(session_key)
        if pending_query is not None and (
            conversation_id is None or pending_query.conversation_id == conversation_id
        ):
            return (
                pending_query.parent_task_id,
                pending_query.correlation_id,
            )

        request = self._request_service.find_open_request(
            session_key=session_key,
            conversation_id=conversation_id,
            limit=20,
        )
        if request is not None:
            return (
                request.active_task_id or request.followup_to_task_id,
                request.correlation_id,
            )

        conversation_state = (
            self._session_repository.get_session_conversation(conversation_id)
            if conversation_id
            else None
        )
        if conversation_state is not None and conversation_state.last_task_id:
            return conversation_state.last_task_id, None

        session = self._session_repository.get_session(session_key)
        if session is not None and session.last_task_id:
            return session.last_task_id, None
        return None, None

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
