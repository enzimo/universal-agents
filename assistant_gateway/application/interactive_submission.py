"""Interactive user message submission application."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.credential_workflow import (
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
)
from assistant_gateway.application.local_responses import (
    GatewayLocalResponseFactory,
)
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.request_context import (
    GatewayAdmissionDecision,
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_coordinator import (
    GatewayResponseCoordinator,
    GatewayTaskSteeringRequest,
    QueryHandler,
)
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.application.task_progress import (
    GatewayTaskProgressRequest,
    GatewayTaskProgressService,
)
from assistant_gateway.application.transcript_service import (
    GatewayTranscriptService,
    TranscriptRequestRecord,
    gateway_response_from_transcript,
)
from assistant_gateway.domain.transcript import GatewayTranscriptMessage
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.domain.request_state import RequestRecord
from assistant_gateway.inbound_attachments import attachment_debug_summary
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    TaskSteeringStatus,
    TaskSteeringStatusCode,
    TaskSteeringType,
)


@dataclass(frozen=True)
class GatewayInteractiveSubmissionSettings:
    """Stable settings required to submit interactive work."""

    response_route: str
    default_session_key: str
    default_owner_canonical_user_id: str


@dataclass(frozen=True)
class GatewayInteractiveSubmissionRequest:
    """One normalized interactive user message."""

    text: str
    session_key: str | None = None
    conversation_id: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()
    query_handler: QueryHandler | None = None
    attachments: tuple[UserTaskAttachment, ...] = ()
    # The chat message this one replies to, for the agent (replied_message_v1).
    replied_message: RepliedMessage | None = None


@runtime_checkable
class GatewayPersonalSetupGate(Protocol):
    """A new person's first-time setup (``assistant_gateway.personal_setup``)."""

    async def before_request(self, person_id: str, *, session: SessionRecord) -> str | None:
        """The reply to send instead of running the message, or None to run it (the setup list may be posted)."""


class GatewayInteractiveSubmissionService:
    """Own admission, routing, follow-up, steering, and task publication."""

    def __init__(
        self,
        *,
        settings: GatewayInteractiveSubmissionSettings,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        request_context: GatewayRequestContextService,
        requests: GatewayRequestService,
        manager: GatewayManagerClient,
        responses: GatewayResponseCoordinator,
        progress: GatewayTaskProgressService,
        local_responses: GatewayLocalResponseFactory,
        id_generator: GatewayIdGenerator,
        refresh_delivery_preferences: Callable[[], None],
        transcript: GatewayTranscriptService,
        personal_setup: GatewayPersonalSetupGate,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._session_repository = session_repository
        self._request_context = request_context
        self._requests = requests
        self._manager = manager
        self._responses = responses
        self._progress = progress
        self._local_responses = local_responses
        self._id_generator = id_generator
        self._refresh_delivery_preferences = refresh_delivery_preferences
        self._transcript = transcript
        self._personal_setup = personal_setup
        self._logger = logger or logging.getLogger(__name__)

    async def submit(
        self,
        request: GatewayInteractiveSubmissionRequest,
    ) -> GatewayResponse:
        self._refresh_delivery_preferences()
        admission = self._request_context.admit_interactive_route(request.route)
        if not admission.allowed:
            self._log_admission_rejection(request.route, admission)
            return self._local_responses.create(
                text=admission.message or "Access denied."
            )
        try:
            session = self._resolve_session(request)
        except PermissionError:
            self._logger.warning(
                "Gateway rejected request for session outside requester owner scope. "
                "event=gateway_session_owner_access_denied session_key=%s "
                "channel_name=%s user_id=%s chat_id=%s",
                request.session_key,
                request.route.channel_name,
                request.route.user_id,
                request.route.chat_id,
            )
            return self._local_responses.create(text="Thread not found.")

        presentation = self._request_context.requester_presentation_for_session(
            session,
            request.route,
        )
        conversation_id, followup_to_task_id = self._resolve_conversation(
            request,
            session,
        )
        resolved_route = self._resolved_route(
            request.route,
            session,
            canonical_user_id=presentation.canonical_user_id,
        )
        if self._transcript.is_web_chat_route(session, resolved_route):
            retry = self._transcript.find_retry_exchange(
                session=session,
                route=resolved_route,
            )
            if retry is not None:
                return self._response_for_retry(retry)
        if self._progress.is_progress_request(
            request.text,
            list(request.attachments) or None,
        ):
            accepted = self._accept_user_message(
                request=request,
                session=session,
                route=resolved_route,
                conversation_id=conversation_id,
            )
            response = await self._progress.request(
                GatewayTaskProgressRequest(
                    session_key=session.session_key,
                    conversation_id=conversation_id,
                    route=resolved_route,
                )
            )
            return self._append_response(
                session=session,
                route=resolved_route,
                response=response,
                accepted=accepted,
            )

        if presentation.canonical_user_id:
            # A newly added person first sees what to set up for themselves.
            held = await self._personal_setup.before_request(presentation.canonical_user_id, session=session)
            if held is not None:
                return self._local_responses.create(text=held)
        active_task_id, active_correlation_id = await self._responses.resolve_target(
            session_key=session.session_key,
            conversation_id=conversation_id,
        )
        active_request = (
            self._requests.get(active_correlation_id) if active_correlation_id else None
        )
        if self._should_steer(active_correlation_id, active_request):
            return await self._submit_steering(
                request=request,
                session=session,
                route=resolved_route,
                conversation_id=conversation_id,
                active_task_id=active_task_id,
                active_correlation_id=active_correlation_id,
                active_request=active_request,
                fallback_followup_to_task_id=followup_to_task_id,
                requester_display_name=presentation.display_name,
                request_timezone=presentation.timezone,
            )
        return await self._submit_new_request(
            request=request,
            session=session,
            route=resolved_route,
            conversation_id=conversation_id,
            followup_to_task_id=followup_to_task_id,
            requester_display_name=presentation.display_name,
            request_timezone=presentation.timezone,
        )

    async def _submit_steering(
        self,
        *,
        request: GatewayInteractiveSubmissionRequest,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        active_task_id: str | None,
        active_correlation_id: str,
        active_request: RequestRecord | None,
        fallback_followup_to_task_id: str | None,
        requester_display_name: str,
        request_timezone: str,
    ) -> GatewayResponse:
        steering_type = (
            TaskSteeringType.INTERRUPT_RESPONSE
            if active_request is not None
            and active_request.status == "waiting_for_steering"
            else TaskSteeringType.PROACTIVE_GUIDANCE
        )
        correlation_id = self._id_generator.new_correlation_id()
        accepted = self._accept_user_message(
            request=request,
            session=session,
            route=route,
            conversation_id=conversation_id,
            request_record=TranscriptRequestRecord(
                correlation_id=correlation_id,
                request_kind="task_steering",
                request_text=request.text,
                followup_to_task_id=active_task_id,
                status="submitted",
            ),
        )
        if accepted is not None and accepted.correlation_id != correlation_id:
            return self._response_for_concurrent_retry(
                session=session,
                route=route,
                accepted=accepted,
            )
        expected_manager_id = self._manager.manager_id
        response = await self._responses.send_task_steering(
            GatewayTaskSteeringRequest(
                text=request.text,
                session_key=session.session_key,
                conversation_id=conversation_id,
                requester_display_name=requester_display_name,
                request_timezone=request_timezone,
                target_task_id=active_task_id,
                target_correlation_id=active_correlation_id,
                steering_type=steering_type,
                attachments=request.attachments,
                replied_message=request.replied_message,
                correlation_id=correlation_id,
                request_already_recorded=accepted is not None,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="interactive",
                    route=route,
                    manager_agent_id=self._manager.manager_id,
                ),
            )
        )
        if self._is_stale_steering_target(
            response,
            expected_manager_id=expected_manager_id,
        ):
            self._requests.update_status(active_correlation_id, "stale")
            self._requests.record(
                correlation_id=correlation_id,
                session_key=session.session_key,
                request_kind="interactive",
                request_text=request.text,
                conversation_id=conversation_id,
                followup_to_task_id=fallback_followup_to_task_id,
                status="pending",
            )
            self._logger.warning(
                "Gateway recovered user input from a stale task-steering target as "
                "a new interactive request. "
                "event=gateway_stale_task_steering_readmitted "
                "correlation_id=%s stale_correlation_id=%s stale_task_id=%s "
                "conversation_id=%s session_key=%s",
                correlation_id,
                active_correlation_id,
                active_task_id or "-",
                conversation_id or "-",
                session.session_key,
            )
            response = await self._submit_new_task(
                request=request,
                session=session,
                route=route,
                conversation_id=conversation_id,
                followup_to_task_id=fallback_followup_to_task_id,
                requester_display_name=requester_display_name,
                request_timezone=request_timezone,
                correlation_id=correlation_id,
                request_already_recorded=True,
            )
        return self._append_response(
            session=session,
            route=route,
            response=response,
            accepted=accepted,
        )

    @staticmethod
    def _is_stale_steering_target(
        response: GatewayResponse,
        *,
        expected_manager_id: str | None,
    ) -> bool:
        envelope = response.raw_envelope
        content = envelope.msg_content if envelope is not None else None
        return bool(
            expected_manager_id
            and envelope is not None
            and envelope.msg_header.from_agent_id == expected_manager_id
            and isinstance(content, TaskSteering)
            and content.status == TaskSteeringStatus.STALE
            and content.status_code
            in {
                TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND,
                TaskSteeringStatusCode.TARGET_TASK_TERMINAL,
            }
        )

    async def _submit_new_request(
        self,
        *,
        request: GatewayInteractiveSubmissionRequest,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        followup_to_task_id: str | None,
        requester_display_name: str,
        request_timezone: str,
    ) -> GatewayResponse:
        correlation_id = self._id_generator.new_correlation_id()
        accepted = self._accept_user_message(
            request=request,
            session=session,
            route=route,
            conversation_id=conversation_id,
            request_record=TranscriptRequestRecord(
                correlation_id=correlation_id,
                request_kind="interactive",
                request_text=request.text,
                followup_to_task_id=followup_to_task_id,
            ),
        )
        if accepted is not None and accepted.correlation_id != correlation_id:
            return self._response_for_concurrent_retry(
                session=session,
                route=route,
                accepted=accepted,
            )
        response = await self._submit_new_task(
            request=request,
            session=session,
            route=route,
            conversation_id=conversation_id,
            followup_to_task_id=followup_to_task_id,
            requester_display_name=requester_display_name,
            request_timezone=request_timezone,
            correlation_id=correlation_id,
            request_already_recorded=accepted is not None,
        )
        return self._append_response(
            session=session,
            route=route,
            response=response,
            accepted=accepted,
        )

    def _accept_user_message(
        self,
        *,
        request: GatewayInteractiveSubmissionRequest,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        request_record: TranscriptRequestRecord | None = None,
    ) -> GatewayTranscriptMessage | None:
        return self._transcript.accept_user_message(
            session=session,
            route=route,
            conversation_id=conversation_id,
            text=request.text,
            attachments=request.attachments,
            request_record=request_record,
        )

    def _response_for_concurrent_retry(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        accepted: GatewayTranscriptMessage,
    ) -> GatewayResponse:
        retry = self._transcript.find_retry_exchange(session=session, route=route)
        return self._response_for_retry(retry or (accepted, None))

    def _response_for_retry(
        self,
        exchange: tuple[GatewayTranscriptMessage, GatewayTranscriptMessage | None],
    ) -> GatewayResponse:
        user_message, reply = exchange
        if reply is not None:
            template = self._local_responses.create(
                text=reply.content,
                message_type=reply.message_type,
                correlation_id=reply.correlation_id or user_message.correlation_id,
                conversation_id=reply.conversation_id,
            )
            return gateway_response_from_transcript(reply, template=template)
        return self._local_responses.create(
            text="Your earlier request was accepted and is still being processed.",
            correlation_id=user_message.correlation_id,
            conversation_id=user_message.conversation_id,
        )

    def _append_response(
        self,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        response: GatewayResponse,
        accepted: GatewayTranscriptMessage | None,
    ) -> GatewayResponse:
        if accepted is not None:
            self._transcript.append_gateway_response(
                session=session,
                route=route,
                response=response,
                reply_to=accepted,
            )
        return response

    def _resolve_session(
        self,
        request: GatewayInteractiveSubmissionRequest,
    ) -> SessionRecord:
        route = request.route
        return self._sessions.resolve_session(
            requested_session_key=request.session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=self._request_context.canonical_user_id(route),
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                thread_id=route.thread_id,
            ),
            enforce_owner_scope=self._request_context.users_enabled,
        )

    def _resolve_conversation(
        self,
        request: GatewayInteractiveSubmissionRequest,
        session: SessionRecord,
    ) -> tuple[str | None, str | None]:
        conversation_id = request.conversation_id or session.conversation_id
        followup_to_task_id = session.last_task_id
        if request.conversation_id is None:
            return conversation_id, followup_to_task_id
        conversation = self._session_repository.get_session_conversation(
            request.conversation_id
        )
        if conversation is None or conversation.session_key != session.session_key:
            raise ValueError(
                f"Conversation {request.conversation_id} is not mapped to "
                f"session {session.session_key}."
            )
        return conversation_id, conversation.last_task_id

    def _should_steer(
        self,
        active_correlation_id: str | None,
        active_request: RequestRecord | None,
    ) -> bool:
        if not active_correlation_id:
            return False
        status = str(getattr(active_request, "status", "") or "")
        if status in {
            REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
            REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
        }:
            # Broker workflows wait independently. A later chat message is a new
            # conversation turn; only the typed broker result resumes the old task.
            return False
        return self._manager.has_pending_response(active_correlation_id) or status == "waiting_for_steering"

    async def _submit_new_task(
        self,
        *,
        request: GatewayInteractiveSubmissionRequest,
        session: SessionRecord,
        route: GatewayRequestRoute,
        conversation_id: str | None,
        followup_to_task_id: str | None,
        requester_display_name: str,
        request_timezone: str,
        correlation_id: str,
        request_already_recorded: bool,
    ) -> GatewayResponse:
        manager_id = self._manager.manager_id
        async with self._manager.response_channel(correlation_id):
            if not request_already_recorded:
                self._requests.record(
                    correlation_id=correlation_id,
                    session_key=session.session_key,
                    request_kind="interactive",
                    request_text=request.text,
                    conversation_id=conversation_id,
                    followup_to_task_id=followup_to_task_id,
                )
            self._log_resolved_request(
                request=request,
                session=session,
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                manager_id=manager_id,
                followup_to_task_id=followup_to_task_id,
            )
            if manager_id is None:
                self._logger.warning(
                    "Gateway cannot publish interactive user request because no "
                    "manager route is currently known. "
                    "event=gateway_user_request_no_manager_route correlation_id=%s "
                    "conversation_id=%s session_key=%s",
                    correlation_id,
                    conversation_id,
                    session.session_key,
                )
                await self._responses.queue_manager_unavailable(correlation_id)
                return await self._responses.await_terminal_response(
                    correlation_id=correlation_id,
                    session_key=session.session_key,
                    query_handler=request.query_handler,
                )
            envelope = MessageEnvelope(
                msg_content=Task(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=request.text.strip(),
                    attachments=list(request.attachments),
                    replied_message=request.replied_message,
                ),
                msg_meta=MessageMeta(
                    from_agent_name=requester_display_name,
                    msg_correlation_id=correlation_id,
                    conversation_id=conversation_id,
                    followup_to_task_id=followup_to_task_id,
                    request_timezone=request_timezone,
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
                    to_routing_path=manager_id,
                    response_routing_key=self._settings.response_route,
                    agent_signature="",
                ),
            )
            self._logger.info(
                "Gateway publishing interactive user request. "
                "event=gateway_user_request_publish correlation_id=%s "
                "conversation_id=%s session_key=%s manager_id=%s routing_key=%s "
                "attachment_count=%d",
                correlation_id,
                conversation_id,
                session.session_key,
                manager_id,
                manager_id,
                len(request.attachments),
            )
            await self._manager.publish_durable(envelope, routing_key=manager_id)
            self._logger.info(
                "Gateway published interactive user request. "
                "event=gateway_user_request_published correlation_id=%s "
                "conversation_id=%s session_key=%s manager_id=%s",
                correlation_id,
                conversation_id,
                session.session_key,
                manager_id,
            )
            return await self._responses.await_terminal_response(
                correlation_id=correlation_id,
                session_key=session.session_key,
                query_handler=request.query_handler,
            )

    def _log_admission_rejection(
        self,
        route: GatewayRequestRoute,
        admission: GatewayAdmissionDecision,
    ) -> None:
        rejection_code = str(admission.rejection_code or "")
        if admission.canonical_user_id:
            # A known person who may not use the team: disabled, or not a
            # member of it (removed).
            self._logger.info(
                "Gateway rejected a known app user before manager admission. "
                "event=gateway_%s_rejected canonical_user_id=%s "
                "status=%s channel_name=%s user_id=%s chat_id=%s thread_id=%s",
                rejection_code,
                admission.canonical_user_id,
                admission.user_status,
                route.channel_name,
                route.user_id,
                route.chat_id,
                route.thread_id,
            )
            return
        self._logger.info(
            "Gateway rejected unregistered app user before manager admission. "
            "event=gateway_unregistered_user_rejected channel_name=%s user_id=%s "
            "chat_id=%s thread_id=%s",
            route.channel_name,
            route.user_id,
            route.chat_id,
            route.thread_id,
        )

    def _log_resolved_request(
        self,
        *,
        request: GatewayInteractiveSubmissionRequest,
        session: SessionRecord,
        conversation_id: str | None,
        correlation_id: str,
        manager_id: str | None,
        followup_to_task_id: str | None,
    ) -> None:
        route = request.route
        self._logger.info(
            "Gateway resolved interactive user request. "
            "event=gateway_user_request_resolved correlation_id=%s "
            "conversation_id=%s session_key=%s manager_id=%s "
            "followup_to_task_id=%s channel_name=%s chat_id=%s thread_id=%s "
            "original_message_id=%s attachment_count=%d attachment_summary=%s",
            correlation_id,
            conversation_id,
            session.session_key,
            manager_id,
            followup_to_task_id,
            route.channel_name or session.channel_name,
            route.chat_id or session.chat_id,
            route.thread_id or session.thread_id,
            route.original_message_id,
            len(request.attachments),
            json.dumps(
                attachment_debug_summary(list(request.attachments) or None),
                sort_keys=True,
            ),
        )

    @staticmethod
    def _resolved_route(
        route: GatewayRequestRoute,
        session: SessionRecord,
        *,
        canonical_user_id: str | None,
    ) -> GatewayRequestRoute:
        return GatewayRequestRoute(
            channel_name=route.channel_name or session.channel_name,
            user_id=route.user_id or session.user_id,
            chat_id=route.chat_id or session.chat_id,
            chat_scope=route.chat_scope,
            thread_id=route.thread_id or session.thread_id,
            original_message_id=route.original_message_id,
            canonical_user_id=canonical_user_id,
        )
