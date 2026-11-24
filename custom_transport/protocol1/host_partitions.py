"""Host-side partition ownership tracking and partition-scoped agent ID generation.

contracts/messages/v1/README.md ("Partitions", "Credentials follow ownership",
"While hosts share one NATS login", "Credential size", "State records") and
decisions 96 to 100 (acceptance criteria A3 and A4):

- Every lead or worker host watches verified ``partition_owner`` records for its
  team in ``UA1_PLACEMENT`` (``<team>.partition.*``), verifying each record's
  signature, team-services writer role, key binding, and monotonic sequence
  (preserving replay protection across deletions).
- Under the shared NATS login (no ``connection`` credentials issued yet), a host
  treats a partition as usable as soon as a verified ``owned`` record names it.
- Once credentials are issued, a host treats a partition as usable only when BOTH
  a verified ``owned`` record names it AND its current NATS credential names it.
- If a host's renewal for a newly owned partition is refused as
  ``credential_too_large`` (``not_allowed`` naming ``max_credential_bytes``),
  the tracker records that partition as rejected so the host reports an unhealthy
  ``partition-<p>`` component (``code="partition_credential_too_large"``) and
  renews without it. Once ``team-services`` moves or deletes that ownership
  record, the rejection clears automatically.
- Auto-generated agent IDs are drawn to fit the contract's 32-character
  ``agent_id`` pattern and land in one of the host's least-filled usable owned
  partitions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Collection, Mapping, Optional

from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT
from custom_messages.wire.partitions import (
    OWNED,
    RELEASED,
    agent_partition,
    check_partition_count,
    partition_owner_key,
)
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import VerifiedRecord
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.state_records import require_record_key

PARTITION_OWNER = "partition_owner"
TEAM_SERVICES_ROLE = "team_services"
PARTITION_COMPONENT_PREFIX = "partition-"
PARTITION_CREDENTIAL_TOO_LARGE_CODE = "partition_credential_too_large"
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_AGENT_ID_LENGTH = 32
_NOT_IN_AGENT_ID = re.compile(r"[^A-Za-z0-9_-]")
_WATCH_RETRY_SECONDS = 5.0
_DEFAULT_SUFFIX_HEX_BYTES = 6  # 12 hex characters -> "agent_<12hex>" = 18 chars (<= 32)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnedPartition:
    """One verified ``partition_owner`` record as observed by a host."""

    partition: int
    state: str
    host: str
    host_epoch: int
    assigned_at: datetime
    issued_at: datetime
    reason: str
    sequence: int
    digest: str


def _sanitize_prefix(prefix: str, suffix_length: int) -> str:
    """Normalize ``prefix`` so ``f"{prefix}_{suffix}"`` always matches ``AGENT_ID_PATTERN``."""

    max_prefix = MAX_AGENT_ID_LENGTH - 1 - suffix_length
    if max_prefix < 1:
        raise ValueError(f"agent ID suffix length {suffix_length} leaves no room within {MAX_AGENT_ID_LENGTH} chars")
    cleaned = _NOT_IN_AGENT_ID.sub("_", prefix).lstrip("_-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "a" + cleaned
    return cleaned[:max_prefix]


def draw_any_agent_id(*, prefix: str = "agent", token_generator: Optional[Callable[[], str]] = None) -> str:
    """Draw a contract-valid random agent ID (<= 32 chars) without partition filtering.

    Used when no protocol 1 host is configured (standalone / unit-test paths) or before
    any partition is owned, replacing legacy 38-char ``agent_ + uuid4().hex`` IDs.
    """

    suffix = token_generator() if token_generator is not None else secrets.token_hex(_DEFAULT_SUFFIX_HEX_BYTES)
    safe_prefix = _sanitize_prefix(prefix, len(suffix))
    candidate = f"{safe_prefix}_{suffix}"
    if not AGENT_ID_PATTERN.fullmatch(candidate):
        raise ValueError(f"generated agent ID {candidate!r} does not match contract agent_id pattern")
    return candidate


def draw_agent_id(
    team: str,
    usable_partitions: Collection[int],
    partition_count: int = DEFAULT_PARTITION_COUNT,
    *,
    prefix: str = "agent",
    partition_loads: Optional[Mapping[int, int]] = None,
    exclude_ids: Collection[str] = (),
    token_generator: Optional[Callable[[], str]] = None,
    max_attempts: Optional[int] = None,
) -> str:
    """Draw a contract-valid agent ID (<= 32 chars) inside the least-filled usable owned partition.

    ``usable_partitions`` must be a non-empty collection of valid partition indices for
    ``partition_count``. When ``partition_loads`` is given, the draw targets only those
    usable partitions with the minimum current load so new agents spread evenly across
    the host's owned partitions.
    """

    count = check_partition_count(partition_count)
    valid = sorted({int(p) for p in usable_partitions if isinstance(p, int) and 0 <= p < count})
    if not valid:
        raise ValueError("cannot draw a partition-scoped agent ID without at least one usable owned partition")

    if partition_loads:
        min_load = min(int(partition_loads.get(p, 0)) for p in valid)
        targets = frozenset(p for p in valid if int(partition_loads.get(p, 0)) == min_load)
    else:
        targets = frozenset(valid)

    excluded = frozenset(exclude_ids)
    attempts = max(10_000, count * 256) if max_attempts is None else int(max_attempts)
    for _ in range(attempts):
        suffix = token_generator() if token_generator is not None else secrets.token_hex(_DEFAULT_SUFFIX_HEX_BYTES)
        safe_prefix = _sanitize_prefix(prefix, len(suffix))
        candidate = f"{safe_prefix}_{suffix}"
        if candidate in excluded or not AGENT_ID_PATTERN.fullmatch(candidate):
            continue
        if agent_partition(team, candidate, count) in targets:
            return candidate
    raise RuntimeError(
        f"Could not draw a valid agent ID for team {team!r} in target partitions {sorted(targets)} "
        f"after {attempts} attempts."
    )


class HostPartitionTracker:
    """Watches verified ``partition_owner`` records for one host and tracks usable/released partitions."""

    def __init__(
        self,
        host: Any,
        team: str,
        *,
        logger: logging.Logger = _LOGGER,
        credentials_issued: Optional[Callable[[], bool]] = None,
        credential_partitions: Optional[Callable[[], frozenset[int]]] = None,
        watch_retry_seconds: float = _WATCH_RETRY_SECONDS,
    ) -> None:
        self.host = host
        self.team = team
        self.logger = logger
        self._credentials_issued = credentials_issued or (lambda: bool(getattr(host, "credentials_issued", False)))
        self._credential_partitions = credential_partitions or (
            lambda: frozenset(getattr(host, "credential_partitions", frozenset()))
        )
        self._watch_retry_seconds = float(watch_retry_seconds)
        self.ready = asyncio.Event()
        self.has_usable = asyncio.Event()

        self._records: dict[int, OwnedPartition] = {}
        # Replay protection per bucket key, retained across deletions (README "State records").
        self._held: dict[str, tuple[int, str]] = {}
        self._rejected: set[int] = set()
        self._refused_logged: dict[str, str] = {}
        self._listeners: list[Callable[[], None]] = []

        # Thread-safe immutable snapshots for synchronous callers (e.g. AgentInstantiationService.launch).
        self._snapshot_lock = threading.Lock()
        self._usable_snapshot: frozenset[int] = frozenset()
        self._owned_snapshot: frozenset[int] = frozenset()
        self._released_snapshot: dict[int, OwnedPartition] = {}
        self._rejected_snapshot: frozenset[int] = frozenset()
        self._partition_count_snapshot: int = self.partition_count

    @property
    def runtime(self) -> str:
        return str(self.host.profile.runtime)

    @property
    def partition_count(self) -> int:
        live = getattr(self.host, "live", None)
        if live is not None:
            return int(getattr(live.trust.store, "partition_count", DEFAULT_PARTITION_COUNT))
        return DEFAULT_PARTITION_COUNT

    @property
    def owned_by_host(self) -> frozenset[int]:
        with self._snapshot_lock:
            return self._owned_snapshot

    @property
    def released_by_host(self) -> dict[int, OwnedPartition]:
        with self._snapshot_lock:
            return dict(self._released_snapshot)

    @property
    def rejected_partitions(self) -> frozenset[int]:
        with self._snapshot_lock:
            return self._rejected_snapshot

    @property
    def usable_partitions(self) -> frozenset[int]:
        with self._snapshot_lock:
            return self._usable_snapshot

    @property
    def partition_count_snapshot(self) -> int:
        with self._snapshot_lock:
            return self._partition_count_snapshot

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def mark_rejected(self, partition: int) -> bool:
        """Record ``partition`` as refused due to ``credential_too_large``; returns True if newly added."""

        if partition in self._rejected:
            return False
        self._rejected.add(partition)
        self._recompute_snapshots(notify=True)
        return True

    def clear_rejected(self, partition: int) -> bool:
        """Clear a previous ``credential_too_large`` refusal for ``partition``."""

        if partition not in self._rejected:
            return False
        self._rejected.discard(partition)
        self._recompute_snapshots(notify=True)
        return True

    def refresh_usable(self) -> None:
        """Recompute usable partitions after the host's NATS credential changes."""

        self._recompute_snapshots(notify=False)

    def draw_agent_id(
        self,
        *,
        prefix: str = "agent",
        partition_loads: Optional[Mapping[int, int]] = None,
        exclude_ids: Collection[str] = (),
        token_generator: Optional[Callable[[], str]] = None,
    ) -> str:
        """Thread-safe draw of a contract-valid agent ID inside this host's least-filled usable partition."""

        with self._snapshot_lock:
            usable = self._usable_snapshot
            count = self._partition_count_snapshot
        return draw_agent_id(
            self.team,
            usable,
            count,
            prefix=prefix,
            partition_loads=partition_loads,
            exclude_ids=exclude_ids,
            token_generator=token_generator,
        )

    async def refresh(self) -> None:
        """List and verify every ``<team>.partition.*`` record currently in ``UA1_PLACEMENT``."""

        records = self.host.records
        assert records is not None and self.host.live is not None
        prefix = f"{self.team}.partition."
        keys = await records.keys(PARTITION_OWNER, prefix, team=self.team)
        seen_partitions: set[int] = set()
        changed = False
        for key in keys:
            raw_entry = await records.raw(PARTITION_OWNER, key)
            raw = None if raw_entry is None else raw_entry[1]
            parsed = await self._apply_raw(key, raw)
            if parsed is not None:
                seen_partitions.add(parsed[0])
                changed = changed or parsed[1]
        for existing in list(self._records):
            if existing not in seen_partitions:
                self._records.pop(existing, None)
                changed = True
        self._prune_rejected()
        self._recompute_snapshots(notify=changed)
        self.ready.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Watch ``<team>.partition.*`` in ``UA1_PLACEMENT`` until ``stop`` is set."""

        records = self.host.records
        assert records is not None and self.host.live is not None
        pattern = f"{self.team}.partition.*"
        while not stop.is_set():
            caught_up = asyncio.Event()
            try:
                async for key, raw in records.watch(PARTITION_OWNER, pattern, team=self.team, caught_up=caught_up):
                    if stop.is_set():
                        return
                    result = await self._apply_raw(key, raw)
                    pruned = self._prune_rejected()
                    if (result is not None and result[1]) or pruned:
                        self._recompute_snapshots(notify=True)
                    if caught_up.is_set() and not self.ready.is_set():
                        self._recompute_snapshots(notify=True)
                        self.ready.set()
                if caught_up.is_set() and not self.ready.is_set():
                    self._recompute_snapshots(notify=True)
                    self.ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - reconnect watch on transient NATS failure
                self.logger.warning(
                    "Watching partition ownership records failed; watching again. "
                    "event=host_partition_watch_failed runtime=%s team=%s error=%s: %s",
                    self.runtime,
                    self.team,
                    type(error).__name__,
                    error,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._watch_retry_seconds)
            except asyncio.TimeoutError:
                pass

    async def apply_event(self, key: str, raw: Optional[bytes]) -> bool:
        """Process one ``(key, raw)`` watch update; returns True if host-relevant ownership changed."""

        result = await self._apply_raw(key, raw)
        pruned = self._prune_rejected()
        changed = bool((result is not None and result[1]) or pruned)
        if changed:
            self._recompute_snapshots(notify=True)
        return changed

    async def _apply_raw(self, key: str, raw: Optional[bytes]) -> Optional[tuple[int, bool]]:
        """Verify and record one ``partition_owner`` value or deletion; returns ``(partition, changed)``."""

        prefix = f"{self.team}.partition."
        if not key.startswith(prefix):
            return None
        suffix = key[len(prefix):]
        if not suffix.isdigit():
            return None
        partition_from_key = int(suffix)
        if raw is None:
            existed = self._records.pop(partition_from_key, None) is not None
            return (partition_from_key, existed)

        verified = await self._verify(key, raw)
        if verified is None:
            return None

        partition = int(verified.body.partition)
        if partition != partition_from_key:
            self._log_refused(key, "invalid_body", "partition number does not match key")
            return None
        self._held[key] = (verified.document.sequence, verified.digest)
        self._refused_logged.pop(key, None)
        entry = OwnedPartition(
            partition=partition,
            state=str(verified.body.state),
            host=str(verified.body.host),
            host_epoch=int(verified.body.host_epoch),
            assigned_at=parse_timestamp(str(verified.body.assigned_at)),
            issued_at=parse_timestamp(str(verified.document.issued_at)),
            reason=str(verified.body.reason),
            sequence=int(verified.document.sequence),
            digest=str(verified.digest),
        )
        previous = self._records.get(partition)
        self._records[partition] = entry
        changed = previous != entry
        return (partition, changed)

    async def _verify(self, key: str, raw: bytes) -> Optional[VerifiedRecord]:
        """Verify a ``partition_owner`` record against the host's trust store (fetching missing certs once)."""

        live = self.host.live
        assert live is not None
        now = self.host.clock()
        held = self._held.get(key)
        try:
            try:
                verified = live.trust.store.verify(raw, record=PARTITION_OWNER, now=now, held=held)
            except FrameRefused as refused:
                fetch_trust = getattr(self.host, "fetch_trust", None)
                if refused.code != "unknown_key" or fetch_trust is None or not await fetch_trust(raw):
                    raise
                verified = live.trust.store.verify(raw, record=PARTITION_OWNER, now=now, held=held)
            require_record_key(verified, key)
            signer = verified.signer
            if signer is None or TEAM_SERVICES_ROLE not in signer.roles:
                raise FrameRefused("wrong_issuer", "only team services sign partition ownership", at="/writer")
            if str(verified.body.team) != self.team:
                raise FrameRefused("wrong_team", f"partition record belongs to {verified.body.team}", at="/body/team")
            expected_key = partition_owner_key(self.team, int(verified.body.partition), self.partition_count)
            if key != expected_key:
                raise FrameRefused("invalid_body", f"unexpected partition record key {key}", at="/key")
            assert isinstance(verified, VerifiedRecord)
            return verified
        except FrameRefused as refused:
            self._log_refused(key, refused.code, str(refused))
            return None

    def _log_refused(self, key: str, code: str, message: str) -> None:
        if self._refused_logged.get(key) == code:
            return
        self._refused_logged[key] = code
        self.logger.warning(
            "A partition ownership record did not verify and is ignored. "
            "event=host_partition_record_refused runtime=%s team=%s key=%s code=%s message=%s",
            self.runtime,
            self.team,
            key,
            code,
            message,
        )

    def _prune_rejected(self) -> bool:
        """Clear rejected partitions that are no longer ``owned`` by this host."""

        runtime = self.runtime
        stale = [
            p for p in self._rejected
            if (rec := self._records.get(p)) is None or rec.state != OWNED or rec.host != runtime
        ]
        for p in stale:
            self._rejected.discard(p)
        return bool(stale)

    def _recompute_snapshots(self, *, notify: bool) -> None:
        runtime = self.runtime
        owned = frozenset(
            p for p, rec in self._records.items() if rec.state == OWNED and rec.host == runtime
        )
        released = {
            p: rec for p, rec in self._records.items() if rec.state == RELEASED and rec.host == runtime
        }
        rejected = frozenset(self._rejected)
        if self._credentials_issued():
            granted = self._credential_partitions()
            usable = (owned & granted) - rejected
        else:
            usable = owned - rejected
        count = self.partition_count

        with self._snapshot_lock:
            self._owned_snapshot = owned
            self._released_snapshot = released
            self._rejected_snapshot = rejected
            self._usable_snapshot = usable
            self._partition_count_snapshot = count

        if usable:
            self.has_usable.set()
        else:
            self.has_usable.clear()

        if notify:
            for listener in list(self._listeners):
                try:
                    listener()
                except Exception:
                    self.logger.exception(
                        "A partition ownership listener failed. event=host_partition_listener_failed runtime=%s team=%s",
                        runtime,
                        self.team,
                    )
