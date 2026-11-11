"""Taking and keeping the team services' single-instance lock through NATS failures (team_services.lock).

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
from types import SimpleNamespace
from typing import Callable, Optional

import pytest
from nats.errors import ConnectionClosedError, NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import (
    BadRequestError,
    InvalidKeyError,
    KeyNotFoundError,
    KeyWrongLastSequenceError,
    ServiceUnavailableError,
)

from team_services import lock as lock_module
from team_services.lock import (
    LEASE,
    RENEW_EVERY,
    RETRY_EVERY,
    LockConnectionClosed,
    LockLost,
    SingleInstanceLock,
    lock_key,
)

START = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
TEAM = "team_one"


@dataclass
class _Entry:
    value: bytes
    revision: int


class _Bucket:
    """One key-value bucket; ``failure`` and ``create_failure`` decide what each write does before it is applied."""

    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self.revision = 0
        self.updates = 0
        self.creates = 0
        # Called with the update's number; returns an error to raise, or None to apply it.
        self.failure: Callable[[int], Optional[Exception]] = lambda number: None
        # An update applied whose answer is lost: the write lands, then this error is raised.
        self.lost_answer: Callable[[int], Optional[Exception]] = lambda number: None
        # The same two for each create.
        self.create_failure: Callable[[int], Optional[Exception]] = lambda number: None
        self.lost_create_answer: Callable[[int], Optional[Exception]] = lambda number: None

    def _write(self, key: str, value: bytes) -> int:
        self.revision += 1
        self.entries[key] = _Entry(value, self.revision)
        return self.revision

    async def create(self, key: str, value: bytes) -> int:
        self.creates += 1
        error = self.create_failure(self.creates)
        if error is not None:
            raise error
        if key in self.entries:
            raise KeyWrongLastSequenceError(description="wrong last sequence")
        revision = self._write(key, value)
        lost = self.lost_create_answer(self.creates)
        if lost is not None:
            raise lost
        return revision

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
        if last is not None and key in self.entries and self.entries[key].revision != last:
            # As nats-py reports it: a bad request with JetStream's wrong last sequence code, not
            # the KeyWrongLastSequenceError it raises for updates.
            raise BadRequestError(code=400, err_code=10071, description=f"wrong last sequence: {self.entries[key].revision}")
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

    # Every attempt fell inside the lease and was logged, then one last compare-and-set at the lease's
    # end failed too; the instance stopped as the lease ended, before any standby could take over (the
    # lease's end plus the clock tolerance).
    attempts = caplog.text.count("event=team_services_lock_renew_failed")
    assert attempts + 1 == bucket.updates and attempts >= 5
    assert "event=team_services_lock_lease_ended" in caplog.text
    assert f"failed_attempts={attempts + 1}" in caplog.text
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
async def test_an_instance_that_lost_the_lock_leaves_it_to_the_other_when_it_stops(world) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    other = SingleInstanceLock(_JetStream(bucket), TEAM, "team-services", lambda: now[0] + LEASE * 2)
    bucket.entries[lock_key(TEAM)] = _Entry(other._lease()[1], bucket.revision + 1)
    with pytest.raises(LockLost):
        await held.keep(asyncio.Event())

    await held.release()  # as serve_team does on its way out

    assert _holder(bucket)["instance"] == other.instance


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


@pytest.mark.asyncio
async def test_taking_the_lock_tries_again_while_nats_fails_and_logs_each_kind_of_error_once(world, caplog) -> None:
    now, bucket, held = world
    errors = [NatsTimeoutError(), NatsTimeoutError(), NatsTimeoutError(), NoRespondersError(), ServiceUnavailableError(),
              ConnectionRefusedError(111, "Connection refused")]
    bucket.create_failure = lambda number: errors[number - 1] if number <= len(errors) else None

    with caplog.at_level(logging.INFO):
        assert await held.acquire(asyncio.Event()) is True

    # Tried again every 2 seconds, and served only after its own create succeeded.
    assert bucket.creates == len(errors) + 1
    assert now[0] - START == RETRY_EVERY * len(errors)
    assert _holder(bucket)["instance"] == held.instance
    assert held.revision == bucket.entries[lock_key(TEAM)].revision and held.lease_ends_at == now[0] + LEASE
    # Logged the first time and when the error changed, not on every attempt.
    failed = [record.getMessage() for record in caplog.records if "event=team_services_lock_acquire_failed" in record.getMessage()]
    assert [line.split("error=")[1].split(":")[0] for line in failed] == [
        "TimeoutError", "NoRespondersError", "ServiceUnavailableError", "ConnectionRefusedError"]
    assert "attempt=1 " in failed[0] and "attempt=4 " in failed[1]
    assert "event=team_services_lock_acquire_recovered" in caplog.text and f"failed_attempts={len(errors)}" in caplog.text
    assert "event=team_services_lock_held" in caplog.text


@pytest.mark.asyncio
async def test_a_stop_while_taking_the_lock_fails_ends_the_wait_without_serving(world, caplog) -> None:
    now, bucket, held = world
    stop = asyncio.Event()

    def failing(number: int) -> Exception:
        if number == 3:
            stop.set()
        return NatsTimeoutError()

    bucket.create_failure = failing

    with caplog.at_level(logging.INFO):
        assert await held.acquire(stop) is False

    assert bucket.creates == 3
    assert lock_key(TEAM) not in bucket.entries and held.lease_ends_at is None
    assert caplog.text.count("event=team_services_lock_acquire_failed") == 1
    assert "event=team_services_lock_held" not in caplog.text


@pytest.mark.asyncio
async def test_a_create_whose_answer_was_lost_takes_the_lock_on_the_next_attempt(world, caplog) -> None:
    now, bucket, held = world
    # The create reaches NATS, but its answer never arrives.
    bucket.lost_create_answer = lambda number: NatsTimeoutError() if number == 1 else None

    with caplog.at_level(logging.INFO):
        assert await held.acquire(asyncio.Event()) is True

    # The next attempt found the key naming this instance and updated it from the key's revision.
    entry = bucket.entries[lock_key(TEAM)]
    assert entry.revision == 2 and held.revision == 2 and bucket.updates == 1
    assert _holder(bucket)["instance"] == held.instance
    assert caplog.text.count("event=team_services_lock_acquire_failed") == 1
    assert "event=team_services_lock_held" in caplog.text
    assert "event=team_services_lock_taken_over" not in caplog.text
    assert "event=team_services_standby" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [b"not json", b'{"runtime": "team-services", "instance": "other"}',
                                   b'{"runtime": "team-services", "instance": "other", "lease_ends_at": "soon"}'])
async def test_a_malformed_lock_record_is_not_tried_again(world, caplog, value: bytes) -> None:
    now, bucket, held = world
    bucket.entries[lock_key(TEAM)] = _Entry(value, 1)

    with caplog.at_level(logging.INFO), pytest.raises((ValueError, KeyError)):
        await held.acquire(asyncio.Event())

    assert bucket.creates == 1 and now[0] == START
    assert "event=team_services_lock_acquire_failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionClosedError(), InvalidKeyError("team_services.team one")])
async def test_a_failure_that_trying_again_cannot_cure_is_not_tried_again(world, caplog, error: Exception) -> None:
    now, bucket, held = world
    bucket.create_failure = lambda number: error

    with caplog.at_level(logging.INFO), pytest.raises(type(error)):
        await held.acquire(asyncio.Event())

    assert bucket.creates == 1 and now[0] == START
    assert "event=team_services_lock_acquire_failed" not in caplog.text


# --- a lease that only looks ended, a value nobody can have written, a closed connection --------------


@pytest.mark.asyncio
async def test_a_lease_that_looks_ended_after_the_machine_slept_is_renewed_when_nobody_else_took_the_lock(
    world, caplog
) -> None:
    """A laptop's sleep stops the event loop's timers while the wall clock runs on, so the first renewal after waking
    finds the lease ended. The key still names this instance at the revision it wrote, so nobody served meanwhile."""

    now, bucket, held = world
    assert await held.try_acquire() is None
    now[0] += timedelta(seconds=60)  # slept: the wall clock ran on, the loop's timers did not
    with caplog.at_level(logging.INFO):
        await _keep_until(held, bucket, updates=2)
    assert "event=team_services_lock_lease_resumed" in caplog.text
    assert "event=team_services_lock_lease_ended" not in caplog.text
    assert _holder(bucket)["instance"] == held.instance and held.lease_ends_at > now[0]


@pytest.mark.asyncio
async def test_a_lease_that_ended_while_another_instance_took_the_lock_stays_lost(world, caplog) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    other = SingleInstanceLock(_JetStream(bucket), TEAM, "team-services", lambda: now[0])
    now[0] += LEASE + timedelta(seconds=9)  # past the lease and the clock tolerance
    assert await other.try_acquire() is None
    with caplog.at_level(logging.INFO), pytest.raises(LockLost):
        await held.keep(asyncio.Event())
    assert _holder(bucket)["instance"] == other.instance
    assert "event=team_services_lock_lease_resumed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [
    b"",
    json.dumps({"runtime": "team-services", "instance": "elsewhere", "lease_ends_at": "2100-01-01T00:00:00.000Z"}).encode(),
], ids=["empty", "lease_far_ahead"])
async def test_a_lock_value_no_instance_can_have_written_is_taken_over_once_unchanged_for_a_lease(
    world, monkeypatch, caplog, value: bytes
) -> None:
    """Another process on the shared NATS login, or a holder whose clock ran ahead, must not keep every instance
    waiting for ever: with nobody renewing, the key's revision stays put."""

    now, bucket, held = world
    waits = [0]

    async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
        waits[0] += 1
        if waits[0] > 50:
            stop.set()  # without the rule this would wait for ever
        now[0] += timedelta(seconds=max(seconds, 0.0))

    monkeypatch.setattr(lock_module, "_wait_or_stop", wait_or_stop)
    bucket._write(lock_key(TEAM), value)
    with caplog.at_level(logging.WARNING):
        assert await held.acquire(asyncio.Event()) is True
    assert _holder(bucket)["instance"] == held.instance
    assert now[0] - START >= LEASE, "taken over only after the value stayed unchanged for a lease"
    assert caplog.text.count("event=team_services_lock_invalid") == 1


@pytest.mark.asyncio
async def test_a_closed_nats_connection_stops_the_instance_at_once(world, caplog) -> None:
    """nats-py never reconnects a closed client; waiting out the lease would only hide why the team stops."""

    now, bucket, held = world
    assert await held.try_acquire() is None
    held.nc = SimpleNamespace(is_closed=True)
    stop = asyncio.Event()
    bucket.failure = lambda number: stop.set()  # any renewal attempt would end the test
    with caplog.at_level(logging.ERROR), pytest.raises(LockConnectionClosed):
        await held.keep(stop)
    assert bucket.updates == 0 and "event=team_services_nats_closed" in caplog.text


@pytest.mark.asyncio
async def test_a_renewal_that_finds_the_connection_closed_stops_the_instance_at_once(world) -> None:
    now, bucket, held = world
    assert await held.try_acquire() is None
    bucket.failure = lambda number: ConnectionClosedError()
    with pytest.raises(LockConnectionClosed):
        await held.keep(asyncio.Event())
    assert bucket.updates == 1
