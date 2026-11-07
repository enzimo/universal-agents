"""A receiver's replay record for live frames (README "Delivery, expiry and idempotency").

``verify_frame`` refuses a frame whose message id it already admitted with other
content (``replayed``), and lets the same content through again, so a retried request
gets its answer. The record keeps each admitted frame's digest until the frame can no
longer be accepted anyway (``accepted_until``): the earlier of its own expiry and its
kind's lifetime after it was issued, plus the clock tolerance. ``verify_frame`` refuses
a frame past either as ``expired``, so a sender cannot keep an entry longer by naming a
far ``expires_at``, and the record holds at most one lifetime's worth of frames: live
frames last at most 5 minutes (events) and usually 30 seconds. Entries leave in the
order they end (a heap), so admitting a frame costs the logarithm of the record's
size, not a pass over every entry.

The record lives in memory, by design. After a restart, or with a new ``LiveHost``, a
frame admitted before can be admitted again within its lifetime, with other content
under its id. Only the frame's signer can sign that, and it can as well sign the same
content under a new id, so a lost record lets no one do what they could not do
anyway; handlers that must not act twice keep their own idempotency records. Queued
work keeps its replay record in the host's store (phase 3), not here.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from datetime import datetime, timedelta
from typing import Optional

from custom_messages.wire.frame import REPLY_SUFFIX, ReplayKey, VerifiedFrame
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS, KINDS
from custom_messages.wire.models import Frame
from custom_messages.wire.values import parse_timestamp

_TOLERANCE = timedelta(seconds=CLOCK_SKEW_SECONDS)
# A kind this receiver does not know (from a newer minor) is capped at the longest lifetime it knows.
_LONGEST = timedelta(seconds=max(spec.expiry_seconds for spec in KINDS.values()))


def accepted_until(frame: Frame) -> datetime:
    """The moment after which ``verify_frame`` refuses ``frame`` as expired, at the latest.

    ``issued_at`` was checked against the receiver's clock, so adding a lifetime to it
    cannot overflow, whatever ``expires_at`` names.
    """

    spec = KINDS.get(frame.kind.removesuffix(REPLY_SUFFIX))
    lifetime = timedelta(seconds=spec.expiry_seconds) if spec is not None else _LONGEST
    return min(parse_timestamp(frame.expires_at), parse_timestamp(frame.issued_at) + lifetime) + _TOLERANCE


class LiveReplayRecord:
    """Digests of the live frames this receiver admitted, kept until each can be accepted no more."""

    def __init__(self) -> None:
        self._held: dict[ReplayKey, tuple[str, datetime]] = {}
        # (end, tie-breaker, key): replay keys need not be ordered, so equal ends never compare them.
        self._ends: list[tuple[datetime, int, ReplayKey]] = []
        self._order = itertools.count()
        self._lock = threading.Lock()

    def lookup(self, key: ReplayKey) -> Optional[str]:
        with self._lock:
            held = self._held.get(key)
        return None if held is None else held[0]

    def admit(self, verified: VerifiedFrame, now: datetime) -> None:
        """Keep the frame's digest until it can be accepted no more; drop the entries that ended."""

        keep_until = accepted_until(verified.frame)
        with self._lock:
            while self._ends and self._ends[0][0] <= now:
                _, _, key = heapq.heappop(self._ends)
                held = self._held.get(key)
                if held is not None and held[1] <= now:
                    del self._held[key]
            held = self._held.get(verified.replay_key)
            if held is None or held[1] < keep_until:
                self._held[verified.replay_key] = (verified.frame_digest, keep_until)
                heapq.heappush(self._ends, (keep_until, next(self._order), verified.replay_key))

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)
