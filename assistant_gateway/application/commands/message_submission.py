"""Typed contract for interactive message-submission commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands.context import GatewayActorContext

TOUR_START_REQUEST = (
    "The user explicitly requested the onboarding tour via /tour. "
    "Start the `user-onboarding-guide` walkthrough now. "
    "Do not ask whether they want the tour first. "
    "Send exactly one short first tour step in this reply, then pause and wait for the user's next message before continuing. "
    "During the rest of the walkthrough, send at most one tour step per user turn. "
    "If the user says finish, stop, end, or cancel, pause the guide and remember where it stopped when possible."
)


def tour_start_request_text() -> str:
    """Return the stable manager-facing request used to start the tour."""

    return TOUR_START_REQUEST


@dataclass(frozen=True)
class MessageSubmissionCommandRequest:
    """Input for one command that submits an ordinary user message."""

    command: str
    current_session_key: str | None
    actor: GatewayActorContext
    query_handler: object | None = None


@dataclass(frozen=True)
class MessageSubmissionCommandResult:
    """Session selection and text returned by message submission."""

    session_key: str | None
    text: str


@runtime_checkable
class GatewayMessageSubmissionCommands(Protocol):
    """Execute typed message-submission commands."""

    async def execute(
        self,
        request: MessageSubmissionCommandRequest,
    ) -> MessageSubmissionCommandResult:
        """Execute one message-submission command."""
