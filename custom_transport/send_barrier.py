"""What has to happen before this process sends to NATS or settles a message.

An agent process lets its own records' SQLite commits skip the disk flush and
flushes them all at once before anything leaves the process (decision 82,
``storage.sqlite_connection``, "Flushing before sending"). The transport does
not know about SQLite: the agent process installs that flush here
(``custom_agents.commit_flushes``), and the NATS adapter awaits
``before_send()`` before every publish, dead-letter copy, object-store upload,
queued-message withdrawal, and before acknowledging or ending a delivery.
Returning a message for another try (``nak``) needs nothing, since the message
comes back. A process that installs nothing (the gateway, the broker, team
services) sends at once; its commits were flushed when they were made.

Every way an agent process sends to NATS, answers a request over it or settles
a message awaits ``before_send()`` first: a new adapter method, and any other
NATS client code an agent process runs. Code that skips it can let a power
loss undo records that something outside the process already acted on.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

_BEFORE_SEND: Optional[Callable[[], Awaitable[None]]] = None


def install_before_send(barrier: Callable[[], Awaitable[None]]) -> None:
    """Install the process's barrier; installing a different one is refused, not replaced."""

    global _BEFORE_SEND
    if _BEFORE_SEND is not None and _BEFORE_SEND is not barrier:
        raise RuntimeError("A different send barrier is already installed in this process.")
    _BEFORE_SEND = barrier


async def before_send() -> None:
    """Wait until what the process recorded so far is safe to act on outside it."""

    if _BEFORE_SEND is not None:
        await _BEFORE_SEND()
