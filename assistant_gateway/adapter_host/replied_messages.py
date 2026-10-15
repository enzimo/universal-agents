"""Carry a replied chat message across the adapter-host boundary.

A hosted channel adapter reads the message a person replied to from its
platform (``custom_messages.contracts.replied_message_v1``). The adapter-host
contract keeps its own wire model so it imports no transport types; these two
functions are the only translation between them, one per direction.
"""

from __future__ import annotations

from datetime import datetime, timezone

from assistant_gateway.contracts.adapter_host_v1 import AdapterRepliedMessageV1
from custom_messages.contracts.replied_message_v1 import RepliedMessage


def replied_message_to_wire(
    replied: RepliedMessage | None,
) -> AdapterRepliedMessageV1 | None:
    """The adapter-host form of a replied message, for an inbound request."""

    if replied is None:
        return None
    sent_at = datetime.fromisoformat(replied.sent_at) if replied.sent_at else None
    if sent_at is not None and sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return AdapterRepliedMessageV1(
        author=replied.author,
        author_name=replied.author_name,
        text=replied.text,
        shortened=replied.shortened,
        quoted_text=replied.quoted_text,
        sent_at_utc=sent_at,
    )


def replied_message_from_wire(
    replied: AdapterRepliedMessageV1 | None,
) -> RepliedMessage | None:
    """The transport form of a replied message an adapter host sent."""

    if replied is None:
        return None
    return RepliedMessage(
        author=replied.author,
        author_name=replied.author_name if replied.author == "other" else None,
        text=replied.text,
        shortened=replied.shortened,
        quoted_text=replied.quoted_text,
        sent_at=(
            replied.sent_at_utc.astimezone(timezone.utc).isoformat()
            if replied.sent_at_utc is not None
            else None
        ),
    )


__all__ = ["replied_message_from_wire", "replied_message_to_wire"]
