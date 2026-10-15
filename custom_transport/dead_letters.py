"""Set-aside (dead-letter) messages and receivers that stopped.

A receiver gives back a message it cannot read or handle, and ends it on its
last allowed delivery (``custom_transport.nats_pull``). Before ending it, the
adapter stores its original bytes and non-reserved headers in a raw record
and publishes a separate small diagnostic record pointing to it. Both share
the dead-letter stream's three-day, 64 MiB limit (``nats_topology``). Keeping
diagnostics separate lets even a broker-sized original be retained safely.
The team lead reads only diagnostic records and tells administrators
(``custom_agents.dead_letter_watch``). Only identifiers from these headers go
into notices and logs, never the message content.

Header names use a ``UA-`` prefix: ``Nats-`` headers are reserved for the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional

from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity

# Why a message was set aside.
REASON_UNDECODABLE = "undecodable"  # this release could not read it
REASON_HANDLER_FAILED = "handler_failed"  # its handling kept failing

HEADER_REASON = "UA-Dead-Letter-Reason"
HEADER_ERROR = "UA-Dead-Letter-Error"
HEADER_ORIGINAL_SUBJECT = "UA-Original-Subject"
HEADER_ORIGINAL_STREAM = "UA-Original-Stream"
HEADER_ORIGINAL_SEQUENCE = "UA-Original-Stream-Sequence"
HEADER_DELIVERY_COUNT = "UA-Delivery-Count"
HEADER_RECEIVER = "UA-Receiver"
HEADER_RELEASE = "UA-Release"
HEADER_SET_ASIDE_AT = "UA-Dead-Lettered-At"
# Copied from the original message's own headers when present.
HEADER_MESSAGE_ID = "message_id"
# The message type, read from the body's ``msg_content.message_class`` (shortened).
HEADER_MESSAGE_CLASS = "UA-Message-Class"
HEADER_COPY_ID = "UA-Dead-Letter-Copy-Id"
HEADER_RAW_SUBJECT = "UA-Dead-Letter-Raw-Subject"

# The team lead's consumer of the dead-letter stream (a route, so it survives restarts).
WATCH_CONSUMER_ROUTE = "dead_letter_watch"


@dataclass(frozen=True)
class DeadLetterRecord:
    """One set-aside message as the watcher reads it: identifiers only."""

    stream_sequence: int
    copy_id: str
    raw_subject: str
    reason: str
    error: str
    original_subject: str
    original_stream: str
    original_sequence: str
    delivery_count: str
    receiver: str
    release: str
    set_aside_at: str
    message_id: str
    message_class: str
    ack: Callable[[], Awaitable[None]]
    # How many newer copies were still waiting for this reader when it was read.
    waiting_after: int = 0

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, Any],
        *,
        stream_sequence: int,
        ack: Callable[[], Awaitable[None]],
        waiting_after: int = 0,
    ) -> "DeadLetterRecord":
        def value(name: str) -> str:
            return str(headers.get(name) or "")

        copy_id = value(HEADER_COPY_ID)
        if not copy_id:
            raise ValueError("A dead-letter diagnostic record has no copy identity.")
        return cls(
            stream_sequence=stream_sequence,
            copy_id=copy_id,
            raw_subject=value(HEADER_RAW_SUBJECT),
            reason=value(HEADER_REASON),
            error=value(HEADER_ERROR),
            original_subject=value(HEADER_ORIGINAL_SUBJECT),
            original_stream=value(HEADER_ORIGINAL_STREAM),
            original_sequence=value(HEADER_ORIGINAL_SEQUENCE),
            delivery_count=value(HEADER_DELIVERY_COUNT),
            receiver=value(HEADER_RECEIVER),
            release=value(HEADER_RELEASE),
            set_aside_at=value(HEADER_SET_ASIDE_AT),
            message_id=value(HEADER_MESSAGE_ID),
            message_class=value(HEADER_MESSAGE_CLASS),
            ack=ack,
            waiting_after=waiting_after,
        )


@dataclass(frozen=True)
class StoppedReceiver:
    """A receive loop that ended without being asked to (reported, not restarted)."""

    subscription: str
    stopped_at: datetime
    error_type: Optional[str]


def stopped_receiver_finding(
    receiver: StoppedReceiver, *, source: str
) -> StatusFindingV1:
    """The Status page problem for a receive loop that stopped (agents and the gateway)."""

    return StatusFindingV1(
        finding_id=f"receiver_stopped:{source}:{receiver.subscription}"[:200],
        severity=StatusSeverity.PROBLEM,
        title=f"{source} stopped receiving messages"[:200],
        summary=(
            f"Its receiver for {receiver.subscription} stopped asking for messages, so work or replies "
            "sent there wait until it starts again."
        )[:2000],
        action="Restart it (or the team). If this comes back, send the logs with event=nats_pull_loop_stopped.",
        source=source[:120],
        since=receiver.stopped_at,
        details=f"error type: {receiver.error_type or 'cancelled'}",
    )
