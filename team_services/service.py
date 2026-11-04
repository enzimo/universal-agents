"""What the team services answer at the team's address (``svc.team``).

Each operation has one handler: the sessions here, and each family of people's
commands as it moves from the team lead's process (``team_services.memory`` first).
Operations not served here yet are answered ``unsupported`` at once (README "Refused
requests"), so a host asking for them never waits out its timeout.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Mapping

from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.live import Unsupported
from team_services.sessions import SessionService


Handler = Callable[[VerifiedFrame], Awaitable[dict[str, Any]]]


class TeamServices:
    def __init__(self, sessions: SessionService, families: Iterable[Mapping[str, Handler]] = ()) -> None:
        self._handlers: dict[str, Handler] = {
            "sessions.open": sessions.open,
            "sessions.renew": sessions.renew,
            "sessions.close": sessions.close,
        }
        for family in families:
            overlap = self._handlers.keys() & family.keys()
            if overlap:
                raise ValueError(f"Two handlers for {sorted(overlap)}: each operation has exactly one.")
            self._handlers.update(family)

    @property
    def served(self) -> frozenset[str]:
        return frozenset(self._handlers)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        handler = self._handlers.get(verified.frame.kind)
        if handler is None:
            raise Unsupported(verified.frame.kind)
        return await handler(verified)
