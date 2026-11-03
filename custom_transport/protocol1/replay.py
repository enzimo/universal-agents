"""A receiver's replay record for live frames (README "Delivery, expiry and idempotency").

``verify_frame`` refuses a frame whose message id it already admitted with other
content (``replayed``), and lets the same content through again, so a retried request
gets its answer. The record keeps each admitted frame's digest until the frame can no
longer be accepted anyway: its expiry plus the clock tolerance, after which the
receiver refuses it as ``expired``. Live frames last at most 5 minutes (events) and
usually 30 seconds, so the record stays small without a size limit. Queued work keeps
its replay record in the host's store (phase 3), not here.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

from custom_messages.wire.frame import ReplayKey, VerifiedFrame
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.values import parse_timestamp

_TOLERANCE = timedelta(seconds=CLOCK_SKEW_SECONDS)


class LiveReplayRecord:
    """Digests of the live frames this receiver admitted, kept until each can expire no more."""

    def __init__(self) -> None:
        self._held: dict[ReplayKey, tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def lookup(self, key: ReplayKey) -> Optional[str]:
        with self._lock:
            held = self._held.get(key)
        return None if held is None else held[0]

    def admit(self, verified: VerifiedFrame, now: datetime) -> None:
        """Keep the frame's digest until its expiry plus the tolerance; drop entries that passed theirs."""

        keep_until = parse_timestamp(verified.frame.expires_at) + _TOLERANCE
        with self._lock:
            self._held = {key: held for key, held in self._held.items() if held[1] > now}
            self._held[verified.replay_key] = (verified.frame_digest, keep_until)

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)
