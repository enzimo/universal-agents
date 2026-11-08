"""Which of a team's agents are live, from their signed records (contract README "State records").

Three report buckets and the team's session records say it, each verified as it is read:
- ``UA1_AGENTS``: each agent's state (starting, idle, busy, paused, exiting), whether it
  accepts work and its name, written by the host it runs on when it changes
  and refreshed every minute, expiring within three (``custom_agents.agent_host``);
- ``UA1_PLACEMENT``: which host runs each agent, written by the team services;
- ``UA1_HOSTS``: each host's liveness, written every 10 seconds, each record lasting 30
  (``custom_transport.protocol1.host_runtime``).
- ``UA1_CERTS``: the team's sessions, binding each role to its agent, type and host.

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
  an older record written back is refused while this reader runs, including after a
  deletion. The history is separate for each signing host. Agent ids are new at every
  start, so an agent's and its placement's keys never outlive one run; a host's liveness
  record is judged by its own expiry, which the bucket's age limit enforces too.
- An expired liveness record gives no change in the bucket, so the view is checked again
  every few seconds and listeners hear about agents whose liveness changed then.
- The view is rebuilt on the reader's loop and replaced as a whole, so callers on other
  threads (the agents of the team lead's process) read a consistent snapshot without locks.
- Failed watches reopen and temporary trust failures retry at each check. Stopping the
  reader clears its view and readiness. A listener can read ``departure_reason``.

The hosts' records also say which of their parts are not healthy (``components``), such as
an agent whose session the team services refused, which has no record of its own and so
no place in the view; ``host_components`` gives those of the team's live hosts, for the
gateway's Status page (plan 13B.5).

A host record that names another contract digest than this reader's (contract README
"Versions": while the label is a pre-release, hosts of one installation refuse a peer whose
digest differs) is authentic, but its body is not read. Its agents stay in the view, so
control messages for the tasks they hold still reach them, marked with their host's
digest (``TeamAgent.other_contract``): they take no new work (``TeamAgent.takes_work``) and
no agent is placed on that host. Each such host is logged once with both digests
(``event=team_agents_host_other_contract``), and ``other_contract_hosts`` lists the live
ones for the gateway's Status page.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from pydantic import MISSING

from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import VerifiedRecord
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.state_records import require_record_key

# How soon after a host's liveness record expires the view notices: an expired record gives
# no change in the bucket.
RECHECK_SECONDS = 5.0
_GONE_STATES = frozenset({"exiting", "stopped"})
_RETRYABLE_REFUSALS = frozenset({"unknown_key", "revocations_not_current"})
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
    # The contract digest the agent's host runs when it is not this reader's (module docstring).
    other_contract: Optional[str] = None

    @property
    def takes_work(self) -> bool:
        """Whether new work may go to the agent: it accepts work, and its host runs this reader's contract."""

        return self.accepting_work and self.state not in {"starting", "paused", "exiting", "stopped"} and self.other_contract is None


@dataclass(frozen=True)
class HostComponent:
    """A part of a live host that its record says is not healthy (``custom_agents.agent_host.REFUSAL_CODES``)."""

    host: str
    component: str
    state: str
    code: Optional[str]


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
        # Replay protection belongs to the signer as well as the key. A different host
        # cannot spend another host's sequences. Deletes never clear this history.
        self._sequences: dict[tuple[str, str, str], _Held] = {}
        self._deleted: set[tuple[str, str]] = set()
        self._pending: dict[tuple[str, str], bytes] = {}
        self._refusals: dict[tuple[str, str], str] = {}
        self._departures: dict[str, str] = {}
        # (host, its digest) of each host already logged as running another contract.
        self._other_contracts_logged: set[tuple[str, str]] = set()
        self._view: Mapping[str, TeamAgent] = MappingProxyType({})
        self._listeners: list[Callable[[frozenset[str]], None]] = []
        self.ready = asyncio.Event()
        self.running = False

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
        return matches[0] if len(matches) == 1 else None

    def host_components(self) -> list[HostComponent]:
        """The components the team's hosts list as not healthy, from their records that have not expired.

        Call it on the reader's loop. A host whose record expired is gone, and so is what it listed.
        """

        now = self._host.clock()
        found: list[HostComponent] = []
        for runtime, held in sorted(self._hosts.items()):
            record = held.record
            if record.body is None or record.signer is None or self.team not in record.signer.teams:
                continue
            expires_at = _present(record.document.expires_at)
            if expires_at is None or parse_timestamp(expires_at) <= now:
                continue
            found.extend(HostComponent(runtime, str(item.component), str(item.state), _present(item.code))
                         for item in record.body.components if item.state != "healthy")
        return found

    def other_contract_hosts(self) -> dict[str, str]:
        """The team's hosts whose record has not expired and names another contract, with that digest.

        Call it on the reader's loop.
        """

        now = self._host.clock()
        found: dict[str, str] = {}
        for runtime, held in sorted(self._hosts.items()):
            record = held.record
            if not record.other_contract or record.signer is None or self.team not in record.signer.teams:
                continue
            expires_at = _present(record.document.expires_at)
            if expires_at is not None and parse_timestamp(expires_at) > now and record.their_contract_sha256:
                found[runtime] = record.their_contract_sha256
        return found

    def lead(self) -> Optional[TeamAgent]:
        leads = [item for item in self._view.values() if item.is_lead]
        return leads[0] if len(leads) == 1 else None

    def departure_reason(self, agent_id: str) -> Optional[str]:
        """Why a previously visible agent left; listeners may read this in their callback."""

        return self._departures.get(agent_id)

    def listen(self, listener: Callable[[frozenset[str]], None]) -> None:
        """Call ``listener`` on the reader's loop with the ids whose view changed (appeared, changed or left)."""

        self._listeners.append(listener)

    # -- keeping it current (the reader's loop) -------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Watch the three buckets until ``stop``; ``ready`` is set once their current values are read."""

        self.running = True
        caught_up = [asyncio.Event() for _ in range(4)]
        sessions = f"session.{self._host.identity.installation}.{self.team}.*"
        watches = [
            asyncio.create_task(self._follow("agent", f"{self.team}.*", self._agents, caught_up[0], stop)),
            asyncio.create_task(self._follow("placement", f"{self.team}.*", self._placements, caught_up[1], stop)),
            asyncio.create_task(self._follow("host", ">", self._hosts, caught_up[2], stop)),
            asyncio.create_task(self._follow("agent_session", sessions, {}, caught_up[3], stop)),
        ]

        async def all_caught_up() -> None:
            await asyncio.gather(*(event.wait() for event in caught_up))
            await self._retry_pending()
            self._rebuild()
            self.ready.set()

        ready = asyncio.create_task(all_caught_up())
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._recheck_seconds)
                except asyncio.TimeoutError:
                    pass
                await self._retry_pending()
                if self.ready.is_set():
                    self._rebuild()
        finally:
            for task in (*watches, ready):
                task.cancel()
            await asyncio.gather(*watches, ready, return_exceptions=True)
            self.running = False
            self.ready.clear()
            self._departures.update({agent: "reader_stopped" for agent in self._view})
            self._publish({})

    async def _follow(self, record: str, keys: str, held: dict[str, _Held], caught_up: asyncio.Event,
                      stop: asyncio.Event) -> None:
        """Reopen failed watches; a failed host watch must not stop liveness expiry checks."""

        while not stop.is_set():
            try:
                await self._watch(record, keys, held, caught_up)
                if not stop.is_set():
                    raise RuntimeError("the record watch ended before the reader stopped")
            except Exception:
                self._logger.warning("A team record watch stopped; reopening it. event=team_agents_watch_failed "
                                     "team=%s record=%s", self.team, record, exc_info=True)
                held.clear()  # the replacement watch supplies a complete current snapshot
                for slot in list(self._pending):
                    if slot[0] == record:
                        self._pending.pop(slot, None)
                self._rebuild()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._recheck_seconds)
            except asyncio.TimeoutError:
                pass

    async def _watch(self, record: str, keys: str, held: dict[str, _Held], caught_up: asyncio.Event) -> None:
        records = self._host.records
        async for key, raw in records.watch(record, keys, caught_up=caught_up):
            name = key.removeprefix(f"{self.team}.") if record != "host" else key
            if raw is None:
                previous = held.get(name)
                if record == "agent" and previous is not None:
                    self._departures[name] = "exiting" if previous.record.body.state in _GONE_STATES else "record_removed"
                held.pop(name, None)
                self._pending.pop((record, key), None)
                self._deleted.add((record, key))
            else:
                self._pending[(record, key)] = raw
                await self._accept(record, key, raw, held)
            if self.ready.is_set():
                self._rebuild()

    async def _retry_pending(self) -> None:
        maps = {"agent": self._agents, "placement": self._placements, "host": self._hosts, "agent_session": {}}
        # Placements and sessions can arrive after their agents. Do not wait for a
        # state change on an otherwise idle agent to finish verifying its record.
        for (record, key), raw in list(self._pending.items()):
            await self._accept(record, key, raw, maps[record])

    async def _accept(self, record: str, key: str, raw: bytes, held: dict[str, _Held]) -> None:
        slot = (record, key)
        try:
            if record == "agent_session":
                await self._host.records.take_trust_record(record, key, now=self._host.clock())
                self._pending.pop(slot, None)
                return
            verified = await self._verified(record, key, raw, None)
            if verified is None:
                return
            name = key.removeprefix(f"{self.team}.") if record != "host" else key
            signer = verified.signer
            assert signer is not None
            if record == "agent":
                if not await self._agent_writer(verified, name):
                    return
            if self._pending.get(slot) != raw:
                return  # replaced or deleted while its trust records were being read
            self._hold(record, key, name, verified, held)
            if record == "host" and verified.other_contract and verified.their_contract_sha256:
                self._log_other_contract(name, verified.their_contract_sha256)
            # A new value may have arrived while a certificate or session was fetched.
            if self._pending.get(slot) == raw:
                self._pending.pop(slot, None)
            self._refusals.pop(slot, None)
        except FrameRefused as refused:
            self._refused(record, key, refused.code, refused.at)
            if refused.code not in _RETRYABLE_REFUSALS and self._pending.get(slot) == raw:
                self._pending.pop(slot, None)
        except Exception as error:
            self._refused(record, key, type(error).__name__, "transport")

    async def _agent_writer(self, verified: VerifiedRecord, name: str) -> bool:
        placement = self._placements.get(name)
        if placement is None:
            return False
        assert verified.signer is not None
        if placement.record.body.host != verified.signer.runtime:
            raise FrameRefused("wrong_issuer", "the agent's placement names another host", at="/writer")
        if placement.record.body.agent_type != verified.body.agent_type:
            raise FrameRefused("invalid_body", "the agent type differs from its placement", at="/body/agent_type")
        return await self._learn_role(verified)

    def _hold(self, record: str, key: str, name: str, verified: VerifiedRecord, held: dict[str, _Held]) -> None:
        assert verified.signer is not None
        history = (record, key, verified.signer.runtime)
        earlier = self._sequences.get(history)
        if earlier is not None and (verified.document.sequence < earlier.sequence or
                (verified.document.sequence == earlier.sequence and
                 (verified.digest != earlier.digest or (record, key) in self._deleted))):
            raise FrameRefused("replayed", at="/sequence")
        item = _Held(verified.document.sequence, verified.digest, verified)
        self._sequences[history] = held[name] = item
        self._deleted.discard((record, key))

    def _refused(self, record: str, key: str, code: str, at: str) -> None:
        if self._refusals.get((record, key)) == code:
            return
        self._refusals[(record, key)] = code
        self._logger.warning("A team record was refused and left out. event=team_agents_record_refused "
                             "record=%s key=%s code=%s at=%s", record, key, code, at)

    def _log_other_contract(self, runtime: str, theirs: str) -> None:
        if (runtime, theirs) in self._other_contracts_logged:
            return
        self._other_contracts_logged.add((runtime, theirs))
        self._logger.warning("A host runs another message contract than this reader: it gets no new work and no "
                             "agent is placed on it; control messages for the tasks it holds still reach it. "
                             "event=team_agents_host_other_contract team=%s host=%s their_contract_sha256=%s "
                             "contract_sha256=%s", self.team, runtime, theirs,
                             self._host.live.trust.store.contract_sha256)

    async def _verified(self, record: str, key: str, raw: bytes, held: Optional[_Held]) -> Optional[VerifiedRecord]:
        now = self._host.clock()
        sequence = None if held is None else (held.sequence, held.digest)
        trust = self._host.live.trust.store
        try:
            verified = trust.verify(raw, record=record, now=now, held=sequence)
        except FrameRefused as refused:
            if refused.code != "unknown_key" or not await self._host.fetch_trust(raw):
                raise
            verified = trust.verify(raw, record=record, now=now, held=sequence)
        require_record_key(verified, key)
        return verified

    async def _learn_role(self, verified: VerifiedRecord) -> bool:
        """Fetch a missing session; its role is checked against this agent on every rebuild."""

        session = str(verified.body.session)
        installation = self._host.identity.installation
        trust = self._host.live.trust.store
        found = trust.agent_session(installation, self.team, session)
        if found is None:
            await self._host.records.take_trust_record("agent_session", f"session.{installation}.{self.team}.{session}",
                                                       now=self._host.clock())
            found = trust.agent_session(installation, self.team, session)
        if found is None:
            self._refused("agent", str(verified.document.key), "unknown_key", "/body/session")
            return False
        assert verified.signer is not None
        if found.agent != verified.body.agent or found.runtime != verified.signer.runtime \
                or found.agent_type != verified.body.agent_type:
            raise FrameRefused("wrong_issuer", "the session belongs to another agent, host or type", at="/body/session")
        return True

    def _rebuild(self) -> None:
        now = self._host.clock()
        view: dict[str, TeamAgent] = {}
        for agent_id, held in self._agents.items():
            item = self._live_agent(agent_id, held, now)
            if item is not None:
                view[agent_id] = item
        for agent_id in self._view.keys() - view.keys():
            self._departures[agent_id] = self._absence_reason(agent_id)
        for agent_id in view:
            self._departures.pop(agent_id, None)
        self._publish(view)

    def _publish(self, view: dict[str, TeamAgent]) -> None:
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
        expires_at = _present(held.record.document.expires_at)
        if expires_at is None or parse_timestamp(expires_at) <= now:
            return None
        placed = placement.record.body
        host = self._hosts.get(signer.runtime)
        if placed.host != signer.runtime or placed.agent_type != body.agent_type or host is None:
            return None
        expires_at = _present(host.record.document.expires_at)
        if expires_at is None or parse_timestamp(expires_at) <= now:
            return None
        # The certificate the host record is signed with, which its body names; read from the signer, since the
        # body of a host on another contract is not read (module docstring).
        assert host.record.signer is not None  # hosts sign their own records
        if host.record.signer.membership_epoch < int(placed.host_epoch):
            return None
        session = str(body.session)
        found = self._host.live.trust.store.agent_session(self._host.identity.installation, self.team, session)
        if found is None or found.agent != agent_id or found.runtime != signer.runtime \
                or found.agent_type != body.agent_type or not found.covers(now) or found.ends_at <= now:
            return None
        return TeamAgent(agent=agent_id, agent_type=str(body.agent_type), display_name=_present(body.display_name),
                         state=str(body.state), accepting_work=bool(body.accepting_work),
                         since=parse_timestamp(str(body.since)), host=signer.runtime, session=session,
                         is_lead=found.agent_role == "lead",
                         other_contract=host.record.their_contract_sha256 if host.record.other_contract else None)

    def _absence_reason(self, agent_id: str) -> str:
        agent = self._agents.get(agent_id)
        if agent is None:
            return self._departures.get(agent_id, "record_removed")
        if agent.record.body.state in _GONE_STATES:
            return "exiting"
        if parse_timestamp(agent.record.document.expires_at) <= self._host.clock():
            return "record_expired"
        if agent_id not in self._placements:
            return "placement_removed"
        signer = agent.record.signer
        host = self._hosts.get(signer.runtime) if signer is not None else None
        if host is None or parse_timestamp(host.record.document.expires_at) <= self._host.clock():
            return "host_unavailable"
        return "session_or_placement_changed"


def _present(value: Any) -> Any:
    """A model's optional field, or None when the record leaves it out."""

    return None if value is MISSING else value
