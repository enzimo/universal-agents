"""Each placed agent's inbox and control consumers follow its team's placements (contract README, "Who sets up the streams").

The team services write which host runs each agent in ``UA1_PLACEMENT`` under
``<team>.<agent>``; the partition ownership records there have three parts
(``<team>.partition.<n>``) and the watch's ``<team>.*`` never reads them. The installation
authority, the topology owner, follows each team it serves:

- A placement that verifies gets the agent's inbox and control consumers in its partition's
  stream, created, or updated where the contract's settings differ. Verifying means what any
  reader checks (``TrustStore.verify``: signed by a host of this installation with the
  team services role, serving the record's team, not revoked) through the authority's own
  trust store, which holds every certificate it issued, plus the record's key and the body's
  team and agent matching the key. A record that does not verify creates nothing and removes
  nothing; it is logged.
- A deleted placement key ends the placement: both consumers are removed once the agent's
  queues hold nothing (``queues.AgentBacklog``: nothing waiting, nothing delivered and
  unacknowledged, nothing stored on their subjects, since a message past its last delivery
  stays in the stream as neither). While they hold work they stay, logged once with
  ``event=agent_consumers_kept`` and the counts, and are checked again every minute until
  they are empty or the agent is placed again.
- At start, and whenever a watch is opened again, the watch's first values are the current
  placements: deleted keys among them are only noted, never looked up, and present keys get
  their consumers. Once caught up, one sweep lists the agent consumers of the team's
  partition streams and ends, by the same rule, those whose agent has no placement key; that
  covers placements that ended while the authority was down. A present key keeps its agent's
  consumers whether or not it verifies.

One lock serialises every consumer change, and the placement key is read again under it
before consumers are removed, so a placement written meanwhile keeps them. A change that
fails for one agent (a partition stream at its consumer limit, a lost reply) is logged and
tried again at the next minute's check from the placement as it is then, while the watch
carries on with the other agents. A watch that fails is logged and opened again after a
pause; the authority's process still exits when its NATS connection closes for good
(``service.InstallationAuthority.upkeep``).

Nothing publishes to these consumers' queues or reads them yet (steps 6 and 7); creating a
consumer grants nothing, since every reader's NATS permissions name its own consumers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional, Protocol

from pydantic import TypeAdapter, ValidationError

from custom_messages.wire.models import AgentId
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import PLACEMENT, VerifiedRecord
from custom_transport.protocol1.state_records import require_record_key
from installation_authority.queues import AgentBacklog

_LOGGER = logging.getLogger(__name__)
# How often consumers kept for work their ended placement left are checked again.
RECHECK_SECONDS = 60.0
# How long a failed watch waits before it is opened again.
WATCH_RETRY_SECONDS = 5.0
_AGENT_ID = TypeAdapter(AgentId)


class PlacementRecords(Protocol):
    """The placement records as ``custom_transport.protocol1.state_records.StateRecords`` reads them."""

    def watch(self, record: str, keys: str = ">", *, team: Optional[str] = None,
              caught_up: Optional[asyncio.Event] = None) -> AsyncIterator[tuple[str, Optional[bytes]]]: ...

    async def raw(self, record: str, key: str) -> Optional[tuple[int, bytes]]: ...


class AgentConsumers(Protocol):
    """Agents' consumers as ``queues.QueueTopology`` makes, inspects and removes them."""

    async def ensure_agent_consumers(self, team: str, agent: str) -> None: ...

    async def agent_backlog(self, team: str, agent: str) -> AgentBacklog: ...

    async def delete_agent_consumers(self, team: str, agent: str) -> int: ...

    async def agents_with_consumers(self, team: str) -> set[str]: ...


class PlacedConsumers:
    """Keeps each placed agent's two consumers while it is placed and its queues hold work, for every team served."""

    def __init__(self, records: PlacementRecords, consumers: AgentConsumers, *,
                 verify: Callable[[bytes], VerifiedRecord], teams: Iterable[str], logger: logging.Logger = _LOGGER,
                 recheck_seconds: float = RECHECK_SECONDS, retry_seconds: float = WATCH_RETRY_SECONDS) -> None:
        self._records = records
        self._consumers = consumers
        # Verifies a placement record as of now through the authority's trust store.
        self._verify = verify
        self.teams = tuple(sorted(set(teams)))
        self._logger = logger
        self._recheck_seconds = recheck_seconds
        self._retry_seconds = retry_seconds
        self._lock = asyncio.Lock()
        # Per team, the agents with a placement key as the current watch has read them.
        self._present: dict[str, set[str]] = {team: set() for team in self.teams}
        # Agents whose placement ended while their queues held work.
        self._kept: set[tuple[str, str]] = set()
        # Agents whose consumer change failed; tried again from their placement at the next check.
        self._failed: set[tuple[str, str]] = set()
        # Teams whose sweep failed; tried again at the next check.
        self._sweep_due: set[str] = set()
        # Set once every team's watch caught up and its sweep ran.
        self.ready = asyncio.Event()
        self._swept: set[str] = set()

    async def run(self, stop: asyncio.Event) -> None:
        """Follow every team's placements and check kept consumers every minute, until ``stop``."""

        tasks = [asyncio.create_task(self._follow(team, stop)) for team in self.teams]
        tasks.append(asyncio.create_task(self._recheck_every(stop)))
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recheck(self) -> None:
        """Check the consumers kept for work again, retry failed changes, and run a sweep that failed again."""

        for team, agent in sorted(self._kept):
            await self._guarded(team, agent, "recheck", self._end(team, agent))
        for team, agent in sorted(self._failed):
            self._failed.discard((team, agent))
            await self._guarded(team, agent, "retry", self._reconcile(team, agent))
        for team in sorted(self._sweep_due):
            await self._sweep(team)

    # -- following a team ---------------------------------------------------------------------

    async def _follow(self, team: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            caught_up = asyncio.Event()
            sweep = asyncio.create_task(self._sweep_once(team, caught_up))
            try:
                await self._watch(team, caught_up)
                raise RuntimeError("the placement watch ended")
            except Exception:
                self._logger.warning("A placement watch stopped; it is opened again in %s seconds. "
                                     "event=placement_watch_failed team=%s", self._retry_seconds, team, exc_info=True)
            finally:
                sweep.cancel()
                await asyncio.gather(sweep, return_exceptions=True)
            if await _stopped_within(stop, self._retry_seconds):
                return

    async def _watch(self, team: str, caught_up: asyncio.Event) -> None:
        present = self._present[team] = set()
        async for key, raw in self._records.watch(PLACEMENT, f"{team}.*", team=team, caught_up=caught_up):
            agent = self._agent_of(team, key)
            if agent is None:
                continue
            if raw is None:
                present.discard(agent)
                if caught_up.is_set():  # a deleted key among the first values ended nothing new
                    await self._guarded(team, agent, "remove", self._end(team, agent))
                continue
            present.add(agent)
            if self._verifies(team, agent, key, raw):
                await self._guarded(team, agent, "create", self._ensure(team, agent))

    async def _sweep_once(self, team: str, caught_up: asyncio.Event) -> None:
        await caught_up.wait()
        await self._sweep(team)

    async def _sweep(self, team: str) -> None:
        """End the placements of agents that have consumers but no placement key, as the watch read them."""

        try:
            agents = await self._consumers.agents_with_consumers(team)
            ended = sorted(agents - self._present[team])
        except Exception:
            self._sweep_due.add(team)
            self._logger.warning("Ending the consumers of agents no longer placed failed; it is tried again at the "
                                 "next check. event=placed_consumers_sweep_failed team=%s", team, exc_info=True)
            return
        self._sweep_due.discard(team)
        for agent in ended:
            await self._guarded(team, agent, "remove", self._end(team, agent))
        self._logger.info("Caught up on the team's placements and their agents' consumers. "
                          "event=placed_consumers_caught_up team=%s placed=%s ended=%s", team,
                          len(self._present[team]), len(ended))
        self._swept.add(team)
        if self._swept >= set(self.teams):
            self.ready.set()

    async def _recheck_every(self, stop: asyncio.Event) -> None:
        while not await _stopped_within(stop, self._recheck_seconds):
            await self.recheck()

    # -- one agent --------------------------------------------------------------------------------

    async def _guarded(self, team: str, agent: str, action: str, change: Awaitable[None]) -> None:
        """Run one agent's consumer change; a failure is logged and tried again at the next check."""

        try:
            await change
        except Exception:
            self._failed.add((team, agent))
            self._logger.warning("Changing an agent's consumers failed; it is tried again at the next check. "
                                 "event=agent_consumers_change_failed team=%s agent=%s action=%s", team, agent, action,
                                 exc_info=True)

    async def _ensure(self, team: str, agent: str) -> None:
        async with self._lock:
            self._kept.discard((team, agent))
            await self._consumers.ensure_agent_consumers(team, agent)

    async def _reconcile(self, team: str, agent: str) -> None:
        """Bring the agent's consumers in line with its placement as the bucket holds it now."""

        stored = await self._records.raw(PLACEMENT, f"{team}.{agent}")
        if stored is None:
            await self._end(team, agent)
        elif self._verifies(team, agent, f"{team}.{agent}", stored[1]):
            await self._ensure(team, agent)

    async def _end(self, team: str, agent: str) -> None:
        """Remove the agent's consumers if its placement is still gone and its queues hold nothing; keep them otherwise."""

        async with self._lock:
            if await self._records.raw(PLACEMENT, f"{team}.{agent}") is not None:
                self._kept.discard((team, agent))  # placed again meanwhile: the watch brings its placement
                return
            backlog = await self._consumers.agent_backlog(team, agent)
            if not backlog.empty:
                if (team, agent) not in self._kept:
                    self._kept.add((team, agent))
                    self._logger.info(
                        "An agent's placement ended while its queues hold work; its consumers stay until they are "
                        "empty. event=agent_consumers_kept team=%s agent=%s pending=%s ack_pending=%s stored=%s",
                        team, agent, backlog.pending, backlog.ack_pending, backlog.stored)
                return
            self._kept.discard((team, agent))
            if await self._consumers.delete_agent_consumers(team, agent):
                self._logger.info("Removed an agent's consumers: its placement ended and its queues are empty. "
                                  "event=agent_consumers_removed team=%s agent=%s", team, agent)

    def _agent_of(self, team: str, key: str) -> Optional[str]:
        """The agent a placement key names; None, logged, for a key that names no valid agent id."""

        _, _, agent = key.partition(".")
        try:
            return _AGENT_ID.validate_python(agent)
        except ValidationError:
            self._logger.warning("A placement key names no valid agent id; it is left alone. "
                                 "event=placement_key_refused team=%s key=%s", team, key)
            return None

    def _verifies(self, team: str, agent: str, key: str, raw: bytes) -> bool:
        try:
            verified = self._verify(raw)
            require_record_key(verified, key)
            body: Any = verified.body
            if (body.team, body.agent) != (team, agent):
                raise FrameRefused("invalid_body", "the placement names another team or agent than its key",
                                   at="/body/agent")
        except FrameRefused as refused:
            self._logger.warning("A placement does not verify; its agent's consumers are neither created nor removed. "
                                 "event=placement_not_verified team=%s key=%s code=%s at=%s", team, key, refused.code,
                                 refused.at)
            return False
        return True


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    """Wait up to ``seconds``; whether ``stop`` was set."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True
