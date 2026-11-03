"""What the team services answer at the team's address (``svc.team``).

Each operation has one handler. Operations not served here yet are answered
``unsupported`` at once (README "Refused requests"), so a host asking for them never
waits out its timeout; they move here from the team lead's process step by step.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.live import Unsupported
from team_services.sessions import SessionService


class TeamServices:
    def __init__(self, sessions: SessionService) -> None:
        self._handlers: dict[str, Callable[[VerifiedFrame], Awaitable[dict[str, Any]]]] = {
            "sessions.open": sessions.open,
            "sessions.renew": sessions.renew,
            "sessions.close": sessions.close,
        }

    @property
    def served(self) -> frozenset[str]:
        return frozenset(self._handlers)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        handler = self._handlers.get(verified.frame.kind)
        if handler is None:
            raise Unsupported(verified.frame.kind)
        return await handler(verified)
