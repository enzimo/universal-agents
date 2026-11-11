"""Message ids: UUIDv7 in lower-case text (contract ``message_id``).

Python gained ``uuid.uuid7`` only in 3.14 and the runtime image runs 3.13, so the
id is built here as RFC 9562 describes: 48 bits of Unix time in milliseconds, the
version, 74 random bits and the variant.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Optional


def new_message_id(now_ms: Optional[int] = None) -> str:
    """A fresh UUIDv7: time-ordered to the millisecond, random after that."""

    millis = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not 0 <= millis < 1 << 48:
        raise ValueError("a UUIDv7 holds a 48-bit millisecond time")
    random_bits = secrets.randbits(74)
    value = (millis << 80) | (0x7 << 76) | ((random_bits >> 62) << 64) | (0b10 << 62) | (random_bits & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))
