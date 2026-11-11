"""The message contract's size limits, for code that must fit what it sends within them.

The numbers come from the generated contract code, so they change with the contract:
``MAX_FRAME_BYTES`` (the registry's ``max_frame_bytes``) and the longest text a frame
carries inline (the contract's ``long_text``; longer text goes by blob reference, which
phase 3 adds). A sender checks before it sends: a receiver refuses a frame over the
limit, and the generated models refuse longer inline text, so the person would see an
error instead of a plain answer (README "Reply size").
"""

from __future__ import annotations

import json
from typing import Any, get_args

from custom_messages.wire.kinds import MAX_FRAME_BYTES
from custom_messages.wire.models import LongText


def _max_length(annotated: Any) -> int:
    for constraint in get_args(annotated)[1:]:
        limit = getattr(constraint, "max_length", None)
        if isinstance(limit, int):
            return limit
    raise RuntimeError("The contract's long_text names no maximum length.")


# The contract's text_content type for text carried in the frame itself.
INLINE_TEXT_TYPE = "inline"
# The longest text a frame carries inline, in characters (32,768 in contract 1.0).
INLINE_TEXT_MAX_CHARS = _max_length(LongText)
# Room for everything around a reply's body in its signed frame: the frame's own fields,
# names and signature take under 2 KiB with the longest ids (tests/test_team_memory.py
# measures it), so this leaves a wide margin.
REPLY_FRAME_ALLOWANCE = 8 * 1024
# The most a reply body may take, encoded as frames are (``custom_transport.protocol1.live.encode``).
REPLY_BODY_MAX_BYTES = MAX_FRAME_BYTES - REPLY_FRAME_ALLOWANCE


def encoded_size(value: Any) -> int:
    """Bytes ``value`` takes inside a frame: compact JSON in UTF-8, as frames are encoded."""

    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
