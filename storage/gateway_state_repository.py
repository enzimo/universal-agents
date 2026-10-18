"""Backend-neutral repository boundary for gateway session and delivery state."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import json
import logging
import sqlite3
from contextvars import ContextVar, Token
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence, cast

from assistant_gateway.domain.delivery import (
    AttachmentRecord,
    DeliveryRecord,
    ScheduledTargetRecord,
)
from assistant_gateway.domain.identity import (
    GatewayPersonRecordsDeleted,
    GatewayTeamPersonRecord,
    GatewayUserAppearanceRecord,
    GatewayUserChannelBindingRecord,
    GatewayUserLinkCodeRecord,
    GatewayUserRecord,
    GatewayUserSignInRevocation,
    GatewayUserTeamMembershipRecord,
    GatewayUserWebInviteRecord,
    GatewayUserWebTokenRecord,
    TeamRoleHolderRequired,
)
from assistant_gateway.domain.request_state import PendingQueryRecord, RequestRecord, ToolSetupRecord
from assistant_gateway.domain.sessions import (
    SessionConversationListRecord,
    SessionConversationRecord,
    SessionListRecord,
    WebChatThreadRecord,
    SessionRecord,
    SessionSearchRecord,
)
from assistant_gateway.domain.transcript import (
    TRANSCRIPT_SCHEMA_VERSION,
    GatewayTranscriptMessage,
    TranscriptAppendRequest,
    TranscriptAttachmentReference,
    TranscriptStreamState,
)
from assistant_gateway.ports.repositories import (
    DeliveryRepository,
    DeliveryUnitOfWork,
    IdentityRepository,
    IdentityUnitOfWork,
    RequestRepository,
    RequestUnitOfWork,
    SessionRepository,
    ToolSetupRepository,
    TranscriptRepository,
    TranscriptUnitOfWork,
)
from id_generation import new_id

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite, ensure_sqlite_column


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _holder_required(
    held_role: str, groups: tuple[tuple[str, ...], ...]
) -> TeamRoleHolderRequired:
    """The refusal for a membership change the last-holder guard stopped: the first group the person holds."""

    held = str(held_role).strip().lower()
    return TeamRoleHolderRequired(next((group for group in groups if held in group), (held,)))


def _json_dumps(value: Optional[dict[str, Any]]) -> str:
    return json.dumps(value or {}, sort_keys=True)


def _transcript_attachment_json(
    references: tuple[TranscriptAttachmentReference, ...],
) -> str:
    return json.dumps(
        [asdict(reference) for reference in references],
        separators=(",", ":"),
        sort_keys=True,
    )


def _transcript_message_from_row(row: Any) -> GatewayTranscriptMessage:
    raw_references = json.loads(str(row["attachment_references_json"] or "[]"))
    references = tuple(
        TranscriptAttachmentReference(**dict(reference)) for reference in raw_references
    )
    return GatewayTranscriptMessage(
        schema_version=int(row["schema_version"]),
        message_id=str(row["message_id"]),
        session_key=str(row["session_key"]),
        conversation_id=row["conversation_id"],
        owner_canonical_user_id=str(row["owner_canonical_user_id"]),
        channel_name=str(row["channel_name"]),
        chat_id=str(row["chat_id"]),
        session_sequence=int(row["session_sequence"]),
        event_sequence=int(row["event_sequence"]),
        role=str(row["role"]),
        content=str(row["content"]),
        message_type=str(row["message_type"]),
        message_level=str(row["message_level"]),
        correlation_id=row["correlation_id"],
        source_kind=str(row["source_kind"]),
        source_id=str(row["source_id"]),
        idempotency_key=str(row["idempotency_key"]),
        attachment_references=references,
        delivery_status=str(row["delivery_status"]),
        reply_to_message_id=row["reply_to_message_id"],
        created_at_utc=str(row["created_at_utc"]),
    )


_TRANSCRIPT_MESSAGE_COLUMNS = """
    schema_version, message_id, session_key, conversation_id,
    owner_canonical_user_id, channel_name, chat_id, session_sequence,
    event_sequence, role, content, message_type, message_level,
    correlation_id, source_kind, source_id, idempotency_key,
    attachment_references_json, delivery_status, reply_to_message_id,
    created_at_utc
"""


class GatewayStateRepository(
    SessionRepository,
    RequestRepository,
    ToolSetupRepository,
    DeliveryRepository,
    IdentityRepository,
    TranscriptRepository,
    Protocol,
):
    """Infrastructure-only compatibility aggregate over domain repository ports."""

    def identity_unit_of_work(self) -> IdentityUnitOfWork: ...

    def request_unit_of_work(self) -> RequestUnitOfWork: ...

    def delivery_unit_of_work(self) -> DeliveryUnitOfWork: ...

    def transcript_unit_of_work(self) -> TranscriptUnitOfWork: ...

    def get_session(self, session_key: str) -> Optional[SessionRecord]: ...

    def get_session_by_conversation_id(
        self, conversation_id: str
    ) -> Optional[SessionRecord]: ...

    def get_session_conversation(
        self, conversation_id: str
    ) -> Optional[SessionConversationRecord]: ...

    def register_conversation_session(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
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
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionRecord]: ...

    def count_sessions(
        self,
        *,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> int: ...

    def list_sessions_with_preview(
        self,
        *,
        limit: int = 7,
        offset: int = 0,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
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
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionSearchRecord]: ...

    def get_session_by_alias(self, session_alias: str) -> Optional[SessionRecord]: ...

    def resolve_session_reference(
        self,
        reference: str,
        *,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Optional[SessionRecord]: ...

    def create_session(
        self,
        *,
        session_key: str,
        session_alias: Optional[str] = None,
        conversation_id: str,
        manager_agent_id: Optional[str],
        channel_name: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        status: str = "active",
        owner_canonical_user_id: Optional[str] = None,
    ) -> SessionRecord: ...

    def upsert_session(
        self,
        *,
        session_key: str,
        session_alias: Optional[str] = None,
        conversation_id: Optional[str],
        last_task_id: Optional[str],
        manager_agent_id: Optional[str],
        channel_name: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None: ...

    def set_session_alias(
        self, session_key: str, session_alias: Optional[str]
    ) -> None: ...

    def set_session_status(self, session_key: str, status: str) -> bool: ...

    def set_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
        session_key: str,
    ) -> None: ...

    def get_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
    ) -> Optional[SessionRecord]: ...

    def record_request(
        self,
        *,
        correlation_id: str,
        session_key: str,
        request_kind: str,
        request_text: str,
        conversation_id: Optional[str],
        followup_to_task_id: Optional[str],
        status: str = "pending",
        owner_canonical_user_id: Optional[str] = None,
    ) -> None: ...

    def get_request(self, correlation_id: str) -> Optional[RequestRecord]: ...

    def update_request_status(self, correlation_id: str, status: str) -> None: ...

    def list_requests_for_session(
        self, session_key: str, *, limit: int = 5
    ) -> list[RequestRecord]: ...

    def add_deferred_delivery(
        self,
        *,
        session_key: str,
        origin_kind: str,
        payload: dict[str, Any],
        rendered_text: str,
        available_after: str,
        correlation_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> str: ...

    def get_due_deliveries(
        self, now_iso: Optional[str] = None
    ) -> list[DeliveryRecord]: ...

    def mark_delivery_delivered(self, delivery_id: str) -> None: ...

    def list_pending_deliveries(
        self, session_key: Optional[str] = None
    ) -> list[DeliveryRecord]: ...

    def upsert_attachment(
        self,
        *,
        attachment_id: str,
        session_key: str,
        conversation_id: Optional[str],
        correlation_id: Optional[str],
        filename: str,
        content_type: str,
        kind: str,
        size_bytes: int,
        sha256: str,
        local_path: str,
        nats_bucket: Optional[str] = None,
        nats_object_name: Optional[str] = None,
        expires_at: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None: ...

    def get_attachment(self, attachment_id: str) -> Optional[AttachmentRecord]: ...

    def update_attachment_nats_ref(
        self,
        *,
        attachment_id: str,
        nats_bucket: str,
        nats_object_name: str,
    ) -> Optional[AttachmentRecord]: ...

    def upsert_pending_query(
        self,
        *,
        session_key: str,
        correlation_id: str,
        task_query_id: str,
        parent_task_id: Optional[str],
        conversation_id: Optional[str],
        query_text: str,
        query_payload_json: Optional[str] = None,
        status: str = "pending",
        owner_canonical_user_id: Optional[str] = None,
    ) -> None: ...

    def get_pending_query(self, session_key: str) -> Optional[PendingQueryRecord]: ...

    def clear_pending_query(
        self, session_key: str, *, status: str = "completed"
    ) -> None: ...

    def expire_pending_queries(
        self, *, cutoff_iso: str, status: str = "expired"
    ) -> int: ...

    def upsert_scheduled_target(
        self,
        *,
        scheduled_job_id: str,
        session_key: str,
        conversation_id: Optional[str],
        owner_canonical_user_id: Optional[str] = None,
    ) -> None: ...

    def get_scheduled_target(
        self, scheduled_job_id: str
    ) -> Optional[ScheduledTargetRecord]: ...

    def get_scheduled_target_by_conversation_id(
        self, conversation_id: str
    ) -> Optional[ScheduledTargetRecord]: ...

    def remove_scheduled_target(self, scheduled_job_id: str) -> bool: ...

    def create_gateway_user(
        self,
        *,
        canonical_user_id: Optional[str] = None,
        display_name: str,
        timezone: str,
        metadata: Optional[dict[str, Any]] = None,
        status: str = "active",
    ) -> GatewayUserRecord: ...

    def get_gateway_user(
        self, canonical_user_id: str
    ) -> Optional[GatewayUserRecord]: ...

    def list_gateway_users(
        self,
        *,
        limit: int = 100,
    ) -> list[GatewayUserRecord]: ...

    def set_gateway_user_status(
        self, canonical_user_id: str, status: str
    ) -> Optional[GatewayUserRecord]: ...

    def update_gateway_user_last_seen(
        self, canonical_user_id: str, *, seen_at_utc: Optional[str] = None
    ) -> None: ...

    def set_gateway_user_default_channel(
        self,
        *,
        canonical_user_id: str,
        channel_name: Optional[str],
        chat_id: Optional[str],
        thread_id: Optional[str],
    ) -> Optional[GatewayUserRecord]: ...

    def get_active_channel_binding(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> Optional[GatewayUserChannelBindingRecord]: ...

    def list_channel_bindings_for_user(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserChannelBindingRecord]: ...

    def list_active_channel_bindings(self) -> list[GatewayUserChannelBindingRecord]: ...

    def create_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserChannelBindingRecord: ...

    def replace_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserChannelBindingRecord: ...

    def create_web_user_token(
        self,
        *,
        token_hash: str,
        canonical_user_id: str,
        label: str = "",
        status: str = "active",
        expires_at_utc: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserWebTokenRecord: ...

    def resolve_web_user_token(
        self,
        *,
        token_hash: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebTokenRecord]: ...

    def activate_web_user_token(
        self,
        *,
        token_id: str,
        canonical_user_id: str,
        expires_at_utc: Optional[str],
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebTokenRecord]: ...

    def revoke_web_user_token(
        self,
        token_id: str,
        *,
        canonical_user_id: Optional[str] = None,
        required_status: Optional[str] = None,
    ) -> bool: ...

    def create_web_user_invite(
        self,
        *,
        invite_hash: str,
        canonical_user_id: str,
        label: str,
        expires_at_utc: str,
        created_by_canonical_user_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserWebInviteRecord: ...

    def create_gateway_user_with_web_invite(
        self,
        *,
        display_name: str,
        timezone: str,
        user_metadata: Optional[dict[str, Any]],
        team_id: str,
        invite_hash: str,
        invite_label: str,
        invite_expires_at_utc: str,
        created_by_canonical_user_id: str,
        invite_metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[
        GatewayUserRecord,
        GatewayUserTeamMembershipRecord,
        GatewayUserWebInviteRecord,
    ]: ...

    def consume_web_user_invite(
        self,
        *,
        invite_hash: str,
        team_id: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebInviteRecord]: ...

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
        status: Optional[str] = None,
    ) -> list[GatewayUserTeamMembershipRecord]: ...

    def change_team_membership_role(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> Optional[GatewayUserTeamMembershipRecord]: ...

    def remove_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> Optional[GatewayUserTeamMembershipRecord]: ...

    def revoke_gateway_user_sign_ins(
        self,
        canonical_user_id: str,
        *,
        now_utc: str,
    ) -> GatewayUserSignInRevocation: ...

    def forget_person_records(self, canonical_user_id: str) -> GatewayPersonRecordsDeleted: ...

    def list_team_members(
        self,
        team_id: str,
    ) -> list[GatewayUserTeamMembershipRecord]: ...

    def list_team_people(
        self,
        team_id: str,
    ) -> list[GatewayTeamPersonRecord]: ...

    def list_web_user_tokens(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserWebTokenRecord]: ...

    def create_link_code(
        self,
        *,
        link_code_hash: str,
        canonical_user_id: str,
        expires_at_utc: str,
        created_by_channel: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserLinkCodeRecord: ...

    def consume_link_code(
        self,
        *,
        link_code_hash: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserLinkCodeRecord]: ...

    def record_gateway_user_event(
        self,
        *,
        event_type: str,
        canonical_user_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> str: ...


class _BorrowedConnection:
    """Prevent nested repository calls from committing an outer transaction."""

    def __init__(self, connection: Any):
        self._connection = connection

    def __enter__(self) -> _BorrowedConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        return None

    def execute(self, query: str, params: object | None = None) -> Any:
        if params is None:
            return self._connection.execute(query)
        return self._connection.execute(query, params)

    def commit(self) -> None:
        """The owning unit of work commits the shared connection."""

    def rollback(self) -> None:
        """The owning unit of work rolls back the shared connection."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ActiveRepositoryTransaction:
    def __init__(self, connection: Any):
        self.connection = connection
        self.rollback_only = False


class _ScopedRepositoryView:
    """Restrict a unit of work to one declared repository port."""

    def __init__(
        self,
        repository: object,
        protocol: type[object],
        *,
        scope_name: str,
    ):
        self._repository = repository
        self._scope_name = scope_name
        self._allowed_methods = frozenset(
            name
            for name, value in vars(protocol).items()
            if callable(value) and not name.startswith("_")
        )
        for name in self._allowed_methods:
            setattr(self, name, getattr(repository, name))

    def __getattr__(self, name: str) -> object:
        if name not in self._allowed_methods:
            raise AttributeError(
                f"{self._scope_name} unit of work does not expose repository "
                f"method {name!r}."
            )
        return getattr(self._repository, name)


class _RepositoryTransaction:
    """Context-local transaction shared by nested repository method calls."""

    def __init__(self, repository: SqliteGatewayStateRepository):
        self._repository = repository
        self._active: _ActiveRepositoryTransaction | None = None
        self._token: Token[_ActiveRepositoryTransaction | None] | None = None
        self._owns_connection = False
        self._commit_requested = False

    def __enter__(self) -> _RepositoryTransaction:
        current = self._repository._active_transaction.get()
        if current is not None:
            self._active = current
            return self
        self._active = _ActiveRepositoryTransaction(self._repository._open_connection())
        self._token = self._repository._active_transaction.set(self._active)
        self._owns_connection = True
        return self

    def commit(self) -> None:
        self._commit_requested = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        if self._active is None:
            raise RuntimeError("Gateway repository transaction was not entered.")
        if exc_type is not None:
            self._active.rollback_only = True
        if not self._owns_connection:
            return None

        connection = self._active.connection
        try:
            if self._active.rollback_only or not self._commit_requested:
                connection.rollback()
            else:
                connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            if self._token is not None:
                self._repository._active_transaction.reset(self._token)
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        return None


class _RepositoryUnitOfWork:
    """Shared rollback-unless-committed unit-of-work behavior."""

    def __init__(self, repository: SqliteGatewayStateRepository) -> None:
        self._transaction = _RepositoryTransaction(repository)

    def __enter__(self) -> _RepositoryUnitOfWork:
        self._transaction.__enter__()
        return self

    def commit(self) -> None:
        self._transaction.commit()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        self._transaction.__exit__(exc_type, exc, traceback)


class _IdentityRepositoryUnitOfWork(_RepositoryUnitOfWork):
    """Storage adapter for the identity-scoped application transaction."""

    def __init__(self, repository: SqliteGatewayStateRepository):
        super().__init__(repository)
        self.identity = cast(
            IdentityRepository,
            _ScopedRepositoryView(
                repository,
                IdentityRepository,
                scope_name="Identity",
            ),
        )

    def __enter__(self) -> _IdentityRepositoryUnitOfWork:
        super().__enter__()
        return self


class _RequestRepositoryUnitOfWork(_RepositoryUnitOfWork):
    """Storage adapter for request lifecycle and session-lineage changes."""

    def __init__(self, repository: SqliteGatewayStateRepository):
        super().__init__(repository)
        self.request = cast(
            RequestRepository,
            _ScopedRepositoryView(
                repository,
                RequestRepository,
                scope_name="Request",
            ),
        )
        self.session = cast(
            SessionRepository,
            _ScopedRepositoryView(
                repository,
                SessionRepository,
                scope_name="Request",
            ),
        )

    def __enter__(self) -> _RequestRepositoryUnitOfWork:
        super().__enter__()
        return self


class _DeliveryRepositoryUnitOfWork(_RepositoryUnitOfWork):
    """Storage adapter for delivery, request, and session coordination."""

    def __init__(self, repository: SqliteGatewayStateRepository):
        super().__init__(repository)
        self.delivery = cast(
            DeliveryRepository,
            _ScopedRepositoryView(
                repository,
                DeliveryRepository,
                scope_name="Delivery",
            ),
        )
        self.request = cast(
            RequestRepository,
            _ScopedRepositoryView(
                repository,
                RequestRepository,
                scope_name="Delivery",
            ),
        )
        self.session = cast(
            SessionRepository,
            _ScopedRepositoryView(
                repository,
                SessionRepository,
                scope_name="Delivery",
            ),
        )

    def __enter__(self) -> _DeliveryRepositoryUnitOfWork:
        super().__enter__()
        return self


class _TranscriptRepositoryUnitOfWork(_RepositoryUnitOfWork):
    """Storage adapter for transcript, request, and attachment changes."""

    def __init__(self, repository: SqliteGatewayStateRepository):
        super().__init__(repository)
        self.transcript = cast(
            TranscriptRepository,
            _ScopedRepositoryView(
                repository,
                TranscriptRepository,
                scope_name="Transcript",
            ),
        )
        self.request = cast(
            RequestRepository,
            _ScopedRepositoryView(
                repository,
                RequestRepository,
                scope_name="Transcript",
            ),
        )
        self.delivery = cast(
            DeliveryRepository,
            _ScopedRepositoryView(
                repository,
                DeliveryRepository,
                scope_name="Transcript",
            ),
        )

    def __enter__(self) -> _TranscriptRepositoryUnitOfWork:
        super().__enter__()
        return self


class _RelationalGatewayStateRepository:
    """Shared record operations for gateway session and delivery state."""

    _dialect: SqlDialect


    def _connect(self) -> sqlite3.Connection | _BorrowedConnection:
        active = self._active_transaction.get()
        if active is not None:
            return _BorrowedConnection(active.connection)
        return self._open_connection()

    def identity_unit_of_work(self) -> IdentityUnitOfWork:
        return _IdentityRepositoryUnitOfWork(self)

    def request_unit_of_work(self) -> RequestUnitOfWork:
        return _RequestRepositoryUnitOfWork(self)

    def delivery_unit_of_work(self) -> DeliveryUnitOfWork:
        return _DeliveryRepositoryUnitOfWork(self)

    def transcript_unit_of_work(self) -> TranscriptUnitOfWork:
        return _TranscriptRepositoryUnitOfWork(self)


    def _init_schema(self) -> None:
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(self._db_path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_sessions (
                    session_key TEXT PRIMARY KEY,
                    session_alias TEXT UNIQUE,
                    conversation_id TEXT,
                    last_task_id TEXT,
                    manager_agent_id TEXT,
                    channel_name TEXT,
                    user_id TEXT,
                    chat_id TEXT,
                    thread_id TEXT,
                    owner_canonical_user_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn, "gateway_sessions", "owner_canonical_user_id", "TEXT"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_session_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    last_task_id TEXT,
                    owner_canonical_user_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn, "gateway_session_conversations", "owner_canonical_user_id", "TEXT"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_requests (
                    correlation_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    request_kind TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    conversation_id TEXT,
                    followup_to_task_id TEXT,
                    active_task_id TEXT,
                    owner_canonical_user_id TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn, "gateway_requests", "owner_canonical_user_id", "TEXT"
            )
            self._ensure_column(conn, "gateway_requests", "active_task_id", "TEXT")
            # Setup pages tasks wait on (assistant_gateway.application.tool_setup).
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_tool_setups (
                    setup_request_id TEXT PRIMARY KEY,
                    parent_task_id TEXT NOT NULL,
                    person_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    owner_kind TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    conversation_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_tool_setups_status
                ON gateway_tool_setups(status, person_id, tool_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    correlation_id TEXT,
                    session_key TEXT NOT NULL,
                    origin_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    rendered_text TEXT NOT NULL,
                    available_after TEXT NOT NULL,
                    owner_canonical_user_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                )
                """
            )
            self._ensure_column(
                conn, "gateway_deliveries", "owner_canonical_user_id", "TEXT"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_attachments (
                    attachment_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    conversation_id TEXT,
                    correlation_id TEXT,
                    owner_canonical_user_id TEXT,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    nats_bucket TEXT,
                    nats_object_name TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            self._ensure_column(
                conn, "gateway_attachments", "owner_canonical_user_id", "TEXT"
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_attachments_session
                ON gateway_attachments(session_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_pending_queries (
                    session_key TEXT PRIMARY KEY,
                    correlation_id TEXT NOT NULL,
                    task_query_id TEXT NOT NULL,
                    parent_task_id TEXT,
                    conversation_id TEXT,
                    owner_canonical_user_id TEXT,
                    query_text TEXT NOT NULL,
                    query_payload_json TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn, "gateway_pending_queries", "owner_canonical_user_id", "TEXT"
            )
            self._ensure_column(
                conn, "gateway_pending_queries", "query_payload_json", "TEXT"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_transcript_streams (
                    stream_key TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL UNIQUE,
                    last_event_sequence INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            transcript_stream_now = utc_now_iso()
            conn.execute(
                f"""
                INSERT INTO gateway_transcript_streams (
                    stream_key, stream_id, last_event_sequence,
                    created_at_utc, updated_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, 0, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(stream_key) DO NOTHING
                """,
                (
                    "gateway",
                    new_id("web_stream"),
                    transcript_stream_now,
                    transcript_stream_now,
                ),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_transcript_session_sequences (
                    session_key TEXT PRIMARY KEY,
                    last_session_sequence INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_transcript_messages (
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    message_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    conversation_id TEXT,
                    owner_canonical_user_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    session_sequence INTEGER NOT NULL CHECK (session_sequence > 0),
                    event_sequence INTEGER NOT NULL UNIQUE CHECK (event_sequence > 0),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    message_level TEXT NOT NULL,
                    correlation_id TEXT,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    attachment_references_json TEXT NOT NULL DEFAULT '[]',
                    delivery_status TEXT NOT NULL,
                    reply_to_message_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(session_key, session_sequence)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_transcript_owner_events
                ON gateway_transcript_messages(
                    owner_canonical_user_id, event_sequence ASC
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_transcript_session_messages
                ON gateway_transcript_messages(
                    owner_canonical_user_id, session_key, session_sequence DESC
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_transcript_conversation
                ON gateway_transcript_messages(conversation_id, session_sequence ASC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_transcript_reply
                ON gateway_transcript_messages(reply_to_message_id, session_sequence ASC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_channel_sessions (
                    channel_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL DEFAULT '',
                    session_key TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_name, user_id, chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_scheduled_targets (
                    scheduled_job_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    conversation_id TEXT,
                    owner_canonical_user_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn, "gateway_scheduled_targets", "owner_canonical_user_id", "TEXT"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_users (
                    canonical_user_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    default_channel_name TEXT,
                    default_chat_id TEXT,
                    default_thread_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    last_seen_at_utc TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_appearance_preferences (
                    canonical_user_id TEXT PRIMARY KEY,
                    appearance_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_channel_bindings (
                    binding_id TEXT PRIMARY KEY,
                    canonical_user_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    chat_scope TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    last_seen_at_utc TEXT,
                    replaced_at_utc TEXT,
                    replaced_by_binding_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_user_channel_bindings_active
                ON gateway_user_channel_bindings(channel_name, external_user_id, chat_scope)
                WHERE status = 'active'
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_user_channel_bindings_user
                ON gateway_user_channel_bindings(canonical_user_id, status, updated_at_utc DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_web_tokens (
                    token_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    canonical_user_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at_utc TEXT NOT NULL,
                    last_used_at_utc TEXT,
                    expires_at_utc TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_user_web_tokens_user
                ON gateway_user_web_tokens(canonical_user_id, status, created_at_utc DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_web_invites (
                    invite_id TEXT PRIMARY KEY,
                    invite_hash TEXT NOT NULL UNIQUE,
                    canonical_user_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL,
                    consumed_at_utc TEXT,
                    created_by_canonical_user_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_user_web_invites_user
                ON gateway_user_web_invites(canonical_user_id, created_at_utc DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_team_memberships (
                    canonical_user_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (canonical_user_id, team_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_user_team_memberships_team
                ON gateway_user_team_memberships(team_id, status, canonical_user_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_link_codes (
                    link_code_hash TEXT PRIMARY KEY,
                    canonical_user_id TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL,
                    consumed_at_utc TEXT,
                    created_at_utc TEXT NOT NULL,
                    created_by_channel TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_user_link_codes_user
                ON gateway_user_link_codes(canonical_user_id, created_at_utc DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_user_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    canonical_user_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_user_events_user
                ON gateway_user_events(canonical_user_id, created_at_utc DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_session_conversations_session
                ON gateway_session_conversations(session_key, updated_at DESC, conversation_id ASC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_requests_session
                ON gateway_requests(session_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_requests_conversation
                ON gateway_requests(conversation_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_deliveries_due
                ON gateway_deliveries(status, available_after ASC, created_at ASC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_scheduled_targets_conversation
                ON gateway_scheduled_targets(conversation_id, updated_at DESC)
                """
            )
            conn.commit()

    def get_session(self, session_key: str) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                       channel_name, user_id, chat_id, thread_id, status, created_at, updated_at,
                       owner_canonical_user_id
                FROM gateway_sessions
                WHERE session_key = {self._dialect.bind}
                """,
                (session_key,),
            ).fetchone()
        if not row:
            return None
        return SessionRecord(**dict(row))

    def get_session_by_conversation_id(
        self, conversation_id: str
    ) -> Optional[SessionRecord]:
        with self._connect() as conn:
            mapped = conn.execute(
                f"""
                SELECT session_key
                FROM gateway_session_conversations
                WHERE conversation_id = {self._dialect.bind}
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if mapped:
                row = conn.execute(
                    f"""
                    SELECT session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                           channel_name, user_id, chat_id, thread_id, status, created_at, updated_at,
                           owner_canonical_user_id
                    FROM gateway_sessions
                    WHERE session_key = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (mapped["session_key"],),
                ).fetchone()
                if row:
                    return SessionRecord(**dict(row))
            row = conn.execute(
                f"""
                SELECT session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                       channel_name, user_id, chat_id, thread_id, status, created_at, updated_at,
                       owner_canonical_user_id
                FROM gateway_sessions
                WHERE conversation_id = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return SessionRecord(**dict(row))

    def get_session_conversation(
        self, conversation_id: str
    ) -> Optional[SessionConversationRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT session_key, conversation_id, last_task_id, created_at, updated_at, owner_canonical_user_id
                FROM gateway_session_conversations
                WHERE conversation_id = {self._dialect.bind}
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return SessionConversationRecord(**dict(row))

    def register_conversation_session(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        now = utc_now_iso()
        owner = owner_canonical_user_id
        if owner is None:
            session = self.get_session(session_key)
            owner = session.owner_canonical_user_id if session else None
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_session_conversations (
                    session_key, conversation_id, last_task_id, owner_canonical_user_id, created_at, updated_at
                )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(conversation_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    last_task_id = COALESCE(excluded.last_task_id, gateway_session_conversations.last_task_id),
                    owner_canonical_user_id = COALESCE(
                        excluded.owner_canonical_user_id,
                        gateway_session_conversations.owner_canonical_user_id
                    ),
                    updated_at = excluded.updated_at
                """,
                (session_key, conversation_id, last_task_id, owner, now, now),
            )
            conn.commit()

    def list_session_conversations(
        self,
        session_key: str,
        *,
        limit: int = 20,
    ) -> list[SessionConversationListRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    c.session_key,
                    c.conversation_id,
                    c.last_task_id,
                    c.created_at,
                    c.updated_at,
                    c.owner_canonical_user_id,
                    (
                        SELECT r.request_text
                        FROM gateway_requests r
                        WHERE r.conversation_id = c.conversation_id
                        ORDER BY r.created_at DESC
                        LIMIT 1
                    ) AS latest_request_text
                FROM gateway_session_conversations c
                WHERE c.session_key = {self._dialect.bind}
                ORDER BY c.updated_at DESC, c.conversation_id ASC
                LIMIT {self._dialect.bind}
                """,
                (session_key, limit),
            ).fetchall()
        return [SessionConversationListRecord(**dict(row)) for row in rows]

    def list_sessions(
        self,
        *,
        limit: int = 50,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionRecord]:
        query = """
            SELECT session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                   channel_name, user_id, chat_id, thread_id, status, created_at, updated_at,
                   owner_canonical_user_id
            FROM gateway_sessions
        """
        params: list[Any] = []
        where_clauses: list[str] = []
        if status is not None:
            where_clauses.append(f"status = {self._dialect.bind}")
            params.append(status)
        if owner_canonical_user_id is not None:
            where_clauses.append(f"owner_canonical_user_id = {self._dialect.bind}")
            params.append(owner_canonical_user_id)
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += f" ORDER BY updated_at DESC LIMIT {self._dialect.bind}"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [SessionRecord(**dict(row)) for row in rows]

    def count_sessions(
        self,
        *,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM gateway_sessions"
        params: list[Any] = []
        where_clauses: list[str] = []
        if status is not None:
            where_clauses.append(f"status = {self._dialect.bind}")
            params.append(status)
        if owner_canonical_user_id is not None:
            where_clauses.append(f"owner_canonical_user_id = {self._dialect.bind}")
            params.append(owner_canonical_user_id)
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return int(row[0]) if row else 0

    def list_sessions_with_preview(
        self,
        *,
        limit: int = 7,
        offset: int = 0,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionListRecord]:
        query = """
            SELECT
                s.session_key,
                s.session_alias,
                s.conversation_id,
                s.last_task_id,
                s.manager_agent_id,
                s.channel_name,
                s.user_id,
                s.chat_id,
                s.thread_id,
                s.status,
                s.created_at,
                s.updated_at,
                s.owner_canonical_user_id,
                (
                    SELECT r.request_text
                    FROM gateway_requests r
                    WHERE r.session_key = s.session_key
                    ORDER BY r.created_at DESC
                    LIMIT 1
                ) AS latest_request_text
            FROM gateway_sessions s
        """
        params: list[Any] = []
        where_clauses: list[str] = []
        if status is not None:
            where_clauses.append(f"s.status = {self._dialect.bind}")
            params.append(status)
        if owner_canonical_user_id is not None:
            where_clauses.append(f"s.owner_canonical_user_id = {self._dialect.bind}")
            params.append(owner_canonical_user_id)
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += f" ORDER BY s.updated_at DESC LIMIT {self._dialect.bind} OFFSET {self._dialect.bind}"
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [SessionListRecord(**dict(row)) for row in rows]

    def list_web_chat_threads(
        self,
        *,
        owner_canonical_user_id: str | None,
        channel_name: str,
        chat_id: str,
        limit: int,
    ) -> list[WebChatThreadRecord]:
        """A person's active threads on one chat, newest first, with what names them.

        Filters by channel and chat in SQL, so threads on other channels
        never crowd Web Chat threads out of the limit. A thread is named by
        its first ordinary message (``WebChatThreadRecord``): the person's
        earliest message that is not a slash command, so later messages
        never rename it. The command prefix is bound, not written into the
        SQL, because PostgreSQL drivers read a literal percent sign as a
        placeholder.
        """

        bind = self._dialect.bind
        first_ordinary = f"""
            FROM gateway_transcript_messages t
            WHERE t.session_key = s.session_key
              AND t.role = 'user'
              AND LTRIM(t.content) NOT LIKE {bind}
              AND (TRIM(t.content) <> '' OR t.attachment_references_json <> '[]')
            ORDER BY t.session_sequence
            LIMIT 1
        """
        query = f"""
            SELECT
                s.session_key,
                s.session_alias,
                s.conversation_id,
                s.owner_canonical_user_id,
                s.updated_at,
                (SELECT t.content {first_ordinary}) AS first_message_text,
                (SELECT t.attachment_references_json {first_ordinary}) AS first_message_attachments_json,
                EXISTS (
                    SELECT 1 FROM gateway_transcript_messages m WHERE m.session_key = s.session_key
                ) AS has_messages
            FROM gateway_sessions s
            WHERE s.status = 'active'
              AND s.channel_name = {bind}
              AND s.chat_id = {bind}
              {"AND s.owner_canonical_user_id = " + bind if owner_canonical_user_id is not None else ""}
            ORDER BY s.updated_at DESC
            LIMIT {bind}
        """
        command = "/%"
        params: list[Any] = [command, command, channel_name, chat_id]
        if owner_canonical_user_id is not None:
            params.append(owner_canonical_user_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        records = []
        for row in rows:
            values = dict(row)
            values["has_messages"] = bool(values["has_messages"])
            records.append(WebChatThreadRecord(**values))
        return records

    def search_sessions(
        self,
        query_text: str,
        *,
        limit: int = 20,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionSearchRecord]:
        like_query = f"%{query_text.lower()}%"
        owner_filter = ""
        params: list[Any] = [
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
            like_query,
        ]
        if owner_canonical_user_id is not None:
            owner_filter = f"AND s.owner_canonical_user_id = {self._dialect.bind}"
            params.append(owner_canonical_user_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    s.session_key,
                    s.session_alias,
                    s.conversation_id,
                    s.last_task_id,
                    s.manager_agent_id,
                    s.channel_name,
                    s.user_id,
                    s.chat_id,
                    s.thread_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    s.owner_canonical_user_id,
                    (
                        SELECT r.request_text
                        FROM gateway_requests r
                        WHERE r.session_key = s.session_key
                          AND lower(r.request_text) LIKE {self._dialect.bind}
                        ORDER BY r.created_at DESC
                        LIMIT 1
                    ) AS matched_request_text
                FROM gateway_sessions s
                WHERE (
                    lower(s.session_key) LIKE {self._dialect.bind}
                    OR lower(COALESCE(s.session_alias, '')) LIKE {self._dialect.bind}
                    OR lower(COALESCE(s.conversation_id, '')) LIKE {self._dialect.bind}
                    OR lower(COALESCE(s.channel_name, '')) LIKE {self._dialect.bind}
                    OR lower(COALESCE(s.user_id, '')) LIKE {self._dialect.bind}
                    OR lower(COALESCE(s.chat_id, '')) LIKE {self._dialect.bind}
                    OR EXISTS (
                        SELECT 1
                        FROM gateway_requests r
                        WHERE r.session_key = s.session_key
                          AND lower(r.request_text) LIKE {self._dialect.bind}
                    )
                )
                    {owner_filter}
                ORDER BY s.updated_at DESC
                LIMIT {self._dialect.bind}
                """,
                tuple(params),
            ).fetchall()
        return [SessionSearchRecord(**dict(row)) for row in rows]

    def get_session_by_alias(self, session_alias: str) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                       channel_name, user_id, chat_id, thread_id, status, created_at, updated_at,
                       owner_canonical_user_id
                FROM gateway_sessions
                WHERE lower(session_alias) = lower({self._dialect.bind})
                LIMIT 1
                """,
                (session_alias,),
            ).fetchone()
        if not row:
            return None
        return SessionRecord(**dict(row))

    def resolve_session_reference(
        self,
        reference: str,
        *,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Optional[SessionRecord]:
        session = self.get_session(reference) or self.get_session_by_alias(reference)
        if session is None:
            return None
        if (
            owner_canonical_user_id is not None
            and session.owner_canonical_user_id != owner_canonical_user_id
        ):
            return None
        return session

    def create_session(
        self,
        *,
        session_key: str,
        session_alias: Optional[str] = None,
        conversation_id: str,
        manager_agent_id: Optional[str],
        channel_name: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        status: str = "active",
        owner_canonical_user_id: Optional[str] = None,
    ) -> SessionRecord:
        with _RepositoryTransaction(self) as transaction:
            session = self._create_session(
                session_key=session_key,
                session_alias=session_alias,
                conversation_id=conversation_id,
                manager_agent_id=manager_agent_id,
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                status=status,
                owner_canonical_user_id=owner_canonical_user_id,
            )
            transaction.commit()
            return session

    def _create_session(
        self,
        *,
        session_key: str,
        session_alias: Optional[str],
        conversation_id: str,
        manager_agent_id: Optional[str],
        channel_name: Optional[str],
        user_id: Optional[str],
        chat_id: Optional[str],
        thread_id: Optional[str],
        status: str,
        owner_canonical_user_id: Optional[str],
    ) -> SessionRecord:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_sessions
                (
                    session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                    channel_name, user_id, chat_id, thread_id, owner_canonical_user_id,
                    status, created_at, updated_at
                )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(session_key) DO UPDATE SET
                    session_alias = excluded.session_alias,
                    conversation_id = excluded.conversation_id,
                    last_task_id = NULL,
                    manager_agent_id = excluded.manager_agent_id,
                    channel_name = excluded.channel_name,
                    user_id = excluded.user_id,
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    session_alias,
                    conversation_id,
                    manager_agent_id,
                    channel_name,
                    user_id,
                    chat_id,
                    thread_id,
                    owner_canonical_user_id,
                    status,
                    now,
                    now,
                ),
            )
            conn.commit()
        if channel_name and user_id and chat_id:
            self.set_active_session_for_channel(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
            )
        self.register_conversation_session(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=None,
            owner_canonical_user_id=owner_canonical_user_id,
        )
        return self.get_session(session_key)  # type: ignore[return-value]

    def upsert_session(
        self,
        *,
        session_key: str,
        session_alias: Optional[str] = None,
        conversation_id: Optional[str],
        last_task_id: Optional[str],
        manager_agent_id: Optional[str],
        channel_name: Optional[str] = None,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        with _RepositoryTransaction(self) as transaction:
            self._upsert_session(
                session_key=session_key,
                session_alias=session_alias,
                conversation_id=conversation_id,
                last_task_id=last_task_id,
                manager_agent_id=manager_agent_id,
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                status=status,
                owner_canonical_user_id=owner_canonical_user_id,
            )
            transaction.commit()

    def _upsert_session(
        self,
        *,
        session_key: str,
        session_alias: Optional[str],
        conversation_id: Optional[str],
        last_task_id: Optional[str],
        manager_agent_id: Optional[str],
        channel_name: Optional[str],
        user_id: Optional[str],
        chat_id: Optional[str],
        thread_id: Optional[str],
        status: Optional[str],
        owner_canonical_user_id: Optional[str],
    ) -> None:
        existing = self.get_session(session_key)
        updated_at = utc_now_iso()
        created_at = existing.created_at if existing else updated_at
        resolved_owner = (
            owner_canonical_user_id
            if owner_canonical_user_id is not None
            else (existing.owner_canonical_user_id if existing else None)
        )
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_sessions
                (
                    session_key, session_alias, conversation_id, last_task_id, manager_agent_id,
                    channel_name, user_id, chat_id, thread_id, owner_canonical_user_id,
                    status, created_at, updated_at
                )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(session_key) DO UPDATE SET
                    session_alias = excluded.session_alias,
                    conversation_id = excluded.conversation_id,
                    last_task_id = excluded.last_task_id,
                    manager_agent_id = excluded.manager_agent_id,
                    channel_name = excluded.channel_name,
                    user_id = excluded.user_id,
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    session_alias
                    if session_alias is not None
                    else (existing.session_alias if existing else None),
                    conversation_id
                    if conversation_id is not None
                    else (existing.conversation_id if existing else None),
                    last_task_id
                    if last_task_id is not None
                    else (existing.last_task_id if existing else None),
                    manager_agent_id
                    if manager_agent_id is not None
                    else (existing.manager_agent_id if existing else None),
                    channel_name
                    if channel_name is not None
                    else (existing.channel_name if existing else None),
                    user_id
                    if user_id is not None
                    else (existing.user_id if existing else None),
                    chat_id
                    if chat_id is not None
                    else (existing.chat_id if existing else None),
                    thread_id
                    if thread_id is not None
                    else (existing.thread_id if existing else None),
                    resolved_owner,
                    status
                    if status is not None
                    else (existing.status if existing else "active"),
                    created_at,
                    updated_at,
                ),
            )
            conn.commit()
        resolved_channel_name = (
            channel_name
            if channel_name is not None
            else (existing.channel_name if existing else None)
        )
        resolved_user_id = (
            user_id if user_id is not None else (existing.user_id if existing else None)
        )
        resolved_chat_id = (
            chat_id if chat_id is not None else (existing.chat_id if existing else None)
        )
        resolved_thread_id = (
            thread_id
            if thread_id is not None
            else (existing.thread_id if existing else None)
        )
        if resolved_channel_name and resolved_user_id and resolved_chat_id:
            self.set_active_session_for_channel(
                channel_name=resolved_channel_name,
                user_id=resolved_user_id,
                chat_id=resolved_chat_id,
                thread_id=resolved_thread_id,
                session_key=session_key,
            )
        resolved_conversation_id = (
            conversation_id
            if conversation_id is not None
            else (existing.conversation_id if existing else None)
        )
        if resolved_conversation_id:
            self.register_conversation_session(
                session_key=session_key,
                conversation_id=resolved_conversation_id,
                last_task_id=last_task_id
                if last_task_id is not None
                else (existing.last_task_id if existing else None),
                owner_canonical_user_id=resolved_owner,
            )

    def set_session_alias(self, session_key: str, session_alias: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_sessions
                SET session_alias = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE session_key = {self._dialect.bind}
                """,
                (session_alias, utc_now_iso(), session_key),
            )
            conn.commit()

    def set_session_status(self, session_key: str, status: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_sessions
                SET status = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE session_key = {self._dialect.bind}
                """,
                (status, utc_now_iso(), session_key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
        session_key: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_channel_sessions (channel_name, user_id, chat_id, thread_id, session_key, updated_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(channel_name, user_id, chat_id, thread_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    updated_at = excluded.updated_at
                """,
                (
                    channel_name,
                    user_id,
                    chat_id,
                    thread_id or "",
                    session_key,
                    utc_now_iso(),
                ),
            )
            conn.commit()

    def get_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
    ) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT session_key
                FROM gateway_channel_sessions
                WHERE channel_name = {self._dialect.bind} AND user_id = {self._dialect.bind} AND chat_id = {self._dialect.bind} AND thread_id = {self._dialect.bind}
                """,
                (channel_name, user_id, chat_id, thread_id or ""),
            ).fetchone()
        if not row:
            return None
        return self.get_session(row["session_key"])

    def record_request(
        self,
        *,
        correlation_id: str,
        session_key: str,
        request_kind: str,
        request_text: str,
        conversation_id: Optional[str],
        followup_to_task_id: Optional[str],
        status: str = "pending",
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        created_at = utc_now_iso()
        owner = owner_canonical_user_id
        if owner is None:
            session = self.get_session(session_key)
            owner = session.owner_canonical_user_id if session else None
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_requests
                (
                    correlation_id, session_key, request_kind, request_text, conversation_id,
                    followup_to_task_id, owner_canonical_user_id, created_at, status
                )
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(correlation_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    request_kind = excluded.request_kind,
                    request_text = excluded.request_text,
                    conversation_id = excluded.conversation_id,
                    followup_to_task_id = excluded.followup_to_task_id,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    created_at = excluded.created_at,
                    status = excluded.status
                """,
                (
                    correlation_id,
                    session_key,
                    request_kind,
                    request_text,
                    conversation_id,
                    followup_to_task_id,
                    owner,
                    created_at,
                    status,
                ),
            )
            conn.commit()

    def get_request(self, correlation_id: str) -> Optional[RequestRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT correlation_id, session_key, request_kind, request_text, conversation_id,
                       followup_to_task_id, created_at, status, owner_canonical_user_id,
                       active_task_id
                FROM gateway_requests
                WHERE correlation_id = {self._dialect.bind}
                """,
                (correlation_id,),
            ).fetchone()
        if not row:
            return None
        return RequestRecord(**dict(row))

    def update_request_status(self, correlation_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"UPDATE gateway_requests SET status = {self._dialect.bind} WHERE correlation_id = {self._dialect.bind}",
                (status, correlation_id),
            )
            conn.commit()

    def update_request_coordination(
        self,
        correlation_id: str,
        *,
        status: str,
        active_task_id: str,
    ) -> None:
        """Atomically bind a gateway request to its current runtime task and state."""

        normalized_task_id = str(active_task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("active_task_id must not be empty")
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_requests
                   SET status = {self._dialect.bind}, active_task_id = {self._dialect.bind}
                 WHERE correlation_id = {self._dialect.bind}
                """,
                (status, normalized_task_id, correlation_id),
            )
            conn.commit()

    def record_tool_setup(self, record: ToolSetupRecord) -> None:
        bind = self._dialect.bind
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_tool_setups
                (
                    setup_request_id, parent_task_id, person_id, tool_id, kind, owner_kind,
                    session_key, correlation_id, conversation_id, status, created_at, finished_at
                )
                VALUES ({", ".join([bind] * 12)})
                """,
                (
                    record.setup_request_id,
                    record.parent_task_id,
                    record.person_id,
                    record.tool_id,
                    record.kind,
                    record.owner_kind,
                    record.session_key,
                    record.correlation_id,
                    record.conversation_id,
                    record.status,
                    record.created_at,
                    record.finished_at,
                ),
            )
            conn.commit()

    def get_tool_setup(self, setup_request_id: str) -> Optional[ToolSetupRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT setup_request_id, parent_task_id, person_id, tool_id, kind, owner_kind,
                       session_key, correlation_id, conversation_id, status, created_at, finished_at
                FROM gateway_tool_setups
                WHERE setup_request_id = {self._dialect.bind}
                """,
                (setup_request_id,),
            ).fetchone()
        return ToolSetupRecord(**dict(row)) if row is not None else None

    def finish_tool_setup(self, setup_request_id: str, *, status: str) -> bool:
        bind = self._dialect.bind
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_tool_setups
                   SET status = {bind}, finished_at = {bind}
                 WHERE setup_request_id = {bind} AND status = 'waiting'
                """,
                (status, utc_now_iso(), setup_request_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_tool_setups(
        self,
        *,
        status: Optional[str] = None,
        person_id: Optional[str] = None,
        tool_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
    ) -> list[ToolSetupRecord]:
        filters = {
            "status": status,
            "person_id": person_id,
            "tool_id": tool_id,
            "parent_task_id": parent_task_id,
        }
        chosen = [(column, value) for column, value in filters.items() if value is not None]
        where = " AND ".join(f"{column} = {self._dialect.bind}" for column, _value in chosen)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT setup_request_id, parent_task_id, person_id, tool_id, kind, owner_kind,
                       session_key, correlation_id, conversation_id, status, created_at, finished_at
                FROM gateway_tool_setups
                {"WHERE " + where if where else ""}
                ORDER BY created_at ASC
                """,
                tuple(value for _column, value in chosen),
            ).fetchall()
        return [ToolSetupRecord(**dict(row)) for row in rows]

    def list_requests_for_session(
        self, session_key: str, *, limit: int = 5
    ) -> list[RequestRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT correlation_id, session_key, request_kind, request_text, conversation_id,
                       followup_to_task_id, created_at, status, owner_canonical_user_id,
                       active_task_id
                FROM gateway_requests
                WHERE session_key = {self._dialect.bind}
                ORDER BY created_at DESC
                LIMIT {self._dialect.bind}
                """,
                (session_key, limit),
            ).fetchall()
        return [RequestRecord(**dict(row)) for row in rows]

    def add_deferred_delivery(
        self,
        *,
        session_key: str,
        origin_kind: str,
        payload: dict[str, Any],
        rendered_text: str,
        available_after: str,
        correlation_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> str:
        delivery_id = new_id("delivery")
        owner = owner_canonical_user_id
        if owner is None:
            session = self.get_session(session_key)
            owner = session.owner_canonical_user_id if session else None
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_deliveries
                (delivery_id, correlation_id, session_key, origin_kind, payload_json, rendered_text,
                 available_after, status, owner_canonical_user_id, created_at, delivered_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, 'pending', {self._dialect.bind}, {self._dialect.bind}, NULL)
                """,
                (
                    delivery_id,
                    correlation_id,
                    session_key,
                    origin_kind,
                    json.dumps(payload),
                    rendered_text,
                    available_after,
                    owner,
                    utc_now_iso(),
                ),
            )
            conn.commit()
        return delivery_id

    def get_due_deliveries(self, now_iso: Optional[str] = None) -> list[DeliveryRecord]:
        threshold = now_iso or utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT delivery_id, correlation_id, session_key, origin_kind, payload_json, rendered_text,
                       available_after, status, created_at, delivered_at, owner_canonical_user_id
                FROM gateway_deliveries
                WHERE status = 'pending' AND available_after <= {self._dialect.bind}
                ORDER BY available_after ASC, created_at ASC
                """,
                (threshold,),
            ).fetchall()
        return [DeliveryRecord(**dict(row)) for row in rows]

    def mark_delivery_delivered(self, delivery_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_deliveries
                SET status = 'delivered', delivered_at = {self._dialect.bind}
                WHERE delivery_id = {self._dialect.bind}
                """,
                (utc_now_iso(), delivery_id),
            )
            conn.commit()

    def list_pending_deliveries(
        self, session_key: Optional[str] = None
    ) -> list[DeliveryRecord]:
        query = """
            SELECT delivery_id, correlation_id, session_key, origin_kind, payload_json, rendered_text,
                   available_after, status, created_at, delivered_at, owner_canonical_user_id
            FROM gateway_deliveries
            WHERE status = 'pending'
        """
        params: list[Any] = []
        if session_key:
            query += f" AND session_key = {self._dialect.bind}"
            params.append(session_key)
        query += " ORDER BY available_after ASC, created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [DeliveryRecord(**dict(row)) for row in rows]

    def upsert_attachment(
        self,
        *,
        attachment_id: str,
        session_key: str,
        conversation_id: Optional[str],
        correlation_id: Optional[str],
        filename: str,
        content_type: str,
        kind: str,
        size_bytes: int,
        sha256: str,
        local_path: str,
        nats_bucket: Optional[str] = None,
        nats_object_name: Optional[str] = None,
        expires_at: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        created_at = utc_now_iso()
        owner = owner_canonical_user_id
        if owner is None:
            session = self.get_session(session_key)
            owner = session.owner_canonical_user_id if session else None
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_attachments
                (attachment_id, session_key, conversation_id, correlation_id, filename, content_type, kind,
                 size_bytes, sha256, local_path, nats_bucket, nats_object_name, owner_canonical_user_id, created_at, expires_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(attachment_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    conversation_id = excluded.conversation_id,
                    correlation_id = excluded.correlation_id,
                    filename = excluded.filename,
                    content_type = excluded.content_type,
                    kind = excluded.kind,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    local_path = excluded.local_path,
                    nats_bucket = excluded.nats_bucket,
                    nats_object_name = excluded.nats_object_name,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    expires_at = excluded.expires_at
                """,
                (
                    attachment_id,
                    session_key,
                    conversation_id,
                    correlation_id,
                    filename,
                    content_type,
                    kind,
                    int(size_bytes),
                    sha256,
                    local_path,
                    nats_bucket,
                    nats_object_name,
                    owner,
                    created_at,
                    expires_at,
                ),
            )
            conn.commit()

    def get_attachment(self, attachment_id: str) -> Optional[AttachmentRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT attachment_id, session_key, conversation_id, correlation_id, filename, content_type, kind,
                       size_bytes, sha256, local_path, nats_bucket, nats_object_name, created_at, expires_at,
                       owner_canonical_user_id
                FROM gateway_attachments
                WHERE attachment_id = {self._dialect.bind}
                """,
                (attachment_id,),
            ).fetchone()
        if not row:
            return None
        return AttachmentRecord(**dict(row))

    def update_attachment_nats_ref(
        self,
        *,
        attachment_id: str,
        nats_bucket: str,
        nats_object_name: str,
    ) -> Optional[AttachmentRecord]:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_attachments
                SET nats_bucket = {self._dialect.bind}, nats_object_name = {self._dialect.bind}
                WHERE attachment_id = {self._dialect.bind}
                """,
                (nats_bucket, nats_object_name, attachment_id),
            )
            conn.commit()
        return self.get_attachment(attachment_id)

    def upsert_pending_query(
        self,
        *,
        session_key: str,
        correlation_id: str,
        task_query_id: str,
        parent_task_id: Optional[str],
        conversation_id: Optional[str],
        query_text: str,
        query_payload_json: Optional[str] = None,
        status: str = "pending",
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        created_at = utc_now_iso()
        owner = owner_canonical_user_id
        if owner is None:
            session = self.get_session(session_key)
            owner = session.owner_canonical_user_id if session else None
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_pending_queries
                (session_key, correlation_id, task_query_id, parent_task_id, conversation_id, query_text,
                 query_payload_json, owner_canonical_user_id, created_at, status)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(session_key) DO UPDATE SET
                    correlation_id = excluded.correlation_id,
                    task_query_id = excluded.task_query_id,
                    parent_task_id = excluded.parent_task_id,
                    conversation_id = excluded.conversation_id,
                    query_text = excluded.query_text,
                    query_payload_json = excluded.query_payload_json,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    created_at = excluded.created_at,
                    status = excluded.status
                """,
                (
                    session_key,
                    correlation_id,
                    task_query_id,
                    parent_task_id,
                    conversation_id,
                    query_text,
                    query_payload_json,
                    owner,
                    created_at,
                    status,
                ),
            )
            conn.commit()

    def get_pending_query(self, session_key: str) -> Optional[PendingQueryRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT session_key, correlation_id, task_query_id, parent_task_id, conversation_id,
                       query_text, query_payload_json, created_at, status, owner_canonical_user_id
                FROM gateway_pending_queries
                WHERE session_key = {self._dialect.bind} AND status = 'pending'
                """,
                (session_key,),
            ).fetchone()
        if not row:
            return None
        return PendingQueryRecord(**dict(row))

    def clear_pending_query(
        self, session_key: str, *, status: str = "completed"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_pending_queries
                SET status = {self._dialect.bind}
                WHERE session_key = {self._dialect.bind}
                """,
                (status, session_key),
            )
            conn.commit()

    def expire_pending_queries(
        self, *, cutoff_iso: str, status: str = "expired"
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_pending_queries
                SET status = {self._dialect.bind}
                WHERE status = 'pending' AND created_at < {self._dialect.bind}
                """,
                (status, cutoff_iso),
            )
            conn.commit()
            return int(cursor.rowcount)

    def upsert_scheduled_target(
        self,
        *,
        scheduled_job_id: str,
        session_key: str,
        conversation_id: Optional[str],
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        now = utc_now_iso()
        owner = owner_canonical_user_id
        if owner is None:
            session = self.get_session(session_key)
            owner = session.owner_canonical_user_id if session else None
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_scheduled_targets
                (scheduled_job_id, session_key, conversation_id, owner_canonical_user_id, created_at, updated_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(scheduled_job_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    conversation_id = excluded.conversation_id,
                    owner_canonical_user_id = excluded.owner_canonical_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    scheduled_job_id,
                    session_key,
                    conversation_id,
                    owner,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_scheduled_target(
        self, scheduled_job_id: str
    ) -> Optional[ScheduledTargetRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT scheduled_job_id, session_key, conversation_id, created_at, updated_at, owner_canonical_user_id
                FROM gateway_scheduled_targets
                WHERE scheduled_job_id = {self._dialect.bind}
                """,
                (scheduled_job_id,),
            ).fetchone()
        if not row:
            return None
        return ScheduledTargetRecord(**dict(row))

    def get_scheduled_target_by_conversation_id(
        self, conversation_id: str
    ) -> Optional[ScheduledTargetRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT scheduled_job_id, session_key, conversation_id, created_at, updated_at, owner_canonical_user_id
                FROM gateway_scheduled_targets
                WHERE conversation_id = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return ScheduledTargetRecord(**dict(row))

    def remove_scheduled_target(self, scheduled_job_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM gateway_scheduled_targets WHERE scheduled_job_id = {self._dialect.bind}",
                (scheduled_job_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def append_transcript_message(
        self,
        request: TranscriptAppendRequest,
    ) -> GatewayTranscriptMessage:
        """Append one message with backend-serialized session and event order."""

        existing = self.get_transcript_message_by_idempotency_key(
            request.idempotency_key
        )
        if existing is not None:
            self._logger.info(
                "event=gateway_transcript_duplicate_suppressed message_id=%s "
                "session_key=%s source_kind=%s",
                existing.message_id,
                existing.session_key,
                existing.source_kind,
            )
            return existing

        created_at = utc_now_iso()
        with _RepositoryTransaction(self) as transaction:
            existing = self.get_transcript_message_by_idempotency_key(
                request.idempotency_key
            )
            if existing is not None:
                self._logger.info(
                    "event=gateway_transcript_duplicate_suppressed message_id=%s "
                    "session_key=%s source_kind=%s",
                    existing.message_id,
                    existing.session_key,
                    existing.source_kind,
                )
                transaction.commit()
                return existing
            with self._connect() as conn:
                stream_row = conn.execute(
                    f"""
                    UPDATE gateway_transcript_streams
                    SET last_event_sequence = last_event_sequence + 1,
                        updated_at_utc = {self._dialect.bind}
                    WHERE stream_key = 'gateway'
                    RETURNING stream_id, last_event_sequence
                    """,
                    (created_at,),
                ).fetchone()
                if stream_row is None:
                    raise RuntimeError("Gateway transcript stream metadata is missing.")
                sequence_row = conn.execute(
                    f"""
                    INSERT INTO gateway_transcript_session_sequences (
                        session_key, last_session_sequence, updated_at_utc
                    ) VALUES ({self._dialect.bind}, 1, {self._dialect.bind})
                    ON CONFLICT(session_key) DO UPDATE SET
                        last_session_sequence =
                            gateway_transcript_session_sequences.last_session_sequence + 1,
                        updated_at_utc = excluded.updated_at_utc
                    RETURNING last_session_sequence
                    """,
                    (request.session_key, created_at),
                ).fetchone()
                if sequence_row is None:
                    raise RuntimeError(
                        "Gateway transcript session sequence allocation failed."
                    )
                inserted = conn.execute(
                    f"""
                    INSERT INTO gateway_transcript_messages (
                        schema_version, message_id, session_key, conversation_id,
                        owner_canonical_user_id, channel_name, chat_id,
                        session_sequence, event_sequence, role, content,
                        message_type, message_level, correlation_id, source_kind,
                        source_id, idempotency_key, attachment_references_json,
                        delivery_status, reply_to_message_id, created_at_utc
                    ) VALUES (
                        {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}
                    )
                    ON CONFLICT(idempotency_key) DO NOTHING
                    RETURNING {_TRANSCRIPT_MESSAGE_COLUMNS}
                    """,
                    (
                        TRANSCRIPT_SCHEMA_VERSION,
                        request.message_id,
                        request.session_key,
                        request.conversation_id,
                        request.owner_canonical_user_id,
                        request.channel_name,
                        request.chat_id,
                        int(sequence_row["last_session_sequence"]),
                        int(stream_row["last_event_sequence"]),
                        request.role,
                        request.content,
                        request.message_type,
                        request.message_level,
                        request.correlation_id,
                        request.source_kind,
                        request.source_id,
                        request.idempotency_key,
                        _transcript_attachment_json(request.attachment_references),
                        request.delivery_status,
                        request.reply_to_message_id,
                        created_at,
                    ),
                ).fetchone()
            if inserted is None:
                existing = self.get_transcript_message_by_idempotency_key(
                    request.idempotency_key
                )
                if existing is None:
                    raise RuntimeError(
                        "Gateway transcript idempotency conflict could not be resolved."
                    )
                self._logger.info(
                    "event=gateway_transcript_duplicate_suppressed message_id=%s "
                    "session_key=%s source_kind=%s",
                    existing.message_id,
                    existing.session_key,
                    existing.source_kind,
                )
                transaction.commit()
                return existing
            transaction.commit()
        message = _transcript_message_from_row(inserted)
        self._logger.info(
            "event=gateway_transcript_message_appended message_id=%s "
            "session_key=%s conversation_id=%s correlation_id=%s source_kind=%s "
            "role=%s message_type=%s session_sequence=%s event_sequence=%s",
            message.message_id,
            message.session_key,
            message.conversation_id or "-",
            message.correlation_id or "-",
            message.source_kind,
            message.role,
            message.message_type,
            message.session_sequence,
            message.event_sequence,
        )
        return message

    def get_transcript_message(
        self,
        message_id: str,
    ) -> GatewayTranscriptMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {_TRANSCRIPT_MESSAGE_COLUMNS}
                FROM gateway_transcript_messages
                WHERE message_id = {self._dialect.bind}
                """,
                (message_id,),
            ).fetchone()
        return _transcript_message_from_row(row) if row is not None else None

    def get_transcript_message_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> GatewayTranscriptMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {_TRANSCRIPT_MESSAGE_COLUMNS}
                FROM gateway_transcript_messages
                WHERE idempotency_key = {self._dialect.bind}
                """,
                (idempotency_key,),
            ).fetchone()
        return _transcript_message_from_row(row) if row is not None else None

    def get_transcript_reply(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        reply_to_message_id: str,
    ) -> GatewayTranscriptMessage | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {_TRANSCRIPT_MESSAGE_COLUMNS}
                FROM gateway_transcript_messages
                WHERE owner_canonical_user_id = {self._dialect.bind}
                  AND session_key = {self._dialect.bind}
                  AND reply_to_message_id = {self._dialect.bind}
                ORDER BY session_sequence ASC
                LIMIT 1
                """,
                (
                    owner_canonical_user_id,
                    session_key,
                    reply_to_message_id,
                ),
            ).fetchone()
        return _transcript_message_from_row(row) if row is not None else None

    def list_transcript_messages(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        before_session_sequence: int | None,
        limit: int,
    ) -> list[GatewayTranscriptMessage]:
        bounded_limit = max(1, min(int(limit), 200))
        query = f"""
            SELECT {_TRANSCRIPT_MESSAGE_COLUMNS}
            FROM gateway_transcript_messages
            WHERE owner_canonical_user_id = {self._dialect.bind} AND session_key = {self._dialect.bind}
        """
        params: list[Any] = [owner_canonical_user_id, session_key]
        if before_session_sequence is not None:
            query += f" AND session_sequence < {self._dialect.bind}"
            params.append(before_session_sequence)
        query += f" ORDER BY session_sequence DESC LIMIT {self._dialect.bind}"
        params.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_transcript_message_from_row(row) for row in reversed(rows)]

    def list_transcript_events(
        self,
        *,
        owner_canonical_user_id: str,
        after_event_sequence: int,
        limit: int,
    ) -> list[GatewayTranscriptMessage]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {_TRANSCRIPT_MESSAGE_COLUMNS}
                FROM gateway_transcript_messages
                WHERE owner_canonical_user_id = {self._dialect.bind} AND event_sequence > {self._dialect.bind}
                ORDER BY event_sequence ASC
                LIMIT {self._dialect.bind}
                """,
                (
                    owner_canonical_user_id,
                    max(0, int(after_event_sequence)),
                    bounded_limit,
                ),
            ).fetchall()
        return [_transcript_message_from_row(row) for row in rows]

    def get_transcript_stream_state(self) -> TranscriptStreamState:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT stream_id, last_event_sequence
                FROM gateway_transcript_streams
                WHERE stream_key = 'gateway'
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("Gateway transcript stream metadata is missing.")
        return TranscriptStreamState(
            stream_id=str(row["stream_id"]),
            latest_event_sequence=int(row["last_event_sequence"]),
        )

    def create_gateway_user(
        self,
        *,
        canonical_user_id: Optional[str] = None,
        display_name: str,
        timezone: str,
        metadata: Optional[dict[str, Any]] = None,
        status: str = "active",
    ) -> GatewayUserRecord:
        now = utc_now_iso()
        user_id = str(canonical_user_id or new_id("usr")).strip()
        if not user_id:
            raise ValueError("canonical_user_id must be non-empty.")
        rendered_name = str(display_name or user_id).strip() or user_id
        timezone_name = str(timezone or "UTC").strip() or "UTC"
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_users (
                    canonical_user_id, display_name, timezone, default_channel_name,
                    default_chat_id, default_thread_id, metadata_json, status,
                    created_at_utc, updated_at_utc, last_seen_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(canonical_user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    timezone = excluded.timezone,
                    metadata_json = excluded.metadata_json,
                    status = excluded.status,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    user_id,
                    rendered_name,
                    timezone_name,
                    _json_dumps(metadata),
                    status,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get_gateway_user(user_id)  # type: ignore[return-value]

    def get_gateway_user(self, canonical_user_id: str) -> Optional[GatewayUserRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT canonical_user_id, display_name, timezone, default_channel_name,
                       default_chat_id, default_thread_id, metadata_json, status,
                       created_at_utc, updated_at_utc, last_seen_at_utc
                FROM gateway_users
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (canonical_user_id,),
            ).fetchone()
        if not row:
            return None
        return GatewayUserRecord(**dict(row))

    def list_gateway_users(
        self,
        *,
        limit: int = 100,
    ) -> list[GatewayUserRecord]:
        """People in the order they were added, at most ``limit`` (500 at most)."""

        bounded_limit = max(1, min(int(limit or 100), 500))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT canonical_user_id, display_name, timezone, default_channel_name,
                       default_chat_id, default_thread_id, metadata_json, status,
                       created_at_utc, updated_at_utc, last_seen_at_utc
                FROM gateway_users
                ORDER BY created_at_utc ASC, canonical_user_id ASC LIMIT {self._dialect.bind}
                """,
                (bounded_limit,),
            ).fetchall()
        return [GatewayUserRecord(**dict(row)) for row in rows]

    def set_gateway_user_status(
        self, canonical_user_id: str, status: str
    ) -> Optional[GatewayUserRecord]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"active", "disabled"}:
            raise ValueError("Gateway user status must be 'active' or 'disabled'.")
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_users
                SET status = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (normalized_status, now, canonical_user_id),
            )
            conn.commit()
        if cursor.rowcount <= 0:
            return None
        return self.get_gateway_user(canonical_user_id)

    def update_gateway_user_last_seen(
        self, canonical_user_id: str, *, seen_at_utc: Optional[str] = None
    ) -> None:
        seen_at = seen_at_utc or utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_users
                SET last_seen_at_utc = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (seen_at, seen_at, canonical_user_id),
            )
            conn.commit()

    def set_gateway_user_default_channel(
        self,
        *,
        canonical_user_id: str,
        channel_name: Optional[str],
        chat_id: Optional[str],
        thread_id: Optional[str],
    ) -> Optional[GatewayUserRecord]:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE gateway_users
                SET default_channel_name = {self._dialect.bind}, default_chat_id = {self._dialect.bind}, default_thread_id = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (channel_name, chat_id, thread_id, now, canonical_user_id),
            )
            conn.commit()
        return self.get_gateway_user(canonical_user_id)

    def get_gateway_user_appearance(
        self,
        canonical_user_id: str,
    ) -> Optional[GatewayUserAppearanceRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT canonical_user_id, appearance_json, created_at_utc, updated_at_utc
                FROM gateway_user_appearance_preferences
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (canonical_user_id,),
            ).fetchone()
        if row is None:
            return None
        return GatewayUserAppearanceRecord(**dict(row))

    def upsert_gateway_user_appearance(
        self,
        *,
        canonical_user_id: str,
        appearance: dict[str, Any],
    ) -> GatewayUserAppearanceRecord:
        now = utc_now_iso()
        with self._connect() as conn:
            user = conn.execute(
                f"""
                SELECT canonical_user_id
                FROM gateway_users
                WHERE canonical_user_id = {self._dialect.bind} AND status = 'active'
                """,
                (canonical_user_id,),
            ).fetchone()
            if user is None:
                raise ValueError("Appearance preferences require an active gateway user.")
            conn.execute(
                f"""
                INSERT INTO gateway_user_appearance_preferences (
                    canonical_user_id, appearance_json, created_at_utc, updated_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(canonical_user_id) DO UPDATE SET
                    appearance_json = excluded.appearance_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (canonical_user_id, _json_dumps(appearance), now, now),
            )
            conn.commit()
        record = self.get_gateway_user_appearance(canonical_user_id)
        if record is None:
            raise RuntimeError("Gateway appearance preference was not persisted.")
        return record

    def delete_gateway_user_appearance(self, canonical_user_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM gateway_user_appearance_preferences
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (canonical_user_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    def get_active_channel_binding(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> Optional[GatewayUserChannelBindingRecord]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                       status, created_at_utc, updated_at_utc, last_seen_at_utc,
                       replaced_at_utc, replaced_by_binding_id, metadata_json
                FROM gateway_user_channel_bindings
                WHERE channel_name = {self._dialect.bind}
                  AND external_user_id = {self._dialect.bind}
                  AND chat_scope = {self._dialect.bind}
                  AND status = 'active'
                LIMIT 1
                """,
                (channel_name, external_user_id, chat_scope or ""),
            ).fetchone()
            if not row:
                return None
            now = utc_now_iso()
            conn.execute(
                f"""
                UPDATE gateway_user_channel_bindings
                SET last_seen_at_utc = {self._dialect.bind}, updated_at_utc = {self._dialect.bind}
                WHERE binding_id = {self._dialect.bind}
                """,
                (now, now, row["binding_id"]),
            )
            conn.commit()
        return GatewayUserChannelBindingRecord(**dict(row))

    def list_channel_bindings_for_user(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserChannelBindingRecord]:
        query = f"""
            SELECT binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                   status, created_at_utc, updated_at_utc, last_seen_at_utc,
                   replaced_at_utc, replaced_by_binding_id, metadata_json
            FROM gateway_user_channel_bindings
            WHERE canonical_user_id = {self._dialect.bind}
        """
        params: list[Any] = [canonical_user_id]
        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            query += f" AND status = {self._dialect.bind}"
            params.append(normalized_status)
        query += " ORDER BY channel_name ASC, chat_scope ASC, external_user_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [GatewayUserChannelBindingRecord(**dict(row)) for row in rows]

    def list_active_channel_bindings(self) -> list[GatewayUserChannelBindingRecord]:
        """All accounts occupying the active unique index, including removed people's.

        Team transfer must detect collisions without updating last-seen times
        or assuming that a binding's person still has a current membership.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                       status, created_at_utc, updated_at_utc, last_seen_at_utc,
                       replaced_at_utc, replaced_by_binding_id, metadata_json
                FROM gateway_user_channel_bindings
                WHERE status = 'active'
                ORDER BY channel_name ASC, chat_scope ASC, external_user_id ASC
                """
            ).fetchall()
        return [GatewayUserChannelBindingRecord(**dict(row)) for row in rows]

    def create_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserChannelBindingRecord:
        now = utc_now_iso()
        binding_id = new_id("gateway_binding")
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_user_channel_bindings (
                    binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                    status, created_at_utc, updated_at_utc, last_seen_at_utc,
                    replaced_at_utc, replaced_by_binding_id, metadata_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, 'active', {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, {self._dialect.bind})
                """,
                (
                    binding_id,
                    canonical_user_id,
                    channel_name,
                    external_user_id,
                    chat_scope or "",
                    now,
                    now,
                    now,
                    _json_dumps(metadata),
                ),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                       status, created_at_utc, updated_at_utc, last_seen_at_utc,
                       replaced_at_utc, replaced_by_binding_id, metadata_json
                FROM gateway_user_channel_bindings
                WHERE binding_id = {self._dialect.bind}
                """,
                (binding_id,),
            ).fetchone()
        return GatewayUserChannelBindingRecord(**dict(row))

    def replace_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserChannelBindingRecord:
        now = utc_now_iso()
        new_binding_id = new_id("gateway_binding")
        scope = chat_scope or ""
        with self._connect() as conn:
            existing_rows = conn.execute(
                f"""
                SELECT binding_id
                FROM gateway_user_channel_bindings
                WHERE channel_name = {self._dialect.bind}
                  AND external_user_id = {self._dialect.bind}
                  AND chat_scope = {self._dialect.bind}
                  AND status = 'active'
                """,
                (channel_name, external_user_id, scope),
            ).fetchall()
            conn.execute(
                f"""
                UPDATE gateway_user_channel_bindings
                SET status = 'replaced',
                    updated_at_utc = {self._dialect.bind},
                    replaced_at_utc = {self._dialect.bind},
                    replaced_by_binding_id = {self._dialect.bind}
                WHERE channel_name = {self._dialect.bind}
                  AND external_user_id = {self._dialect.bind}
                  AND chat_scope = {self._dialect.bind}
                  AND status = 'active'
                """,
                (now, now, new_binding_id, channel_name, external_user_id, scope),
            )
            conn.execute(
                f"""
                INSERT INTO gateway_user_channel_bindings (
                    binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                    status, created_at_utc, updated_at_utc, last_seen_at_utc,
                    replaced_at_utc, replaced_by_binding_id, metadata_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, 'active', {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, {self._dialect.bind})
                """,
                (
                    new_binding_id,
                    canonical_user_id,
                    channel_name,
                    external_user_id,
                    scope,
                    now,
                    now,
                    now,
                    _json_dumps(
                        {
                            **(metadata or {}),
                            "replaced_binding_ids": [
                                str(row["binding_id"]) for row in existing_rows
                            ],
                        }
                    ),
                ),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT binding_id, canonical_user_id, channel_name, external_user_id, chat_scope,
                       status, created_at_utc, updated_at_utc, last_seen_at_utc,
                       replaced_at_utc, replaced_by_binding_id, metadata_json
                FROM gateway_user_channel_bindings
                WHERE binding_id = {self._dialect.bind}
                """,
                (new_binding_id,),
            ).fetchone()
        return GatewayUserChannelBindingRecord(**dict(row))

    def create_web_user_token(
        self,
        *,
        token_hash: str,
        canonical_user_id: str,
        label: str = "",
        status: str = "active",
        expires_at_utc: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserWebTokenRecord:
        now = utc_now_iso()
        token_id = new_id("gw_web_tok")
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_user_web_tokens (
                    token_id, token_hash, canonical_user_id, label, status,
                    created_at_utc, last_used_at_utc, expires_at_utc, metadata_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    token_id,
                    token_hash,
                    canonical_user_id,
                    str(label or "").strip(),
                    str(status or "active").strip().lower(),
                    now,
                    expires_at_utc,
                    _json_dumps(metadata),
                ),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT token_id, token_hash, canonical_user_id, label, status,
                       created_at_utc, last_used_at_utc, expires_at_utc, metadata_json
                FROM gateway_user_web_tokens
                WHERE token_id = {self._dialect.bind}
                """,
                (token_id,),
            ).fetchone()
        return GatewayUserWebTokenRecord(**dict(row))

    def list_web_user_tokens(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserWebTokenRecord]:
        """A person's Web Chat sign-in tokens (hashes only), oldest first; the team transfer carries active ones."""

        bind = self._dialect.bind
        query = f"""
            SELECT token_id, token_hash, canonical_user_id, label, status,
                   created_at_utc, last_used_at_utc, expires_at_utc, metadata_json
            FROM gateway_user_web_tokens
            WHERE canonical_user_id = {bind}
        """
        params: list[Any] = [canonical_user_id]
        if status:
            query += f" AND status = {bind}"
            params.append(str(status).strip().lower())
        query += " ORDER BY created_at_utc ASC, token_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [GatewayUserWebTokenRecord(**dict(row)) for row in rows]

    def resolve_web_user_token(
        self,
        *,
        token_hash: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebTokenRecord]:
        now = now_utc or utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT token_id, token_hash, canonical_user_id, label, status,
                       created_at_utc, last_used_at_utc, expires_at_utc, metadata_json
                FROM gateway_user_web_tokens
                WHERE token_hash = {self._dialect.bind}
                  AND status = 'active'
                  AND (expires_at_utc IS NULL OR expires_at_utc > {self._dialect.bind})
                LIMIT 1
                """,
                (token_hash, now),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                f"""
                UPDATE gateway_user_web_tokens
                SET last_used_at_utc = {self._dialect.bind}
                WHERE token_id = {self._dialect.bind}
                """,
                (now, row["token_id"]),
            )
            conn.commit()
        return GatewayUserWebTokenRecord(**dict(row))

    def activate_web_user_token(
        self,
        *,
        token_id: str,
        canonical_user_id: str,
        expires_at_utc: Optional[str],
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebTokenRecord]:
        now = now_utc or utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_user_web_tokens
                SET status = 'active',
                    expires_at_utc = {self._dialect.bind}
                WHERE token_id = {self._dialect.bind}
                  AND canonical_user_id = {self._dialect.bind}
                  AND status = 'pending'
                  AND (expires_at_utc IS NULL OR expires_at_utc > {self._dialect.bind})
                """,
                (expires_at_utc, token_id, canonical_user_id, now),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            row = conn.execute(
                f"""
                SELECT token_id, token_hash, canonical_user_id, label, status,
                       created_at_utc, last_used_at_utc, expires_at_utc, metadata_json
                FROM gateway_user_web_tokens
                WHERE token_id = {self._dialect.bind}
                """,
                (token_id,),
            ).fetchone()
            conn.commit()
        return GatewayUserWebTokenRecord(**dict(row))

    def revoke_web_user_token(
        self,
        token_id: str,
        *,
        canonical_user_id: Optional[str] = None,
        required_status: Optional[str] = None,
    ) -> bool:
        conditions = [f"token_id = {self._dialect.bind}", "status != 'revoked'"]
        parameters: list[Any] = [token_id]
        if canonical_user_id is not None:
            conditions.append(f"canonical_user_id = {self._dialect.bind}")
            parameters.append(canonical_user_id)
        if required_status is not None:
            conditions.append(f"status = {self._dialect.bind}")
            parameters.append(required_status)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_user_web_tokens
                SET status = 'revoked'
                WHERE {" AND ".join(conditions)}
                """,
                tuple(parameters),
            )
            conn.commit()
        return cursor.rowcount > 0

    def create_web_user_invite(
        self,
        *,
        invite_hash: str,
        canonical_user_id: str,
        label: str,
        expires_at_utc: str,
        created_by_canonical_user_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserWebInviteRecord:
        now = utc_now_iso()
        invite_id = new_id("gw_web_inv")
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_user_web_invites (
                    invite_id, invite_hash, canonical_user_id, label, created_at_utc,
                    expires_at_utc, consumed_at_utc, created_by_canonical_user_id,
                    metadata_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    invite_id,
                    invite_hash,
                    canonical_user_id,
                    str(label or "").strip(),
                    now,
                    expires_at_utc,
                    created_by_canonical_user_id,
                    _json_dumps(metadata),
                ),
            )
            row = conn.execute(
                f"""
                SELECT invite_id, invite_hash, canonical_user_id, label, created_at_utc,
                       expires_at_utc, consumed_at_utc, created_by_canonical_user_id,
                       metadata_json
                FROM gateway_user_web_invites
                WHERE invite_id = {self._dialect.bind}
                """,
                (invite_id,),
            ).fetchone()
            conn.commit()
        return GatewayUserWebInviteRecord(**dict(row))

    def create_gateway_user_with_web_invite(
        self,
        *,
        display_name: str,
        timezone: str,
        user_metadata: Optional[dict[str, Any]],
        team_id: str,
        invite_hash: str,
        invite_label: str,
        invite_expires_at_utc: str,
        created_by_canonical_user_id: str,
        invite_metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[
        GatewayUserRecord,
        GatewayUserTeamMembershipRecord,
        GatewayUserWebInviteRecord,
    ]:
        now = utc_now_iso()
        canonical_user_id = new_id("usr")
        invite_id = new_id("gw_web_inv")
        rendered_name = (
            str(display_name or canonical_user_id).strip() or canonical_user_id
        )
        timezone_name = str(timezone or "UTC").strip() or "UTC"
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_users (
                    canonical_user_id, display_name, timezone, default_channel_name,
                    default_chat_id, default_thread_id, metadata_json, status,
                    created_at_utc, updated_at_utc, last_seen_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, NULL, NULL, {self._dialect.bind}, 'active', {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    canonical_user_id,
                    rendered_name,
                    timezone_name,
                    _json_dumps(user_metadata),
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                f"""
                INSERT INTO gateway_user_team_memberships (
                    canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, 'member', 'active', {self._dialect.bind}, {self._dialect.bind})
                """,
                (canonical_user_id, team_id, now, now),
            )
            conn.execute(
                f"""
                INSERT INTO gateway_user_web_invites (
                    invite_id, invite_hash, canonical_user_id, label, created_at_utc,
                    expires_at_utc, consumed_at_utc, created_by_canonical_user_id,
                    metadata_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    invite_id,
                    invite_hash,
                    canonical_user_id,
                    str(invite_label or "").strip(),
                    now,
                    invite_expires_at_utc,
                    created_by_canonical_user_id,
                    _json_dumps(invite_metadata),
                ),
            )
            user_row = conn.execute(
                f"""
                SELECT canonical_user_id, display_name, timezone, default_channel_name,
                       default_chat_id, default_thread_id, metadata_json, status,
                       created_at_utc, updated_at_utc, last_seen_at_utc
                FROM gateway_users
                WHERE canonical_user_id = {self._dialect.bind}
                """,
                (canonical_user_id,),
            ).fetchone()
            membership_row = conn.execute(
                f"""
                SELECT canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc
                FROM gateway_user_team_memberships
                WHERE canonical_user_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                """,
                (canonical_user_id, team_id),
            ).fetchone()
            invite_row = conn.execute(
                f"""
                SELECT invite_id, invite_hash, canonical_user_id, label, created_at_utc,
                       expires_at_utc, consumed_at_utc, created_by_canonical_user_id,
                       metadata_json
                FROM gateway_user_web_invites
                WHERE invite_id = {self._dialect.bind}
                """,
                (invite_id,),
            ).fetchone()
            conn.commit()
        return (
            GatewayUserRecord(**dict(user_row)),
            GatewayUserTeamMembershipRecord(**dict(membership_row)),
            GatewayUserWebInviteRecord(**dict(invite_row)),
        )

    def consume_web_user_invite(
        self,
        *,
        invite_hash: str,
        team_id: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebInviteRecord]:
        now = now_utc or utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT invite.invite_id
                FROM gateway_user_web_invites AS invite
                INNER JOIN gateway_users AS gateway_user
                    ON gateway_user.canonical_user_id = invite.canonical_user_id
                   AND gateway_user.status = 'active'
                INNER JOIN gateway_user_team_memberships AS membership
                    ON membership.canonical_user_id = invite.canonical_user_id
                   AND membership.team_id = {self._dialect.bind}
                   AND membership.status = 'active'
                WHERE invite.invite_hash = {self._dialect.bind}
                  AND invite.consumed_at_utc IS NULL
                  AND invite.expires_at_utc > {self._dialect.bind}
                LIMIT 1
                """,
                (team_id, invite_hash, now),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                f"""
                UPDATE gateway_user_web_invites
                SET consumed_at_utc = {self._dialect.bind}
                WHERE invite_id = {self._dialect.bind}
                  AND consumed_at_utc IS NULL
                  AND expires_at_utc > {self._dialect.bind}
                """,
                (now, row["invite_id"], now),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            consumed = conn.execute(
                f"""
                SELECT invite_id, invite_hash, canonical_user_id, label, created_at_utc,
                       expires_at_utc, consumed_at_utc, created_by_canonical_user_id,
                       metadata_json
                FROM gateway_user_web_invites
                WHERE invite_id = {self._dialect.bind}
                """,
                (row["invite_id"],),
            ).fetchone()
            conn.commit()
        return GatewayUserWebInviteRecord(**dict(consumed))

    def create_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str = "member",
        status: str = "active",
    ) -> GatewayUserTeamMembershipRecord:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_user_team_memberships (
                    canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(canonical_user_id, team_id) DO UPDATE SET
                    role = excluded.role,
                    status = excluded.status,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (canonical_user_id, team_id, role, status, now, now),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc
                FROM gateway_user_team_memberships
                WHERE canonical_user_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                """,
                (canonical_user_id, team_id),
            ).fetchone()
        return GatewayUserTeamMembershipRecord(**dict(row))

    def list_team_memberships(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserTeamMembershipRecord]:
        query = f"""
            SELECT canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc
            FROM gateway_user_team_memberships
            WHERE canonical_user_id = {self._dialect.bind}
        """
        params: list[Any] = [canonical_user_id]
        if status:
            query += f" AND status = {self._dialect.bind}"
            params.append(status)
        query += " ORDER BY team_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [GatewayUserTeamMembershipRecord(**dict(row)) for row in rows]

    def change_team_membership_role(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> Optional[GatewayUserTeamMembershipRecord]:
        """Change an active member's role; None when there is no active membership.

        Each group in ``keep_holders_of`` must keep an active holder (an active
        membership of an active user): the update only applies when the person
        does not hold a role of the group, the new role is in the group, or
        someone else still holds one. Checking and changing in one statement
        keeps two changes at the same moment from removing the last owner.
        Raises ``TeamRoleHolderRequired`` when a group would lose its last holder.
        """

        bind = self._dialect.bind
        new_role = str(role or "").strip().lower()
        guarded = tuple(group for group in keep_holders_of if new_role not in group)
        changed, row = self._update_active_membership(
            canonical_user_id=canonical_user_id,
            team_id=team_id,
            assignment=f"role = {bind}",
            assignment_params=[new_role],
            guarded_groups=guarded,
        )
        if row is None or row["status"] != "active":
            return None
        if not changed:
            raise _holder_required(row["role"], guarded)
        return GatewayUserTeamMembershipRecord(**dict(row))

    def remove_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> Optional[GatewayUserTeamMembershipRecord]:
        """Mark an active membership removed and return it; None when there is no active membership.

        The row stays (status ``removed``) so the person's history keeps its
        team. Every group in ``keep_holders_of`` must keep an active holder,
        checked in the same statement as for role changes. Raises
        ``TeamRoleHolderRequired`` when a group would lose its last holder.
        """

        guarded = tuple(keep_holders_of)
        changed, row = self._update_active_membership(
            canonical_user_id=canonical_user_id,
            team_id=team_id,
            assignment="status = 'removed'",
            assignment_params=[],
            guarded_groups=guarded,
        )
        if changed and row is not None:
            return GatewayUserTeamMembershipRecord(**dict(row))
        if row is None or row["status"] != "active":
            return None
        raise _holder_required(row["role"], guarded)

    def _update_active_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        assignment: str,
        assignment_params: list[Any],
        guarded_groups: tuple[tuple[str, ...], ...],
    ) -> tuple[bool, Any]:
        """Apply one change to an active membership unless a guarded group would lose its last holder.

        Returns whether the row changed and the membership row as it is after
        the statement (None when there is none). Checking and changing in one
        statement keeps two changes at the same moment from removing the last
        holder.
        """

        bind = self._dialect.bind
        guard_sql, guard_params = self._last_holder_guards(
            canonical_user_id=canonical_user_id, team_id=team_id, groups=guarded_groups
        )
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE gateway_user_team_memberships
                SET {assignment}, updated_at_utc = {bind}
                WHERE canonical_user_id = {bind} AND team_id = {bind} AND status = 'active'
                {guard_sql}
                """,
                (*assignment_params, utc_now_iso(), canonical_user_id, team_id, *guard_params),
            )
            conn.commit()
            changed = cursor.rowcount > 0
            row = self._membership_row(conn, canonical_user_id, team_id)
        return changed, row

    def _membership_row(self, conn: Any, canonical_user_id: str, team_id: str) -> Any:
        bind = self._dialect.bind
        return conn.execute(
            f"""
            SELECT canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc
            FROM gateway_user_team_memberships
            WHERE canonical_user_id = {bind} AND team_id = {bind}
            """,
            (canonical_user_id, team_id),
        ).fetchone()

    def _last_holder_guards(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        groups: tuple[tuple[str, ...], ...],
    ) -> tuple[str, list[Any]]:
        """SQL conditions keeping an active holder of each group besides this person.

        A condition passes when the person holds no role of the group or
        someone else (an active membership of an active user) still does.
        """

        bind = self._dialect.bind
        clauses: list[str] = []
        params: list[Any] = []
        for group in groups:
            in_group = ", ".join(bind for _ in group)
            clauses.append(
                f"""
                AND (
                    LOWER(role) NOT IN ({in_group})
                    OR EXISTS (
                        SELECT 1
                        FROM gateway_user_team_memberships AS other
                        INNER JOIN gateway_users AS other_user
                            ON other_user.canonical_user_id = other.canonical_user_id
                           AND other_user.status = 'active'
                        WHERE other.team_id = {bind}
                          AND other.status = 'active'
                          AND other.canonical_user_id <> {bind}
                          AND LOWER(other.role) IN ({in_group})
                    )
                )
                """
            )
            params.extend([*group, team_id, canonical_user_id, *group])
        return "".join(clauses), params

    def revoke_gateway_user_sign_ins(
        self,
        canonical_user_id: str,
        *,
        now_utc: str,
    ) -> GatewayUserSignInRevocation:
        """End every way the person signs in or links an identity: Web Chat tokens, unused invites and link codes.

        Tokens (active and pending) are revoked; unused invites and link codes
        expire now. Nothing is deleted, so the records stay for history.
        """

        bind = self._dialect.bind
        with self._connect() as conn:
            tokens = conn.execute(
                f"""
                UPDATE gateway_user_web_tokens SET status = 'revoked'
                WHERE canonical_user_id = {bind} AND status != 'revoked'
                """,
                (canonical_user_id,),
            ).rowcount
            counts = [
                conn.execute(
                    f"""
                    UPDATE {table} SET expires_at_utc = {bind}
                    WHERE canonical_user_id = {bind} AND consumed_at_utc IS NULL
                      AND expires_at_utc > {bind}
                    """,
                    (now_utc, canonical_user_id, now_utc),
                ).rowcount
                for table in ("gateway_user_web_invites", "gateway_user_link_codes")
            ]
            conn.commit()
        return GatewayUserSignInRevocation(
            web_tokens=max(tokens, 0),
            web_invites=max(counts[0], 0),
            link_codes=max(counts[1], 0),
        )

    # Rows of a removed person's chat sessions: deleted with the person
    # (forget_person_records). Keyed by the session.
    _PERSON_SESSION_TABLES = (
        "gateway_session_conversations",
        "gateway_requests",
        "gateway_deliveries",
        "gateway_attachments",
        "gateway_pending_queries",
        "gateway_transcript_messages",
        "gateway_transcript_session_sequences",
        "gateway_channel_sessions",
        "gateway_scheduled_targets",
    )
    # A removed person's own rows: sign-ins and preferences. Their person row,
    # channel bindings, membership and events stay (adding them back finds them,
    # and the events are the audit record).
    _PERSON_TABLES = (
        "gateway_user_web_tokens",
        "gateway_user_web_invites",
        "gateway_user_link_codes",
        "gateway_user_appearance_preferences",
    )

    def forget_person_records(self, canonical_user_id: str) -> GatewayPersonRecordsDeleted:
        """Delete a removed person's chat sessions, transcripts, sign-ins and preferences in one transaction."""

        bind = self._dialect.bind
        with self._connect() as conn:
            session_keys = sorted(
                {
                    str(row[0])
                    for table in ("gateway_sessions", "gateway_transcript_messages", "gateway_attachments")
                    for row in conn.execute(
                        f"SELECT session_key FROM {table} WHERE owner_canonical_user_id = {bind}",
                        (canonical_user_id,),
                    ).fetchall()
                }
            )
            paths = tuple(
                str(row[0])
                for row in conn.execute(
                    f"SELECT local_path FROM gateway_attachments WHERE owner_canonical_user_id = {bind}",
                    (canonical_user_id,),
                ).fetchall()
                if row[0]
            )
            transcript = conn.execute(
                f"DELETE FROM gateway_transcript_messages WHERE owner_canonical_user_id = {bind}",
                (canonical_user_id,),
            ).rowcount
            for table in self._PERSON_SESSION_TABLES:
                for session_key in session_keys:
                    conn.execute(f"DELETE FROM {table} WHERE session_key = {bind}", (session_key,))
            sessions = conn.execute(
                f"DELETE FROM gateway_sessions WHERE owner_canonical_user_id = {bind}", (canonical_user_id,)
            ).rowcount
            sign_ins = sum(
                max(conn.execute(f"DELETE FROM {table} WHERE canonical_user_id = {bind}", (canonical_user_id,)).rowcount, 0)
                for table in self._PERSON_TABLES
            )
            conn.commit()
        return GatewayPersonRecordsDeleted(
            sessions=max(sessions, 0),
            transcript_messages=max(transcript, 0),
            sign_ins=sign_ins,
            attachment_paths=paths,
        )

    def list_team_members(
        self,
        team_id: str,
    ) -> list[GatewayUserTeamMembershipRecord]:
        """Active memberships of active users in the team."""

        bind = self._dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT membership.canonical_user_id, membership.team_id, membership.role,
                       membership.status, membership.created_at_utc, membership.updated_at_utc
                FROM gateway_user_team_memberships AS membership
                INNER JOIN gateway_users AS gateway_user
                    ON gateway_user.canonical_user_id = membership.canonical_user_id
                   AND gateway_user.status = 'active'
                WHERE membership.team_id = {bind} AND membership.status = 'active'
                ORDER BY membership.canonical_user_id ASC
                """,
                (team_id,),
            ).fetchall()
        return [GatewayUserTeamMembershipRecord(**dict(row)) for row in rows]

    def list_team_people(
        self,
        team_id: str,
    ) -> list[GatewayTeamPersonRecord]:
        """Everyone with a membership in the team, whatever its status, with their own status."""

        bind = self._dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT membership.canonical_user_id, membership.team_id, membership.role,
                       membership.status AS membership_status,
                       gateway_user.status AS person_status
                FROM gateway_user_team_memberships AS membership
                INNER JOIN gateway_users AS gateway_user
                    ON gateway_user.canonical_user_id = membership.canonical_user_id
                WHERE membership.team_id = {bind}
                ORDER BY membership.canonical_user_id ASC
                """,
                (team_id,),
            ).fetchall()
        return [GatewayTeamPersonRecord(**dict(row)) for row in rows]

    def create_link_code(
        self,
        *,
        link_code_hash: str,
        canonical_user_id: str,
        expires_at_utc: str,
        created_by_channel: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> GatewayUserLinkCodeRecord:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_user_link_codes (
                    link_code_hash, canonical_user_id, expires_at_utc, consumed_at_utc,
                    created_at_utc, created_by_channel, metadata_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, NULL, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    link_code_hash,
                    canonical_user_id,
                    expires_at_utc,
                    now,
                    created_by_channel,
                    _json_dumps(metadata),
                ),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT link_code_hash, canonical_user_id, expires_at_utc, consumed_at_utc,
                       created_at_utc, created_by_channel, metadata_json
                FROM gateway_user_link_codes
                WHERE link_code_hash = {self._dialect.bind}
                """,
                (link_code_hash,),
            ).fetchone()
        return GatewayUserLinkCodeRecord(**dict(row))

    def consume_link_code(
        self,
        *,
        link_code_hash: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserLinkCodeRecord]:
        now = now_utc or utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT link_code_hash, canonical_user_id, expires_at_utc, consumed_at_utc,
                       created_at_utc, created_by_channel, metadata_json
                FROM gateway_user_link_codes
                WHERE link_code_hash = {self._dialect.bind}
                  AND consumed_at_utc IS NULL
                  AND expires_at_utc > {self._dialect.bind}
                LIMIT 1
                """,
                (link_code_hash, now),
            ).fetchone()
            if not row:
                return None
            # Guarded like consume_web_user_invite: only the use that marks the
            # code used gets it, even if another use read it unused first.
            cursor = conn.execute(
                f"""
                UPDATE gateway_user_link_codes
                SET consumed_at_utc = {self._dialect.bind}
                WHERE link_code_hash = {self._dialect.bind}
                  AND consumed_at_utc IS NULL
                  AND expires_at_utc > {self._dialect.bind}
                """,
                (now, link_code_hash, now),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            conn.commit()
        return GatewayUserLinkCodeRecord(**dict(row))

    def record_gateway_user_event(
        self,
        *,
        event_type: str,
        canonical_user_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> str:
        event_id = new_id("gw_user_evt")
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO gateway_user_events (
                    event_id, event_type, canonical_user_id, payload_json, created_at_utc
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    event_id,
                    event_type,
                    canonical_user_id,
                    _json_dumps(payload),
                    utc_now_iso(),
                ),
            )
            conn.commit()
        return event_id

    def gateway_user_event_types(self, *, canonical_user_id: str, event_types: Sequence[str]) -> set[str]:
        """Which of ``event_types`` were ever recorded for the person (for example a first-time setup shown)."""

        wanted = tuple(dict.fromkeys(str(item) for item in event_types))
        if not wanted:
            return set()
        binds = ", ".join(self._dialect.bind for _ in wanted)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT event_type FROM gateway_user_events "
                f"WHERE canonical_user_id = {self._dialect.bind} AND event_type IN ({binds})",
                (canonical_user_id, *wanted),
            ).fetchall()
        return {str(row[0]) for row in rows}


class SqliteGatewayStateRepository(_RelationalGatewayStateRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._active_transaction: ContextVar[_ActiveRepositoryTransaction | None] = (
            ContextVar(
                f"gateway_state_transaction_{id(self)}",
                default=None,
            )
        )
        self._init_schema()


    def _open_connection(self) -> sqlite3.Connection:
        return connect_sqlite(
            self._db_path, logger=self._logger, row_factory=sqlite3.Row
        )


    def _ensure_column(self, conn: Any, table_name: str, column_name: str, column_type: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=self._logger)


class PostgresGatewayStateRepository(_RelationalGatewayStateRepository):
    """Postgres implementation for gateway session and delivery state."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        self._active_transaction: ContextVar[_ActiveRepositoryTransaction | None] = (
            ContextVar(
                f"gateway_state_transaction_{id(self)}",
                default=None,
            )
        )
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _open_connection(self) -> PostgresConnection:
        return connect_postgres(self._dsn)


    def _ensure_column(self, conn: Any, table_name: str, column_name: str, column_type: str) -> None:
        conn.execute(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}')
        return


def create_gateway_state_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> GatewayStateRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="GatewayStateRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if (
        storage_settings.backend == STORAGE_BACKEND_SQLITE
        and storage_settings.sqlite_path
    ):
        return SqliteGatewayStateRepository(storage_settings.sqlite_path, logger=logger)
    if (
        storage_settings.backend == STORAGE_BACKEND_POSTGRES
        and storage_settings.postgres_dsn
    ):
        return PostgresGatewayStateRepository(
            storage_settings.postgres_dsn, logger=logger
        )
    raise NotImplementedError(
        f"GatewayStateRepository has no implementation for backend {storage_settings.backend!r}."
    )
