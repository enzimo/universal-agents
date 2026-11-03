"""The team services' single-instance lock (``transport.yaml`` ``UA1_LOCKS``; plan decision 44).

Two instances serving one team would each place agents and sign sessions, so the
contract allows one. An instance takes the key ``team_services.<team>`` with
compare-and-set, holding a 30-second lease it renews every 10 seconds. A second
instance does not serve while another holds an unexpired lease: it logs which
instance holds it and waits as a standby, taking over once the lease runs out. An
instance whose renewal finds another holder has lost the lock and must stop serving
at once (``LockLost``). The lock names the process instance, a random id chosen at
start, not only the runtime: a second copy of the same host, or a restart while the old
process still runs, has the same runtime id and must still wait.
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
_SKEW = timedelta(seconds=CLOCK_SKEW_SECONDS)
_LOGGER = logging.getLogger(__name__)


class LockLost(RuntimeError):
    """Another instance took the lock: this one must stop serving."""


def lock_key(team: str) -> str:
    return f"team_services.{team}"


@dataclass
class SingleInstanceLock:
    js: Any
    team: str
    runtime: str
    clock: Callable[[], datetime]
    logger: logging.Logger = _LOGGER
    revision: Optional[int] = None
    instance: str = field(default_factory=lambda: secrets.token_hex(8))

    def _value(self) -> bytes:
        return json.dumps({"runtime": self.runtime, "instance": self.instance,
                           "lease_ends_at": format_timestamp(self.clock() + LEASE)}).encode()

    async def try_acquire(self) -> Optional[tuple[str, datetime]]:
        """Take the lock; ``None`` when taken, else who holds it and until when."""

        bucket = await self.js.key_value(LOCKS_BUCKET)
        key = lock_key(self.team)
        try:
            self.revision = int(await bucket.create(key, self._value()))
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
        try:
            self.revision = int(await bucket.update(key, self._value(), last=entry.revision))
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
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(wait, RENEW_EVERY.total_seconds()))
            except asyncio.TimeoutError:
                pass
        return False

    async def renew(self) -> None:
        """Extend the lease; raise ``LockLost`` when another instance holds the lock now."""

        bucket = await self.js.key_value(LOCKS_BUCKET)
        try:
            self.revision = int(await bucket.update(lock_key(self.team), self._value(), last=self.revision))
        except KeyWrongLastSequenceError as error:
            raise LockLost(f"another team services instance took the lock of team {self.team}") from error

    async def keep(self, stop: asyncio.Event) -> None:
        """Renew every 10 seconds until ``stop``; raise ``LockLost`` if the lock is lost."""

        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=RENEW_EVERY.total_seconds())
            except asyncio.TimeoutError:
                await self.renew()

    async def release(self) -> None:
        """Let a standby take over at once instead of after the lease."""

        bucket = await self.js.key_value(LOCKS_BUCKET)
        try:
            await bucket.delete(lock_key(self.team), last=self.revision)
        except (KeyWrongLastSequenceError, KeyNotFoundError):
            pass
