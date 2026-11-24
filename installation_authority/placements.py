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

One lock serialises every consumer change. Removals and retries read the placement key
again under it and act on the key as it is then, so a placement written or deleted
meanwhile is never undone by an answer read before it. A change that fails for one agent
(a partition stream at its consumer limit, a lost reply) is logged and tried again from
the placement as it is then, after 5 seconds and then after a wait that doubles up to the
minute, while the watch carries on with the other agents. A watch that fails is logged and
opened again after 5 seconds. A NATS restart needs neither: the NATS client notices the
missed heartbeats of the watch's reader within 10 to 20 seconds and opens it again from
the last change it read, so nothing is lost. The authority's process still exits when its
NATS connection closes for good (``service.InstallationAuthority.upkeep``).

Nothing publishes to these consumers' queues or reads them yet (steps 6 and 7); creating a
consumer grants nothing, since every reader's NATS permissions name its own consumers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional, Protocol

from pydantic import TypeAdapter, ValidationError

from custom_messages.wire.models import AgentId
from custom_messages.wire.partitions import RELEASED
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import PARTITION_OWNER, PLACEMENT, VerifiedRecord
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.state_records import require_record_key
from installation_authority.queues import AgentBacklog

_LOGGER = logging.getLogger(__name__)
# How often consumers kept for work their ended placement left are checked again, and the
# longest wait before a failed consumer change is tried again.
RECHECK_SECONDS = 60.0
# How long a failed watch waits before it is opened again, and the first wait before a
# failed consumer change is tried again (it doubles at each failure, up to RECHECK_SECONDS).
RETRY_SECONDS = 5.0
_AGENT_ID = TypeAdapter(AgentId)


@dataclass(frozen=True)
class _Retry:
    """When a failed consumer change is tried again, and how long it waited for that."""

    due: float
    wait: float


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
                 recheck_seconds: float = RECHECK_SECONDS, retry_seconds: float = RETRY_SECONDS) -> None:
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
        # Agents whose consumer change failed, and when each is tried again from its placement.
        self._failed: dict[tuple[str, str], _Retry] = {}
        # Teams whose sweep failed; tried again at the next check.
        self._sweep_due: set[str] = set()
        # Set once every team's watch caught up and its sweep ran.
        self.ready = asyncio.Event()
        self._swept: set[str] = set()

    async def run(self, stop: asyncio.Event) -> None:
        """Follow every team's placements and check kept consumers every minute, until ``stop``."""

        tasks = [asyncio.create_task(self._follow(team, stop)) for team in self.teams]
        tasks.append(asyncio.create_task(self._recheck_every(stop)))
        tasks.append(asyncio.create_task(self._retry_every(stop)))
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def recheck(self) -> None:
        """Check the consumers kept for work again, and run a sweep that failed again."""

        for team, agent in sorted(self._kept):
            await self._guarded(team, agent, "recheck", self._end(team, agent))
        for team in sorted(self._sweep_due):
            await self._sweep(team)

    async def retry_failed(self) -> None:
        """Try again each failed consumer change whose wait has passed, from its placement as it is now."""

        now = asyncio.get_running_loop().time()
        for team, agent in sorted(key for key, retry in self._failed.items() if retry.due <= now):
            retry = self._failed.pop((team, agent))
            await self._guarded(team, agent, "retry", self._reconcile(team, agent), waited=retry.wait)

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
            unplaced = sorted(agents - self._present[team])
        except Exception:
            self._sweep_due.add(team)
            self._logger.warning("Ending the consumers of agents no longer placed failed; it is tried again at the "
                                 "next check. event=placed_consumers_sweep_failed team=%s", team, exc_info=True)
            return
        self._sweep_due.discard(team)
        for agent in unplaced:
            await self._guarded(team, agent, "remove", self._end(team, agent))
        self._logger.info("Caught up on the team's placements and their agents' consumers. "
                          "event=placed_consumers_caught_up team=%s placed=%s unplaced=%s", team,
                          len(self._present[team]), len(unplaced))
        self._swept.add(team)
        if self._swept >= set(self.teams):
            self.ready.set()

    async def _recheck_every(self, stop: asyncio.Event) -> None:
        while not await _stopped_within(stop, self._recheck_seconds):
            await self.recheck()

    async def _retry_every(self, stop: asyncio.Event) -> None:
        while not await _stopped_within(stop, self._retry_seconds):
            await self.retry_failed()

    # -- one agent --------------------------------------------------------------------------------

    async def _guarded(self, team: str, agent: str, action: str, change: Awaitable[None], *,
                       waited: float = 0.0) -> None:
        """Run one agent's consumer change; a failure is logged and tried again after a wait.

        The first wait is ``retry_seconds``; a retry that fails again (``waited`` is the wait
        before it) waits twice as long, up to ``recheck_seconds``, so a change that keeps
        failing, such as a partition stream at its consumer limit, is logged at most once a
        minute once the wait has grown.
        """

        try:
            await change
        except Exception:
            wait = min(max(2 * waited, self._retry_seconds), self._recheck_seconds)
            self._failed[(team, agent)] = _Retry(asyncio.get_running_loop().time() + wait, wait)
            self._logger.warning("Changing an agent's consumers failed; it is tried again after a wait. "
                                 "event=agent_consumers_change_failed team=%s agent=%s action=%s retry_in=%s", team,
                                 agent, action, wait, exc_info=True)

    async def _ensure(self, team: str, agent: str) -> None:
        async with self._lock:
            await self._create(team, agent)

    async def _reconcile(self, team: str, agent: str) -> None:
        """Bring the agent's consumers in line with its placement as the bucket holds it under the lock."""

        async with self._lock:
            stored = await self._records.raw(PLACEMENT, f"{team}.{agent}")
            if stored is None:
                await self._remove_if_idle(team, agent)
                return
            self._kept.discard((team, agent))  # a present key keeps its consumers, verified or not
            if self._verifies(team, agent, f"{team}.{agent}", stored[1]):
                await self._create(team, agent)

    async def _end(self, team: str, agent: str) -> None:
        """Remove the agent's consumers if its placement is still gone and its queues hold nothing; keep them otherwise."""

        async with self._lock:
            if await self._records.raw(PLACEMENT, f"{team}.{agent}") is not None:
                self._kept.discard((team, agent))  # placed again meanwhile: the watch brings its placement
                return
            await self._remove_if_idle(team, agent)

    async def _create(self, team: str, agent: str) -> None:
        """Create the agent's consumers, or update them to the contract's settings; the lock is held."""

        self._kept.discard((team, agent))
        await self._consumers.ensure_agent_consumers(team, agent)

    async def _remove_if_idle(self, team: str, agent: str) -> None:
        """Remove the agent's consumers if its queues hold nothing, keep them otherwise.

        The lock is held and the agent's placement key was just read as gone. Until senders
        publish to the partition streams (step 6) nothing can arrive between the backlog read
        and the removal; from then on a message arriving in between stays stored with no
        consumer, where the next agent placed under that id would get it, which the step
        that adds the senders weighs.
        """

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


@dataclass(frozen=True)
class HostLostRelease:
    """Verified ``host_lost`` partition release observed in ``UA1_PLACEMENT`` (C01, C11)."""

    team: str
    partition: int
    host: str
    host_epoch: int
    key: str
    sequence: int
    issued_at: datetime


class ReleasedPartitionWatcher:
    """Watches ``<team>.partition.*`` in ``UA1_PLACEMENT`` and triggers NATS key revocation
    for verified ``host_lost`` released records (C01, C11)."""

    def __init__(
        self,
        records: PlacementRecords,
        *,
        verify: Callable[[bytes], VerifiedRecord],
        on_host_lost: Callable[[HostLostRelease], Awaitable[Any]],
        teams: Iterable[str],
        verify_keyed: Optional[Callable[[str, bytes], VerifiedRecord]] = None,
        logger: logging.Logger = _LOGGER,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._records = records
        self._verify = verify
        self._verify_keyed = verify_keyed
        self._on_host_lost = on_host_lost
        self.teams = tuple(sorted(set(teams)))
        self._logger = logger
        self._retry_seconds = retry_seconds
        self._handled_releases: dict[str, int] = {}

    async def run(self, stop: asyncio.Event) -> None:
        if not self.teams:
            await stop.wait()
            return
        tasks = [asyncio.create_task(self._follow(team, stop)) for team in self.teams]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _follow(self, team: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async for key, raw in self._records.watch(PARTITION_OWNER, f"{team}.partition.*", team=team):
                    if stop.is_set():
                        return
                    if raw is None:
                        continue
                    await self._handle_record(team, key, raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.warning(
                    "Partition release watch failed; reopening in %s seconds. "
                    "event=authority_release_watch_failed team=%s",
                    self._retry_seconds,
                    team,
                    exc_info=True,
                )
            if await _stopped_within(stop, self._retry_seconds):
                return

    async def _handle_record(self, team: str, key: str, raw: bytes) -> None:
        try:
            verified = self._verify_keyed(key, raw) if self._verify_keyed is not None else self._verify(raw)
            require_record_key(verified, key)
            body: Any = verified.body
            if body.team != team:
                raise FrameRefused("invalid_body", "partition_owner team differs from watch team", at="/body/team")
        except FrameRefused as refused:
            self._logger.warning(
                "A partition ownership record did not verify and is ignored for host_lost revocation. "
                "event=authority_partition_owner_refused team=%s key=%s code=%s at=%s",
                team,
                key,
                refused.code,
                refused.at,
            )
            return
        if body.state != RELEASED or body.reason != "host_lost":
            return
        sequence = int(verified.document.sequence)
        if self._handled_releases.get(key, -1) >= sequence:
            return
        release = HostLostRelease(
            team=team,
            partition=int(body.partition),
            host=str(body.host),
            host_epoch=int(body.host_epoch),
            key=key,
            sequence=sequence,
            issued_at=parse_timestamp(verified.document.issued_at),
        )
        await self._on_host_lost(release)
        self._handled_releases[key] = sequence


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    """Wait up to ``seconds``; whether ``stop`` was set."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True
