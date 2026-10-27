"""Composed channel application over explicit gateway use-case owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from assistant_gateway.application.clarification import GatewayClarificationApplication
from assistant_gateway.application.deferred_delivery import (
    GatewayDeferredDeliveryApplication,
)
from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewayCommandResult,
    GatewayQueryHandler,
    GatewayScheduledJob,
    GatewaySession,
)
from assistant_gateway.application.access_reviews import (
    AccessReviewDecisionRequestV1,
    AccessReviewDecisionResultV1,
)
from assistant_gateway.application.interactive_commands import GatewayCommandApplication
from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.application.interactive_submission import (
    GatewayInteractiveSubmissionRequest,
    GatewayInteractiveSubmissionService,
)
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.schedule_management import (
    GatewayScheduleApplication,
    GatewayScheduleCreateRequest,
)
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.application.task_progress import (
    GatewayTaskProgressRequest,
    GatewayTaskProgressService,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import DeliveryRepository, SessionRepository
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.replied_message_v1 import RepliedMessage


@dataclass(frozen=True)
class GatewayRuntimeApplicationSettings:
    """Stable settings consumed by the channel-facing application."""

    default_session_key: str
    default_owner_canonical_user_id: str


class GatewayAccessReviewDecisions(Protocol):
    """Access review decisions from chat buttons (``GatewayAccessReviewControls``)."""

    async def decide(self, request: AccessReviewDecisionRequestV1) -> AccessReviewDecisionResultV1: ...


def gateway_session_view(session: SessionRecord) -> GatewaySession:
    """Project a persistence-owned session into the application contract."""

    return GatewaySession(
        session_key=session.session_key,
        conversation_id=session.conversation_id,
        session_alias=session.session_alias,
        last_task_id=session.last_task_id,
        channel_name=session.channel_name,
        user_id=session.user_id,
        chat_id=session.chat_id,
        thread_id=session.thread_id,
        owner_canonical_user_id=session.owner_canonical_user_id,
        latest_request_text=None,
        updated_at=session.updated_at,
        parallel_thread=session.parallel_thread,
    )


def gateway_response_view(response: GatewayResponse) -> GatewayApplicationResponse:
    """Project an internal response without reflection or fallback discovery."""

    message_level = str(response.message_level or "").strip()
    if not message_level:
        message_level = str(
            GatewayDeliveryPresentation.message_level(response.raw_envelope.msg_content)
            or "normal"
        )
    return GatewayApplicationResponse(
        text=response.text,
        conversation_id=response.conversation_id,
        correlation_id=response.correlation_id,
        task_id=response.task_id,
        message_type=response.message_type,
        message_level=message_level,
        attachments=response.attachments,
        transcript_text=response.transcript_text,
        transient_action=response.transient_action,
    )


class GatewayRuntimeApplication:
    """Channel-facing application composed only from narrow use-case owners."""

    def __init__(
        self,
        *,
        settings: GatewayRuntimeApplicationSettings,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        delivery_repository: DeliveryRepository,
        request_context: GatewayRequestContextService,
        manager: GatewayManagerClient,
        commands: GatewayCommandApplication,
        progress: GatewayTaskProgressService,
        clarification: GatewayClarificationApplication,
        schedules: GatewayScheduleApplication,
        deferred_delivery: GatewayDeferredDeliveryApplication,
        submission: GatewayInteractiveSubmissionService,
        access_reviews: GatewayAccessReviewDecisions,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._session_repository = session_repository
        self._delivery_repository = delivery_repository
        self._request_context = request_context
        self._manager = manager
        self._commands = commands
        self._progress = progress
        self._clarification = clarification
        self._schedules = schedules
        self._deferred_delivery = deferred_delivery
        self._submission = submission
        self._access_reviews = access_reviews

    @property
    def default_session_key(self) -> str:
        return self._settings.default_session_key

    def get_active_session(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> GatewaySession | None:
        session = self._session_repository.get_active_session_for_channel(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return gateway_session_view(session) if session is not None else None

    def is_registered_user(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> bool:
        return (
            self._request_context.resolve_canonical_user(
                GatewayRequestRoute(
                    channel_name=channel_name,
                    user_id=external_user_id,
                    chat_scope=chat_scope,
                )
            )
            is not None
        )

    async def execute_interactive_command(
        self,
        *,
        raw_text: str,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
        current_session_key: str | None = None,
        canonical_user_id: str | None = None,
        help_text: str | None = None,
        enable_debug_commands: bool = True,
        enable_reply_command: bool = True,
        enable_channel_binding_commands: bool = True,
        query_handler: GatewayQueryHandler | None = None,
        correlation_id: str | None = None,
    ) -> GatewayCommandResult:
        if current_session_key is None:
            active = self._session_repository.get_active_session_for_channel(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            current_session_key = active.session_key if active is not None else None
        return await self._commands.execute(
            GatewayCommandRequest(
                raw_text=raw_text,
                current_session_key=current_session_key,
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                canonical_user_id=canonical_user_id,
                help_text=help_text,
                enable_debug_commands=enable_debug_commands,
                enable_reply_command=enable_reply_command,
                enable_channel_binding_commands=enable_channel_binding_commands,
                query_handler=query_handler,
                correlation_id=correlation_id,
            )
        )

    def is_task_progress_status_request(
        self,
        text: str,
        attachments: list[UserTaskAttachment] | None = None,
    ) -> bool:
        return self._progress.is_progress_request(text, attachments)

    async def request_task_progress_status(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> GatewayApplicationResponse:
        response = await self._progress.request(
            GatewayTaskProgressRequest(
                session_key=session_key,
                route=GatewayRequestRoute(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                ),
            )
        )
        return gateway_response_view(response)

    async def waiting_question_id(self, session_key: str) -> str | None:
        return await self._clarification.waiting_question_id(session_key)

    def start_new_session(
        self,
        *,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
        parallel_thread: bool = False,
    ) -> GatewaySession:
        """Start a new chat thread; ``parallel_thread`` marks one Web Chat launched as parallel."""

        route = GatewayRequestRoute(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            canonical_user_id=canonical_user_id,
        )
        session = self._sessions.start_new_session(
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=self._request_context.canonical_user_id(route),
            route=GatewaySessionRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
            parallel_thread=parallel_thread,
        )
        return gateway_session_view(session)

    async def reply_to_pending_query(
        self,
        *,
        session_key: str,
        task_query_id: str,
        response_text: str,
        replied_message: RepliedMessage | None = None,
    ) -> GatewayApplicationResponse:
        return gateway_response_view(
            await self._clarification.reply(
                session_key=session_key,
                task_query_id=task_query_id,
                response_text=response_text,
                replied_message=replied_message,
            )
        )

    async def list_scheduled_jobs(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[GatewayScheduledJob]:
        schedules = await self._schedules.list(
            session_key=session_key,
            route=GatewayRequestRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
        )
        result: list[GatewayScheduledJob] = []
        for schedule in schedules:
            context = (
                schedule.payload_meta.gateway_context if schedule.payload_meta else None
            )
            scheduled_job_id = (
                context.scheduled_job_id
                if context is not None and context.scheduled_job_id
                else schedule.schedule_id
            )
            result.append(
                GatewayScheduledJob(
                    scheduled_job_id=scheduled_job_id,
                    next_fire_at=schedule.next_fire_at,
                    task_summary=(
                        schedule.payload.problem_statement or ""
                    ).splitlines()[-1],
                    schedule_name=schedule.schedule_name,
                    enabled=schedule.enabled,
                    paused_until=schedule.paused_until,
                    session_key=context.session_key if context is not None else None,
                )
            )
        return result

    def requester_timezone_for_context(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._request_context.requester_presentation(
            GatewayRequestRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            )
        ).timezone

    async def release_due_deliveries(self) -> list[str]:
        return await self._deferred_delivery.release_due()

    async def add_cron_job(
        self,
        *,
        session_key: str | None,
        name: str,
        instruction: str,
        interval_seconds: int | None = None,
        cron_expression: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        original_message_id: str | None = None,
    ) -> str:
        return await self._schedules.add(
            GatewayScheduleCreateRequest(
                session_key=session_key,
                name=name,
                instruction=instruction,
                interval_seconds=interval_seconds,
                cron_expression=cron_expression,
                route=GatewayRequestRoute(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    original_message_id=original_message_id,
                ),
            )
        )

    async def pause_scheduled_job(
        self,
        schedule_id: str,
        *,
        paused_until: str,
        session_key: str | None = None,
    ) -> bool:
        return await self._schedules.pause(
            schedule_id,
            paused_until=paused_until,
            session_key=session_key,
        )

    async def resume_scheduled_job(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
    ) -> bool:
        return await self._schedules.resume(schedule_id, session_key=session_key)

    async def disable_scheduled_job(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
    ) -> bool:
        return await self._schedules.disable(schedule_id, session_key=session_key)

    async def remove_scheduled_job(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
    ) -> bool:
        return await self._schedules.remove(schedule_id, session_key=session_key)

    def resolve_session(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> GatewaySession:
        route = GatewayRequestRoute(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            canonical_user_id=canonical_user_id,
        )
        session = self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=self._request_context.canonical_user_id(route),
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
            enforce_owner_scope=self._request_context.users_enabled,
        )
        return gateway_session_view(session)

    def register_inbound_attachment(
        self,
        attachment: UserTaskAttachment,
        *,
        session_key: str,
        conversation_id: str | None,
        correlation_id: str | None = None,
        expires_at: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> None:
        local_path = attachment.local_path()
        if local_path is None:
            raise ValueError(
                "Inbound attachments must include a validated local storage reference."
            )
        self._delivery_repository.upsert_attachment(
            attachment_id=attachment.attachment_id,
            session_key=session_key,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            kind=attachment.kind,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            local_path=local_path,
            expires_at=expires_at,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    async def send_user_message(
        self,
        text: str,
        *,
        session_key: str | None = None,
        conversation_id: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        original_message_id: str | None = None,
        canonical_user_id: str | None = None,
        attachments: list[UserTaskAttachment] | None = None,
        query_handler: GatewayQueryHandler | None = None,
        replied_message: RepliedMessage | None = None,
    ) -> GatewayApplicationResponse:
        response = await self._submission.submit(
            GatewayInteractiveSubmissionRequest(
                text=text,
                session_key=session_key,
                conversation_id=conversation_id,
                route=GatewayRequestRoute(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    original_message_id=original_message_id,
                    canonical_user_id=canonical_user_id,
                ),
                query_handler=query_handler,
                attachments=tuple(attachments or ()),
                replied_message=replied_message,
            )
        )
        return gateway_response_view(response)

    async def decide_access_review(
        self,
        request: AccessReviewDecisionRequestV1,
    ) -> AccessReviewDecisionResultV1:
        return await self._access_reviews.decide(request)
