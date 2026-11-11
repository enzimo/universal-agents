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
- **Leaving for good.** An agent that drops out of the view has left for good
  (``left_for_good``) when its record's last state said it was exiting, or when its
  session has ended: the team services re-sign a session with its end when its host closes
  it, as an agent exits or as the host's next start cleans up after a crash. A host that
  only stopped reporting leaves the session open, and the agent may come back. The
  evidence often arrives after the agent has left the view: a reader that reconnects sees
  only the record's deletion, since the bucket keeps one value per key, and the placement's
  deletion can be read before the exiting record, which is then never accepted. So the last
  view entry of a departed agent is kept for ``DEPARTED_KEEP_SECONDS``
  (``departed_agent``), and listeners hear its id once more when the session record shows
  it ended, or when the exiting record arrives. The evidence is the team services' signed
  session record, or an exiting record from the agent's last host with its last session;
  another host cannot forge either. A closed session ends that run of the agent, not its
  work: work it had claimed lives in saved state, which the restarted agent takes over.
  A fresh reader also recovers departures directly from retained signed closed sessions,
  even if the agent's record and placement disappeared before the reader started. An
  agent attached after catch-up receives those retained departures too, once per requester.
- **Which session's end counts.** One run of an agent can hold several sessions: a refused
  renewal, a missing placement or a lost ``sessions.open`` reply opens another, with a later
  ``placed_at``, and the earlier one is never closed and runs out seven days later. So a
  session's end counts as the run's end only when no later session of the agent is known
  (``_session_order``), and, for an agent this reader did not see leave, only while no
  placement of the agent is held and its held record, if any, names a session this reader
  holds (``_run_may_go_on``). A placement means a newer session or a handover; a record
  naming an unknown session may name a newer one. When an agent's last session record is
  deleted (the team services keep them about 30 days past their end), what this reader kept
  about its departure is dropped.
- Failed watches reopen, and a refusal that can pass later is verified again at each check:
  the writer's certificate or session is not readable yet while hosts join
  (``unknown_key``), revocations are not current yet, or a record is dated just ahead of
  this host's clock (``not_yet_valid``). Placements and an idle agent's record are written
  rarely, so dropping them would hide a live agent until it next changed. Stopping the
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
from custom_messages.wire.trust import AgentSession, VerifiedRecord
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.state_records import require_record_key

# How soon after a host's liveness record expires the view notices: an expired record gives
# no change in the bucket.
RECHECK_SECONDS = 5.0
_GONE_STATES = frozenset({"exiting", "stopped"})
# Why an agent left, when it left for good (module docstring): its record said it was
# exiting, or its session was closed.
EXITING, SESSION_CLOSED = "exiting", "session_closed"
LEFT_FOR_GOOD = frozenset({EXITING, SESSION_CLOSED})
# How long a departed agent's last view entry is kept for evidence that it left for good.
DEPARTED_KEEP_SECONDS = 600.0
# Refusals that can pass later; the record is verified again at each check (module docstring).
_RETRYABLE_REFUSALS = frozenset({"unknown_key", "revocations_not_current", "not_yet_valid"})
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


@dataclass
class _Departed:
    """A departed agent's last view entry, when it left, and whether listeners heard it left for good."""

    last: TeamAgent
    since: datetime
    told: bool


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
        # Verified sessions also identify agents that exited before this reader started.
        self._sessions: dict[str, AgentSession] = {}
        # Replay protection belongs to the signer as well as the key. A different host
        # cannot spend another host's sequences. Deletes never clear this history.
        self._sequences: dict[tuple[str, str, str], _Held] = {}
        self._deleted: set[tuple[str, str]] = set()
        self._pending: dict[tuple[str, str], bytes] = {}
        self._refusals: dict[tuple[str, str], str] = {}
        self._departures: dict[str, str] = {}
        # Each departed agent's last view entry and when it left, while it is kept (module docstring).
        self._departed: dict[str, _Departed] = {}
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

    def left_for_good(self, agent_id: str) -> bool:
        """Whether a previously visible agent left for good: it said it was exiting, or its session ended."""

        return self._departures.get(agent_id) in LEFT_FOR_GOOD

    def departed_agent(self, agent_id: str) -> Optional[TeamAgent]:
        """A departed agent's last view entry, while it is kept (``DEPARTED_KEEP_SECONDS``)."""

        departed = self._departed.get(agent_id)
        return departed.last if departed is not None else None

    def departed_agents(self) -> Mapping[str, TeamAgent]:
        """Retained exits for a requester attached after the reader caught up."""

        return {agent: departed.last for agent, departed in self._departed.items() if self.left_for_good(agent)}

    def listen(self, listener: Callable[[frozenset[str]], None]) -> None:
        """Call ``listener`` on the reader's loop with the ids whose view changed (appeared, changed or left).

        A departed agent's id is heard once more when the reader learns, after it left, that
        it left for good (module docstring); ``left_for_good`` says so then.
        """

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
                self._remove(record, key, name, held)
            else:
                self._pending[(record, key)] = raw
                await self._accept(record, key, raw, held)
            if self.ready.is_set():
                self._rebuild()

    def _remove(self, record: str, key: str, name: str, held: dict[str, _Held]) -> None:
        """Forget a deleted record, keeping any last observation of an agent's exit."""

        if record == "agent_session":
            removed = self._sessions.pop(key, None)
            if removed is not None:
                self._forget_departure(removed.agent)
        previous = held.get(name)
        if record == "agent" and previous is not None and not self.left_for_good(name):
            self._departures[name] = EXITING if previous.record.body.state in _GONE_STATES else "record_removed"
        held.pop(name, None)
        self._pending.pop((record, key), None)
        self._deleted.add((record, key))

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
                await self._accept_session(key, raw)
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

    async def _accept_session(self, key: str, raw: bytes) -> None:
        """Keep only the verified session currently stored at this key."""

        await self._host.records.take_trust_record("agent_session", key, now=self._host.clock())
        slot = ("agent_session", key)
        if self._pending.get(slot) != raw:
            return
        session_id = key.removeprefix(f"session.{self._host.identity.installation}.{self.team}.")
        session = self._host.live.trust.store.agent_session(self._host.identity.installation, self.team, session_id)
        if session is not None:
            self._sessions[key] = session
        self._pending.pop(slot, None)

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
        leaving = self._view.keys() - view.keys()
        for agent_id in leaving:
            last = self._view[agent_id]
            reason = self._absence_reason(agent_id)
            if reason not in LEFT_FOR_GOOD:
                reason = self._left_for_good_evidence(agent_id, last, now) or reason
            self._departures[agent_id] = reason
            self._departed[agent_id] = _Departed(last, now, told=reason in LEFT_FOR_GOOD)
        for agent_id in view:
            self._departures.pop(agent_id, None)
            self._departed.pop(agent_id, None)
        late = self._late_departures(leaving, now)
        self._publish(view, late | self._unseen_exits(view, now))

    def _unseen_exits(self, view: Mapping[str, TeamAgent], now: datetime) -> frozenset[str]:
        """Recover missed exits from signed sessions, without needing a previous live view.

        Agent ids name one run, but one run can hold several sessions (module docstring), so
        only the agent's newest known session counts, and only while nothing else this reader
        holds says the run goes on (``_run_may_go_on``). Then its end proves the run ended,
        even after the agent's record and placement were deleted. The departure reason is
        kept after the last view entry is dropped, so later checks and watch reconnects do
        not announce the same exit again.
        """

        found: set[str] = set()
        for agent_id, session in self._newest_sessions().items():
            if session.ends_at > now or agent_id in view or agent_id in self._departed \
                    or self.left_for_good(agent_id) or self._run_may_go_on(agent_id):
                continue
            last = TeamAgent(agent=session.agent, agent_type=session.agent_type, display_name=None,
                             state="stopped", accepting_work=False, since=session.ends_at,
                             host=session.runtime, session=session.session, is_lead=session.agent_role == "lead")
            self._departed[session.agent] = _Departed(last, now, told=True)
            self._departures[session.agent] = SESSION_CLOSED
            found.add(session.agent)
            self._logger.info("An agent exited before this reader saw it. event=team_agents_unseen_exit "
                              "team=%s agent=%s session=%s", self.team, session.agent, session.session)
        return frozenset(found)

    def _newest_sessions(self) -> dict[str, AgentSession]:
        """Each agent's newest session this reader holds (``_session_order``)."""

        newest: dict[str, AgentSession] = {}
        for session in self._sessions.values():
            known = newest.get(session.agent)
            if known is None or _session_order(session) > _session_order(known):
                newest[session.agent] = session
        return newest

    def _later_session_known(self, session: AgentSession) -> bool:
        """Whether this reader holds a session of the same agent opened after ``session``: its run goes on there."""

        return any(other.agent == session.agent and _session_order(other) > _session_order(session)
                   for other in self._sessions.values())

    def _run_may_go_on(self, agent_id: str) -> bool:
        """Whether something besides the agent's newest session says its run may go on (module docstring).

        A held placement: a newer placement belongs to a newer session, and one a handover keeps
        means the agent moves. A held record naming a session this reader does not hold: that
        session may be newer than any it knows. A record naming a session it does hold is no
        such sign: after a crash a record can outlive its expiry here (expiry gives no
        deletion), and one written before the run's last opening names an older session.
        """

        if agent_id in self._placements:
            return True
        held = self._agents.get(agent_id)
        if held is None:
            return False
        named = f"session.{self._host.identity.installation}.{self.team}.{held.record.body.session}"
        return named not in self._sessions

    def _forget_departure(self, agent_id: str) -> None:
        """Drop what this reader keeps about a departure once the agent's last session record is deleted.

        Agent ids are never used again, so nothing can bring it back or announce it again; this
        keeps the departures bounded by how long the team services keep session records.
        """

        if agent_id in self._view or any(session.agent == agent_id for session in self._sessions.values()):
            return
        self._departures.pop(agent_id, None)
        self._departed.pop(agent_id, None)

    def _late_departures(self, leaving: set[str], now: datetime) -> frozenset[str]:
        """Departed agents found to have left for good only now (module docstring); old entries are dropped."""

        late: set[str] = set()
        for agent_id, departed in list(self._departed.items()):
            if agent_id in leaving:
                continue
            if not departed.told:
                reason = self._left_for_good_evidence(agent_id, departed.last, now)
                if reason is not None:
                    self._departures[agent_id] = reason
                    departed.told = True
                    late.add(agent_id)
                    self._logger.info("A departed agent turned out to have left for good. "
                                      "event=team_agents_left_for_good_late team=%s agent=%s reason=%s",
                                      self.team, agent_id, reason)
                    continue
            if (now - departed.since).total_seconds() > DEPARTED_KEEP_SECONDS:
                self._departed.pop(agent_id, None)
        return frozenset(late)

    def _left_for_good_evidence(self, agent_id: str, last: TeamAgent, now: datetime) -> Optional[str]:
        """Why ``agent_id``, which left the view as ``last``, left for good, or None while nothing says so.

        Only its last host's exiting record for its last session (it passed the placement
        check when it was accepted), or its last session's end as the team services signed it,
        when no later session of the agent is known (its run goes on there).
        """

        held = self._agents.get(agent_id)
        if held is not None and held.record.body.state in _GONE_STATES and held.record.signer is not None \
                and held.record.signer.runtime == last.host and str(held.record.body.session) == last.session:
            return EXITING
        session = self._host.live.trust.store.agent_session(self._host.identity.installation, self.team, last.session)
        if session is not None and session.agent == agent_id and session.runtime == last.host \
                and session.ends_at <= now and not self._later_session_known(session):
            return SESSION_CLOSED
        return None

    def _publish(self, view: dict[str, TeamAgent], late: frozenset[str] = frozenset()) -> None:
        changed = late | frozenset(agent_id for agent_id in set(view) | set(self._view)
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


def _session_order(session: AgentSession) -> tuple[datetime, int, str]:
    """Orders one agent's sessions by when they were opened.

    Each placement of an agent gets a later ``placed_at``, which its session's ``started_at``
    records (``team_services.sessions``); the placement revision and the id only break ties.
    """

    return session.started_at, session.placement_revision, session.session


def _present(value: Any) -> Any:
    """A model's optional field, or None when the record leaves it out."""

    return None if value is MISSING else value
