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

One stream information reply lists at most 100,000 subjects (nats-server's
``JSMaxSubjectDetails``), sorted, from the offset the request names, and says how many
match the filter (``total``). Deleted keys count: their delete markers stay in the stream,
so a team's placement and session keys grow with every worker launch. nats-py's
``stream_info`` sends no offset, so the read sends the same request itself through the
JetStream context's request helper and reads the list page by page. Each page after the
first starts at the previous page's last subject, so two pages share one subject. A page
that holds it joins the one before with nothing left out between them; a page that does
not (a key before it was dropped by age between the two requests) starts the list over,
and after ``PAGE_ATTEMPTS`` tries the read raises ``BucketKeyListShifted``.

The read sets no cap on the subject count, but one limit remains. The server sends the
consumer's messages while the read is still asking for the pages, and nats-py holds at
most 524,288 messages per subscription that the reader has not taken (nats-py's default,
not this module's ``QUEUE_SIZE``; the comment on ``QUEUE_SIZE`` says how the two queues
relate). Once one filter holds more than about 524,000 subjects, delete markers included,
the messages past that are dropped, their keys never arrive, and the read raises
``BucketReadTimedOut``, each time, until the markers are removed (a follow-up).

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
import json
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
# It does not hold the server back: for a subscription with a callback, nats-py answers the
# consumer's flow control as soon as it arrives and keeps what this queue cannot take yet in its
# own subscription queue, at most 524,288 messages or 256 MiB of values. Past that it drops
# messages and reports a slow consumer only to the connection's error callback. Before the first
# values end, the read then raises ``BucketReadTimedOut``; after them, a watch misses those
# changes, because nats-py's ordered consumer counts a dropped message as delivered.
QUEUE_SIZE = 256
# How many of the awaited keys a timed-out read names in its error.
NAMED_KEYS = 5
# How long the server keeps a watch's consumer after its client stops answering (nats-py's default).
WATCH_INACTIVE_SECONDS = 300.0
# How many times one read of the stream's key list starts over when two of its pages do not join.
PAGE_ATTEMPTS = 3
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


class BucketKeyListShifted(Exception):
    """The stream's key list lost keys between two of its pages on every try; the bucket's contents are unknown."""

    def __init__(self, bucket: str, subject: str) -> None:
        super().__init__(f"Reading the keys of bucket {bucket} ({subject}) failed: keys were dropped between two pages "
                         f"of the stream's key list in each of {PAGE_ATTEMPTS} tries.")
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
    ``BucketReadTimedOut`` or ``BucketKeyListShifted`` before the first values end, and
    whatever the client raises for a failed request. The subscription is removed however
    the read ends: finished, failed, closed or cancelled.
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
    """The keys the bucket's stream holds under ``subject`` now, deletion markers included.

    Started over when two pages of the list do not join; raises ``BucketKeyListShifted``
    after ``PAGE_ATTEMPTS`` tries.
    """

    key_start = len(bucket_subject(bucket, ""))
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        subjects = await _stream_subjects(js, bucket_stream_name(bucket), subject)
        if subjects is not None:
            return {held[key_start:] for held in subjects}
        _LOGGER.info("Keys were dropped between two pages of a stream's key list; the list is read again. "
                     "event=bucket_read_pages_shifted bucket=%s subject=%s attempt=%d attempts=%d",
                     bucket, subject, attempt, PAGE_ATTEMPTS)
    raise BucketKeyListShifted(bucket, subject)


async def _stream_subjects(js: Any, stream: str, subject: str) -> Optional[set[str]]:
    """Every subject ``stream`` holds under ``subject``, read page by page; ``None`` when two pages did not join.

    Each page is a sorted slice of the list as the stream held it when it answered. The next
    page is asked from one subject before the end of this one, so a joined pair shares
    ``last``. When the next page holds ``last``, it holds every subject the stream then held
    from ``last`` to its own end, so the two pages hold every subject the stream held
    through both requests from the first page's start to the second page's end (a key
    written meanwhile before ``last`` moves it further down the list, which repeats subjects
    but skips none). When it does not, ``last`` or keys before it were dropped, and the page
    may begin past keys it should hold.
    """

    found: set[str] = set()
    offset = 0
    last: Optional[str] = None
    while True:
        page, total = await _subjects_page(js, stream, subject, offset)
        if last is not None and last not in page:
            return None
        found.update(page)
        if offset + len(page) >= total:  # this page reaches the end of the list it was cut from
            return found
        last = max(page)
        offset += max(len(page) - 1, 1)  # one subject shared, and always forward: a one-subject page cannot join


async def _subjects_page(js: Any, stream: str, subject: str, offset: int) -> tuple[dict[str, int], int]:
    """The subjects ``stream`` holds under ``subject`` from ``offset`` on (one reply's worth), and how many match in all.

    nats-py's ``stream_info`` sends no offset, so this sends the request it sends, with the
    offset, through the context's own request helper: the same subject under the context's
    API prefix (``$JS.API.STREAM.INFO.<stream>``, granted to every bucket reader), the same
    time limit, and the same errors (``NotFoundError`` for a missing stream).
    """

    request = json.dumps({"subjects_filter": subject, "offset": offset}).encode()
    reply = await js._api_request(f"{js._prefix}.STREAM.INFO.{stream}", request, timeout=js._timeout)
    return (reply.get("state") or {}).get("subjects") or {}, int(reply.get("total") or 0)


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
