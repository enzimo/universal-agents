"""Narrow gateway application contract consumed by channel-facing code."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol, runtime_checkable

from assistant_gateway.application.access_reviews import (
    AccessReviewDecisionRequestV1,
    AccessReviewDecisionResultV1,
)
from assistant_gateway.application.transient_presentation import (
    GatewayTransientAction,
)

from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.task_v1 import TaskQuery
from id_generation import new_id


GatewayQueryHandler = Callable[[TaskQuery], Awaitable[str] | str]


@dataclass(frozen=True)
class GatewaySession:
    """Application-facing view of a gateway conversation session."""

    session_key: str
    conversation_id: str | None
    session_alias: str | None = None
    last_task_id: str | None = None
    channel_name: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    owner_canonical_user_id: str | None = None
    latest_request_text: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class GatewayApplicationResponse:
    """Application-facing response returned by an interactive gateway use case."""

    text: str
    conversation_id: str | None
    correlation_id: str | None = None
    task_id: str | None = None
    message_type: str | None = None
    message_level: str = "normal"
    attachments: tuple[UserNotificationAttachment, ...] = ()
    transcript_text: str | None = None
    transient_action: GatewayTransientAction | None = None


@dataclass(frozen=True)
class GatewayCommandResult:
    """Result of normalizing and executing one interactive channel command."""

    handled: bool
    session_key: str | None
    text: str | None
    deferred: bool = False
    transient_action: GatewayTransientAction | None = None


@dataclass(frozen=True)
class GatewayScheduledJob:
    """Application-facing scheduled-job summary needed by channel rendering."""

    scheduled_job_id: str
    next_fire_at: str | None
    task_summary: str
    schedule_name: str | None = None
    enabled: bool = True
    paused_until: str | None = None
    session_key: str | None = None


@dataclass
class GatewayDeliveryEvent:
    """Application-owned delivery payload consumed by interactive adapters."""

    text: str
    session_key: str
    channel_name: str | None
    user_id: str | None
    chat_id: str | None
    thread_id: str | None
    conversation_id: str | None
    scheduled_job_id: str | None
    correlation_id: str | None
    original_message_id: str | None
    message_type: str | None = None
    message_level: str = "normal"
    attachments: tuple[UserNotificationAttachment, ...] = ()
    event_id: str = field(default_factory=lambda: new_id("gateway_event"))
    message_id: str | None = None
    session_sequence: int | None = None
    event_sequence: int | None = None
    created_at_utc: str | None = None
    review_url: str | None = None
    # An access review announcement: the waiting request and the choices the
    # chat may offer as buttons (assistant_gateway.application.access_reviews).
    access_request_id: str | None = None
    review_choices: tuple[str, ...] = ()

    def text_with_attachment_paths(self) -> str:
        if not self.attachments:
            return self.text
        lines = [self.text.strip()] if self.text.strip() else []
        lines.append("Attachments:")
        for attachment in self.attachments:
            label = attachment.filename or Path(attachment.path).name
            lines.append(f"- {label}: {attachment.path}")
        return "\n".join(lines)


@runtime_checkable
class GatewayApplication(Protocol):
    """Use cases available to a normalized interactive channel bridge.

    Implementations own all access to gateway configuration, persistence,
    manager transport, and internal response models.
    """

    @property
    def default_session_key(self) -> str:
        """Return the fallback session key for channel requests."""

    def get_active_session(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> GatewaySession | None:
        """Return the active session for one external channel route."""

    def is_registered_user(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> bool:
        """Return whether an external identity maps to an active gateway user."""

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
        """Execute a normalized slash/session command for one channel route."""

    def is_task_progress_status_request(
        self,
        text: str,
        attachments: list[UserTaskAttachment] | None = None,
    ) -> bool:
        """Return whether the normalized input requests out-of-band progress."""

    async def request_task_progress_status(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> GatewayApplicationResponse:
        """Return the current task-progress status for a channel route."""

    async def has_pending_query(self, session_key: str) -> bool:
        """Return whether a valid pending clarification owns the next response."""

    def start_new_session(
        self,
        *,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> GatewaySession:
        """Create and activate a fresh gateway session."""

    async def reply_to_pending_query(
        self,
        *,
        session_key: str,
        response_text: str,
        replied_message: RepliedMessage | None = None,
    ) -> GatewayApplicationResponse:
        """Reply to the pending clarification for a session."""

    async def list_scheduled_jobs(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[GatewayScheduledJob]:
        """List user-visible scheduled jobs for a channel route."""

    def requester_timezone_for_context(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        """Return the authenticated requester's configured IANA timezone."""

    async def release_due_deliveries(self) -> list[str]:
        """Release due deferred deliveries and return rendered summaries."""

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
        """Create a manager-owned scheduled job."""

    async def pause_scheduled_job(
        self,
        schedule_id: str,
        *,
        paused_until: str,
        session_key: str | None = None,
    ) -> bool:
        """Pause one manager-owned scheduled job."""

    async def resume_scheduled_job(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
    ) -> bool:
        """Resume one manager-owned scheduled job."""

    async def disable_scheduled_job(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
    ) -> bool:
        """Disable one manager-owned scheduled job."""

    async def remove_scheduled_job(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
    ) -> bool:
        """Remove one manager-owned scheduled job."""

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
        """Resolve or create the session for a channel route."""

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
        """Register validated attachment metadata with gateway-owned state."""

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
        """Submit a normalized user message to the gateway application."""

    async def decide_access_review(
        self,
        request: AccessReviewDecisionRequestV1,
    ) -> AccessReviewDecisionResultV1:
        """Decide an access review from a chat button as the linked person."""
