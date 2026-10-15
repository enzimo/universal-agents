"""Typed contract for session and conversation administration commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands.context import GatewayActorContext

SESSION_EXACT_COMMAND_NAMES = frozenset(
    {
        "/new",
        "/list_conversations",
    }
)
SESSION_ARGUMENT_COMMAND_NAMES = frozenset(
    {
        "/list_sessions",
        "/preview_session",
        "/search_session",
        "/switch_session",
        "/switch_conversation",
        "/rename_session",
    }
)


def is_session_command(command: str) -> bool:
    """Return whether ``command`` belongs to session administration."""

    normalized = command.strip()
    if normalized in SESSION_EXACT_COMMAND_NAMES:
        return True
    return any(
        normalized == command_name or normalized.startswith(f"{command_name} ")
        for command_name in SESSION_ARGUMENT_COMMAND_NAMES
    )


@dataclass(frozen=True)
class SessionCommandRequest:
    """Input for one session or conversation administration command."""

    command: str
    current_session_key: str | None
    actor: GatewayActorContext
    debug_commands_enabled: bool = True


@dataclass(frozen=True)
class SessionCommandResult:
    """Active session selection and text produced by a session command."""

    session_key: str | None
    text: str


@runtime_checkable
class GatewaySessionCommands(Protocol):
    """Execute typed session and conversation administration commands."""

    async def execute(self, request: SessionCommandRequest) -> SessionCommandResult:
        """Execute one session-owned command."""
