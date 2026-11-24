"""Team services partition ownership, credential-fit assignment, and four-step handovers.

contracts/messages/v1/README.md ("Partitions", "Credentials follow ownership", "Credential size",
"State records"), decisions 92 and 96 to 100, and PR #151 CR-2 / CR-4:

- Every partition ``0 .. partition_count - 1`` of a team has at most one owner recorded in
  ``UA1_PLACEMENT`` under ``<team>.partition.<p>`` (record type ``partition_owner``).
- All ownership writes run under the exact same ``asyncio.Lock`` as ``SessionService``
  (``sessions._changing``, CR-2), so a partition release and a concurrent ``sessions.open``
  are serialized and an agent can never be placed into a released partition.
- Before assigning ``owned`` to any host, ``PartitionService`` checks that the host's NATS user
  JWT would still fit ``max_credential_bytes`` using ``custom_transport.protocol1.nats_jwt.fits_credential_limit``
  (which shares ``roles_permissions`` and JWT encoding with ``InstallationAuthority``).
- Empty unowned partitions are assigned balanced across live accepting ``lead_host``/``worker_host``
  hosts of the team, and empty partitions rebalance across newly joined hosts. Partitions holding
  placed agents are never moved silently: they move only through an explicit four-step handover
  (moving all placed agents in the partition as a group) or a cut-off/retired host release.
- Connected-host handovers run one partition at a time per host (no overlapping ``released``
  record on the same connected host). Cut-off and retired hosts release all partitions at once
  with ``reason="host_lost"``.
- If a host's renewal for a newly owned partition is refused as ``not_allowed``
  (``credential_too_large``), the host reports an unhealthy ``partition-<p>`` component with
  ``code="partition_credential_too_large"`` in ``UA1_HOSTS`` and renews without ``p``.
  ``PartitionService`` watches ``UA1_HOSTS`` and recovers by reassigning ``p`` to another fitting
  host (including ``previous_host``) or deleting the record to leave ``p`` unowned.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from nats.js.errors import KeyWrongLastSequenceError

from custom_messages.contracts.runtime_v1 import MANAGER_AGENT_TYPE
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS, DEFAULT_PARTITION_COUNT
from custom_messages.wire.partitions import (
    OWNED,
    RELEASED,
    agent_partition,
    partition_owner_key,
)
from custom_messages.wire.permissions import HostPlacement
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import AgentSession, HostCertificate, VerifiedRecord
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.contract_files import max_credential_bytes as contract_max_credential_bytes
from custom_transport.protocol1.contract_files import transport as contract_transport
from custom_transport.protocol1.nats_jwt import fits_credential_limit
from custom_transport.protocol1.state_records import StateRecords, rising_sequence
from custom_transport.protocol1.trust_state import PersistentTrustStore
from default_values import TRANSPORT_DEFAULTS
from team_services.sessions import AGENT_SESSION, LEAD_ROLE, PLACEMENT, placement_key

PARTITION_OWNER = "partition_owner"
HOST_RECORD = "host"
CERTIFICATE_RECORD = "certificate"
REVOCATIONS_RECORD = "revocations"
PARTITION_OWNING_ROLES = frozenset({"lead_host", "worker_host"})
PARTITION_COMPONENT_PREFIX = "partition-"
PARTITION_CREDENTIAL_TOO_LARGE_CODE = "partition_credential_too_large"
RECONCILE_INTERVAL_SECONDS = 5.0
WATCH_RETRY_SECONDS = 5.0
DEFAULT_CUT_OFF_GRACE_SECONDS = 30.0
_PLACED_STEP = timedelta(milliseconds=1)
_LOGGER = logging.getLogger(__name__)

RevocationCheck = Callable[[str], Awaitable[bool] | bool]


def default_handover_seconds() -> int:
    """The contract's maximum step-2 handover window (``transport.yaml`` ``partitions.handover_seconds``)."""

    return int(contract_transport()["partitions"]["handover_seconds"])


def parse_refused_partitions(components: Iterable[Any]) -> frozenset[int]:
    """Partition numbers a host's verified liveness record refuses as ``partition_credential_too_large``."""

    refused: set[int] = set()
    for item in components:
        if isinstance(item, Mapping):
            comp = str(item.get("component", ""))
            state = str(item.get("state", ""))
            code = str(item.get("code", ""))
        else:
            comp = str(getattr(item, "component", ""))
            state = str(getattr(item, "state", ""))
            code = str(getattr(item, "code", "") or "")
        if (
            comp.startswith(PARTITION_COMPONENT_PREFIX)
            and state in ("degraded", "unhealthy")
            and code == PARTITION_CREDENTIAL_TOO_LARGE_CODE
        ):
            suffix = comp[len(PARTITION_COMPONENT_PREFIX):]
            if suffix.isdigit():
                refused.add(int(suffix))
    return frozenset(refused)


@dataclass(frozen=True)
class LivePartitionHost:
    """One live host of the team capable of owning partitions."""

    runtime: str
    certificate: HostCertificate
    state: str
    other_contract: bool
    refused_partitions: frozenset[int]

    @property
    def eligible(self) -> bool:
        return self.state == "accepting" and not self.other_contract


@dataclass(frozen=True)
class PartitionRecord:
    """A verified ``partition_owner`` record read from ``UA1_PLACEMENT``."""

    partition: int
    key: str
    revision: int
    record: VerifiedRecord

    @property
    def state(self) -> str:
        return str(self.record.body.state)

    @property
    def host(self) -> str:
        return str(self.record.body.host)

    @property
    def host_epoch(self) -> int:
        return int(self.record.body.host_epoch)

    @property
    def reason(self) -> str:
        return str(self.record.body.reason)

    @property
    def previous_host(self) -> Optional[str]:
        prev = getattr(self.record.body, "previous_host", None)
        return None if prev is None else str(prev)

    @property
    def assigned_at(self) -> datetime:
        return parse_timestamp(str(self.record.body.assigned_at))


@dataclass(frozen=True)
class PlacedAgent:
    """A verified agent placement read from ``UA1_PLACEMENT``."""

    agent: str
    agent_type: str
    partition: int
    host: str
    revision: int
    record: VerifiedRecord
    handed_over: bool


class PartitionService:
    """Owns and balances a team's ``partition_owner`` records and drives four-step handovers."""

    def __init__(
        self,
        records: StateRecords,
        trust: PersistentTrustStore,
        installation: str,
        team: str,
        lock: asyncio.Lock,
        clock: Callable[[], datetime],
        logger: logging.Logger = _LOGGER,
        *,
        credential_lifetime_seconds: int = TRANSPORT_DEFAULTS.nats_credential_lifetime_seconds.value,
        handover_seconds: Optional[int] = None,
        credentials_enforced: bool = False,
        is_nats_key_revoked: Optional[RevocationCheck] = None,
        cut_off_grace_seconds: float = DEFAULT_CUT_OFF_GRACE_SECONDS,
        reconcile_interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
        max_credential_bytes: Optional[int] = None,
    ) -> None:
        self.records = records
        self.trust = trust
        self.installation = installation
        self.team = team
        # Shared with SessionService._changing (CR-2): serializes partition ownership writes and sessions.open/close.
        self._lock = lock
        self.clock = clock
        self.logger = logger
        self.credential_lifetime_seconds = int(credential_lifetime_seconds)
        self.handover_seconds = default_handover_seconds() if handover_seconds is None else int(handover_seconds)
        self.credentials_enforced = credentials_enforced
        self._is_nats_key_revoked = is_nats_key_revoked
        self.cut_off_grace_seconds = float(cut_off_grace_seconds)
        self.reconcile_interval_seconds = float(reconcile_interval_seconds)
        self._max_credential_bytes = (
            contract_max_credential_bytes() if max_credential_bytes is None else int(max_credential_bytes)
        )
        self._seen_live: set[str] = set()
        self._first_missing_at: dict[str, datetime] = {}
        self._planned_targets: dict[int, str] = {}
        # Maps (partition, host, assigned_at) -> highest host membership_epoch observed in UA1_CERTS
        # at release time, so clock skew between authority and team services cannot exclude a pre-release
        # certificate on host_lost releases without altering body.host_epoch (C19).
        self._release_pre_epochs: dict[tuple[int, str, datetime], int] = {}
        self._woke = asyncio.Event()

    @property
    def partition_count(self) -> int:
        return int(getattr(self.trust.store, "partition_count", DEFAULT_PARTITION_COUNT))

    # -- lifecycle & watches ----------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Watch ``UA1_HOSTS``, ``revocations.<installation>``, and (when enforced) ``UA1_CERTS`` until ``stop``."""

        try:
            await self.reconcile()
        except Exception:
            self.logger.exception(
                "Initial partition ownership reconciliation failed; retrying on schedule. "
                "event=partition_reconcile_failed team=%s",
                self.team,
            )
        tasks = [
            asyncio.create_task(self._watch_hosts(stop)),
            asyncio.create_task(self._watch_revocations(stop)),
            asyncio.create_task(self._reconcile_loop(stop)),
        ]
        if self.credentials_enforced:
            tasks.append(asyncio.create_task(self._watch_certificates(stop)))
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_hosts(self, stop: asyncio.Event) -> None:
        seen: dict[str, tuple[str, int, bool, frozenset[int]]] = {}
        while not stop.is_set():
            try:
                async for key, raw in self.records.watch(HOST_RECORD, ">"):
                    if stop.is_set():
                        return
                    if self._host_watch_event_relevant(key, raw, seen):
                        self._woke.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "Host liveness watch for partition ownership stopped; reopening. "
                    "event=partition_host_watch_failed team=%s",
                    self.team,
                    exc_info=True,
                )
                seen.clear()
            if await _stopped_within(stop, WATCH_RETRY_SECONDS):
                return

    def _host_watch_event_relevant(
        self,
        key: str,
        raw: Optional[bytes],
        seen: dict[str, tuple[str, int, bool, frozenset[int]]],
    ) -> bool:
        """Ignore non-team/non-owning host records and unchanged steady-state heartbeats (C23)."""

        if raw is None:
            return (seen.pop(key, None) is not None) or (key in self._seen_live)
        try:
            verified = self.trust.store.verify(raw, record=HOST_RECORD, now=self.clock())
        except FrameRefused as refused:
            if refused.code == "unknown_key":
                # New host or newly rotated certificate not yet fetched into trust.store: wake to fetch it.
                return True
            return (seen.pop(key, None) is not None) or (key in self._seen_live)
        cert = verified.signer
        if cert is None or cert.runtime != key or self.team not in cert.teams or not (cert.roles & PARTITION_OWNING_ROLES):
            return seen.pop(key, None) is not None
        if verified.other_contract:
            fingerprint: tuple[str, int, bool, frozenset[int]] = (
                "other_contract",
                cert.membership_epoch,
                True,
                frozenset(),
            )
        else:
            body: Any = verified.body
            fingerprint = (
                str(body.state),
                cert.membership_epoch,
                False,
                parse_refused_partitions(getattr(body, "components", ())),
            )
        if seen.get(key) == fingerprint:
            return False
        seen[key] = fingerprint
        return True

    async def _watch_revocations(self, stop: asyncio.Event) -> None:
        key = f"revocations.{self.installation}"
        while not stop.is_set():
            try:
                async for _key, _raw in self.records.watch(REVOCATIONS_RECORD, key):
                    if stop.is_set():
                        return
                    self._woke.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "Revocations watch for partition ownership stopped; reopening. "
                    "event=partition_revocations_watch_failed team=%s",
                    self.team,
                    exc_info=True,
                )
            if await _stopped_within(stop, WATCH_RETRY_SECONDS):
                return

    async def _watch_certificates(self, stop: asyncio.Event) -> None:
        prefix = f"host.{self.installation}.>"
        while not stop.is_set():
            try:
                async for _key, _raw in self.records.watch(CERTIFICATE_RECORD, prefix):
                    if stop.is_set():
                        return
                    self._woke.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "Certificate watch for partition handovers stopped; reopening. "
                    "event=partition_cert_watch_failed team=%s",
                    self.team,
                    exc_info=True,
                )
            if await _stopped_within(stop, WATCH_RETRY_SECONDS):
                return

    async def _reconcile_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            woke_task = asyncio.create_task(self._woke.wait())
            stop_task = asyncio.create_task(stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {woke_task, stop_task},
                    timeout=self.reconcile_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            except asyncio.CancelledError:
                woke_task.cancel()
                stop_task.cancel()
                await asyncio.gather(woke_task, stop_task, return_exceptions=True)
                raise
            if stop.is_set():
                return
            self._woke.clear()
            try:
                await self.reconcile()
            except Exception:
                self.logger.exception(
                    "Partition ownership reconciliation failed; retrying on schedule. "
                    "event=partition_reconcile_failed team=%s",
                    self.team,
                )

    # -- public operations ------------------------------------------------------------------------

    async def reconcile(self) -> None:
        """Reconcile cut-off/retired releases, refusals, in-flight handovers, initial assignments, and balance."""

        async with self._lock:
            await self._reconcile_locked()

    async def ensure_unowned_assigned(self) -> None:
        """Assign unowned partitions while the caller already holds ``self._lock`` (called from ``SessionService``)."""

        assert self._lock.locked(), "ensure_unowned_assigned requires the shared mutation lock"
        now = self.clock()
        await self._refresh_revocations(now)
        live_hosts = await self._read_live_hosts(now)
        ownership, raw_revisions = await self._read_ownership(now)
        placements = await self._read_placements(now)
        await self._assign_unowned_locked(live_hosts, ownership, raw_revisions, placements, now)

    async def release_partition(
        self,
        partition: int,
        *,
        reason: str = "operator",
        target_host: Optional[str] = None,
    ) -> bool:
        """Start step 1 of a connected-host handover for ``partition`` under the shared mutation lock.

        Refuses overlapping releases on the same connected host, and refuses moves whose destination
        cannot fit the partition's credential or placed agents.
        """

        async with self._lock:
            now = self.clock()
            live_hosts = await self._read_live_hosts(now)
            ownership, _raw_revisions = await self._read_ownership(now)
            placements = await self._read_placements(now)
            return await self._start_connected_handover_locked(
                partition,
                reason=reason,
                target_host=target_host,
                live_hosts=live_hosts,
                ownership=ownership,
                placements=placements,
                now=now,
            )

    async def release_cut_off_host(self, runtime: str, *, reason: str = "host_lost") -> list[int]:
        """Release every owned partition of cut-off or retired ``runtime`` at once (and escalate non-``host_lost`` releases)."""

        async with self._lock:
            now = self.clock()
            ownership, _raw_revisions = await self._read_ownership(now)
            return await self._release_host_all_locked(runtime, ownership, reason=reason, now=now)

    async def maintain(self) -> None:
        """Re-sign ``partition_owner`` records written under an earlier team-services certificate epoch."""

        async with self._lock:
            epoch = self.records.writer().certificate.membership_epoch
            now = self.clock()
            ownership, _raw_revisions = await self._read_ownership(now)
            for rec in ownership.values():
                signer = rec.record.signer
                if signer is None or signer.membership_epoch == epoch:
                    continue
                document = self.records.signed(
                    PARTITION_OWNER,
                    rec.key,
                    dict(rec.record.document.body),
                    sequence=rising_sequence(rec.record.document.sequence, now),
                    issued_at=now,
                )
                try:
                    await self.records.put(PARTITION_OWNER, rec.key, document, last_revision=rec.revision)
                except KeyWrongLastSequenceError:
                    continue
                self.logger.info(
                    "Signed a partition ownership record again under the current certificate. "
                    "event=partition_owner_signed_again key=%s previous_epoch=%s membership_epoch=%s",
                    rec.key,
                    signer.membership_epoch,
                    epoch,
                )

    # -- reconciliation phases (all called with self._lock held) ----------------------------------

    async def _reconcile_locked(self) -> None:
        now = self.clock()
        await self._refresh_revocations(now)
        live_hosts = await self._read_live_hosts(now)
        ownership, raw_revisions = await self._read_ownership(now)
        placements = await self._read_placements(now)
        self._update_host_presence(live_hosts, ownership, now)

        await self._release_lost_and_retired_hosts_locked(live_hosts, ownership, now)
        await self._recover_refused_partitions_locked(live_hosts, ownership, raw_revisions, placements, now)
        await self._reconcile_stranded_placements_locked(live_hosts, ownership, placements, now)
        await self._advance_releases_locked(live_hosts, ownership, placements, now)
        await self._release_draining_hosts_locked(live_hosts, ownership, placements, now)
        await self._assign_unowned_locked(live_hosts, ownership, raw_revisions, placements, now)
        await self._rebalance_empty_partitions_locked(live_hosts, ownership, placements, now)

    def _update_host_presence(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: Mapping[int, PartitionRecord],
        now: datetime,
    ) -> None:
        for runtime in live_hosts:
            self._seen_live.add(runtime)
            self._first_missing_at.pop(runtime, None)
        named_hosts = {rec.host for rec in ownership.values()}
        for runtime in named_hosts:
            if runtime not in live_hosts and runtime not in self._first_missing_at:
                self._first_missing_at[runtime] = now
        for runtime in list(self._first_missing_at):
            if runtime not in named_hosts:
                self._first_missing_at.pop(runtime, None)

    async def _release_lost_and_retired_hosts_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        now: datetime,
    ) -> None:
        hosts_in_records = sorted({rec.host for rec in ownership.values()})
        for runtime in hosts_in_records:
            certs = await self._load_host_certificates(runtime, now)
            latest_cert = certs[-1] if certs else None
            retired = latest_cert is not None and self.trust.store.revoked(latest_cert)
            if not retired and certs:
                owned_epochs = {rec.host_epoch for rec in ownership.values() if rec.host == runtime}
                retired = any(
                    c.membership_epoch in owned_epochs and self.trust.store.revoked(c)
                    for c in certs
                )
            if retired:
                await self._release_host_all_locked(
                    runtime, ownership, reason="host_lost", now=now, known_certs=certs
                )
                continue
            if runtime in live_hosts:
                continue
            if latest_cert is not None and latest_cert.connection_class == "intermittent":
                continue
            first_missing = self._first_missing_at.get(runtime, now)
            grace_passed = (now - first_missing) >= timedelta(seconds=self.cut_off_grace_seconds)
            if runtime in self._seen_live or grace_passed:
                await self._release_host_all_locked(
                    runtime, ownership, reason="host_lost", now=now, known_certs=certs
                )

    async def _recover_refused_partitions_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        raw_revisions: dict[int, int],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        """Recover partitions whose assigned host reported ``partition_credential_too_large`` (A4, CR-4)."""

        for host in sorted(live_hosts.values(), key=lambda item: item.runtime):
            for partition in sorted(host.refused_partitions):
                rec = ownership.get(partition)
                if rec is None or rec.state != OWNED or rec.host != host.runtime:
                    continue
                part_agents = [p for p in placements if p.partition == partition]
                has_active_agents = any(p.host == host.runtime and not p.handed_over for p in part_agents)
                if has_active_agents:
                    await self._start_connected_handover_locked(
                        partition,
                        reason="rebalanced",
                        target_host=None,
                        live_hosts=live_hosts,
                        ownership=ownership,
                        placements=placements,
                        now=now,
                    )
                    continue
                target = await self._select_destination_host(
                    partition,
                    live_hosts=live_hosts,
                    ownership=ownership,
                    placements=placements,
                    moving_agents=part_agents,
                    exclude_hosts={host.runtime},
                    preferred_runtime=rec.previous_host,
                    now=now,
                )
                if target is not None:
                    await self._write_owned_and_move_agents_locked(
                        rec,
                        target=target,
                        reason="rebalanced",
                        old_host=host.runtime,
                        ownership=ownership,
                        placements=placements,
                        now=now,
                    )
                    self.logger.warning(
                        "Reassigned a partition after its host refused it as credential_too_large. "
                        "event=partition_reassigned_after_credential_refusal team=%s partition=%s "
                        "refused_host=%s new_host=%s",
                        self.team,
                        partition,
                        host.runtime,
                        target.runtime,
                    )
                else:
                    if await self.records.delete(PARTITION_OWNER, rec.key, last_revision=rec.revision):
                        ownership.pop(partition, None)
                        raw_revisions[partition] = 0
                        self._planned_targets.pop(partition, None)
                        self._clear_release_pre_epochs(partition)
                        self.logger.warning(
                            "Left a partition unowned after its host refused it as credential_too_large and no "
                            "other host could fit it. event=partition_left_unowned_credential_too_large "
                            "team=%s partition=%s refused_host=%s",
                            self.team,
                            partition,
                            host.runtime,
                        )

    async def _reconcile_stranded_placements_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: Mapping[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        """Finish moving any verified placements still naming an earlier host in an ``owned`` partition (C18).

        Step 4 writes ``partition_owner`` as ``owned`` before updating each agent's placement record.
        If team services crash, lose their lock, time out, or hit a placement CAS conflict mid-group,
        this idempotent pass finishes ending stale sessions and moving every stranded agent to the
        partition's current owner on the next reconcile.
        """

        for partition, rec in sorted(ownership.items()):
            if rec.state != OWNED:
                continue
            stranded = [p for p in placements if p.partition == partition and p.host != rec.host]
            if not stranded:
                continue
            target_epoch = (
                live_hosts[rec.host].certificate.membership_epoch if rec.host in live_hosts else rec.host_epoch
            )
            await self._move_placements_to_owner_locked(
                stranded,
                target_runtime=rec.host,
                target_epoch=target_epoch,
                reason=rec.reason,
                placements=placements,
                now=now,
            )
            self.logger.warning(
                "Reconciled stranded agent placements onto the partition's current owner. "
                "event=partition_stranded_placements_reconciled team=%s partition=%s host=%s agents=%s",
                self.team,
                partition,
                rec.host,
                len(stranded),
            )

    async def _advance_releases_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        for partition in sorted(ownership):
            rec = ownership[partition]
            if rec.state != RELEASED:
                continue
            ready = await self._is_release_ready_locked(rec, live_hosts, ownership, placements, now)
            if not ready:
                continue
            await self._complete_release_locked(ownership[partition], live_hosts, ownership, placements, now)

    async def _release_draining_hosts_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        for host in sorted(live_hosts.values(), key=lambda item: item.runtime):
            if host.state not in ("draining", "drained") and not host.other_contract:
                continue
            if self._host_has_active_release(host.runtime, ownership):
                continue
            owned_parts = sorted(p for p, rec in ownership.items() if rec.state == OWNED and rec.host == host.runtime)
            if not owned_parts:
                continue
            # Prefer empty partitions first so they clear cleanly before agent-bearing partitions.
            occupied = {agent.partition for agent in placements if agent.host == host.runtime}
            ordered = [p for p in owned_parts if p not in occupied] + [p for p in owned_parts if p in occupied]
            for partition in ordered:
                started = await self._start_connected_handover_locked(
                    partition,
                    reason="drained",
                    target_host=None,
                    live_hosts=live_hosts,
                    ownership=ownership,
                    placements=placements,
                    now=now,
                    allow_without_target=True,
                )
                if started:
                    if not self.credentials_enforced and partition not in occupied:
                        completed = await self._complete_release_locked(
                            ownership[partition], live_hosts, ownership, placements, now
                        )
                        if completed:
                            continue
                    break

    async def _assign_unowned_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        raw_revisions: dict[int, int],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        eligible = [h for h in live_hosts.values() if h.eligible]
        if not eligible:
            return
        for partition in range(self.partition_count):
            if partition in ownership:
                continue
            part_agents = [p for p in placements if p.partition == partition]
            if part_agents:
                # Occupied unowned partition (e.g. left unowned by CR-4 refusal recovery, C20):
                # check agent_types, lead_host role, session_limit, and credential fit, then move placements.
                target = await self._select_destination_host(
                    partition,
                    live_hosts=live_hosts,
                    ownership=ownership,
                    placements=placements,
                    moving_agents=part_agents,
                    exclude_hosts=set(),
                    preferred_runtime=None,
                    now=now,
                )
                reason = "rebalanced"
            else:
                target = self._pick_least_loaded_for_empty(partition, eligible, ownership)
                reason = "initial"
            if target is None:
                continue
            key = partition_owner_key(self.team, partition, self.partition_count)
            last_rev = raw_revisions.get(partition, 0)
            body = {
                "team": self.team,
                "partition": partition,
                "state": OWNED,
                "host": target.runtime,
                "host_epoch": target.certificate.membership_epoch,
                "assigned_at": format_timestamp(now),
                "reason": reason,
            }
            seq = rising_sequence(0, now)
            document = self.records.signed(PARTITION_OWNER, key, body, sequence=seq, issued_at=now)
            try:
                revision = await self.records.put(PARTITION_OWNER, key, document, last_revision=last_rev)
            except KeyWrongLastSequenceError:
                continue
            verified = self.trust.store.verify(json.dumps(document).encode(), record=PARTITION_OWNER, now=now)
            ownership[partition] = PartitionRecord(partition, key, revision, verified)
            raw_revisions[partition] = revision
            self._clear_release_pre_epochs(partition)
            to_move = [p for p in part_agents if p.host != target.runtime]
            if to_move:
                await self._move_placements_to_owner_locked(
                    to_move,
                    target_runtime=target.runtime,
                    target_epoch=target.certificate.membership_epoch,
                    reason=reason,
                    placements=placements,
                    now=now,
                )
            self.logger.info(
                "Assigned an unowned partition to a host. event=partition_assigned team=%s partition=%s "
                "host=%s host_epoch=%s agents_moved=%s",
                self.team,
                partition,
                target.runtime,
                target.certificate.membership_epoch,
                len(to_move),
            )

    async def _rebalance_empty_partitions_locked(
        self,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        eligible = sorted((h for h in live_hosts.values() if h.eligible), key=lambda item: item.runtime)
        if len(eligible) < 2:
            return
        occupied = {agent.partition for agent in placements}
        blocked_sources: set[str] = set()
        while True:
            counts = {h.runtime: self._owned_count(h.runtime, ownership) for h in eligible}
            candidates_from = [
                h for h in eligible
                if h.runtime not in blocked_sources and not self._host_has_active_release(h.runtime, ownership)
            ]
            if not candidates_from:
                return
            source = max(candidates_from, key=lambda h: (counts[h.runtime], h.runtime))
            min_count = min(counts.values())
            if counts[source.runtime] < min_count + 2:
                return
            receivers = sorted(
                (h for h in eligible if h.runtime != source.runtime and counts[h.runtime] <= counts[source.runtime] - 2),
                key=lambda h: (counts[h.runtime], h.runtime),
            )
            empty_owned = sorted(
                p for p, rec in ownership.items()
                if rec.state == OWNED
                and rec.host == source.runtime
                and rec.reason != "operator"
                and p not in occupied
            )
            moved = False
            for partition in empty_owned:
                target = next(
                    (
                        h for h in receivers
                        if partition not in h.refused_partitions and self._host_fits_partition(h, partition, ownership)
                    ),
                    None,
                )
                if target is None:
                    continue
                started = await self._start_connected_handover_locked(
                    partition,
                    reason="rebalanced",
                    target_host=target.runtime,
                    live_hosts=live_hosts,
                    ownership=ownership,
                    placements=placements,
                    now=now,
                )
                if not started:
                    continue
                moved = True
                if not self.credentials_enforced:
                    await self._complete_release_locked(ownership[partition], live_hosts, ownership, placements, now)
                else:
                    # One connected-host handover at a time per host when per-process credentials are enforced.
                    blocked_sources.add(source.runtime)
                break
            if not moved:
                blocked_sources.add(source.runtime)

    # -- handover primitives (all called with self._lock held) ------------------------------------

    async def _start_connected_handover_locked(
        self,
        partition: int,
        *,
        reason: str,
        target_host: Optional[str],
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
        allow_without_target: bool = False,
    ) -> bool:
        rec = ownership.get(partition)
        if rec is None or rec.state != OWNED:
            return False
        old_host = rec.host
        if self._host_has_active_release(old_host, ownership):
            return False
        part_agents = [agent for agent in placements if agent.partition == partition]
        if target_host is not None:
            dest = live_hosts.get(target_host)
            if dest is None or dest.runtime == old_host or not await self._host_can_take_partition(
                dest, partition, ownership, placements, part_agents, now
            ):
                return False
        elif not allow_without_target:
            dest = await self._select_destination_host(
                partition,
                live_hosts=live_hosts,
                ownership=ownership,
                placements=placements,
                moving_agents=part_agents,
                exclude_hosts={old_host},
                preferred_runtime=None,
                now=now,
            )
            if dest is None:
                return False
            target_host = dest.runtime

        # Record the host's highest pre-release certificate epoch in the released record so clock skew
        # between authority and team services cannot let an earlier renewal satisfy step 3 (C19).
        certs = await self._load_host_certificates(old_host, now)
        known_epochs = [rec.host_epoch, *(c.membership_epoch for c in certs)]
        if old_host in live_hosts:
            known_epochs.append(live_hosts[old_host].certificate.membership_epoch)
        release_epoch = max(known_epochs)

        body = {
            "team": self.team,
            "partition": partition,
            "state": RELEASED,
            "host": old_host,
            "host_epoch": release_epoch,
            "assigned_at": format_timestamp(now),
            "reason": reason,
        }
        seq = rising_sequence(rec.record.document.sequence, now)
        document = self.records.signed(PARTITION_OWNER, rec.key, body, sequence=seq, issued_at=now)
        try:
            revision = await self.records.put(PARTITION_OWNER, rec.key, document, last_revision=rec.revision)
        except KeyWrongLastSequenceError:
            return False
        verified = self.trust.store.verify(json.dumps(document).encode(), record=PARTITION_OWNER, now=now)
        updated = PartitionRecord(partition, rec.key, revision, verified)
        ownership[partition] = updated
        self._release_pre_epochs[(partition, old_host, updated.assigned_at)] = release_epoch
        if target_host is not None:
            self._planned_targets[partition] = target_host
        self.logger.info(
            "Released a partition to start a handover. event=partition_released team=%s partition=%s "
            "host=%s host_epoch=%s reason=%s target_host=%s",
            self.team,
            partition,
            old_host,
            release_epoch,
            reason,
            target_host or "-",
        )
        return True

    async def _release_host_all_locked(
        self,
        runtime: str,
        ownership: dict[int, PartitionRecord],
        *,
        reason: str,
        now: datetime,
        known_certs: Optional[list[HostCertificate]] = None,
    ) -> list[int]:
        released: list[int] = []
        certs = await self._load_host_certificates(runtime, now) if known_certs is None else known_certs
        max_known_epoch = max((c.membership_epoch for c in certs), default=0)
        for partition in sorted(ownership):
            rec = ownership[partition]
            if rec.host != runtime:
                continue
            if rec.state == RELEASED and rec.reason == reason:
                continue
            # Preserve rec.host_epoch in the record so InstallationAuthority's host_lost watcher
            # (C17) never skips the original epoch or revokes a freshly rejoined host; track
            # max_known_epoch separately in _release_pre_epochs for clock-skew-safe waiting (C19).
            body = {
                "team": self.team,
                "partition": partition,
                "state": RELEASED,
                "host": runtime,
                "host_epoch": rec.host_epoch,
                "assigned_at": format_timestamp(now),
                "reason": reason,
            }
            seq = rising_sequence(rec.record.document.sequence, now)
            document = self.records.signed(PARTITION_OWNER, rec.key, body, sequence=seq, issued_at=now)
            try:
                revision = await self.records.put(PARTITION_OWNER, rec.key, document, last_revision=rec.revision)
            except KeyWrongLastSequenceError:
                continue
            verified = self.trust.store.verify(json.dumps(document).encode(), record=PARTITION_OWNER, now=now)
            updated = PartitionRecord(partition, rec.key, revision, verified)
            ownership[partition] = updated
            self._release_pre_epochs[(partition, runtime, updated.assigned_at)] = max(rec.host_epoch, max_known_epoch)
            released.append(partition)
            self.logger.warning(
                "Released a partition for a cut-off or retired host. event=partition_released_host_lost "
                "team=%s partition=%s host=%s host_epoch=%s reason=%s",
                self.team,
                partition,
                runtime,
                rec.host_epoch,
                reason,
            )
        return released

    async def _is_release_ready_locked(
        self,
        rec: PartitionRecord,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> bool:
        old_host = rec.host
        released_at = rec.assigned_at
        if not self.credentials_enforced:
            if rec.reason == "host_lost" or old_host not in live_hosts:
                return True
            part_agents = [p for p in placements if p.partition == rec.partition and p.host == old_host]
            if not part_agents or all(agent.handed_over for agent in part_agents):
                return True
            return now >= released_at + timedelta(seconds=self.handover_seconds)

        certs = await self._load_host_certificates(old_host, now)
        skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
        recorded_epoch = self._release_pre_epochs.get((rec.partition, old_host, released_at))
        pre_epoch = max(rec.host_epoch, recorded_epoch or 0)
        if rec.reason == "host_lost":
            base_old_certs = [
                c for c in certs
                if c.membership_epoch <= pre_epoch
                or (recorded_epoch is None and c.membership_epoch > pre_epoch and c.issued_at <= released_at)
            ]
        else:
            # Connected handovers persist pre_epoch in rec.host_epoch at release time (C19).
            base_old_certs = [c for c in certs if c.membership_epoch <= pre_epoch]
        base_old_epochs = {c.membership_epoch for c in base_old_certs}
        base_old_keys = {c.nats_public_key for c in base_old_certs}
        old_certs = [
            c for c in certs
            if c.membership_epoch in base_old_epochs or c.nats_public_key in base_old_keys
        ]
        active_old_certs = [c for c in old_certs if c.expires_at + skew > now]
        if old_certs and not active_old_certs:
            return True
        old_nats_keys = sorted({c.nats_public_key for c in active_old_certs})
        retired = any(self.trust.store.revoked(c) for c in active_old_certs)

        if rec.reason == "host_lost" or retired:
            if old_nats_keys and await self._all_keys_revoked(old_nats_keys):
                return True
            wait_until = released_at + timedelta(seconds=self.credential_lifetime_seconds + CLOCK_SKEW_SECONDS)
            if active_old_certs:
                latest_cert_end = max(c.expires_at for c in active_old_certs) + skew
                wait_until = min(wait_until, latest_cert_end)
            return now >= wait_until

        renewed = next(
            (
                c for c in certs
                if c.membership_epoch > pre_epoch
                and c.nats_public_key not in base_old_keys
                and c.nats_public_key not in old_nats_keys
                and c.issued_at >= released_at - skew
                and not self.trust.store.revoked(c)
            ),
            None,
        )
        if renewed is None:
            if now >= released_at + timedelta(seconds=self.handover_seconds):
                # Connected host missed the step-2/3 deadline: escalate to host_lost so authority revokes its key.
                await self._release_host_all_locked(
                    old_host, ownership, reason="host_lost", now=now, known_certs=certs
                )
            return False

        if old_nats_keys and await self._all_keys_revoked(old_nats_keys):
            return True
        wait_until = renewed.issued_at + timedelta(seconds=self.credential_lifetime_seconds + CLOCK_SKEW_SECONDS)
        return now >= wait_until

    async def _is_key_revoked(self, nats_key: str) -> bool:
        if self._is_nats_key_revoked is None:
            return False
        result = self._is_nats_key_revoked(nats_key)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _all_keys_revoked(self, nats_keys: Iterable[str]) -> bool:
        if self._is_nats_key_revoked is None:
            return False
        for key in nats_keys:
            if not await self._is_key_revoked(key):
                return False
        return True

    def _clear_release_pre_epochs(self, partition: int) -> None:
        for key in [k for k in self._release_pre_epochs if k[0] == partition]:
            self._release_pre_epochs.pop(key, None)

    async def _complete_release_locked(
        self,
        rec: PartitionRecord,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> bool:
        old_host = rec.host
        part_agents = [p for p in placements if p.partition == rec.partition]
        # Always prefer healthy peers over old_host first (C22).
        target = await self._select_destination_host(
            rec.partition,
            live_hosts=live_hosts,
            ownership=ownership,
            placements=placements,
            moving_agents=part_agents,
            exclude_hosts={old_host},
            preferred_runtime=self._planned_targets.get(rec.partition),
            now=now,
        )
        if target is None and rec.reason == "host_lost":
            # Fall back to old_host only if it rejoined at a strictly newer epoch with an unrevoked
            # certificate and (when credentials are enforced) an unrevoked NATS key (C22).
            candidate = live_hosts.get(old_host)
            recorded_epoch = self._release_pre_epochs.get((rec.partition, old_host, rec.assigned_at))
            min_recovered_epoch = max(rec.host_epoch, recorded_epoch or 0)
            if (
                candidate is not None
                and candidate.certificate.membership_epoch > min_recovered_epoch
                and not self.trust.store.revoked(candidate.certificate)
                and (
                    not self.credentials_enforced
                    or not await self._is_key_revoked(candidate.certificate.nats_public_key)
                )
                and await self._host_can_take_partition(
                    candidate, rec.partition, ownership, placements, part_agents, now
                )
            ):
                target = candidate
        if target is None:
            return False
        moved = await self._write_owned_and_move_agents_locked(
            rec,
            target=target,
            reason=rec.reason,
            old_host=old_host,
            ownership=ownership,
            placements=placements,
            now=now,
        )
        if moved:
            self._planned_targets.pop(rec.partition, None)
            self._clear_release_pre_epochs(rec.partition)
        return moved

    async def _write_owned_and_move_agents_locked(
        self,
        rec: PartitionRecord,
        *,
        target: LivePartitionHost,
        reason: str,
        old_host: str,
        ownership: dict[int, PartitionRecord],
        placements: list[PlacedAgent],
        now: datetime,
    ) -> bool:
        body: dict[str, Any] = {
            "team": self.team,
            "partition": rec.partition,
            "state": OWNED,
            "host": target.runtime,
            "host_epoch": target.certificate.membership_epoch,
            "assigned_at": format_timestamp(now),
            "reason": reason,
        }
        if old_host != target.runtime:
            body["previous_host"] = old_host
        seq = rising_sequence(rec.record.document.sequence, now)
        document = self.records.signed(PARTITION_OWNER, rec.key, body, sequence=seq, issued_at=now)
        try:
            revision = await self.records.put(PARTITION_OWNER, rec.key, document, last_revision=rec.revision)
        except KeyWrongLastSequenceError:
            return False
        verified = self.trust.store.verify(json.dumps(document).encode(), record=PARTITION_OWNER, now=now)
        ownership[rec.partition] = PartitionRecord(rec.partition, rec.key, revision, verified)
        self._clear_release_pre_epochs(rec.partition)

        moving = [p for p in placements if p.partition == rec.partition and p.host != target.runtime]
        if moving:
            await self._move_placements_to_owner_locked(
                moving,
                target_runtime=target.runtime,
                target_epoch=target.certificate.membership_epoch,
                reason=reason,
                placements=placements,
                now=now,
            )
        self.logger.info(
            "Completed partition ownership transfer. event=partition_reassigned team=%s partition=%s "
            "previous_host=%s host=%s host_epoch=%s reason=%s agents_moved=%s",
            self.team,
            rec.partition,
            old_host,
            target.runtime,
            target.certificate.membership_epoch,
            reason,
            len(moving),
        )
        return True

    async def _move_placements_to_owner_locked(
        self,
        moving: list[PlacedAgent],
        *,
        target_runtime: str,
        target_epoch: int,
        reason: str,
        placements: list[PlacedAgent],
        now: datetime,
    ) -> None:
        """Move every placed agent in ``moving`` to ``target_runtime`` and end stale sessions on their old hosts."""

        by_old_host: dict[str, set[str]] = {}
        for agent in moving:
            by_old_host.setdefault(agent.host, set()).add(agent.agent)
        for src_host, agent_names in sorted(by_old_host.items()):
            await self._end_old_host_sessions(src_host, agent_names, now)
        now_ms = parse_timestamp(format_timestamp(now))
        for agent in moving:
            prev_body = dict(agent.record.document.body)
            prev_placed = parse_timestamp(str(prev_body["placed_at"]))
            placed_at = now_ms if now_ms > prev_placed else prev_placed + _PLACED_STEP
            new_body: dict[str, Any] = {
                "team": self.team,
                "agent": agent.agent,
                "agent_type": agent.agent_type,
                "host": target_runtime,
                "host_epoch": target_epoch,
                "definition_sha256": str(prev_body["definition_sha256"]),
                "placed_at": format_timestamp(placed_at),
                "reason": reason,
                # Keep handed_over=True so SessionService._remove_orphan_placements preserves the
                # moved placement until the new host opens its session (replacing the placement).
                "handed_over": True,
            }
            if "required_labels" in prev_body:
                new_body["required_labels"] = list(prev_body["required_labels"])
            if agent.host != target_runtime:
                new_body["previous_host"] = agent.host
            key = placement_key(self.team, agent.agent)
            seq = rising_sequence(agent.record.document.sequence, placed_at)
            doc = self.records.signed(PLACEMENT, key, new_body, sequence=seq, issued_at=placed_at)
            try:
                rev = await self.records.put(PLACEMENT, key, doc, last_revision=agent.revision)
            except KeyWrongLastSequenceError:
                self.logger.warning(
                    "Placement CAS conflict while moving agent to new partition owner; scheduling retry. "
                    "event=partition_placement_move_conflict team=%s agent=%s old_host=%s new_host=%s",
                    self.team,
                    agent.agent,
                    agent.host,
                    target_runtime,
                )
                self._woke.set()
                continue
            verified = self.trust.store.verify(json.dumps(doc).encode(), record=PLACEMENT, now=placed_at)
            updated = PlacedAgent(
                agent=agent.agent,
                agent_type=agent.agent_type,
                partition=agent.partition,
                host=target_runtime,
                revision=rev,
                record=verified,
                handed_over=True,
            )
            for idx, existing in enumerate(placements):
                if existing.agent == agent.agent:
                    placements[idx] = updated
                    break

    async def _end_old_host_sessions(self, old_host: str, agents: set[str], now: datetime) -> None:
        """End any still-open sessions on ``old_host`` for moved agents (critical for lead failover)."""

        prefix = f"session.{self.installation}.{self.team}."
        for key in await self.records.keys(AGENT_SESSION, prefix):
            session_id = key[len(prefix):]
            try:
                await self.records.take_trust_record(AGENT_SESSION, key, now=now)
            except FrameRefused:
                continue
            stored: Optional[AgentSession] = self.trust.store.agent_session(self.installation, self.team, session_id)
            if stored is None or stored.runtime != old_host or stored.agent not in agents or stored.ends_at <= now:
                continue
            held = self.trust.store.held_record(AGENT_SESSION, key)
            if held is None:
                continue
            ended = max(now, stored.started_at + _PLACED_STEP)
            body = {
                "session": stored.session,
                "installation": stored.installation,
                "team": stored.team,
                "agent": stored.agent,
                "agent_type": stored.agent_type,
                "agent_role": stored.agent_role,
                "runtime": stored.runtime,
                "placement_revision": stored.placement_revision,
                "started_at": format_timestamp(stored.started_at),
                "ends_at": format_timestamp(ended),
            }
            record = self.records.signed(AGENT_SESSION, key, body, sequence=held[0] + 1, issued_at=now)
            await self.records.put(AGENT_SESSION, key, record)
            self.trust.add(json.dumps(record).encode(), now=now)

    # -- selection & credential fit ---------------------------------------------------------------

    def _host_fits_partition(
        self,
        host: LivePartitionHost,
        partition: int,
        ownership: Mapping[int, PartitionRecord],
    ) -> bool:
        cert = host.certificate
        current = {
            p for p, rec in ownership.items()
            if rec.state == OWNED and rec.host == host.runtime
        }
        candidate = tuple(sorted(current | {partition}))
        teams = tuple(sorted(cert.teams))
        pools = {self.team: tuple(sorted(cert.agent_types))} if cert.agent_types else {}
        placement = HostPlacement(
            runtime=cert.runtime,
            teams=teams,
            partitions={self.team: candidate},
            pools=pools,
            installation=self.installation,
        )
        return fits_credential_limit(
            tuple(sorted(cert.roles)),
            placement,
            partition_count=self.partition_count,
            max_bytes=self._max_credential_bytes,
        )

    async def _host_can_take_partition(
        self,
        host: LivePartitionHost,
        partition: int,
        ownership: Mapping[int, PartitionRecord],
        placements: list[PlacedAgent],
        moving_agents: list[PlacedAgent],
        now: datetime,
    ) -> bool:
        if not host.eligible or partition in host.refused_partitions:
            return False
        if not self._host_fits_partition(host, partition, ownership):
            return False
        if not moving_agents:
            return True
        cert = host.certificate
        existing_on_host = sum(1 for p in placements if p.host == host.runtime and p.partition != partition)
        if existing_on_host + len(moving_agents) > cert.session_limit:
            return False
        lead_agents = await self._lead_agent_names(moving_agents, now)
        for agent in moving_agents:
            if agent.agent_type not in cert.agent_types:
                return False
            if agent.agent in lead_agents and "lead_host" not in cert.roles:
                return False
        return True

    async def _lead_agent_names(self, moving_agents: list[PlacedAgent], now: datetime) -> set[str]:
        candidates = {a.agent for a in moving_agents if a.agent_type == MANAGER_AGENT_TYPE}
        if not candidates:
            return set()
        leads: set[str] = set()
        # Check retained sessions first; if a manager agent had a lead session (or was on a lead_host),
        # require lead_host on the destination.
        prefix = f"session.{self.installation}.{self.team}."
        for key in await self.records.keys(AGENT_SESSION, prefix):
            session_id = key[len(prefix):]
            try:
                await self.records.take_trust_record(AGENT_SESSION, key, now=now)
            except FrameRefused:
                continue
            stored = self.trust.store.agent_session(self.installation, self.team, session_id)
            if stored is not None and stored.agent in candidates and stored.agent_role == LEAD_ROLE:
                leads.add(stored.agent)
        if leads:
            return leads
        # Fallback if session was already forgotten/absent: check if old host held lead_host role.
        for agent in moving_agents:
            if agent.agent in candidates:
                certs = await self._load_host_certificates(agent.host, now)
                if any("lead_host" in c.roles for c in certs):
                    leads.add(agent.agent)
        return leads

    def _pick_least_loaded_for_empty(
        self,
        partition: int,
        eligible: list[LivePartitionHost],
        ownership: Mapping[int, PartitionRecord],
    ) -> Optional[LivePartitionHost]:
        candidates = [
            h for h in eligible
            if partition not in h.refused_partitions and self._host_fits_partition(h, partition, ownership)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda h: (self._owned_count(h.runtime, ownership), h.runtime))

    async def _select_destination_host(
        self,
        partition: int,
        *,
        live_hosts: Mapping[str, LivePartitionHost],
        ownership: Mapping[int, PartitionRecord],
        placements: list[PlacedAgent],
        moving_agents: list[PlacedAgent],
        exclude_hosts: set[str],
        preferred_runtime: Optional[str],
        now: datetime,
    ) -> Optional[LivePartitionHost]:
        if preferred_runtime is not None and preferred_runtime not in exclude_hosts:
            preferred = live_hosts.get(preferred_runtime)
            if preferred is not None and await self._host_can_take_partition(
                preferred, partition, ownership, placements, moving_agents, now
            ):
                return preferred
        eligible = sorted(
            (h for h in live_hosts.values() if h.eligible and h.runtime not in exclude_hosts),
            key=lambda h: (self._owned_count(h.runtime, ownership), h.runtime),
        )
        for candidate in eligible:
            if await self._host_can_take_partition(candidate, partition, ownership, placements, moving_agents, now):
                return candidate
        return None

    @staticmethod
    def _owned_count(runtime: str, ownership: Mapping[int, PartitionRecord]) -> int:
        return sum(1 for rec in ownership.values() if rec.state == OWNED and rec.host == runtime)

    @staticmethod
    def _host_has_active_release(runtime: str, ownership: Mapping[int, PartitionRecord]) -> bool:
        return any(rec.state == RELEASED and rec.host == runtime for rec in ownership.values())

    # -- bucket reads -----------------------------------------------------------------------------

    async def _read_live_hosts(self, now: datetime) -> dict[str, LivePartitionHost]:
        hosts: dict[str, LivePartitionHost] = {}
        for key in await self.records.keys(HOST_RECORD, ""):
            try:
                stored = await self.records.read_report(HOST_RECORD, key, now=now)
            except FrameRefused as refused:
                if refused.code != "expired":
                    self.logger.warning(
                        "A host record did not verify and is ignored for partition ownership. "
                        "event=team_services_record_refused record=host key=%s code=%s at=%s",
                        key,
                        refused.code,
                        refused.at,
                    )
                continue
            if stored is None or stored.record.signer is None:
                continue
            cert = stored.record.signer
            if cert.runtime != key or self.team not in cert.teams or not (cert.roles & PARTITION_OWNING_ROLES):
                continue
            if self.trust.store.revoked(cert):
                continue
            if stored.record.other_contract:
                hosts[key] = LivePartitionHost(
                    runtime=key,
                    certificate=cert,
                    state="draining",
                    other_contract=True,
                    refused_partitions=frozenset(),
                )
                continue
            body: Any = stored.record.body
            hosts[key] = LivePartitionHost(
                runtime=key,
                certificate=cert,
                state=str(body.state),
                other_contract=False,
                refused_partitions=parse_refused_partitions(getattr(body, "components", ())),
            )
        return hosts

    async def _read_ownership(self, now: datetime) -> tuple[dict[int, PartitionRecord], dict[int, int]]:
        ownership: dict[int, PartitionRecord] = {}
        raw_revisions: dict[int, int] = {}
        prefix = f"{self.team}.partition."
        for key in await self.records.keys(PARTITION_OWNER, prefix, team=self.team):
            suffix = key[len(prefix):]
            if not suffix.isdigit():
                continue
            partition = int(suffix)
            if not 0 <= partition < self.partition_count:
                continue
            try:
                stored = await self.records.read_report(PARTITION_OWNER, key, now=now)
            except FrameRefused as refused:
                self.logger.warning(
                    "A partition ownership record did not verify and counts as absent. "
                    "event=team_services_record_refused record=partition_owner key=%s code=%s at=%s",
                    key,
                    refused.code,
                    refused.at,
                )
                raw_revisions[partition] = await self.records.revision(PARTITION_OWNER, key)
                continue
            if stored is None:
                raw_revisions[partition] = await self.records.revision(PARTITION_OWNER, key)
                continue
            raw_revisions[partition] = stored.revision
            ownership[partition] = PartitionRecord(partition, key, stored.revision, stored.record)
        return ownership, raw_revisions

    async def _read_placements(self, now: datetime) -> list[PlacedAgent]:
        placed: list[PlacedAgent] = []
        prefix = f"{self.team}."
        for key in await self.records.keys(PLACEMENT, prefix, team=self.team):
            if key.count(".") != 1:
                continue
            try:
                stored = await self.records.read_report(PLACEMENT, key, now=now)
            except FrameRefused as refused:
                self.logger.warning(
                    "A placement record did not verify and is skipped during partition reconciliation. "
                    "event=team_services_record_refused record=placement key=%s code=%s at=%s",
                    key,
                    refused.code,
                    refused.at,
                )
                continue
            if stored is None:
                continue
            body: Any = stored.record.body
            agent = str(body.agent)
            placed.append(
                PlacedAgent(
                    agent=agent,
                    agent_type=str(body.agent_type),
                    partition=agent_partition(self.team, agent, self.partition_count),
                    host=str(body.host),
                    revision=stored.revision,
                    record=stored.record,
                    handed_over=body.handed_over is True,
                )
            )
        return placed

    async def _refresh_revocations(self, now: datetime) -> None:
        """Read ``revocations.<installation>`` into the trust store so retired hosts are released promptly (C21)."""

        key = f"revocations.{self.installation}"
        try:
            await self.records.take_trust_record(REVOCATIONS_RECORD, key, now=now)
        except FrameRefused as refused:
            self.logger.warning(
                "The revocations record did not verify and was not applied. "
                "event=team_services_record_refused record=revocations key=%s code=%s at=%s",
                key,
                refused.code,
                refused.at,
            )

    async def _load_host_certificates(self, runtime: str, now: datetime) -> list[HostCertificate]:
        # Let listing failures propagate (fail closed) so reconcile never decides step-4 readiness on a partial cache (C19).
        prefix = f"host.{self.installation}.{runtime}."
        keys = await self.records.keys(CERTIFICATE_RECORD, prefix)
        for key in keys:
            with contextlib.suppress(FrameRefused):
                await self.records.take_trust_record(CERTIFICATE_RECORD, key, now=now)
        return self.trust.store.certificates_of_runtime(self.installation, runtime)


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True
