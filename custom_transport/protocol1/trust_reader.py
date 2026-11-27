"""Read-only protocol 1 trust reader for processes that verify tokens without being hosts.

Long-running verifiers that are not protocol 1 hosts (such as ``secure_tool_broker``) and
one-shot operator commands (such as ``strands_main.py``) still need host certificates and
the installation's current revocations record from ``UA1_CERTS`` so they can verify
protocol 1 authority tokens against the same trust anchors every host uses.

A ``TrustReader`` holds no private key and never joins the installation authority or writes
any NATS record:

- Starts from ``layout.trust_bootstrap_path`` (``trust/bootstrap.json``) into a
  ``PersistentTrustStore`` under ``layout.reader_trust_dir(reader_id)`` so revocations
  persist across restarts.
- Reads ``UA1_CERTS`` exclusively through ``StateRecords`` (which uses ``bucket_reads``),
  staying inside the transport boundary with no raw NATS handle of its own.
- Supports one-shot sync (``snapshot`` / ``sync_once``), on-demand issuer certificate
  lookup (``fetch_token_issuer`` / ``fetch_certificate``), and background live watching
  plus periodic revocations refresh and trust forgetting (``watch_and_refresh``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from custom_messages.wire.authority_bindings import extract_token_issuer
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import TrustStore
from custom_transport.nats_adapter import NatsTransportConfig
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1.host_runtime import FORGET_INTERVAL, TRUST_REFRESH_INTERVAL
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.live import utc_now
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import PersistentTrustStore
from runtime_settings.instance_layout import InstanceLayout

_LOGGER = logging.getLogger(__name__)
_WATCH_RETRY_SECONDS = 5.0
_UPKEEP_CHECK_INTERVAL = timedelta(seconds=10)


def trust_reader_transport_config(config: Any, *, reader_id: str) -> NatsTransportConfig:
    """Build a read-only NATS transport config identifying ``reader_id`` (not ``team-services``).

    Uses a short connect timeout and single attempt so an unreachable NATS server fails fast
    while ``TrustReader`` handles background retries and reconnects after connection close (C18).
    """

    transport = config.transport
    servers = list(transport.nats_servers or ([transport.nats_url] if transport.nats_url else []))
    return NatsTransportConfig(
        servers=servers,
        name=f"{config.instance_name}-{reader_id}",
        subject_prefix=transport.subject_prefix,
        jetstream_enabled=False,
        user=transport.nats_user,
        password=transport.nats_password,
        credentials_file=transport.credentials_file,
        reply_inbox_runtime=reader_id,
        connect_timeout=2.0,
        max_reconnect_attempts=1,
        reconnect_time_wait=0.2,
    )


def _no_signing_identity() -> HostIdentity:
    raise RuntimeError("TrustReader is read-only and holds no host signing key.")


class TrustReader:
    """Read-only ``UA1_CERTS`` reader and watcher backed by ``PersistentTrustStore``."""

    def __init__(
        self,
        trust: PersistentTrustStore,
        records: Optional[StateRecords],
        *,
        reader_id: str,
        nc: Any = None,
        connect: Optional[Callable[[], Awaitable[Any]]] = None,
        clock: Callable[[], datetime] = utc_now,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self.trust = trust
        self.records = records
        self._nc = nc
        self.reader_id = reader_id
        self._connect = connect
        self.clock = clock
        self.logger = logger
        self._last_refresh: Optional[datetime] = None
        self._last_forget: Optional[datetime] = None

    @classmethod
    def open(
        cls,
        layout: InstanceLayout,
        reader_id: str,
        nc: Any = None,
        *,
        connect: Optional[Callable[[], Awaitable[Any]]] = None,
        clock: Callable[[], datetime] = utc_now,
        logger: logging.Logger = _LOGGER,
    ) -> "TrustReader":
        """Open a reader from ``layout.trust_bootstrap_path`` without loading any private key.

        When ``nc`` is ``None`` (for example, NATS is unreachable at startup), the reader still
        loads its persisted trust state so callers fail closed with the trust refusal reason,
        and ``watch_and_refresh`` connects via ``connect`` in the background once NATS answers.
        """

        bundle = read_bundle(layout.trust_bootstrap_path)
        trust = PersistentTrustStore.open(bundle, layout.reader_trust_dir(reader_id), clock())
        records = StateRecords.on_connection(nc, _no_signing_identity, trust) if nc is not None else None
        return cls(trust, records, reader_id=reader_id, nc=nc, connect=connect, clock=clock, logger=logger)

    def attach_connection(self, nc: Any) -> StateRecords:
        """Bind a live NATS connection to this reader once connected."""

        self._nc = nc
        self.records = StateRecords.on_connection(nc, _no_signing_identity, self.trust)
        return self.records

    def detach_connection(self) -> None:
        """Drop a closed NATS connection so background upkeep reconnects via ``connect``."""

        self.records = None
        self._nc = None

    def _is_connection_closed(self) -> bool:
        return self._nc is not None and bool(getattr(self._nc, "is_closed", False))

    def _looks_closed_error(self, error: Exception) -> bool:
        if self._is_connection_closed():
            return True
        name = type(error).__name__.lower()
        text = str(error).lower()
        return "connectionclosed" in name or "connection closed" in text

    @property
    def connected(self) -> bool:
        return self.records is not None and not self._is_connection_closed()

    @classmethod
    async def snapshot(
        cls,
        layout: InstanceLayout,
        reader_id: str,
        nc: Any,
        *,
        clock: Callable[[], datetime] = utc_now,
        logger: logging.Logger = _LOGGER,
    ) -> "TrustReader":
        """Perform a one-shot verified read of certificates and revocations for short-lived commands."""

        reader = cls.open(layout, reader_id, nc, clock=clock, logger=logger)
        await reader.sync_once()
        return reader

    @property
    def installation(self) -> str:
        return self.trust.store.installation

    @property
    def store(self) -> TrustStore:
        return self.trust.store

    @property
    def lock(self) -> threading.RLock:
        return self.trust.lock

    def trust_state(self) -> tuple[TrustStore, threading.RLock]:
        """The underlying ``TrustStore`` paired with the lock that serializes its mutations."""

        return self.trust.trust_state()

    @contextmanager
    def read_lock(self) -> Iterator[TrustStore]:
        """Acquire the trust store lock while inspecting ``TrustStore`` from another thread."""

        with self.trust.lock:
            yield self.trust.store

    async def _take_watch_bytes(self, key: str, raw: bytes) -> bool:
        """Verify key binding and add one watched trust record through ``StateRecords``."""

        if self.records is None:
            return False
        try:
            return await self.records.take_trust_bytes(key, raw, now=self.clock())
        except FrameRefused as refused:
            self.logger.warning(
                "A watched trust record did not verify and is skipped. event=trust_reader_record_refused "
                "reader=%s key=%s code=%s",
                self.reader_id,
                key,
                refused.code,
            )
            return False

    async def _take_key(self, record: str, key: str) -> bool:
        """Read one key from ``UA1_CERTS`` through ``StateRecords.take_trust_record``; skip invalid records."""

        if self.records is None:
            return False
        try:
            return await self.records.take_trust_record(record, key, now=self.clock())
        except FrameRefused as refused:
            self.logger.warning(
                "A trust record did not verify and is skipped. event=trust_reader_record_refused "
                "reader=%s key=%s code=%s",
                self.reader_id,
                key,
                refused.code,
            )
            return False

    async def sync_once(self) -> None:
        """Load every host certificate of this installation and the current revocations record."""

        if self.records is None:
            raise RuntimeError("TrustReader is not connected to NATS yet.")
        installation = self.installation
        prefix = f"host.{installation}."
        cert_keys = await self.records.keys("certificate", prefix)
        for key in cert_keys:
            await self._take_key("certificate", key)
        await self._take_key("revocations", f"revocations.{installation}")
        self._last_refresh = self.clock()

    async def refresh_revocations(self) -> bool:
        """Read the installation's newest revocations record from ``UA1_CERTS``."""

        if self.records is None:
            return False
        taken = await self._take_key("revocations", f"revocations.{self.installation}")
        self._last_refresh = self.clock()
        return taken

    async def fetch_certificate(self, installation: str, runtime: str, membership_epoch: int) -> bool:
        """Fetch one host certificate of this installation on demand (returns whether it was new)."""

        if self.records is None or installation != self.installation:
            return False
        key = f"host.{installation}.{runtime}.{membership_epoch}"
        return await self._take_key("certificate", key)

    async def fetch_token_issuer(self, token: Any) -> bool:
        """Fetch the issuer certificate named by ``token`` (and revocations if not current)."""

        if self._is_connection_closed():
            self.detach_connection()
        if self.records is None:
            return False
        extracted = extract_token_issuer(token)
        if extracted is None:
            return False
        installation, runtime, epoch = extracted
        if installation != self.installation:
            return False
        taken = False
        try:
            taken = await self.fetch_certificate(installation, runtime, epoch)
        except Exception as error:  # noqa: BLE001 - caller fails closed on verification if fetch fails
            self.logger.warning(
                "Could not fetch the token issuer's certificate. event=trust_reader_cert_fetch_failed "
                "reader=%s runtime=%s epoch=%s error=%s: %s",
                self.reader_id,
                runtime,
                epoch,
                type(error).__name__,
                error,
            )
        with self.trust.lock:
            current = self.trust.store.revocations_current(installation, self.clock())
        if not current:
            try:
                taken = await self.refresh_revocations() or taken
            except Exception as error:  # noqa: BLE001 - caller fails closed on stale revocations
                self.logger.warning(
                    "Could not refresh revocations on demand. event=trust_reader_revocations_fetch_failed "
                    "reader=%s error=%s: %s",
                    self.reader_id,
                    type(error).__name__,
                    error,
                )
        return taken

    async def _ensure_connected(self, stop: asyncio.Event) -> bool:
        """Connect in the background when offline or after a closed connection; False if stopped."""

        if self._is_connection_closed():
            self.detach_connection()
        if self.records is not None:
            return True
        if self._connect is None:
            return False
        while not stop.is_set():
            try:
                nc = await self._connect()
                self.attach_connection(nc)
                await self.sync_once()
                self.logger.info(
                    "Trust reader connected to NATS and synced UA1_CERTS. event=trust_reader_connected reader=%s",
                    self.reader_id,
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - keep retrying while NATS is unreachable
                self.detach_connection()
                self.logger.warning(
                    "Trust reader could not connect to NATS; retrying. event=trust_reader_connect_failed "
                    "reader=%s error=%s: %s",
                    self.reader_id,
                    type(error).__name__,
                    error,
                )
            if await _stopped_within(stop, timedelta(seconds=_WATCH_RETRY_SECONDS)):
                return False
        return False

    async def watch_and_refresh(self, stop: asyncio.Event) -> None:
        """Connect if offline, watch ``UA1_CERTS`` for changes, and reconnect if NATS closes permanently."""

        installation = self.installation
        while not stop.is_set():
            if not await self._ensure_connected(stop):
                return
            connection_lost = asyncio.Event()
            cert_task = asyncio.create_task(
                self._watch_pattern("certificate", f"host.{installation}.>", stop, connection_lost)
            )
            rev_task = asyncio.create_task(
                self._watch_pattern("revocations", f"revocations.{installation}", stop, connection_lost)
            )
            refresh_task = asyncio.create_task(self._refresh_loop(stop, connection_lost))
            stop_wait = asyncio.create_task(stop.wait())
            lost_wait = asyncio.create_task(connection_lost.wait())
            tasks = (cert_task, rev_task, refresh_task)
            try:
                await asyncio.wait({stop_wait, lost_wait}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (stop_wait, lost_wait):
                    waiter.cancel()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(stop_wait, lost_wait, *tasks, return_exceptions=True)
            if stop.is_set():
                return
            self.detach_connection()
            if self._connect is None:
                return
            if await _stopped_within(stop, timedelta(seconds=_WATCH_RETRY_SECONDS)):
                return

    async def _watch_pattern(
        self, record: str, pattern: str, stop: asyncio.Event, connection_lost: asyncio.Event
    ) -> None:
        while not stop.is_set() and not connection_lost.is_set():
            if self._is_connection_closed():
                self.logger.warning(
                    "Trust reader NATS connection closed; scheduling reconnect. "
                    "event=trust_reader_connection_closed reader=%s pattern=%s",
                    self.reader_id,
                    pattern,
                )
                self.detach_connection()
                connection_lost.set()
                return
            if self.records is None:
                connection_lost.set()
                return
            try:
                async for key, raw in self.records.watch(record, pattern):
                    if stop.is_set() or connection_lost.is_set():
                        return
                    if raw is not None:
                        await self._take_watch_bytes(key, raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - retry watch on transient NATS errors
                if self._looks_closed_error(error):
                    self.logger.warning(
                        "Trust reader NATS connection closed during watch; scheduling reconnect. "
                        "event=trust_reader_connection_closed reader=%s pattern=%s error=%s: %s",
                        self.reader_id,
                        pattern,
                        type(error).__name__,
                        error,
                    )
                    self.detach_connection()
                    connection_lost.set()
                    return
                self.logger.warning(
                    "Trust reader watch failed; retrying. event=trust_reader_watch_failed "
                    "reader=%s pattern=%s error=%s: %s",
                    self.reader_id,
                    pattern,
                    type(error).__name__,
                    error,
                )
            if self._is_connection_closed():
                self.detach_connection()
                connection_lost.set()
                return
            if await _stopped_within(stop, timedelta(seconds=_WATCH_RETRY_SECONDS)):
                return

    async def _refresh_loop(self, stop: asyncio.Event, connection_lost: asyncio.Event) -> None:
        while not stop.is_set() and not connection_lost.is_set():
            if await _stopped_within(stop, _UPKEEP_CHECK_INTERVAL):
                return
            if self._is_connection_closed():
                self.logger.warning(
                    "Trust reader NATS connection closed; scheduling reconnect. "
                    "event=trust_reader_connection_closed reader=%s",
                    self.reader_id,
                )
                self.detach_connection()
                connection_lost.set()
                return
            now = self.clock()
            if self._last_refresh is None or now - self._last_refresh >= TRUST_REFRESH_INTERVAL:
                try:
                    await self.refresh_revocations()
                except Exception as error:  # noqa: BLE001 - reconnect on closed connection, log otherwise
                    if self._looks_closed_error(error):
                        self.detach_connection()
                        connection_lost.set()
                        return
                    self.logger.exception(
                        "Periodic revocations refresh failed. event=trust_reader_refresh_failed reader=%s",
                        self.reader_id,
                    )
            if self._last_forget is None or now - self._last_forget >= FORGET_INTERVAL:
                self._last_forget = now
                self.trust.forget(now)


async def _stopped_within(stop: asyncio.Event, interval: timedelta) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
    except asyncio.TimeoutError:
        pass
    return stop.is_set()


__all__ = ["TrustReader", "trust_reader_transport_config"]
