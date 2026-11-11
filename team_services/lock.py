"""The team services' single-instance lock (``transport.yaml`` ``UA1_LOCKS``; plan decision 44).

Two instances serving one team would each place agents and sign sessions, so the
contract allows one. An instance takes the key ``team_services.<team>`` with
compare-and-set, holding a 30-second lease it renews every 10 seconds. A second
instance does not serve while another holds an unexpired lease: it logs which
instance holds it and waits as a standby, taking over once the lease runs out (plus the
clock tolerance). The lock names the process instance, a random id chosen at start, not
only the runtime: a second copy of the same host, or a restart while the old process
still runs, has the same runtime id and must still wait.

Taking the lock (``acquire``):
- An instance serves only once its own create or compare-and-set update of the key has
  succeeded.
- A failure of NATS or of the connection to it, such as NATS not answering in time, no
  responders, a JetStream error or a dropped connection, is tried again every
  ``RETRY_EVERY`` until the lock is taken or the process is stopped. It is logged the
  first time and whenever the error changes (``event=team_services_lock_acquire_failed``),
  and once NATS answers again (``event=team_services_lock_acquire_recovered``). Letting
  the error escape stopped a standby, or a first instance starting while NATS was briefly
  unavailable, and with it the whole team.
- No lease bounds these attempts, so only such failures are tried again. A closed NATS
  client, which never reconnects, an invalid key and a malformed lock record still stop
  the process.
- A create whose write reached NATS while its answer was lost leaves the key naming this
  instance: the next attempt finds its own name and takes the lock by updating from the
  key's revision, not as a takeover.
- A lock value no instance can have written, a lease ending later than a whole lease from
  now plus the clock tolerance or an empty value (another process on the shared NATS login,
  or a holder whose clock ran ahead), names no live holder once the key has stayed at the
  same revision for a lease plus the clock tolerance: a holder renews every 10 seconds,
  which changes the revision. It is logged (``event=team_services_lock_invalid``) and taken
  over then, rather than waited for forever.

Keeping the lock (``keep``):
- A renewal that finds another instance holding the lock has lost it: this instance
  stops serving at once (``LockLost``, ``event=team_services_lock_lost``).
- A renewal that fails for any other reason, such as NATS not answering in time or a
  short outage, is tried again every ``RETRY_EVERY`` while the lease lasts, and each
  failed attempt is logged (``event=team_services_lock_renew_failed``). The instance
  keeps serving meanwhile: its unexpired lease still keeps every other instance out.
  Stopping the team services at the first failed renewal stopped the whole team, since
  the launcher stops every service when one exits.
- When the lease runs out without a successful renewal, the instance tries one last
  compare-and-set at the revision it wrote, cut off well before a standby may take over
  (the lease's end plus the clock tolerance). It succeeds only when no other instance
  wrote the key since, so nobody else served: this instance keeps the lock and logs it
  (``event=team_services_lock_lease_resumed``). That is what a laptop's sleep or a paused
  machine looks like: the event loop's timers stop with the machine while the wall clock
  runs on, so the lease looks ended at the first renewal after waking. Otherwise the
  instance stops (``LockLost``, ``event=team_services_lock_lease_ended``): a standby may take
  over from then on, so serving longer could let two instances serve. Each attempt is cut
  off when the lease ends.
- A NATS connection that closed for good (nats-py never reconnects it) stops the instance
  at once (``LockConnectionClosed``, ``event=team_services_nats_closed``), so the launcher's
  supervisor sees it instead of a lease that ran out.
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

from nats.errors import ConnectionClosedError
from nats.errors import Error as NatsError
from nats.js.errors import BadRequestError, InvalidKeyError, KeyNotFoundError, KeyWrongLastSequenceError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.state_records import wrong_revision

LOCKS_BUCKET = "UA1_LOCKS"
LEASE = timedelta(seconds=30)
RENEW_EVERY = timedelta(seconds=10)
# After a failed renewal, or a failed attempt to take the lock, how soon it is tried again (module docstring).
RETRY_EVERY = timedelta(seconds=2)
# Failures of NATS or of the connection to it, which taking the lock tries again; a JetStream error is a NATS error.
_UNAVAILABLE = (NatsError, OSError, asyncio.TimeoutError)
# Failures among those that trying again cannot cure: a closed client never reconnects, an invalid key stays invalid.
_LASTING = (ConnectionClosedError, InvalidKeyError)
_SKEW = timedelta(seconds=CLOCK_SKEW_SECONDS)
# How long the last renewal at the lease's end may take: well inside the clock tolerance a standby waits.
_LAST_RENEWAL = _SKEW / 2
_LOGGER = logging.getLogger(__name__)


class LockLost(RuntimeError):
    """Another instance took the lock, or the lease ran out unrenewed: this one must stop serving."""


class LockConnectionClosed(LockLost):
    """The NATS connection closed for good, so the lease can never be renewed: this instance stops."""


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
    # The NATS connection, to tell a closed one from a short outage (module docstring); None in tests.
    nc: Any = None
    # An invalid lock value (module docstring): its revision and when this instance first saw it there.
    _invalid_seen: Optional[tuple[int, datetime]] = None

    @classmethod
    def on_connection(cls, nc: Any, team: str, runtime: str, clock: Callable[[], datetime],
                      logger: logging.Logger = _LOGGER) -> "SingleInstanceLock":
        """The lock on a host's NATS connection; its JetStream handle stays in this module (NATS_CLIENT_MODULES)."""

        return cls(nc.jetstream(), team, runtime, clock, logger, nc=nc)

    def _connection_closed(self) -> bool:
        return bool(self.nc is not None and getattr(self.nc, "is_closed", False))

    def _holder_lease(self, held: dict[str, Any], revision: int) -> datetime:
        """When the lease another instance wrote ends; for an invalid value, when this instance may take it over."""

        now = self.clock()
        ends = parse_timestamp(held["lease_ends_at"]) if held else None
        if ends is not None and ends <= now + LEASE + _SKEW:
            self._invalid_seen = None
            return ends
        if self._invalid_seen is None or self._invalid_seen[0] != revision:
            self._invalid_seen = (revision, now)
            self.logger.warning("The team services lock holds a value no instance can have written; it is taken over "
                                "once it stays unchanged for a lease. event=team_services_lock_invalid team=%s "
                                "revision=%s lease_ends_at=%s", self.team, revision,
                                format_timestamp(ends) if ends is not None else "none")
        return self._invalid_seen[1] + LEASE
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
        if held.get("instance") != self.instance:
            ends = self._holder_lease(held, int(entry.revision))
            if self.clock() < ends + _SKEW:
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
        """Wait until this instance holds the lock; ``False`` if ``stop`` is set first (module docstring)."""

        reported: Optional[str] = None
        failures = 0
        failing: Optional[type] = None
        while not stop.is_set():
            try:
                held = await self.try_acquire()
            except Exception as error:
                if not isinstance(error, _UNAVAILABLE) or isinstance(error, _LASTING):
                    raise
                failures += 1
                if type(error) is not failing:
                    self.logger.warning("The team services lock could not be taken; trying again every %s seconds. "
                                        "event=team_services_lock_acquire_failed team=%s instance=%s attempt=%s "
                                        "error=%s: %s", RETRY_EVERY.seconds, self.team, self.instance, failures,
                                        type(error).__name__, error)
                    failing = type(error)
                await _wait_or_stop(stop, RETRY_EVERY.total_seconds())
                continue
            if failures:
                self.logger.info("NATS answered the team services lock again after failed attempts. "
                                 "event=team_services_lock_acquire_recovered team=%s instance=%s failed_attempts=%s",
                                 self.team, self.instance, failures)
                failures, failing = 0, None
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
            if self._connection_closed():
                self.logger.error("The NATS connection closed for good, so the team services lock cannot be renewed; "
                                  "this instance stops. event=team_services_nats_closed team=%s instance=%s",
                                  self.team, self.instance)
                raise LockConnectionClosed(f"the NATS connection of team {self.team}'s services closed")
            left = self._lease_left()
            if left <= 0:
                await self._renew_at_lease_end(failures)
                failures, wait = 0, RENEW_EVERY.total_seconds()
                continue
            try:
                await asyncio.wait_for(self.renew(), timeout=left)
            except LockLost as lost:
                self.logger.error("Another team services instance holds the lock, so this one stops serving. "
                                  "event=team_services_lock_lost team=%s instance=%s reason=%s",
                                  self.team, self.instance, lost)
                raise
            except ConnectionClosedError as closed:
                self.logger.error("The NATS connection closed for good, so the team services lock cannot be renewed; "
                                  "this instance stops. event=team_services_nats_closed team=%s instance=%s",
                                  self.team, self.instance)
                raise LockConnectionClosed(f"the NATS connection of team {self.team}'s services closed") from closed
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

    async def _renew_at_lease_end(self, failures: int) -> None:
        """The last compare-and-set once the lease looks ended; ``LockLost`` unless it succeeds (module docstring)."""

        ended_at = format_timestamp(self.lease_ends_at) if self.lease_ends_at else "none"
        try:
            await asyncio.wait_for(self.renew(), timeout=_LAST_RENEWAL.total_seconds())
        except LockLost as lost:
            self.logger.error("Another team services instance holds the lock, so this one stops serving. "
                              "event=team_services_lock_lost team=%s instance=%s reason=%s", self.team, self.instance,
                              lost)
            raise
        except Exception as error:  # noqa: BLE001 - any failure here means the lease ended unrenewed
            self.logger.error("The team services lock could not be renewed before its lease ran out, so this "
                              "instance stops serving; another instance may take over from now. "
                              "event=team_services_lock_lease_ended team=%s instance=%s failed_attempts=%s "
                              "lease_ended_at=%s error=%s: %s", self.team, self.instance, failures + 1, ended_at,
                              type(error).__name__, error)
            raise LockLost(f"the lease on the lock of team {self.team} ran out without a successful renewal") from error
        self.logger.warning("The team services lock's lease looked ended, but the lock still named this instance at "
                            "the revision it wrote, so nobody else served; it was renewed (a machine that slept or "
                            "paused, or a slow start). event=team_services_lock_lease_resumed team=%s instance=%s "
                            "lease_ended_at=%s failed_attempts=%s", self.team, self.instance, ended_at, failures)

    async def release(self) -> None:
        """Let a standby take over at once instead of after the lease; a lock another instance holds now stays."""

        bucket = await self.js.key_value(LOCKS_BUCKET)
        try:
            await bucket.delete(lock_key(self.team), last=self.revision)
        except KeyNotFoundError:
            pass
        except BadRequestError as error:
            # nats-py reports a delete under another revision as a bad request (state_records.wrong_revision).
            if not wrong_revision(error):
                raise
