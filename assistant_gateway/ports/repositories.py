"""Domain-scoped repository ports for gateway-owned state."""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from assistant_gateway.domain.delivery import (
    AttachmentRecord,
    DeliveryRecord,
    ScheduledTargetRecord,
)
from assistant_gateway.domain.identity import (
    GatewayTeamPersonRecord,
    GatewayUserAppearanceRecord,
    GatewayUserChannelBindingRecord,
    GatewayUserLinkCodeRecord,
    GatewayUserRecord,
    GatewayUserSignInRevocation,
    GatewayUserTeamMembershipRecord,
    GatewayUserWebInviteRecord,
    GatewayUserWebTokenRecord,
)
from assistant_gateway.domain.request_state import (
    CredentialCollectionRecord,
    PendingQueryAdded,
    HeldGuidanceRecord,
    PendingQueryRecord,
    RequestRecord,
    ToolSetupRecord,
)
from assistant_gateway.domain.sessions import (
    SessionConversationListRecord,
    SessionConversationRecord,
    SessionListRecord,
    WebChatThreadRecord,
    SessionRecord,
    SessionSearchRecord,
)
from assistant_gateway.domain.transcript import (
    GatewayTranscriptMessage,
    TranscriptAppendRequest,
    TranscriptStreamState,
)


@runtime_checkable
class SessionRepository(Protocol):
    """Persist gateway sessions, conversation mappings, and active bindings."""

    def get_session(self, session_key: str) -> SessionRecord | None: ...

    def get_session_by_conversation_id(
        self,
        conversation_id: str,
    ) -> SessionRecord | None: ...

    def get_session_conversation(
        self,
        conversation_id: str,
    ) -> SessionConversationRecord | None: ...

    def register_conversation_session(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> None: ...

    def list_session_conversations(
        self,
        session_key: str,
        *,
        limit: int = 20,
    ) -> list[SessionConversationListRecord]: ...

    def list_sessions(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> list[SessionRecord]: ...

    def count_sessions(
        self,
        *,
        status: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> int: ...

    def list_sessions_with_preview(
        self,
        *,
        limit: int = 7,
        offset: int = 0,
        status: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> list[SessionListRecord]: ...

    def list_web_chat_threads(
        self,
        *,
        owner_canonical_user_id: str | None,
        channel_name: str,
        chat_id: str,
        limit: int,
    ) -> list[WebChatThreadRecord]: ...

    def search_sessions(
        self,
        query_text: str,
        *,
        limit: int = 20,
        owner_canonical_user_id: str | None = None,
    ) -> list[SessionSearchRecord]: ...

    def get_session_by_alias(self, session_alias: str) -> SessionRecord | None: ...

    def resolve_session_reference(
        self,
        reference: str,
        *,
        owner_canonical_user_id: str | None = None,
    ) -> SessionRecord | None: ...

    def create_session(
        self,
        *,
        session_key: str,
        conversation_id: str,
        manager_agent_id: str | None,
        session_alias: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        status: str = "active",
        owner_canonical_user_id: str | None = None,
        parallel_thread: bool = False,
    ) -> SessionRecord: ...

    def upsert_session(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
        last_task_id: str | None,
        manager_agent_id: str | None,
        session_alias: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        status: str | None = None,
        owner_canonical_user_id: str | None = None,
        parallel_thread: bool | None = None,
    ) -> None:
        """Save a session and make it its chat's current one (the person's own chat traffic only).

        Fields left ``None`` keep their saved values.
        """

    def upsert_session_without_switching_chat(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
        last_task_id: str | None,
        manager_agent_id: str | None,
        session_alias: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        status: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> None:
        """Save a session without changing its chat's current one (replies, task lineage)."""

    def set_session_alias(
        self,
        session_key: str,
        session_alias: str | None,
    ) -> None: ...

    def set_session_status(self, session_key: str, status: str) -> bool: ...

    def mark_session_parallel(self, session_key: str, conversation_id: str) -> bool:
        """Mark only the current conversation, without changing routing or history."""
        ...

    def set_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        session_key: str,
        thread_id: str | None = None,
    ) -> None: ...

    def get_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> SessionRecord | None: ...


@runtime_checkable
class RequestRepository(Protocol):
    """Persist request lifecycle and pending-clarification state."""

    def record_request(
        self,
        *,
        correlation_id: str,
        session_key: str,
        request_kind: str,
        request_text: str,
        conversation_id: str | None,
        followup_to_task_id: str | None,
        status: str = "pending",
        owner_canonical_user_id: str | None = None,
        active_task_id: str | None = None,
        run_state: str | None = None,
    ) -> None:
        """Insert or update a request row.

        ``run_state="running"`` admits the thread's request and raises
        ``ThreadRequestRunning`` when another request already runs in the
        same session conversation. An update keeps the row's task and run
        state when the caller gives none.
        """

    def get_request(self, correlation_id: str) -> RequestRecord | None: ...

    def get_running_request(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> RequestRecord | None:
        """The request running in this session conversation, if any."""

    def finish_request(self, correlation_id: str) -> bool:
        """Mark a running request finished; False when it was not running."""

    def hold_guidance(self, record: HeldGuidanceRecord) -> None:
        """Keep guidance until its running request confirms it."""

    def list_held_guidance(self) -> list[HeldGuidanceRecord]:
        """Every held guidance message, oldest first."""

    def get_held_guidance(self, correlation_id: str) -> HeldGuidanceRecord | None: ...

    def release_held_guidance(self, correlation_id: str, *, status: str) -> bool:
        """Stop holding guidance and record how it ended; False when already released."""

    def update_request_status(self, correlation_id: str, status: str) -> None: ...

    def update_request_coordination(
        self,
        correlation_id: str,
        *,
        status: str,
        active_task_id: str,
    ) -> None: ...

    def list_requests_for_session(
        self,
        session_key: str,
        *,
        limit: int = 5,
    ) -> list[RequestRecord]: ...

    # Agent questions to people, by question id (``gateway_questions``).
    def add_pending_query(
        self,
        *,
        session_key: str,
        correlation_id: str,
        task_query_id: str,
        parent_task_id: str | None,
        conversation_id: str | None,
        query_text: str,
        query_payload_json: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> PendingQueryAdded: ...

    def get_pending_query(self, task_query_id: str) -> PendingQueryRecord | None: ...

    def list_open_pending_queries(self, session_key: str) -> list[PendingQueryRecord]: ...

    def mark_pending_query_shown(self, task_query_id: str) -> bool: ...

    def clear_pending_query_shown(self, task_query_id: str) -> None:
        """A failed delivery leaves an open question waiting to be shown."""

    def close_pending_query(self, task_query_id: str, *, status: str) -> bool: ...

    def reopen_answered_pending_query(self, task_query_id: str) -> bool: ...

    def close_open_pending_queries_for_task(
        self, *, correlation_id: str, parent_task_id: str, status: str
    ) -> list[PendingQueryRecord]: ...

    def expire_pending_queries(self, *, cutoff_iso: str) -> int: ...


@runtime_checkable
class CredentialCollectionRepository(Protocol):
    """Keep open broker form watches until their result has been published."""

    def record_credential_collection(self, record: CredentialCollectionRecord) -> None: ...

    def list_credential_collections(self) -> list[CredentialCollectionRecord]: ...

    def remove_credential_collection(self, credential_request_id: str) -> None: ...


@runtime_checkable
class ToolSetupRepository(Protocol):
    """Persist the setup pages tasks wait on (``ToolSetupRecord``)."""

    def record_tool_setup(self, record: ToolSetupRecord) -> None: ...

    def get_tool_setup(self, setup_request_id: str) -> ToolSetupRecord | None: ...

    def finish_tool_setup(self, setup_request_id: str, *, status: str) -> bool:
        """Mark a waiting setup finished; False when it was already finished or is unknown."""
        ...

    def list_tool_setups(
        self,
        *,
        status: str | None = None,
        person_id: str | None = None,
        tool_id: str | None = None,
        parent_task_id: str | None = None,
    ) -> list[ToolSetupRecord]: ...


@runtime_checkable
class DeliveryRepository(Protocol):
    """Persist deferred delivery, attachment, and proactive target state."""

    def add_deferred_delivery(
        self,
        *,
        session_key: str,
        origin_kind: str,
        payload: dict[str, object],
        rendered_text: str,
        available_after: str,
        correlation_id: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> str: ...

    def get_due_deliveries(
        self,
        now_iso: str | None = None,
    ) -> list[DeliveryRecord]: ...

    def mark_delivery_delivered(self, delivery_id: str) -> None: ...

    def list_pending_deliveries(
        self,
        session_key: str | None = None,
    ) -> list[DeliveryRecord]: ...

    def upsert_attachment(
        self,
        *,
        attachment_id: str,
        session_key: str,
        conversation_id: str | None,
        correlation_id: str | None,
        filename: str,
        content_type: str,
        kind: str,
        size_bytes: int,
        sha256: str,
        local_path: str,
        nats_bucket: str | None = None,
        nats_object_name: str | None = None,
        expires_at: str | None = None,
        owner_canonical_user_id: str | None = None,
    ) -> None: ...

    def get_attachment(self, attachment_id: str) -> AttachmentRecord | None: ...

    def delete_uploads_created_before(self, cutoff: str, *, uploads_dir: str, limit: int) -> tuple[str, ...]:
        """Delete old records inside ``uploads_dir`` only; return their file paths."""
        ...

    def update_attachment_nats_ref(
        self,
        *,
        attachment_id: str,
        nats_bucket: str,
        nats_object_name: str,
    ) -> AttachmentRecord | None: ...

    def upsert_scheduled_target(
        self,
        *,
        scheduled_job_id: str,
        session_key: str,
        conversation_id: str | None,
        owner_canonical_user_id: str | None = None,
    ) -> None: ...

    def get_scheduled_target(
        self,
        scheduled_job_id: str,
    ) -> ScheduledTargetRecord | None: ...

    def get_scheduled_target_by_conversation_id(
        self,
        conversation_id: str,
    ) -> ScheduledTargetRecord | None: ...

    def remove_scheduled_target(self, scheduled_job_id: str) -> bool: ...


@runtime_checkable
class TranscriptRepository(Protocol):
    """Persist the gateway-owned presentation transcript and durable cursor."""

    def append_transcript_message(
        self,
        request: TranscriptAppendRequest,
    ) -> GatewayTranscriptMessage: ...

    def get_transcript_message(
        self,
        message_id: str,
    ) -> GatewayTranscriptMessage | None: ...

    def get_transcript_message_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> GatewayTranscriptMessage | None: ...

    def get_transcript_reply(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        reply_to_message_id: str,
    ) -> GatewayTranscriptMessage | None: ...

    def list_transcript_messages(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        before_session_sequence: int | None,
        limit: int,
    ) -> list[GatewayTranscriptMessage]: ...

    def list_transcript_events(
        self,
        *,
        owner_canonical_user_id: str,
        after_event_sequence: int,
        limit: int,
    ) -> list[GatewayTranscriptMessage]: ...

    def get_transcript_stream_state(self) -> TranscriptStreamState: ...


@runtime_checkable
class IdentityRepository(Protocol):
    """Persist canonical users and their gateway-owned credentials/bindings."""

    def create_gateway_user(
        self,
        *,
        display_name: str,
        timezone: str,
        canonical_user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "active",
    ) -> GatewayUserRecord: ...

    def get_gateway_user(
        self,
        canonical_user_id: str,
    ) -> GatewayUserRecord | None: ...

    def list_gateway_users(
        self,
        *,
        limit: int = 100,
    ) -> list[GatewayUserRecord]: ...

    def set_gateway_user_status(
        self,
        canonical_user_id: str,
        status: str,
    ) -> GatewayUserRecord | None: ...

    def update_gateway_user_last_seen(
        self,
        canonical_user_id: str,
        *,
        seen_at_utc: str | None = None,
    ) -> None: ...

    def set_gateway_user_default_channel(
        self,
        *,
        canonical_user_id: str,
        channel_name: str | None,
        chat_id: str | None,
        thread_id: str | None,
    ) -> GatewayUserRecord | None: ...

    def get_gateway_user_appearance(
        self,
        canonical_user_id: str,
    ) -> GatewayUserAppearanceRecord | None: ...

    def upsert_gateway_user_appearance(
        self,
        *,
        canonical_user_id: str,
        appearance: dict[str, Any],
    ) -> GatewayUserAppearanceRecord: ...

    def delete_gateway_user_appearance(
        self,
        canonical_user_id: str,
    ) -> bool: ...

    def get_active_channel_binding(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> GatewayUserChannelBindingRecord | None: ...

    def list_channel_bindings_for_user(
        self,
        canonical_user_id: str,
        *,
        status: str | None = None,
    ) -> list[GatewayUserChannelBindingRecord]: ...

    def create_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> GatewayUserChannelBindingRecord: ...

    def replace_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> GatewayUserChannelBindingRecord: ...

    def create_web_user_token(
        self,
        *,
        token_hash: str,
        canonical_user_id: str,
        label: str = "",
        status: str = "active",
        expires_at_utc: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GatewayUserWebTokenRecord: ...

    def resolve_web_user_token(
        self,
        *,
        token_hash: str,
        now_utc: str | None = None,
    ) -> GatewayUserWebTokenRecord | None: ...

    def activate_web_user_token(
        self,
        *,
        token_id: str,
        canonical_user_id: str,
        expires_at_utc: str | None,
        now_utc: str | None = None,
    ) -> GatewayUserWebTokenRecord | None: ...

    def revoke_web_user_token(
        self,
        token_id: str,
        *,
        canonical_user_id: str | None = None,
        required_status: str | None = None,
    ) -> bool: ...

    def create_web_user_invite(
        self,
        *,
        invite_hash: str,
        canonical_user_id: str,
        label: str,
        expires_at_utc: str,
        created_by_canonical_user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> GatewayUserWebInviteRecord: ...

    def consume_web_user_invite(
        self,
        *,
        invite_hash: str,
        team_id: str,
        now_utc: str | None = None,
    ) -> GatewayUserWebInviteRecord | None: ...

    def create_gateway_user_with_web_invite(
        self,
        *,
        display_name: str,
        timezone: str,
        user_metadata: dict[str, Any] | None,
        team_id: str,
        invite_hash: str,
        invite_label: str,
        invite_expires_at_utc: str,
        created_by_canonical_user_id: str,
        invite_metadata: dict[str, Any] | None = None,
    ) -> tuple[
        GatewayUserRecord,
        GatewayUserTeamMembershipRecord,
        GatewayUserWebInviteRecord,
    ]: ...

    def create_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str = "member",
        status: str = "active",
    ) -> GatewayUserTeamMembershipRecord: ...

    def list_team_memberships(
        self,
        canonical_user_id: str,
        *,
        status: str | None = None,
    ) -> list[GatewayUserTeamMembershipRecord]: ...

    def change_team_membership_role(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> GatewayUserTeamMembershipRecord | None: ...

    def remove_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> GatewayUserTeamMembershipRecord | None: ...

    def revoke_gateway_user_sign_ins(
        self,
        canonical_user_id: str,
        *,
        now_utc: str,
    ) -> GatewayUserSignInRevocation: ...

    def list_team_members(
        self,
        team_id: str,
    ) -> list[GatewayUserTeamMembershipRecord]: ...

    def list_team_people(
        self,
        team_id: str,
    ) -> list[GatewayTeamPersonRecord]: ...

    def create_link_code(
        self,
        *,
        link_code_hash: str,
        canonical_user_id: str,
        expires_at_utc: str,
        created_by_channel: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GatewayUserLinkCodeRecord: ...

    def consume_link_code(
        self,
        *,
        link_code_hash: str,
        now_utc: str | None = None,
    ) -> GatewayUserLinkCodeRecord | None: ...

    def record_gateway_user_event(
        self,
        *,
        event_type: str,
        canonical_user_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str: ...

    def gateway_user_event_types(self, *, canonical_user_id: str, event_types: Sequence[str]) -> set[str]: ...


class IdentityUnitOfWork(Protocol):
    """One explicit transaction over identity-owned tables.

    Callers must invoke :meth:`commit`; leaving the context without a commit
    rolls back all writes.
    """

    identity: IdentityRepository

    def __enter__(self) -> IdentityUnitOfWork: ...

    def commit(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class IdentityUnitOfWorkFactory(Protocol):
    """Create identity-scoped transactions without exposing the store adapter."""

    def identity_unit_of_work(self) -> IdentityUnitOfWork: ...


class RequestUnitOfWork(Protocol):
    """One explicit transaction over request lifecycle and session lineage."""

    request: RequestRepository
    session: SessionRepository

    def __enter__(self) -> RequestUnitOfWork: ...

    def commit(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class RequestUnitOfWorkFactory(Protocol):
    """Create request-coordination transactions."""

    def request_unit_of_work(self) -> RequestUnitOfWork: ...


class DeliveryUnitOfWork(Protocol):
    """One explicit transaction over delivery, request, and session state."""

    delivery: DeliveryRepository
    request: RequestRepository
    session: SessionRepository

    def __enter__(self) -> DeliveryUnitOfWork: ...

    def commit(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class DeliveryUnitOfWorkFactory(Protocol):
    """Create delivery-coordination transactions."""

    def delivery_unit_of_work(self) -> DeliveryUnitOfWork: ...


class TranscriptUnitOfWork(Protocol):
    """One transaction over transcript, request, and attachment state."""

    transcript: TranscriptRepository
    request: RequestRepository
    delivery: DeliveryRepository

    def __enter__(self) -> TranscriptUnitOfWork: ...

    def commit(self) -> None: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class TranscriptUnitOfWorkFactory(Protocol):
    """Create transcript-scoped transactions without exposing infrastructure."""

    def transcript_unit_of_work(self) -> TranscriptUnitOfWork: ...
