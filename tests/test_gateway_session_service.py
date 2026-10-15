from __future__ import annotations

from unittest.mock import Mock

import pytest

from assistant_gateway.application.session_service import (
    GatewaySessionService,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository


class _FixedIds:
    def new_correlation_id(self) -> str:
        return "corr_id_fixed"

    def new_local_correlation_id(self) -> str:
        return "local_corr_fixed"

    def new_scheduled_job_id(self) -> str:
        return "schedule_fixed"

    def new_session_key(self) -> str:
        return "session_fixed"

    def new_conversation_id(self) -> str:
        return "conversation_fixed"


class _OwnerAuthorizer:
    def can_access(
        self,
        *,
        actor_canonical_user_id: str | None,
        owner_canonical_user_id: str | None,
        **_kwargs: object,
    ) -> bool:
        return (
            actor_canonical_user_id is not None
            and actor_canonical_user_id == owner_canonical_user_id
        )


def _service(repository: Mock) -> GatewaySessionService:
    return GatewaySessionService(
        repository=repository,
        id_generator=_FixedIds(),
        authorizer=_OwnerAuthorizer(),
        team_id="team_test",
    )


def _session(
    *,
    session_key: str = "session_existing",
    conversation_id: str = "conversation_existing",
    owner_canonical_user_id: str | None = "user_owner",
) -> SessionRecord:
    return SessionRecord(
        session_key=session_key,
        session_alias=None,
        conversation_id=conversation_id,
        last_task_id=None,
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="external_1",
        chat_id="chat_1",
        thread_id=None,
        status="active",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        owner_canonical_user_id=owner_canonical_user_id,
    )


def _repository() -> Mock:
    repository = Mock(spec=SessionRepository)

    def create_session(**kwargs: object) -> SessionRecord:
        return SessionRecord(
            session_key=str(kwargs["session_key"]),
            session_alias=kwargs.get("session_alias"),  # type: ignore[arg-type]
            conversation_id=str(kwargs["conversation_id"]),
            last_task_id=None,
            manager_agent_id=kwargs.get("manager_agent_id"),  # type: ignore[arg-type]
            channel_name=kwargs.get("channel_name"),  # type: ignore[arg-type]
            user_id=kwargs.get("user_id"),  # type: ignore[arg-type]
            chat_id=kwargs.get("chat_id"),  # type: ignore[arg-type]
            thread_id=kwargs.get("thread_id"),  # type: ignore[arg-type]
            status=str(kwargs.get("status", "active")),
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            owner_canonical_user_id=kwargs.get(  # type: ignore[arg-type]
                "owner_canonical_user_id"
            ),
        )

    repository.create_session.side_effect = create_session
    return repository


def test_session_service_fails_closed_for_other_owner() -> None:
    repository = _repository()
    repository.resolve_session_reference.return_value = _session(
        owner_canonical_user_id="user_a"
    )
    service = _service(repository)

    with pytest.raises(PermissionError, match="Session not found"):
        service.resolve_session(
            requested_session_key="session_existing",
            default_session_key="session_default",
            manager_agent_id="manager_1",
            owner_canonical_user_id="user_b",
            default_owner_canonical_user_id="user_b",
        )

    repository.upsert_session.assert_not_called()
    repository.create_session.assert_not_called()


def test_session_service_does_not_treat_two_missing_owners_as_access() -> None:
    repository = _repository()
    repository.resolve_session_reference.return_value = _session(
        owner_canonical_user_id=None
    )
    service = _service(repository)

    with pytest.raises(PermissionError, match="Session not found"):
        service.resolve_session(
            requested_session_key="session_existing",
            default_session_key="session_default",
            manager_agent_id="manager_1",
            owner_canonical_user_id=None,
            default_owner_canonical_user_id=None,
        )
