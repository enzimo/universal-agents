"""Helpers shared by tests that run against a real NATS server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nats.aio.client import Client

if TYPE_CHECKING:
    # Only for the annotation: the protocol 1 contract test uses this module
    # without loading the transport adapter.
    from custom_transport.nats_adapter import NatsTransportAdapter


async def flush_fully(nc: Client) -> None:
    """Return once the server has processed every command sent on ``nc`` so far.

    Call it after subscribing when another connection sends next. Commands on
    one connection already reach the server in order, so a publish on ``nc``
    itself needs no wait.

    One ``flush()`` does not promise that in nats-py 2.16.0
    (nats-io/nats.py#1085). ``subscribe()`` and ``publish()`` put their commands
    in a buffer that a writer task sends later, but ``flush()`` writes its PING
    straight to the socket. The PONG can then come back before the server has
    read a SUB queued earlier, so a message sent from another connection right
    after can find no subscriber ("no responders", or a lost publish). That
    happens in a small share of attempts, more often under load. While the
    first flush waits for its PONG, the writer task sends the buffer, so the
    second PING follows everything queued before the call.
    """

    await nc.flush()
    await nc.flush()


async def flush_adapter_fully(adapter: NatsTransportAdapter) -> None:
    """``flush_fully`` on a connected transport adapter's own connection."""

    connection = adapter._nc
    assert connection is not None, "connect the adapter first"
    await flush_fully(connection)
