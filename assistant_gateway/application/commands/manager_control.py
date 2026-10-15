"""Typed contract for interactive manager-control commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands.context import GatewayActorContext


@dataclass(frozen=True)
class ManagerControlCommandRequest:
    """Input for one manager-control command."""

    command: str
    current_session_key: str | None
    actor: GatewayActorContext


@dataclass(frozen=True)
class ManagerControlCommandResult:
    """Session selection and text returned by manager control."""

    session_key: str | None
    text: str


@runtime_checkable
class GatewayManagerControlCommands(Protocol):
    """Execute typed manager-control commands."""

    async def execute(
        self,
        request: ManagerControlCommandRequest,
    ) -> ManagerControlCommandResult:
        """Execute one manager-control command."""
