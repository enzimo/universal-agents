"""What the team services answer at the team's address (``svc.team``).

Each operation has one handler: the sessions here, and each family of people's
commands as it moves from the team lead's process (``team_services.memory`` first).
Operations not served here yet are answered ``unsupported`` at once (README "Refused
requests"), so a host asking for them never waits out its timeout.

A family (``OperationFamily``) also names the kinds whose requests may run side by side
(``LiveHost.serve``; every other kind runs in arrival order) and the upkeep it needs
while the team services serve, which runs with theirs (``team_services.process``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.live import Unsupported
from team_services.sessions import SessionService


Handler = Callable[[VerifiedFrame], Awaitable[dict[str, Any]]]
Upkeep = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class OperationFamily:
    """One family of people's commands: its handlers, the kinds that run side by side, and its upkeep."""

    handlers: Mapping[str, Handler]
    side_by_side: frozenset[str] = field(default_factory=frozenset)
    upkeep: Optional[Upkeep] = None
    # Called by the process after requests drain, before its connection closes.
    close: Optional[Callable[[], None]] = None


class TeamServices:
    def __init__(self, sessions: SessionService, families: Iterable[OperationFamily] = ()) -> None:
        self._handlers: dict[str, Handler] = {
            "sessions.open": sessions.open,
            "sessions.renew": sessions.renew,
            "sessions.close": sessions.close,
        }
        side_by_side: set[str] = set()
        self.upkeep: list[Upkeep] = []
        for family in families:
            overlap = self._handlers.keys() & family.handlers.keys()
            if overlap:
                raise ValueError(f"Two handlers for {sorted(overlap)}: each operation has exactly one.")
            self._handlers.update(family.handlers)
            side_by_side |= family.side_by_side & family.handlers.keys()
            if family.upkeep is not None:
                self.upkeep.append(family.upkeep)
        self.side_by_side = frozenset(side_by_side)

    @property
    def served(self) -> frozenset[str]:
        return frozenset(self._handlers)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        handler = self._handlers.get(verified.frame.kind)
        if handler is None:
            raise Unsupported(verified.frame.kind)
        return await handler(verified)
