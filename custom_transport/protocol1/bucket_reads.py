"""Reading a key-value bucket's keys and values: listings and watches whose first values cannot end early.

README_ARCHITECTURE.md "Bucket reads". A watch first gives each key's current value (its
"first values"), then every later change. The read has to know where the first values
end: a listing stops there, and a watch's reader then knows it holds the whole bucket.
nats-py's ``KeyValue.watch()`` and ``keys()`` decide that from the consumer's pending
count, which can end the first values early while other processes write to the bucket:

1. nats-py puts its end marker first when the consumer information it asks for says
   nothing is pending and the client has received nothing. The server answers that
   request on another path than the messages, so the answer can arrive before messages
   the server already counted as delivered; every key then follows the marker and a
   listing comes back empty.
2. The server's pending count lags a rewrite: rewriting a key the consumer has not sent
   yet lowers the count at once and raises it for the new message only a moment later.
   A message stamped "0 pending" can then be followed by keys the bucket held the whole
   time, so a listing comes back short.

This module does not use the pending count. Right after the consumer exists, it asks the
stream which keys it holds under the same filter (stream information with a subjects
filter, a request every bucket reader is already granted), and the first values end once
each of those keys has been given, as a value or a deletion. A key the stream held then
was either already passed by the consumer, and so sent, or is still ahead of it and will
be sent, in that version or a later one. Keys written later may or may not come before
the end, which is right either way.

What can still stop a key from arriving, and what the read does:

- A bucket that drops records by age (``UA1_HOSTS``, ``UA1_AGENTS``) removes a key with
  no message. Each time the read has spent ``QUIET_SECONDS`` waiting for messages since
  it last asked, while keys are still awaited, it asks the stream again and stops waiting
  for the keys it no longer holds (``event=bucket_read_keys_expired``). The waiting adds
  up across messages, so other keys that keep changing do not put the question off.
- After ``FIRST_VALUES_SECONDS`` spent waiting for messages, the read raises
  ``BucketReadTimedOut``. Callers treat a failed read as a failure, never as an empty bucket.
- The server lists at most ``MAX_SUBJECTS`` subjects in one reply and nats-py reads no
  paging fields, so a reply that large raises ``BucketTooLarge`` instead of being trusted.

Subscriptions are nats-py ordered consumers with the settings its own watch uses, so a
reader needs no permission beyond the contract's key-value read grants (an unnamed
consumer, its information, its flow control, the stream's information). A key-value
watch is an ordered push subscription on state, never work, so ADR-019's pull-only rule
does not apply to it; the architecture boundary test lists this module among the NATS
subscribers for that reason. A listing's consumer leaves the server a few seconds after
the listing ends; a watch's consumer stays for ``WATCH_INACTIVE_SECONDS`` so a short
disconnection does not remove it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Collection, Optional

from nats.errors import BadSubscriptionError, ConnectionClosedError, ConnectionDrainingError
from nats.js.api import DeliverPolicy
from nats.js.kv import KV_DEL, KV_MARKER_REASON, KV_OP, KV_PURGE

from custom_messages.wire.buckets import bucket_stream_name, bucket_subject

_LOGGER = logging.getLogger(__name__)

# How long a read may wait for messages before its first values have all arrived.
FIRST_VALUES_SECONDS = 10.0
# With keys still awaited, ask the stream which it still holds each time this much waiting has added up.
QUIET_SECONDS = 1.0
# Messages handed over by nats-py, kept in order for the reader (the size nats-py's watch uses).
# It does not hold the server back: nats-py answers the consumer's flow control as messages
# arrive and keeps what the reader has not taken yet in its own subscription queue, up to its
# limits per subscription (over 500,000 messages or 256 MiB), far above the product's buckets.
QUEUE_SIZE = 256
# How many of the awaited keys a timed-out read names in its error.
NAMED_KEYS = 5
# How long the server keeps a watch's consumer after its client stops answering (nats-py's default).
WATCH_INACTIVE_SECONDS = 300.0
# nats-server's JSMaxSubjectDetails: a stream information reply lists at most this many subjects.
MAX_SUBJECTS = 100_000
# KV-Operation values for a deleted or purged key, and the server's marker reasons that mean the same.
_DELETIONS = frozenset({KV_DEL, KV_PURGE})
_MARKER_DELETIONS = frozenset({"MaxAge", "Purge", "Remove"})
# A key prefix that names whole tokens ("team_one.", "session.install.team."): a listing
# can then ask the server for those keys only (``<prefix>>``), without wildcards.
_TOKEN_PREFIX = re.compile(r"[-/_=a-zA-Z0-9]+(?:\.[-/_=a-zA-Z0-9]+)*\.")


class BucketReadTimedOut(Exception):
    """A read's first values did not all arrive in time; the bucket's contents are unknown.

    The message names the first ``NAMED_KEYS`` awaited keys in order, so a log shows which
    records never arrived; ``awaited`` is how many there were.
    """

    def __init__(self, bucket: str, subject: str, awaited: Collection[str], received: int) -> None:
        ordered = sorted(awaited)
        named = ", ".join(ordered[:NAMED_KEYS])
        more = f" and {len(ordered) - NAMED_KEYS} more" if len(ordered) > NAMED_KEYS else ""
        super().__init__(f"Reading bucket {bucket} ({subject}) timed out after {FIRST_VALUES_SECONDS:g} s "
                         f"waiting for {len(ordered)} keys ({named}{more}); {received} messages arrived.")
        self.bucket = bucket
        self.subject = subject
        self.awaited = len(ordered)
        self.received = received


class BucketTooLarge(Exception):
    """The stream listed as many subjects as one reply can hold, so the list may be cut short."""

    def __init__(self, bucket: str, subject: str) -> None:
        super().__init__(f"Bucket {bucket} holds {MAX_SUBJECTS} or more keys under {subject}; "
                         "one stream information reply cannot list them all.")
        self.bucket = bucket
        self.subject = subject


@dataclass(frozen=True)
class KeyChange:
    """One key's change: its value, or ``None`` when the key was deleted or purged.

    A headers-only read (a listing) gives ``b""`` as the value of a key that holds one.
    """

    key: str
    value: Optional[bytes]


async def watch_keys(js: Any, bucket: str, keys: str = ">", *,
                     values: bool = True) -> AsyncGenerator[Optional[KeyChange], None]:
    """Each key under ``keys`` in ``bucket`` (a filled bucket name) with its current value, then every change.

    Yields ``None`` once, right after the first values (first of all when the bucket holds
    no key under ``keys``). With ``values=False`` the server sends headers only. Raises
    ``BucketReadTimedOut`` or ``BucketTooLarge`` before the first values end, and whatever
    the client raises for a failed request. The subscription is removed however the read
    ends: finished, failed, closed or cancelled.
    """

    subject = bucket_subject(bucket, keys)
    key_start = len(bucket_subject(bucket, ""))
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_SIZE)
    subscription = await js.subscribe(
        subject, stream=bucket_stream_name(bucket), cb=queue.put, ordered_consumer=True,
        deliver_policy=DeliverPolicy.LAST_PER_SUBJECT, headers_only=not values,
        inactive_threshold=WATCH_INACTIVE_SECONDS if values else None)
    try:
        # Asked only now that the consumer exists: every key held at this moment is sent.
        awaited = await _held_keys(js, bucket, subject)
        loop = asyncio.get_running_loop()
        waited = 0.0  # time spent waiting for messages; the reader's own time between changes does not count
        asked_at = 0.0  # ``waited`` when the stream was last asked which keys it holds
        received = 0
        while awaited:
            if waited >= min(asked_at + QUIET_SECONDS, FIRST_VALUES_SECONDS):
                # Asked again at the deadline too, so a key dropped just before it does not fail the read.
                awaited = await _still_held(js, bucket, subject, awaited)
                asked_at = waited
                if awaited and waited >= FIRST_VALUES_SECONDS:
                    raise BucketReadTimedOut(bucket, subject, awaited, received)
                continue
            started = loop.time()
            try:
                message = await asyncio.wait_for(
                    queue.get(), min(asked_at + QUIET_SECONDS, FIRST_VALUES_SECONDS) - waited)
            except TimeoutError:
                message = None
            waited += loop.time() - started
            if message is None:
                continue
            received += 1
            key = message.subject[key_start:]
            awaited.discard(key)
            change = _change(bucket, key, message)
            if change is not None:
                yield change
        yield None
        while True:
            message = await queue.get()
            change = _change(bucket, message.subject[key_start:], message)
            if change is not None:
                yield change
    finally:
        await _unsubscribe(subscription)


async def list_keys(js: Any, bucket: str, prefix: str = "") -> list[str]:
    """The keys of ``bucket`` (a filled bucket name) that start with ``prefix`` and hold a value.

    The whole list, or an error: an empty list means the stream held no such key when the
    listing began. Keys come in the order first seen, each once; a key deleted while the
    listing runs is left out.
    """

    keys = f"{prefix}>" if _TOKEN_PREFIX.fullmatch(prefix) else ">"
    found: dict[str, None] = {}
    async with aclosing(watch_keys(js, bucket, keys, values=False)) as changes:
        async for change in changes:
            if change is None:
                return list(found)
            if change.value is None:
                found.pop(change.key, None)
            elif change.key.startswith(prefix):
                found[change.key] = None
    raise RuntimeError(f"a watch of bucket {bucket} ended before its first values")  # watch_keys never ends by itself


async def _held_keys(js: Any, bucket: str, subject: str) -> set[str]:
    """The keys the bucket's stream holds under ``subject`` now, deletion markers included."""

    info = await js.stream_info(bucket_stream_name(bucket), subjects_filter=subject)
    subjects = info.state.subjects or {}
    if len(subjects) >= MAX_SUBJECTS:
        raise BucketTooLarge(bucket, subject)
    key_start = len(bucket_subject(bucket, ""))
    return {held[key_start:] for held in subjects}


async def _still_held(js: Any, bucket: str, subject: str, awaited: set[str]) -> set[str]:
    """The awaited keys the stream still holds; a key it dropped by age will never be sent."""

    held = awaited & await _held_keys(js, bucket, subject)
    if len(held) < len(awaited):
        _LOGGER.info("Keys a bucket read awaited were dropped by age before they were sent; the read no longer "
                     "waits for them. event=bucket_read_keys_expired bucket=%s subject=%s dropped=%d awaited=%d",
                     bucket, subject, len(awaited) - len(held), len(held))
    return held


def _change(bucket: str, key: str, message: Any) -> Optional[KeyChange]:
    """The change a message carries; ``None`` (logged) for a server marker this module does not know."""

    headers = message.headers or {}
    operation = headers.get(KV_OP)
    if operation is None and KV_MARKER_REASON in headers:
        reason = headers[KV_MARKER_REASON]
        if reason not in _MARKER_DELETIONS:
            _LOGGER.warning("A bucket message carries a marker reason this version does not know; it is skipped. "
                            "event=bucket_read_marker_unknown bucket=%s key=%s reason=%s", bucket, key, reason)
            return None
        return KeyChange(key, None)
    if operation in _DELETIONS:
        return KeyChange(key, None)
    return KeyChange(key, message.data)


async def _unsubscribe(subscription: Any) -> None:
    """Remove the read's subscription; on a closed or draining connection it is already gone."""

    try:
        await subscription.unsubscribe()
    except (ConnectionClosedError, ConnectionDrainingError, BadSubscriptionError):
        pass  # the connection removed it (closed or draining), or it was removed before
