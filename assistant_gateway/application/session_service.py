"""Gateway session use cases over the session repository port."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from assistant_gateway.application.ports import (
    GatewayIdGenerator,
    GatewayPrivateResourceAuthorizer,
)
from assistant_gateway.application.private_resources import (
    PRIVATE_ACTION_READ,
    RESOURCE_GATEWAY_SESSION,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository


@dataclass(frozen=True)
class GatewaySessionRoute:
    """External route metadata persisted with a gateway session."""

    channel_name: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None


class GatewaySessionService:
    """Own session creation, lookup, owner checks, and active-route selection."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        id_generator: GatewayIdGenerator,
        authorizer: GatewayPrivateResourceAuthorizer,
        team_id: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._id_generator = id_generator
        self._authorizer = authorizer
        self._team_id = team_id
        self._logger = logger or logging.getLogger(__name__)

    def start_new_session(
        self,
        *,
        manager_agent_id: str | None,
        owner_canonical_user_id: str | None,
        route: GatewaySessionRoute = GatewaySessionRoute(),
        parallel_thread: bool = False,
    ) -> SessionRecord:
        return self._repository.create_session(
            session_key=self._id_generator.new_session_key(),
            conversation_id=self._id_generator.new_conversation_id(),
            manager_agent_id=manager_agent_id,
            channel_name=route.channel_name,
            user_id=route.user_id,
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            owner_canonical_user_id=owner_canonical_user_id,
            parallel_thread=parallel_thread,
        )

    def resolve_session(
        self,
        *,
        requested_session_key: str | None,
        default_session_key: str,
        manager_agent_id: str | None,
        owner_canonical_user_id: str | None,
        default_owner_canonical_user_id: str | None,
        route: GatewaySessionRoute = GatewaySessionRoute(),
        enforce_owner_scope: bool = True,
    ) -> SessionRecord:
        if requested_session_key:
            return self._resolve_requested_session(
                requested_session_key=requested_session_key,
                manager_agent_id=manager_agent_id,
                owner_canonical_user_id=owner_canonical_user_id,
                route=route,
                enforce_owner_scope=enforce_owner_scope,
            )

        if route.channel_name and route.user_id and route.chat_id:
            existing = self._repository.get_active_session_for_channel(
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                thread_id=route.thread_id,
            )
            if existing is not None:
                if not enforce_owner_scope or self._owner_allows_access(
                    existing,
                    owner_canonical_user_id,
                ):
                    return existing
                self._logger.warning(
                    "Ignoring active session with mismatched owner. "
                    "event=gateway_session_owner_mismatch session_key=%s "
                    "channel_name=%s chat_id=%s requested_owner=%s "
                    "existing_owner=%s",
                    existing.session_key,
                    route.channel_name,
                    route.chat_id,
                    owner_canonical_user_id or "-",
                    existing.owner_canonical_user_id or "-",
                )
            return self.start_new_session(
                manager_agent_id=manager_agent_id,
                owner_canonical_user_id=owner_canonical_user_id,
                route=route,
            )

        default_session = self._repository.get_session(default_session_key)
        if default_session is not None and (
            not enforce_owner_scope
            or self._owner_allows_access(
                default_session,
                default_owner_canonical_user_id,
            )
        ):
            return default_session
        if default_session is not None and enforce_owner_scope:
            raise PermissionError("Session not found.")
        return self._repository.create_session(
            session_key=default_session_key,
            conversation_id=self._id_generator.new_conversation_id(),
            manager_agent_id=manager_agent_id,
            owner_canonical_user_id=default_owner_canonical_user_id,
        )

    def switch_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ) -> SessionRecord | None:
        """Atomically select a conversation while preserving session routing."""

        existing = self._repository.get_session(session_key)
        if existing is None:
            return None
        self._repository.upsert_session(
            session_key=existing.session_key,
            session_alias=existing.session_alias,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
            manager_agent_id=existing.manager_agent_id,
            channel_name=existing.channel_name,
            user_id=existing.user_id,
            chat_id=existing.chat_id,
            thread_id=existing.thread_id,
            status=existing.status,
            owner_canonical_user_id=existing.owner_canonical_user_id,
        )
        return self._repository.get_session(session_key)

    def _resolve_requested_session(
        self,
        *,
        requested_session_key: str,
        manager_agent_id: str | None,
        owner_canonical_user_id: str | None,
        route: GatewaySessionRoute,
        enforce_owner_scope: bool,
    ) -> SessionRecord:
        existing = self._repository.resolve_session_reference(requested_session_key)
        if existing is None:
            return self._repository.create_session(
                session_key=requested_session_key,
                conversation_id=self._id_generator.new_conversation_id(),
                manager_agent_id=manager_agent_id,
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                thread_id=route.thread_id,
                owner_canonical_user_id=owner_canonical_user_id,
            )
        if enforce_owner_scope and not self._owner_allows_access(
            existing,
            owner_canonical_user_id,
        ):
            raise PermissionError("Session not found.")
        if (
            route.channel_name is not None
            or route.user_id is not None
            or route.chat_id is not None
        ):
            self._repository.upsert_session(
                session_key=existing.session_key,
                session_alias=existing.session_alias,
                conversation_id=existing.conversation_id,
                last_task_id=existing.last_task_id,
                manager_agent_id=existing.manager_agent_id,
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                thread_id=route.thread_id,
                owner_canonical_user_id=owner_canonical_user_id,
            )
            existing = self._repository.get_session(existing.session_key) or existing
        return existing

    def _owner_allows_access(
        self,
        session: SessionRecord,
        owner_canonical_user_id: str | None,
    ) -> bool:
        return self._authorizer.can_access(
            actor_canonical_user_id=(
                str(owner_canonical_user_id or "").strip() or None
            ),
            owner_canonical_user_id=(
                str(session.owner_canonical_user_id or "").strip() or None
            ),
            resource_type=RESOURCE_GATEWAY_SESSION,
            resource_id=session.session_key,
            action=PRIVATE_ACTION_READ,
            team_id=self._team_id,
        )
