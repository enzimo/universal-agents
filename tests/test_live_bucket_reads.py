"""Listing and watching a key-value bucket while other processes write to it (nats-server 2.15.1).

A listing of a bucket's keys (``StateRecords.keys``) must hold every key the bucket holds
for the whole listing, and a watch (``StateRecords.watch``) must give every such key
before it says its first values are all given (``caught_up``). The team services decide
from listings which placements are abandoned and whether a lead is already open, and the
team reader forgets every record a reopened watch did not give before ``caught_up``; so a
listing or watch that ends early removes or hides live records.

The bucket is the placement bucket with the contract's stream settings (history 1). It
holds four groups of keys under one team: keys nobody touches, keys the writers only
rewrite (never delete), keys the writers rewrite and delete, and keys deleted before the
writers start and never written again. Writers run on their own connections, as other
processes write. The keys of the first two groups are in the bucket for the whole read,
so every listing and every watch's first values must hold them all; the deleted keys are
never in the bucket during a read, so no listing may hold them and no watch may give them
a value. Each test checks every read and reports how many of them were bad.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from typing import AsyncIterator, Sequence

import pytest
from nats.js.api import StreamConfig

from custom_messages.wire.buckets import bucket_stream
from custom_messages.wire.kinds import STATE_RECORDS
from custom_transport.protocol1.state_records import StateRecords
from tests.test_live_message_contract import STORAGE, TRANSPORT
from tests.test_live_team_services import broker, connect  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

RECORD = "placement"
BUCKET = STATE_RECORDS[RECORD].bucket
TEAM = "team_one"
OTHER_TEAM = "team_two"
UNTOUCHED = frozenset(f"{TEAM}.untouched-{number}" for number in range(150))
REWRITTEN = frozenset(f"{TEAM}.rewritten-{number}" for number in range(50))  # put again, never deleted
CHURNED = frozenset(f"{TEAM}.churned-{number}" for number in range(50))  # put again or deleted
DELETED = frozenset(f"{TEAM}.deleted-{number}" for number in range(10))  # deleted before the reads, never written again
HELD = UNTOUCHED | REWRITTEN  # in the bucket for the whole read
MAY_HOLD = HELD | CHURNED  # every key a read may give with a value
# The only key under OTHER_TEAM: a listing of that team's keys must hold exactly this one.
SOLO = f"{OTHER_TEAM}.solo"
ROUNDS = 500
WRITERS = 4
# How long one watch may take to say its first values are all given.
WATCH_SECONDS = 10.0


async def filled_bucket(stack: AsyncExitStack, url: str) -> StateRecords:
    """Create the bucket as the topology owner does, fill it, and return a reader on its own connection."""

    owner = await connect(stack, url, "owner")
    js = owner.jetstream()
    settings = bucket_stream(TRANSPORT, BUCKET)
    await js.add_stream(config=StreamConfig(**{**settings, "storage": STORAGE[settings["storage"]]}))
    bucket = await js.key_value(BUCKET)
    for key in sorted(UNTOUCHED | REWRITTEN | CHURNED | DELETED | {SOLO}):
        await bucket.put(key, b"first value")
    for key in sorted(DELETED):
        await bucket.delete(key)
    reader = await connect(stack, url, "reader")
    # Listings and watches read no signatures, so the reader needs no identity or trust store.
    return StateRecords(reader.jetstream(), lambda: None, None)


@asynccontextmanager
async def writers(stack: AsyncExitStack, url: str, rewrite: Sequence[str],
                  churn: Sequence[str] = ()) -> AsyncIterator[list[int]]:
    """WRITERS clients on their own connections write until the block ends; yields the count of writes.

    Each write puts a new value under a key of ``rewrite`` or, half the time when ``churn``
    is given, puts or deletes a key of ``churn``. A writer's error fails the test.
    """

    stop = asyncio.Event()
    written = [0]

    async def write(number: int) -> None:
        connection = await connect(stack, url, f"writer-{number}")
        bucket = await connection.jetstream().key_value(BUCKET)
        chosen = random.Random(number)
        while not stop.is_set():
            if churn and chosen.random() < 0.5:
                key = chosen.choice(churn)
                if chosen.random() < 0.5:
                    await bucket.delete(key)
                else:
                    await bucket.put(key, b"c" * chosen.randrange(1, 400))
            else:
                await bucket.put(chosen.choice(rewrite), b"r" * chosen.randrange(1, 400))
            written[0] += 1
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(write(number)) for number in range(WRITERS)]
    try:
        await asyncio.sleep(0.2)  # the writers are writing before the first read
        yield written
    finally:
        stop.set()
        await asyncio.gather(*tasks)


# A bad read: its number, the keys it gave, the held keys it missed, the keys it gave that it
# must not, and how many keys a listing named more than once.
BadRead = tuple[int, set[str], set[str], set[str], int]


def bad_reads(bad: list[BadRead], reads: int, what: str, written: int) -> str:
    """A summary of the bad reads: how many, how they were wrong, and the first few."""

    empty = sum(1 for _round, found, _missing, _extra, _repeated in bad if not found)
    untouched = sum(1 for _round, _found, missing, _extra, _repeated in bad if missing & UNTOUCHED)
    rewritten = sum(1 for _round, _found, missing, _extra, _repeated in bad if missing & REWRITTEN)
    extra = sum(1 for _round, _found, _missing, extra, _repeated in bad if extra)
    repeated = sum(1 for *_read, repeats in bad if repeats)
    first = "; ".join(f"read {round_number}: {len(found)} keys, {len(missing)} missing, {len(extra)} not held, "
                      f"{repeats} repeated" for round_number, found, missing, extra, repeats in bad[:5])
    return (f"{len(bad)} of {reads} {what} were wrong while {WRITERS} writers wrote {written} times "
            f"({empty} empty, {untouched} missing an untouched key, {rewritten} missing a rewritten key, "
            f"{extra} giving a deleted or unknown key, {repeated} naming a key twice); {first}")


@pytest.mark.asyncio
async def test_a_listing_holds_every_key_the_bucket_holds_throughout(broker: str) -> None:  # noqa: F811
    async with AsyncExitStack() as stack:
        records = await filled_bucket(stack, broker)
        bad: list[BadRead] = []
        async with writers(stack, broker, sorted(REWRITTEN), sorted(CHURNED)) as written:
            for round_number in range(ROUNDS):
                listed = await records.keys(RECORD, f"{TEAM}.")
                found = set(listed)
                if HELD - found or found - MAY_HOLD or len(listed) != len(found):
                    bad.append((round_number, found, HELD - found, found - MAY_HOLD, len(listed) - len(found)))
        assert written[0] > ROUNDS, "the writers barely wrote while the bucket was listed"
        assert not bad, bad_reads(bad, ROUNDS, "listings", written[0])


@pytest.mark.asyncio
async def test_a_listing_of_a_single_rewritten_key_is_never_empty(broker: str) -> None:  # noqa: F811
    """The other team's only key is rewritten all the time; the bucket holds TEAM's keys besides it."""

    async with AsyncExitStack() as stack:
        records = await filled_bucket(stack, broker)
        bad: list[BadRead] = []
        async with writers(stack, broker, [SOLO]) as written:
            for round_number in range(ROUNDS):
                listed = await records.keys(RECORD, f"{OTHER_TEAM}.")
                if listed != [SOLO]:
                    bad.append((round_number, set(listed), {SOLO} - set(listed), set(listed) - {SOLO},
                                len(listed) - len(set(listed))))
        assert written[0] > ROUNDS, "the writers barely wrote while the bucket was listed"
        assert not bad, bad_reads(bad, ROUNDS, "listings of one key", written[0])


async def first_values(records: StateRecords) -> tuple[set[str], set[str]]:
    """What a watch of TEAM's keys gave before it said its first values were all given.

    Returns the keys whose last change was a value, and every key it gave a value at all.
    """

    caught_up = asyncio.Event()
    given: set[str] = set()
    valued: set[str] = set()

    async def read() -> None:
        async with aclosing(records.watch(RECORD, f"{TEAM}.>", team=TEAM, caught_up=caught_up)) as changes:
            async for key, value in changes:
                if caught_up.is_set():
                    return  # a change after the first values
                if value is None:
                    given.discard(key)
                else:
                    given.add(key)
                    valued.add(key)

    reading = asyncio.create_task(read())
    waiting = asyncio.create_task(caught_up.wait())
    try:
        await asyncio.wait({reading, waiting}, timeout=WATCH_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if reading.done():
            reading.result()  # the watch's own error
        assert caught_up.is_set(), f"a watch did not say its first values were all given within {WATCH_SECONDS} s"
    finally:
        for task in (reading, waiting):
            task.cancel()
        await asyncio.gather(reading, waiting, return_exceptions=True)
    return given, valued


@pytest.mark.asyncio
async def test_a_watch_gives_every_key_the_bucket_holds_before_it_says_caught_up(broker: str) -> None:  # noqa: F811
    async with AsyncExitStack() as stack:
        records = await filled_bucket(stack, broker)
        bad: list[BadRead] = []
        async with writers(stack, broker, sorted(REWRITTEN), sorted(CHURNED)) as written:
            for round_number in range(ROUNDS):
                given, valued = await first_values(records)
                if HELD - given or valued - MAY_HOLD:
                    bad.append((round_number, given, HELD - given, valued - MAY_HOLD, 0))  # a watch may repeat a key
        assert written[0] > ROUNDS, "the writers barely wrote while the bucket was watched"
        assert not bad, bad_reads(bad, ROUNDS, "watches", written[0])
