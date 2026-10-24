"""Helpers shared by tests that run against a real NATS server."""

from __future__ import annotations

from nats.aio.client import Client


async def flush_fully(nc: Client) -> None:
    """Return once the server has processed every command sent on ``nc`` so far.

    One ``flush()`` does not promise that in nats-py 2.16.0
    (nats-io/nats.py#1085). ``subscribe()`` and ``publish()`` put their commands
    in a buffer that a writer task sends later, but ``flush()`` writes its PING
    straight to the socket. The PONG can then come back before the server has
    read a SUB queued earlier, so a message sent from another connection right
    after can find no subscriber ("no responders", or a lost publish; about 1-6%
    of attempts under parallel load). While the first flush waits for its PONG,
    the writer task sends the buffer, so the second PING follows everything
    queued before the call.
    """

    await nc.flush()
    await nc.flush()
