"""Interactive message-submission command application."""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

from assistant_gateway.application.commands import (
    GatewayCommandActorResolver,
    MessageSubmissionCommandRequest,
    MessageSubmissionCommandResult,
    resolve_actor_canonical_user_id,
    tour_start_request_text,
)
from assistant_gateway.application.response_coordinator import QueryHandler
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.domain.sessions import SessionRecord


@runtime_checkable
class GatewayMessageSubmissionCommandPort(
    GatewayCommandActorResolver,
    Protocol,
):
    """Session resolution and user submission needed by command handling."""

    def resolve_session(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> SessionRecord: ...

    async def send_user_message(
        self,
        text: str,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        query_handler: QueryHandler | None = None,
        canonical_user_id: str | None = None,
    ) -> GatewayResponse: ...


class GatewayMessageSubmissionCommandService:
    """Own commands that intentionally enter ordinary message submission."""

    def __init__(self, operations: GatewayMessageSubmissionCommandPort) -> None:
        self._operations = operations

    async def execute(
        self,
        request: MessageSubmissionCommandRequest,
    ) -> MessageSubmissionCommandResult:
        if request.command.strip() != "/tour":
            raise ValueError(
                f"Unsupported message-submission command: {request.command.strip()}"
            )

        actor = request.actor
        owner = resolve_actor_canonical_user_id(actor, self._operations)
        session = self._operations.resolve_session(
            session_key=request.current_session_key,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            canonical_user_id=owner,
        )
        response = await self._operations.send_user_message(
            tour_start_request_text(),
            session_key=session.session_key,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            query_handler=cast(QueryHandler | None, request.query_handler),
            canonical_user_id=owner,
        )
        return MessageSubmissionCommandResult(
            session_key=session.session_key,
            text=response.text,
        )
