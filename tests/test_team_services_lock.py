"""Keeping the team services' single-instance lock through failed renewals (team_services.lock).

A stand-in key-value bucket answers or fails each call as a test says, on a fake clock: the
lock's waits return at once and move the clock on. The lock over a real NATS server is
covered by tests/test_live_team_services.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pytest
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import KeyNotFoundError, KeyWrongLastSequenceError

from team_services import lock as lock_module
from team_services.lock import LEASE, RENEW_EVERY, LockLost, SingleInstanceLock, lock_key

START = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
TEAM = "team_one"


@dataclass
class _Entry:
    value: bytes
    revision: int


class _Bucket:
    """One key-value bucket; ``failures`` decides what each ``update`` does before it is applied."""

    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self.revision = 0
        self.updates = 0
        # Called with the update's number; returns an error to raise, or None to apply it.
        self.failure: Callable[[int], Optional[Exception]] = lambda number: None
        # An update applied whose answer is lost: the write lands, then this error is raised.
        self.lost_answer: Callable[[int], Optional[Exception]] = lambda number: None

    def _write(self, key: str, value: bytes) -> int:
        self.revision += 1
        self.entries[key] = _Entry(value, self.revision)
        return self.revision

    async def create(self, key: str, value: bytes) -> int:
        if key in self.entries:
            raise KeyWrongLastSequenceError(description="wrong last sequence")
        return self._write(key, value)

    async def update(self, key: str, value: bytes, last: Optional[int] = None) -> int:
        self.updates += 1
        error = self.failure(self.updates)
        if error is not None:
            raise error
        if key not in self.entries or self.entries[key].revision != last:
            raise KeyWrongLastSequenceError(description="wrong last sequence")
        revision = self._write(key, value)
        lost = self.lost_answer(self.updates)
        if lost is not None:
            raise lost
        return revision

    async def get(self, key: str) -> _Entry:
        if key not in self.entries:
            raise KeyNotFoundError()
        return self.entries[key]

    async def delete(self, key: str, last: Optional[int] = None) -> None:
        self.entries.pop(key, None)


class _JetStream:
    def __init__(self, bucket: _Bucket) -> None:
        self.bucket = bucket

    async def key_value(self, name: str) -> _Bucket:
        assert name == "UA1_LOCKS"
        return self.bucket


@pytest.fixture
def world(monkeypatch):
    now = [START]

    async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
        now[0] += timedelta(seconds=max(seconds, 0.0))

    monkeypatch.setattr(lock_module, "_wait_or_stop", wait_or_stop)
    bucket = _Bucket()
    held = SingleInstanceLock(_JetStream(bucket), TEAM, "team-services", lambda: now[0],
                              logging.getLogger("test.team_services_lock"))
    return now, bucket, held


def _holder(bucket: _Bucket) -> dict:
    return json.loads(bucket.entries[lock_key(TEAM)].value)


async def _keep_until(held: SingleInstanceLock, bucket: _Bucket, updates: int) -> asyncio.Event:
    """Keep the lock until ``updates`` updates were attempted, then stop as a clean stop does."""

    stop = asyncio.Event()
    failure = bucket.failure

    def counting(number: int) -> Optional[Exception]:
        if number >= updates:
            stop.set()
        return failure(number)

    bucket.failure = counting
    await held.keep(stop)
    return stop


@pytest.mark.asyncio
async def test_a_failed_renewal_is_tried_again_and_the_instance_keeps_serving(world, caplog) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    bucket.failure = lambda number: NatsTimeoutError() if number == 1 else None

    with caplog.at_level(logging.INFO):
        stop = await _keep_until(held, bucket, updates=3)

    assert stop.is_set()  # kept until the clean stop; nothing was raised
    assert caplog.text.count("event=team_services_lock_renew_failed") == 1
    assert "attempt=1" in caplog.text and "TimeoutError" in caplog.text
    assert "event=team_services_lock_renewed " in caplog.text and "failed_attempts=1" in caplog.text
    # Tried again 2 seconds after the failure, well inside the lease, then back to every 10 seconds.
    assert now[0] - START == RENEW_EVERY + timedelta(seconds=2) + RENEW_EVERY
    assert _holder(bucket)["instance"] == held.instance
    assert held.lease_ends_at == now[0] + LEASE  # the last renewal, applied as the stop came


@pytest.mark.asyncio
async def test_renewals_failing_until_the_lease_ends_stop_the_instance_with_a_clear_log(world, caplog) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    lease_end = held.lease_ends_at
    bucket.failure = lambda number: NatsTimeoutError()

    with caplog.at_level(logging.INFO), pytest.raises(LockLost, match="ran out without a successful renewal"):
        await held.keep(asyncio.Event())

    # Every attempt fell inside the lease and was logged; the instance stopped as the lease ended,
    # before any standby could take over (the lease's end plus the clock tolerance).
    attempts = caplog.text.count("event=team_services_lock_renew_failed")
    assert attempts == bucket.updates and attempts >= 5
    assert "event=team_services_lock_lease_ended" in caplog.text
    assert f"failed_attempts={attempts}" in caplog.text
    assert now[0] == lease_end
    assert "event=team_services_lock_lost" not in caplog.text


@pytest.mark.asyncio
async def test_losing_the_compare_and_set_to_another_instance_stops_at_once(world, caplog) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    # Another instance took the key (its lease ran out while this one was paused, say).
    other = SingleInstanceLock(_JetStream(bucket), TEAM, "team-services", lambda: now[0] + LEASE * 2)
    bucket.entries[lock_key(TEAM)] = _Entry(other._lease()[1], bucket.revision + 1)

    with caplog.at_level(logging.INFO), pytest.raises(LockLost, match="took the lock"):
        await held.keep(asyncio.Event())

    assert bucket.updates == 1  # no second attempt
    assert now[0] - START == RENEW_EVERY
    assert "event=team_services_lock_lost" in caplog.text and other.instance in caplog.text
    assert "event=team_services_lock_renew_failed" not in caplog.text


@pytest.mark.asyncio
async def test_a_renewal_whose_answer_was_lost_does_not_count_as_a_lost_lock(world, caplog) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    # The first renewal reaches NATS, but its answer never arrives.
    bucket.lost_answer = lambda number: NatsTimeoutError() if number == 1 else None

    with caplog.at_level(logging.INFO):
        stop = await _keep_until(held, bucket, updates=4)

    assert stop.is_set()
    assert "event=team_services_lock_renew_failed" in caplog.text
    assert "event=team_services_lock_revision_caught_up" in caplog.text
    assert "event=team_services_lock_lost" not in caplog.text
    assert held.revision == bucket.entries[lock_key(TEAM)].revision
    assert _holder(bucket)["instance"] == held.instance


@pytest.mark.asyncio
async def test_a_removed_lock_is_a_lost_lock(world) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    del bucket.entries[lock_key(TEAM)]

    with pytest.raises(LockLost, match="was removed"):
        await held.keep(asyncio.Event())
