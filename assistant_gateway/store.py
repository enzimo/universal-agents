"""Gateway state store backed by the selected storage repository."""

from __future__ import annotations

import logging
from typing import Callable, Optional, Sequence, cast

from assistant_gateway.domain.delivery import (
    AttachmentRecord,
    DeliveryRecord,
    ScheduledTargetRecord,
)
from assistant_gateway.domain.history import GatewayHistoryDeleted, GatewayHistoryScope
from assistant_gateway.domain.identity import (
    GatewayTeamPersonRecord,
    GatewayUserChannelBindingRecord,
    GatewayUserLinkCodeRecord,
    GatewayUserRecord,
    GatewayPersonRecordsDeleted,
    GatewayUserSignInRevocation,
    GatewayUserTeamMembershipRecord,
    GatewayUserWebInviteRecord,
    GatewayUserWebTokenRecord,
)
from assistant_gateway.domain.request_state import (
    PendingQueryAdded,
    PendingQueryRecord,
    RequestRecord,
)
from assistant_gateway.domain.sessions import (
    SessionConversationListRecord,
    SessionConversationRecord,
    SessionListRecord,
    WebChatThreadRecord,
    SessionRecord,
    SessionSearchRecord,
)
from assistant_gateway.ports.repositories import (
    CredentialCollectionRepository,
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
from default_values import STORAGE_DEFAULTS
from storage.backends import STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings, resolve_storage_settings
from storage.gateway_state_repository import (
    GatewayStateRepository,
    create_gateway_state_repository,
)


logger = logging.getLogger(__name__)


class _DomainRepositoryView:
    """Expose only the methods declared by one domain repository port."""

    def __init__(self, repository: object, protocol: type[object]) -> None:
        self._repository = repository
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
                f"{type(self).__name__} does not expose repository method {name!r}."
            )
        return getattr(self._repository, name)


class GatewayStateStore:
    """Runtime-facing wrapper for gateway session and delivery persistence."""

    def __init__(
        self,
        db_path: str,
        logger_: Optional[logging.Logger] = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self._db_path = (
            storage_settings.sqlite_path
            if storage_settings and storage_settings.sqlite_path
            else db_path
        )
        self._logger = logger_ or logger
        self._storage_settings = storage_settings or resolve_storage_settings(
            {
                "backend": STORAGE_DEFAULTS.backend.value,
                STORAGE_BACKEND_SQLITE: {"path": db_path},
            }
        )
        self._repository: GatewayStateRepository = create_gateway_state_repository(
            self._storage_settings,
            logger=self._logger,
        )
        self._identity_repository = _DomainRepositoryView(
            self._repository,
            IdentityRepository,
        )
        self._session_repository = _DomainRepositoryView(
            self._repository,
            SessionRepository,
        )
        self._request_repository = _DomainRepositoryView(
            self._repository,
            RequestRepository,
        )
        self._delivery_repository = _DomainRepositoryView(
            self._repository,
            DeliveryRepository,
        )
        self._transcript_repository = _DomainRepositoryView(
            self._repository,
            TranscriptRepository,
        )
        self._tool_setup_repository = _DomainRepositoryView(
            self._repository,
            ToolSetupRepository,
        )
        self._credential_collection_repository = _DomainRepositoryView(
            self._repository, CredentialCollectionRepository,
        )

    @property
    def identity_repository(self) -> IdentityRepository:
        return cast(IdentityRepository, self._identity_repository)

    @property
    def storage_settings(self) -> StorageSettings:
        """Return the explicit backend settings used by this aggregate adapter."""

        return self._storage_settings

    @property
    def session_repository(self) -> SessionRepository:
        return cast(SessionRepository, self._session_repository)

    @property
    def request_repository(self) -> RequestRepository:
        return cast(RequestRepository, self._request_repository)

    @property
    def tool_setup_repository(self) -> ToolSetupRepository:
        """The setup pages tasks wait on (assistant_gateway.application.tool_setup)."""

        return cast(ToolSetupRepository, self._tool_setup_repository)

    @property
    def credential_collection_repository(self) -> CredentialCollectionRepository:
        return cast(CredentialCollectionRepository, self._credential_collection_repository)

    @property
    def delivery_repository(self) -> DeliveryRepository:
        return cast(DeliveryRepository, self._delivery_repository)

    @property
    def transcript_repository(self) -> TranscriptRepository:
        return cast(TranscriptRepository, self._transcript_repository)

    def identity_unit_of_work(self) -> IdentityUnitOfWork:
        return self._repository.identity_unit_of_work()

    def request_unit_of_work(self) -> RequestUnitOfWork:
        return self._repository.request_unit_of_work()

    def delivery_unit_of_work(self) -> DeliveryUnitOfWork:
        return self._repository.delivery_unit_of_work()

    def transcript_unit_of_work(self) -> TranscriptUnitOfWork:
        return self._repository.transcript_unit_of_work()

    def get_session(self, session_key: str) -> Optional[SessionRecord]:
        return self._repository.get_session(session_key)

    def get_session_by_conversation_id(
        self, conversation_id: str
    ) -> Optional[SessionRecord]:
        return self._repository.get_session_by_conversation_id(conversation_id)

    def get_session_conversation(
        self, conversation_id: str
    ) -> Optional[SessionConversationRecord]:
        return self._repository.get_session_conversation(conversation_id)

    def register_conversation_session(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        self._repository.register_conversation_session(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def list_session_conversations(
        self,
        session_key: str,
        *,
        limit: int = 20,
    ) -> list[SessionConversationListRecord]:
        return self._repository.list_session_conversations(session_key, limit=limit)

    def set_active_conversation(
        self, *, session_key: str, conversation_id: str
    ) -> Optional[SessionRecord]:
        existing = self.get_session(session_key)
        mapping = self.get_session_conversation(conversation_id)
        if existing is None or mapping is None or mapping.session_key != session_key:
            return None
        self.upsert_session(
            session_key=session_key,
            session_alias=existing.session_alias,
            conversation_id=conversation_id,
            last_task_id=mapping.last_task_id,
            manager_agent_id=existing.manager_agent_id,
            channel_name=existing.channel_name,
            user_id=existing.user_id,
            chat_id=existing.chat_id,
            thread_id=existing.thread_id,
            status=existing.status,
            owner_canonical_user_id=existing.owner_canonical_user_id,
        )
        return self.get_session(session_key)

    def list_sessions(
        self,
        *,
        limit: int = 50,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionRecord]:
        return self._repository.list_sessions(
            limit=limit,
            status=status,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def count_sessions(
        self,
        *,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> int:
        return self._repository.count_sessions(
            status=status, owner_canonical_user_id=owner_canonical_user_id
        )

    def list_sessions_with_preview(
        self,
        *,
        limit: int = 7,
        offset: int = 0,
        status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionListRecord]:
        return self._repository.list_sessions_with_preview(
            limit=limit,
            offset=offset,
            status=status,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def list_web_chat_threads(
        self,
        *,
        owner_canonical_user_id: str | None,
        channel_name: str,
        chat_id: str,
        limit: int,
    ) -> list[WebChatThreadRecord]:
        return self._repository.list_web_chat_threads(
            owner_canonical_user_id=owner_canonical_user_id,
            channel_name=channel_name,
            chat_id=chat_id,
            limit=limit,
        )

    def search_sessions(
        self,
        query_text: str,
        *,
        limit: int = 20,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[SessionSearchRecord]:
        return self._repository.search_sessions(
            query_text,
            limit=limit,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_session_by_alias(self, session_alias: str) -> Optional[SessionRecord]:
        return self._repository.get_session_by_alias(session_alias)

    def resolve_session_reference(
        self,
        reference: str,
        *,
        owner_canonical_user_id: Optional[str] = None,
    ) -> Optional[SessionRecord]:
        return self._repository.resolve_session_reference(
            reference,
            owner_canonical_user_id=owner_canonical_user_id,
        )

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
        return self._repository.create_session(
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
        self._repository.upsert_session(
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

    def set_session_alias(self, session_key: str, session_alias: Optional[str]) -> None:
        self._repository.set_session_alias(session_key, session_alias)

    def set_session_status(self, session_key: str, status: str) -> bool:
        return self._repository.set_session_status(session_key, status)

    def set_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
        session_key: str,
    ) -> None:
        self._repository.set_active_session_for_channel(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            session_key=session_key,
        )

    def get_active_session_for_channel(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: Optional[str] = None,
    ) -> Optional[SessionRecord]:
        return self._repository.get_active_session_for_channel(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )

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
        active_task_id: Optional[str] = None,
        run_state: Optional[str] = None,
    ) -> None:
        self._repository.record_request(
            correlation_id=correlation_id,
            session_key=session_key,
            request_kind=request_kind,
            request_text=request_text,
            conversation_id=conversation_id,
            followup_to_task_id=followup_to_task_id,
            status=status,
            owner_canonical_user_id=owner_canonical_user_id,
            active_task_id=active_task_id,
            run_state=run_state,
        )

    def get_request(self, correlation_id: str) -> Optional[RequestRecord]:
        return self._repository.get_request(correlation_id)

    def update_request_status(self, correlation_id: str, status: str) -> None:
        self._repository.update_request_status(correlation_id, status)

    def update_request_coordination(
        self,
        correlation_id: str,
        *,
        status: str,
        active_task_id: str,
    ) -> None:
        self._repository.update_request_coordination(
            correlation_id,
            status=status,
            active_task_id=active_task_id,
        )

    def list_requests_for_session(
        self, session_key: str, *, limit: int = 5
    ) -> list[RequestRecord]:
        return self._repository.list_requests_for_session(session_key, limit=limit)

    def add_deferred_delivery(
        self,
        *,
        session_key: str,
        origin_kind: str,
        payload: dict[str, object],
        rendered_text: str,
        available_after: str,
        correlation_id: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> str:
        return self._repository.add_deferred_delivery(
            session_key=session_key,
            origin_kind=origin_kind,
            payload=payload,
            rendered_text=rendered_text,
            available_after=available_after,
            correlation_id=correlation_id,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_due_deliveries(self, now_iso: Optional[str] = None) -> list[DeliveryRecord]:
        return self._repository.get_due_deliveries(now_iso=now_iso)

    def mark_delivery_delivered(self, delivery_id: str) -> None:
        self._repository.mark_delivery_delivered(delivery_id)

    def list_pending_deliveries(
        self, session_key: Optional[str] = None
    ) -> list[DeliveryRecord]:
        return self._repository.list_pending_deliveries(session_key=session_key)

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
        self._repository.upsert_attachment(
            attachment_id=attachment_id,
            session_key=session_key,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            filename=filename,
            content_type=content_type,
            kind=kind,
            size_bytes=size_bytes,
            sha256=sha256,
            local_path=local_path,
            nats_bucket=nats_bucket,
            nats_object_name=nats_object_name,
            expires_at=expires_at,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_attachment(self, attachment_id: str) -> Optional[AttachmentRecord]:
        return self._repository.get_attachment(attachment_id)

    def delete_uploads_created_before(self, cutoff: str, *, uploads_dir: str, limit: int) -> tuple[str, ...]:
        return self._repository.delete_uploads_created_before(cutoff, uploads_dir=uploads_dir, limit=limit)

    def update_attachment_nats_ref(
        self,
        *,
        attachment_id: str,
        nats_bucket: str,
        nats_object_name: str,
    ) -> Optional[AttachmentRecord]:
        return self._repository.update_attachment_nats_ref(
            attachment_id=attachment_id,
            nats_bucket=nats_bucket,
            nats_object_name=nats_object_name,
        )

    def add_pending_query(
        self,
        *,
        session_key: str,
        correlation_id: str,
        task_query_id: str,
        parent_task_id: Optional[str],
        conversation_id: Optional[str],
        query_text: str,
        query_payload_json: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> PendingQueryAdded:
        return self._repository.add_pending_query(
            session_key=session_key,
            correlation_id=correlation_id,
            task_query_id=task_query_id,
            parent_task_id=parent_task_id,
            conversation_id=conversation_id,
            query_text=query_text,
            query_payload_json=query_payload_json,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_pending_query(self, task_query_id: str) -> Optional[PendingQueryRecord]:
        return self._repository.get_pending_query(task_query_id)

    def list_open_pending_queries(self, session_key: str) -> list[PendingQueryRecord]:
        return self._repository.list_open_pending_queries(session_key)

    def mark_pending_query_shown(self, task_query_id: str) -> bool:
        return self._repository.mark_pending_query_shown(task_query_id)

    def close_pending_query(self, task_query_id: str, *, status: str) -> bool:
        return self._repository.close_pending_query(task_query_id, status=status)

    def reopen_answered_pending_query(self, task_query_id: str) -> bool:
        return self._repository.reopen_answered_pending_query(task_query_id)

    def close_open_pending_queries_for_task(
        self, *, correlation_id: str, parent_task_id: str, status: str
    ) -> list[PendingQueryRecord]:
        return self._repository.close_open_pending_queries_for_task(
            correlation_id=correlation_id, parent_task_id=parent_task_id, status=status
        )

    def expire_pending_queries(self, *, cutoff_iso: str) -> int:
        return self._repository.expire_pending_queries(cutoff_iso=cutoff_iso)

    def upsert_scheduled_target(
        self,
        *,
        scheduled_job_id: str,
        session_key: str,
        conversation_id: Optional[str],
        owner_canonical_user_id: Optional[str] = None,
    ) -> None:
        self._repository.upsert_scheduled_target(
            scheduled_job_id=scheduled_job_id,
            session_key=session_key,
            conversation_id=conversation_id,
            owner_canonical_user_id=owner_canonical_user_id,
        )

    def get_scheduled_target(
        self, scheduled_job_id: str
    ) -> Optional[ScheduledTargetRecord]:
        return self._repository.get_scheduled_target(scheduled_job_id)

    def get_scheduled_target_by_conversation_id(
        self, conversation_id: str
    ) -> Optional[ScheduledTargetRecord]:
        return self._repository.get_scheduled_target_by_conversation_id(conversation_id)

    def remove_scheduled_target(self, scheduled_job_id: str) -> bool:
        return self._repository.remove_scheduled_target(scheduled_job_id)

    def create_gateway_user(
        self,
        *,
        canonical_user_id: Optional[str] = None,
        display_name: str,
        timezone: str,
        metadata: Optional[dict] = None,
        status: str = "active",
    ) -> GatewayUserRecord:
        return self._repository.create_gateway_user(
            canonical_user_id=canonical_user_id,
            display_name=display_name,
            timezone=timezone,
            metadata=metadata,
            status=status,
        )

    def get_gateway_user(self, canonical_user_id: str) -> Optional[GatewayUserRecord]:
        return self._repository.get_gateway_user(canonical_user_id)

    def list_gateway_users(
        self,
        *,
        limit: int = 100,
    ) -> list[GatewayUserRecord]:
        return self._repository.list_gateway_users(limit=limit)

    def set_gateway_user_status(
        self, canonical_user_id: str, status: str
    ) -> Optional[GatewayUserRecord]:
        return self._repository.set_gateway_user_status(canonical_user_id, status)

    def update_gateway_user_last_seen(
        self, canonical_user_id: str, *, seen_at_utc: Optional[str] = None
    ) -> None:
        self._repository.update_gateway_user_last_seen(
            canonical_user_id, seen_at_utc=seen_at_utc
        )

    def set_gateway_user_default_channel(
        self,
        *,
        canonical_user_id: str,
        channel_name: Optional[str],
        chat_id: Optional[str],
        thread_id: Optional[str],
    ) -> Optional[GatewayUserRecord]:
        return self._repository.set_gateway_user_default_channel(
            canonical_user_id=canonical_user_id,
            channel_name=channel_name,
            chat_id=chat_id,
            thread_id=thread_id,
        )

    def get_active_channel_binding(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> Optional[GatewayUserChannelBindingRecord]:
        return self._repository.get_active_channel_binding(
            channel_name=channel_name,
            external_user_id=external_user_id,
            chat_scope=chat_scope,
        )

    def list_channel_bindings_for_user(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserChannelBindingRecord]:
        return self._repository.list_channel_bindings_for_user(
            canonical_user_id, status=status
        )

    def create_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: Optional[dict] = None,
    ) -> GatewayUserChannelBindingRecord:
        return self._repository.create_channel_binding(
            canonical_user_id=canonical_user_id,
            channel_name=channel_name,
            external_user_id=external_user_id,
            chat_scope=chat_scope,
            metadata=metadata,
        )

    def replace_channel_binding(
        self,
        *,
        canonical_user_id: str,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
        metadata: Optional[dict] = None,
    ) -> GatewayUserChannelBindingRecord:
        return self._repository.replace_channel_binding(
            canonical_user_id=canonical_user_id,
            channel_name=channel_name,
            external_user_id=external_user_id,
            chat_scope=chat_scope,
            metadata=metadata,
        )

    def create_web_user_token(
        self,
        *,
        token_hash: str,
        canonical_user_id: str,
        label: str = "",
        status: str = "active",
        expires_at_utc: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> GatewayUserWebTokenRecord:
        return self._repository.create_web_user_token(
            token_hash=token_hash,
            canonical_user_id=canonical_user_id,
            label=label,
            status=status,
            expires_at_utc=expires_at_utc,
            metadata=metadata,
        )

    def resolve_web_user_token(
        self,
        *,
        token_hash: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebTokenRecord]:
        return self._repository.resolve_web_user_token(
            token_hash=token_hash, now_utc=now_utc
        )

    def activate_web_user_token(
        self,
        *,
        token_id: str,
        canonical_user_id: str,
        expires_at_utc: Optional[str],
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebTokenRecord]:
        return self._repository.activate_web_user_token(
            token_id=token_id,
            canonical_user_id=canonical_user_id,
            expires_at_utc=expires_at_utc,
            now_utc=now_utc,
        )

    def revoke_web_user_token(
        self,
        token_id: str,
        *,
        canonical_user_id: Optional[str] = None,
        required_status: Optional[str] = None,
    ) -> bool:
        return self._repository.revoke_web_user_token(
            token_id,
            canonical_user_id=canonical_user_id,
            required_status=required_status,
        )

    def create_web_user_invite(
        self,
        *,
        invite_hash: str,
        canonical_user_id: str,
        label: str,
        expires_at_utc: str,
        created_by_canonical_user_id: str,
        metadata: Optional[dict] = None,
    ) -> GatewayUserWebInviteRecord:
        return self._repository.create_web_user_invite(
            invite_hash=invite_hash,
            canonical_user_id=canonical_user_id,
            label=label,
            expires_at_utc=expires_at_utc,
            created_by_canonical_user_id=created_by_canonical_user_id,
            metadata=metadata,
        )

    def create_gateway_user_with_web_invite(
        self,
        *,
        display_name: str,
        timezone: str,
        user_metadata: Optional[dict],
        team_id: str,
        invite_hash: str,
        invite_label: str,
        invite_expires_at_utc: str,
        created_by_canonical_user_id: str,
        invite_metadata: Optional[dict] = None,
    ) -> tuple[
        GatewayUserRecord, GatewayUserTeamMembershipRecord, GatewayUserWebInviteRecord
    ]:
        return self._repository.create_gateway_user_with_web_invite(
            display_name=display_name,
            timezone=timezone,
            user_metadata=user_metadata,
            team_id=team_id,
            invite_hash=invite_hash,
            invite_label=invite_label,
            invite_expires_at_utc=invite_expires_at_utc,
            created_by_canonical_user_id=created_by_canonical_user_id,
            invite_metadata=invite_metadata,
        )

    def consume_web_user_invite(
        self,
        *,
        invite_hash: str,
        team_id: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserWebInviteRecord]:
        return self._repository.consume_web_user_invite(
            invite_hash=invite_hash,
            team_id=team_id,
            now_utc=now_utc,
        )

    def create_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str = "member",
        status: str = "active",
    ) -> GatewayUserTeamMembershipRecord:
        return self._repository.create_team_membership(
            canonical_user_id=canonical_user_id,
            team_id=team_id,
            role=role,
            status=status,
        )

    def list_team_memberships(
        self,
        canonical_user_id: str,
        *,
        status: Optional[str] = None,
    ) -> list[GatewayUserTeamMembershipRecord]:
        return self._repository.list_team_memberships(canonical_user_id, status=status)

    def change_team_membership_role(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        role: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> Optional[GatewayUserTeamMembershipRecord]:
        return self._repository.change_team_membership_role(
            canonical_user_id=canonical_user_id,
            team_id=team_id,
            role=role,
            keep_holders_of=keep_holders_of,
        )

    def remove_team_membership(
        self,
        *,
        canonical_user_id: str,
        team_id: str,
        keep_holders_of: tuple[tuple[str, ...], ...] = (),
    ) -> Optional[GatewayUserTeamMembershipRecord]:
        return self._repository.remove_team_membership(
            canonical_user_id=canonical_user_id,
            team_id=team_id,
            keep_holders_of=keep_holders_of,
        )

    def revoke_gateway_user_sign_ins(
        self, canonical_user_id: str, *, now_utc: str
    ) -> GatewayUserSignInRevocation:
        return self._repository.revoke_gateway_user_sign_ins(
            canonical_user_id, now_utc=now_utc
        )

    def forget_person_records(self, canonical_user_id: str) -> GatewayPersonRecordsDeleted:
        return self._repository.forget_person_records(canonical_user_id)

    def delete_history_records(
        self, scope: GatewayHistoryScope, *, removable_thread: Callable[[SessionRecord], bool]
    ) -> GatewayHistoryDeleted:
        return self._repository.delete_history_records(scope, removable_thread=removable_thread)

    def list_team_members(self, team_id: str) -> list[GatewayUserTeamMembershipRecord]:
        return self._repository.list_team_members(team_id)

    def list_team_people(self, team_id: str) -> list[GatewayTeamPersonRecord]:
        return self._repository.list_team_people(team_id)

    def create_link_code(
        self,
        *,
        link_code_hash: str,
        canonical_user_id: str,
        expires_at_utc: str,
        created_by_channel: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> GatewayUserLinkCodeRecord:
        return self._repository.create_link_code(
            link_code_hash=link_code_hash,
            canonical_user_id=canonical_user_id,
            expires_at_utc=expires_at_utc,
            created_by_channel=created_by_channel,
            metadata=metadata,
        )

    def consume_link_code(
        self,
        *,
        link_code_hash: str,
        now_utc: Optional[str] = None,
    ) -> Optional[GatewayUserLinkCodeRecord]:
        return self._repository.consume_link_code(
            link_code_hash=link_code_hash, now_utc=now_utc
        )

    def record_gateway_user_event(
        self,
        *,
        event_type: str,
        canonical_user_id: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> str:
        return self._repository.record_gateway_user_event(
            event_type=event_type,
            canonical_user_id=canonical_user_id,
            payload=payload,
        )

    def gateway_user_event_types(self, *, canonical_user_id: str, event_types: Sequence[str]) -> set[str]:
        return self._repository.gateway_user_event_types(canonical_user_id=canonical_user_id, event_types=event_types)
