"""Pinned-memory and topic command handler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands.context import (
    DEBUG_ONLY_COMMAND_NOTICE,
    GatewayActorContext,
)


@runtime_checkable
class GatewayMemoryTopicCommandPort(Protocol):
    """Manager-backed memory and topic use cases exposed to commands."""

    async def execute_pinned_memory_command(
        self,
        *,
        bank: str,
        command: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        session_key: str | None = None,
    ) -> str: ...

    async def execute_topic_command(
        self,
        *,
        command: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        session_key: str | None = None,
    ) -> tuple[str | None, str]: ...


@dataclass(frozen=True)
class MemoryTopicCommandRequest:
    """Input for one pinned-memory or topic command."""

    command: str
    current_session_key: str | None
    actor: GatewayActorContext
    debug_commands_enabled: bool = True


@dataclass(frozen=True)
class MemoryTopicCommandResult:
    """Session selection and text produced by a memory/topic command."""

    session_key: str | None
    text: str


@runtime_checkable
class GatewayMemoryTopicCommands(Protocol):
    """Execute typed pinned-memory and topic commands."""

    async def execute(
        self, request: MemoryTopicCommandRequest
    ) -> MemoryTopicCommandResult:
        """Execute one memory or topic command."""


class MemoryTopicCommandHandler:
    """Parse memory/topic commands and invoke only their manager use cases."""

    def __init__(self, operations: GatewayMemoryTopicCommandPort) -> None:
        self._operations = operations

    async def execute(
        self,
        request: MemoryTopicCommandRequest,
    ) -> MemoryTopicCommandResult:
        command = request.command
        actor = request.actor
        if command == "/coremem" or command.startswith("/coremem "):
            text = await self._execute_memory(
                bank="coremem",
                command=command,
                actor=actor,
                session_key=request.current_session_key,
            )
            return MemoryTopicCommandResult(request.current_session_key, text)
        if command == "/usermem" or command.startswith("/usermem "):
            text = await self._execute_memory(
                bank="usermem",
                command=command,
                actor=actor,
                session_key=request.current_session_key,
            )
            return MemoryTopicCommandResult(request.current_session_key, text)
        if command == "/switch_topic" or command.startswith("/switch_topic "):
            if not request.debug_commands_enabled:
                return MemoryTopicCommandResult(
                    request.current_session_key,
                    DEBUG_ONLY_COMMAND_NOTICE,
                )
            if command == "/switch_topic":
                return MemoryTopicCommandResult(
                    request.current_session_key,
                    "Usage: /switch_topic <tag>",
                )
        return await self._execute_topic(request)

    async def _execute_memory(
        self,
        *,
        bank: str,
        command: str,
        actor: GatewayActorContext,
        session_key: str | None,
    ) -> str:
        return await self._operations.execute_pinned_memory_command(
            bank=bank,
            command=command,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            session_key=session_key,
        )

    async def _execute_topic(
        self,
        request: MemoryTopicCommandRequest,
    ) -> MemoryTopicCommandResult:
        actor = request.actor
        session_key, text = await self._operations.execute_topic_command(
            command=request.command,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            session_key=request.current_session_key,
        )
        return MemoryTopicCommandResult(session_key, text)
