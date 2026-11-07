"""How many bytes a reply's body may have (message contract README "Reply size").

A reply never exceeds ``max_frame_bytes``, like every frame, and a receiver refuses a
longer one as ``too_large`` before reading it. A sender that answers with records whose
size the settings decide (knowledge pages, notes, lists) measures the body before it
answers: a list returns fewer rows with ``next_offset``, and a single record that does not
fit is refused plainly rather than sent and lost.

The body is measured as the frame carries it (``custom_transport.protocol1.live.encode``:
compact JSON, UTF-8, characters outside ASCII unescaped). The rest of a reply frame (the
envelope: kind, ids, issuer, audience, subject, times, the request it answers, the
signature) is bounded by the contract's field limits, far below
``REPLY_ENVELOPE_BYTES``; ``tests/test_reply_size.py`` checks that against a signed reply
whose every envelope field is as long as the contract allows.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from custom_messages.wire.kinds import MAX_FRAME_BYTES

# Room left for a reply frame's envelope (module docstring).
REPLY_ENVELOPE_BYTES = 8192
REPLY_BODY_MAX_BYTES = MAX_FRAME_BYTES - REPLY_ENVELOPE_BYTES


def body_bytes(body: Mapping[str, Any]) -> int:
    """The size of ``body`` as a frame carries it."""

    return len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def body_fits(body: Mapping[str, Any]) -> bool:
    return body_bytes(body) <= REPLY_BODY_MAX_BYTES


__all__ = ["REPLY_BODY_MAX_BYTES", "REPLY_ENVELOPE_BYTES", "body_bytes", "body_fits"]
