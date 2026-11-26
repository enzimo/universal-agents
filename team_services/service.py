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

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine, Iterable, Mapping, Optional

from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.live import Unsupported
from team_services.sessions import SessionService


Handler = Callable[[VerifiedFrame], Awaitable[dict[str, Any]]]
Upkeep = Callable[[], Awaitable[None]]
BackgroundRunner = Callable[[asyncio.Event], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class OperationFamily:
    """One family of people's commands: its handlers, the kinds that run side by side, and its upkeep."""

    handlers: Mapping[str, Handler]
    side_by_side: frozenset[str] = field(default_factory=frozenset)
    upkeep: Optional[Upkeep] = None
    # Called by the process after requests drain, before its connection closes.
    close: Optional[Callable[[], None]] = None
    # Called by the process after acquiring the team lock and before serving svc.team.
    prepare: Optional[Upkeep] = None
    # Long-running background task started once serving and cancelled immediately on LockLost or stop.
    run: Optional[BackgroundRunner] = None
    # Which protocol 1 service address serves these handlers ("svc.team" or "svc.scheduler").
    address: str = "svc.team"


class TeamServices:
    def __init__(self, sessions: SessionService, families: Iterable[OperationFamily] = ()) -> None:
        self._handlers_by_address: dict[str, dict[str, Handler]] = {
            "svc.team": {
                "sessions.open": sessions.open,
                "sessions.renew": sessions.renew,
                "sessions.close": sessions.close,
            },
        }
        side_by_side_by_address: dict[str, set[str]] = {"svc.team": set()}
        self.upkeep: list[Upkeep] = []
        self.prepare: list[Upkeep] = []
        self.runners: list[BackgroundRunner] = []
        for family in families:
            bucket = self._handlers_by_address.setdefault(family.address, {})
            overlap = bucket.keys() & family.handlers.keys()
            if overlap:
                raise ValueError(f"Two handlers for {sorted(overlap)} at {family.address}: each operation has exactly one.")
            bucket.update(family.handlers)
            side_by_side_by_address.setdefault(family.address, set()).update(
                family.side_by_side & family.handlers.keys()
            )
            if family.upkeep is not None:
                self.upkeep.append(family.upkeep)
            if family.prepare is not None:
                self.prepare.append(family.prepare)
            if family.run is not None:
                self.runners.append(family.run)
        self._handlers = self._handlers_by_address["svc.team"]
        self._side_by_side_by_address = {
            addr: frozenset(kinds) for addr, kinds in side_by_side_by_address.items()
        }
        self.side_by_side = self._side_by_side_by_address["svc.team"]

    @property
    def served(self) -> frozenset[str]:
        all_kinds: set[str] = set()
        for bucket in self._handlers_by_address.values():
            all_kinds.update(bucket)
        return frozenset(all_kinds)

    def served_at(self, address: str) -> frozenset[str]:
        return frozenset(self._handlers_by_address.get(address, ()))

    def side_by_side_at(self, address: str) -> frozenset[str]:
        return self._side_by_side_by_address.get(address, frozenset())

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await self.handle_address("svc.team", verified)

    async def handle_address(self, address: str, verified: VerifiedFrame) -> dict[str, Any]:
        handler = self._handlers_by_address.get(address, {}).get(verified.frame.kind)
        if handler is None:
            raise Unsupported(verified.frame.kind)
        return await handler(verified)
