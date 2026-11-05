"""Which of a team's agents are live, from their signed records (contract README "State records").

Three buckets together say it, each record verified as it is read:
- ``UA1_AGENTS``: each agent's state (starting, idle, busy, paused, exiting), whether it
  accepts work and its name, written by the host it runs on when it changes
  (``custom_agents.agent_host``);
- ``UA1_PLACEMENT``: which host runs each agent, written by the team services;
- ``UA1_HOSTS``: each host's liveness, written every 10 seconds, each record lasting 30
  (``custom_transport.protocol1.host_runtime``).

An agent is live while its record is not exiting, its placement names the host that signed
the record, and that host's liveness record has not expired and names a certificate of the
placement's membership epoch or later. Whether it is the team lead comes from its session
(``agent_role``), which the team services signed. This replaces the presence heartbeat:
nothing is broadcast, and a stopped host's agents drop out without anyone saying so, at
most 35 seconds after its last write: its record's 30 seconds plus this reader's 5-second
check, about the 30 seconds after which the presence heartbeat counted an agent gone.

How it reads:
- Each bucket is watched; a record that fails verification is logged and left out
  (``event=team_agents_record_refused``). The sequence held per key is kept in memory, so
  an older record written back is refused while this reader runs. Agent ids are new at every
  start, so an agent's and its placement's keys never outlive one run; a host's liveness
  record is judged by its own expiry, which the bucket's age limit enforces too.
- An expired liveness record gives no change in the bucket, so the view is checked again
  every few seconds and listeners hear about agents whose liveness changed then.
- The view is rebuilt on the reader's loop and replaced as a whole, so callers on other
  threads (the agents of the team lead's process) read a consistent snapshot without locks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from pydantic import MISSING

from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import VerifiedRecord
from custom_messages.wire.values import parse_timestamp

# How soon after a host's liveness record expires the view notices: an expired record gives
# no change in the bucket.
RECHECK_SECONDS = 5.0
_GONE_STATES = frozenset({"exiting", "stopped"})
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamAgent:
    """One live agent as its records say."""

    agent: str
    agent_type: str
    display_name: Optional[str]
    state: str
    accepting_work: bool
    since: datetime
    host: str
    session: str
    is_lead: bool


@dataclass(frozen=True)
class _Held:
    sequence: int
    digest: str
    record: VerifiedRecord


class TeamAgents:
    """A verified, live view of one team's agents (module docstring)."""

    def __init__(self, host: Any, team: str, *, logger: Optional[logging.Logger] = None,
                 recheck_seconds: float = RECHECK_SECONDS) -> None:
        # ``host`` is a started ``custom_transport.protocol1.host_runtime.MessageHost``.
        self._host = host
        self.team = team
        self._logger = logger or _LOGGER
        self._recheck_seconds = recheck_seconds
        self._agents: dict[str, _Held] = {}
        self._placements: dict[str, _Held] = {}
        self._hosts: dict[str, _Held] = {}
        self._roles: dict[str, str] = {}
        self._view: Mapping[str, TeamAgent] = MappingProxyType({})
        self._listeners: list[Callable[[frozenset[str]], None]] = []
        self.ready = asyncio.Event()

    # -- reading the view (any thread) ----------------------------------------------------

    def live(self) -> Mapping[str, TeamAgent]:
        """Every live agent by id, as of the last change or check."""

        return self._view

    def agent(self, agent_id: str) -> Optional[TeamAgent]:
        return self._view.get(agent_id)

    def of_type(self, agent_type: str) -> list[TeamAgent]:
        return sorted((item for item in self._view.values() if item.agent_type == agent_type), key=lambda a: a.agent)

    def named(self, display_name: str) -> Optional[TeamAgent]:
        matches = [item for item in self._view.values() if item.display_name == display_name]
        return min(matches, key=lambda a: a.agent) if matches else None

    def lead(self) -> Optional[TeamAgent]:
        leads = [item for item in self._view.values() if item.is_lead]
        return min(leads, key=lambda a: a.agent) if leads else None

    def listen(self, listener: Callable[[frozenset[str]], None]) -> None:
        """Call ``listener`` on the reader's loop with the ids whose view changed (appeared, changed or left)."""

        self._listeners.append(listener)

    # -- keeping it current (the reader's loop) -------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Watch the three buckets until ``stop``; ``ready`` is set once their current values are read."""

        caught_up = [asyncio.Event() for _ in range(3)]
        watches = [
            asyncio.create_task(self._watch("agent", f"{self.team}.*", self._agents, caught_up[0])),
            asyncio.create_task(self._watch("placement", f"{self.team}.*", self._placements, caught_up[1])),
            asyncio.create_task(self._watch("host", ">", self._hosts, caught_up[2])),
        ]

        async def all_caught_up() -> None:
            await asyncio.gather(*(event.wait() for event in caught_up))
            self._rebuild()
            self.ready.set()

        ready = asyncio.create_task(all_caught_up())
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._recheck_seconds)
                except asyncio.TimeoutError:
                    pass
                for watch in watches:
                    if watch.done() and not watch.cancelled() and watch.exception() is not None:
                        raise watch.exception()  # type: ignore[misc]
                if self.ready.is_set():
                    self._rebuild()
        finally:
            for task in (*watches, ready):
                task.cancel()
            await asyncio.gather(*watches, ready, return_exceptions=True)

    async def _watch(self, record: str, keys: str, held: dict[str, _Held], caught_up: asyncio.Event) -> None:
        records = self._host.records
        async for key, raw in records.watch(record, keys, caught_up=caught_up):
            name = key.removeprefix(f"{self.team}.") if record != "host" else key
            if raw is None:
                held.pop(name, None)
            else:
                verified = await self._verified(record, key, raw, held.get(name))
                if verified is None:
                    continue
                held[name] = _Held(verified.document.sequence, verified.digest, verified)
                if record == "agent":
                    await self._learn_role(verified)
            if self.ready.is_set():
                self._rebuild()

    async def _verified(self, record: str, key: str, raw: bytes, held: Optional[_Held]) -> Optional[VerifiedRecord]:
        now = self._host.clock()
        sequence = None if held is None else (held.sequence, held.digest)
        trust = self._host.live.trust.store
        try:
            try:
                verified = trust.verify(raw, record=record, now=now, held=sequence)
            except FrameRefused as refused:
                if refused.code != "unknown_key" or not await self._host.fetch_trust(raw):
                    raise
                verified = trust.verify(raw, record=record, now=now, held=sequence)
        except FrameRefused as refused:
            if refused.code == "replayed" and held is not None and _same(raw, held):
                return None  # the record this reader holds, seen again
            self._logger.warning("A team record was refused and left out. event=team_agents_record_refused "
                                 "record=%s key=%s code=%s at=%s", record, key, refused.code, refused.at)
            return None
        return verified

    async def _learn_role(self, verified: VerifiedRecord) -> None:
        """Whether the record's session is the team lead's, from the session the team services signed."""

        session = str(verified.body.session)
        if session in self._roles:
            return
        installation = self._host.identity.installation
        trust = self._host.live.trust.store
        found = trust.agent_session(installation, self.team, session)
        if found is None:
            try:
                await self._host.records.take_trust_record("agent_session", f"session.{installation}.{self.team}.{session}",
                                                           now=self._host.clock())
            except FrameRefused as refused:
                self._logger.warning("An agent's session could not be read; it is not taken as the lead. "
                                     "event=team_agents_session_refused session=%s code=%s", session, refused.code)
            found = trust.agent_session(installation, self.team, session)
        if found is not None and found.agent == verified.body.agent:
            self._roles[session] = found.agent_role

    def _rebuild(self) -> None:
        now = self._host.clock()
        view: dict[str, TeamAgent] = {}
        for agent_id, held in self._agents.items():
            item = self._live_agent(agent_id, held, now)
            if item is not None:
                view[agent_id] = item
        changed = frozenset(agent_id for agent_id in set(view) | set(self._view)
                            if view.get(agent_id) != self._view.get(agent_id))
        self._view = MappingProxyType(view)
        if changed:
            for listener in list(self._listeners):
                try:
                    listener(changed)
                except Exception:
                    self._logger.exception("A team agents listener failed. event=team_agents_listener_failed")

    def _live_agent(self, agent_id: str, held: _Held, now: datetime) -> Optional[TeamAgent]:
        body = held.record.body
        signer = held.record.signer
        placement = self._placements.get(agent_id)
        if body.state in _GONE_STATES or signer is None or placement is None:
            return None
        placed = placement.record.body
        host = self._hosts.get(signer.runtime)
        if placed.host != signer.runtime or host is None:
            return None
        expires_at = _present(host.record.document.expires_at)
        if expires_at is None or parse_timestamp(expires_at) <= now:
            return None
        if int(host.record.body.certificate.membership_epoch) < int(placed.host_epoch):
            return None
        session = str(body.session)
        return TeamAgent(agent=agent_id, agent_type=str(body.agent_type), display_name=_present(body.display_name),
                         state=str(body.state), accepting_work=bool(body.accepting_work),
                         since=parse_timestamp(str(body.since)), host=signer.runtime, session=session,
                         is_lead=self._roles.get(session) == "lead")


def _present(value: Any) -> Any:
    """A model's optional field, or None when the record leaves it out."""

    return None if value is MISSING else value


def _same(raw: bytes, held: _Held) -> bool:
    try:
        return int(json.loads(raw).get("sequence", -1)) == held.sequence
    except ValueError:
        return False
