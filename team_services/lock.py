"""The team services' single-instance lock (``transport.yaml`` ``UA1_LOCKS``; plan decision 44).

Two instances serving one team would each place agents and sign sessions, so the
contract allows one. An instance takes the key ``team_services.<team>`` with
compare-and-set, holding a 30-second lease it renews every 10 seconds. A second
instance does not serve while another holds an unexpired lease: it logs which
instance holds it and waits as a standby, taking over once the lease runs out (plus the
clock tolerance). The lock names the process instance, a random id chosen at start, not
only the runtime: a second copy of the same host, or a restart while the old process
still runs, has the same runtime id and must still wait.

Keeping the lock (``keep``):
- A renewal that finds another instance holding the lock has lost it: this instance
  stops serving at once (``LockLost``, ``event=team_services_lock_lost``).
- A renewal that fails for any other reason, such as NATS not answering in time or a
  short outage, is tried again every ``RETRY_EVERY`` while the lease lasts, and each
  failed attempt is logged (``event=team_services_lock_renew_failed``). The instance
  keeps serving meanwhile: its unexpired lease still keeps every other instance out.
  Stopping the team services at the first failed renewal stopped the whole team, since
  the launcher stops every service when one exits.
- When the lease runs out without a successful renewal, the instance stops
  (``LockLost``, ``event=team_services_lock_lease_ended``): a standby may take over from
  then on, so serving longer could let two instances serve. Each attempt is cut off
  when the lease ends.
- A renewal whose write reached NATS while its answer was lost leaves the key at a
  revision this instance does not know, so the next compare-and-set fails although the
  lock is still its own. The renewal then reads the key: when it names this instance, it
  renews from the key's revision; only another instance's name means the lock is lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from nats.js.errors import KeyNotFoundError, KeyWrongLastSequenceError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.values import parse_timestamp

LOCKS_BUCKET = "UA1_LOCKS"
LEASE = timedelta(seconds=30)
RENEW_EVERY = timedelta(seconds=10)
# After a failed renewal, how soon it is tried again while the lease lasts (module docstring).
RETRY_EVERY = timedelta(seconds=2)
_SKEW = timedelta(seconds=CLOCK_SKEW_SECONDS)
_LOGGER = logging.getLogger(__name__)


class LockLost(RuntimeError):
    """Another instance took the lock, or the lease ran out unrenewed: this one must stop serving."""


def lock_key(team: str) -> str:
    return f"team_services.{team}"


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Wait ``seconds``, or until ``stop`` is set if that comes first."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=max(seconds, 0.0))
    except asyncio.TimeoutError:
        pass


@dataclass
class SingleInstanceLock:
    js: Any
    team: str
    runtime: str
    clock: Callable[[], datetime]
    logger: logging.Logger = _LOGGER
    revision: Optional[int] = None
    # When the lease this instance last wrote ends; None until it holds the lock.
    lease_ends_at: Optional[datetime] = None
    instance: str = field(default_factory=lambda: secrets.token_hex(8))

    def _lease(self) -> tuple[datetime, bytes]:
        """A new lease's end, and the value that writes it."""

        ends = self.clock() + LEASE
        return ends, json.dumps({"runtime": self.runtime, "instance": self.instance,
                                 "lease_ends_at": format_timestamp(ends)}).encode()

    def _lease_left(self) -> float:
        """Seconds until the lease this instance holds ends; 0 when it holds none."""

        if self.lease_ends_at is None:
            return 0.0
        return (self.lease_ends_at - self.clock()).total_seconds()

    async def try_acquire(self) -> Optional[tuple[str, datetime]]:
        """Take the lock; ``None`` when taken, else who holds it and until when."""

        bucket = await self.js.key_value(LOCKS_BUCKET)
        key = lock_key(self.team)
        ends, value = self._lease()
        try:
            self.revision = int(await bucket.create(key, value))
            self.lease_ends_at = ends
            return None
        except KeyWrongLastSequenceError:
            pass
        try:
            entry = await bucket.get(key)
        except KeyNotFoundError:
            return await self.try_acquire()
        held = json.loads(entry.value) if entry.value else {}
        holder = f"{held.get('runtime', '')}/{held.get('instance', '')}"
        ends = parse_timestamp(held["lease_ends_at"]) if held else self.clock()
        if held.get("instance") != self.instance and self.clock() < ends + _SKEW:
            return holder, ends
        ends, value = self._lease()
        try:
            self.revision = int(await bucket.update(key, value, last=entry.revision))
            self.lease_ends_at = ends
        except KeyWrongLastSequenceError:
            return await self.try_acquire()
        if held.get("instance") != self.instance:
            self.logger.warning("Took the team services lock after its lease ran out. event=team_services_lock_taken_over "
                                "team=%s previous=%s", self.team, holder)
        return None

    async def acquire(self, stop: asyncio.Event) -> bool:
        """Wait until this instance holds the lock; ``False`` if ``stop`` is set first."""

        reported: Optional[str] = None
        while not stop.is_set():
            held = await self.try_acquire()
            if held is None:
                self.logger.info("Holding the team services lock. event=team_services_lock_held team=%s runtime=%s "
                                 "instance=%s", self.team, self.runtime, self.instance)
                return True
            holder, ends = held
            if reported != holder:
                self.logger.warning("Another team services instance serves this team; waiting as a standby. "
                                    "event=team_services_standby team=%s holder=%s lease_ends_at=%s",
                                    self.team, holder, format_timestamp(ends))
                reported = holder
            wait = max((ends + _SKEW - self.clock()).total_seconds(), 1.0)
            await _wait_or_stop(stop, min(wait, RENEW_EVERY.total_seconds()))
        return False

    async def renew(self) -> None:
        """Extend the lease; raise ``LockLost`` when another instance holds the lock now.

        Any other failure (NATS not answering) is raised as it is, for ``keep`` to try again.
        """

        bucket = await self.js.key_value(LOCKS_BUCKET)
        key = lock_key(self.team)
        ends, value = self._lease()
        try:
            self.revision = int(await bucket.update(key, value, last=self.revision))
        except KeyWrongLastSequenceError as error:
            revision = await self._own_revision(bucket, key, error)
            ends, value = self._lease()
            try:
                self.revision = int(await bucket.update(key, value, last=revision))
            except KeyWrongLastSequenceError as again:
                raise LockLost(f"another team services instance took the lock of team {self.team}") from again
        self.lease_ends_at = ends

    async def _own_revision(self, bucket: Any, key: str, error: Exception) -> int:
        """The key's revision while it still names this instance (module docstring); else ``LockLost``."""

        try:
            entry = await bucket.get(key)
        except KeyNotFoundError as missing:
            raise LockLost(f"the lock of team {self.team} was removed") from missing
        held = json.loads(entry.value) if entry.value else {}
        if held.get("instance") != self.instance:
            holder = f"{held.get('runtime', '')}/{held.get('instance', '')}"
            raise LockLost(f"another team services instance ({holder}) took the lock of team {self.team}") from error
        self.logger.info("The team services lock still names this instance; an earlier renewal reached NATS but its "
                         "answer was lost. Renewing from the lock's current revision. "
                         "event=team_services_lock_revision_caught_up team=%s instance=%s revision=%s known=%s",
                         self.team, self.instance, entry.revision, self.revision)
        return int(entry.revision)

    async def keep(self, stop: asyncio.Event) -> None:
        """Renew every 10 seconds until ``stop``; raise ``LockLost`` when the lock is lost (module docstring)."""

        failures = 0
        wait = RENEW_EVERY.total_seconds()
        while not stop.is_set():
            await _wait_or_stop(stop, wait)
            if stop.is_set():
                return
            left = self._lease_left()
            if left <= 0:
                self.logger.error("The team services lock could not be renewed before its lease ran out, so this "
                                  "instance stops serving; another instance may take over from now. "
                                  "event=team_services_lock_lease_ended team=%s instance=%s failed_attempts=%s "
                                  "lease_ended_at=%s", self.team, self.instance, failures,
                                  format_timestamp(self.lease_ends_at) if self.lease_ends_at else "none")
                raise LockLost(f"the lease on the lock of team {self.team} ran out without a successful renewal")
            try:
                await asyncio.wait_for(self.renew(), timeout=left)
            except LockLost as lost:
                self.logger.error("Another team services instance holds the lock, so this one stops serving. "
                                  "event=team_services_lock_lost team=%s instance=%s reason=%s",
                                  self.team, self.instance, lost)
                raise
            except Exception as error:  # noqa: BLE001 - any other failure is tried again while the lease lasts
                failures += 1
                left = max(self._lease_left(), 0.0)
                self.logger.warning("The team services lock could not be renewed; trying again while its lease "
                                    "lasts. event=team_services_lock_renew_failed team=%s instance=%s attempt=%s "
                                    "seconds_left=%.1f error=%s: %s", self.team, self.instance, failures, left,
                                    type(error).__name__, error)
                wait = min(RETRY_EVERY.total_seconds(), left)
                continue
            if failures:
                self.logger.info("Renewed the team services lock after failed attempts. "
                                 "event=team_services_lock_renewed team=%s instance=%s failed_attempts=%s",
                                 self.team, self.instance, failures)
            failures = 0
            wait = RENEW_EVERY.total_seconds()

    async def release(self) -> None:
        """Let a standby take over at once instead of after the lease."""

        bucket = await self.js.key_value(LOCKS_BUCKET)
        try:
            await bucket.delete(lock_key(self.team), last=self.revision)
        except (KeyWrongLastSequenceError, KeyNotFoundError):
            pass
