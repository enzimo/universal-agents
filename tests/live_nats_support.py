"""Helpers shared by tests that run against a real NATS server."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from nats.aio.client import Client

if TYPE_CHECKING:
    # Only for the annotation: the protocol 1 contract test uses this module
    # without loading the transport adapter.
    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig


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


def _server_address(url: str) -> tuple[str, int]:
    parts = urlsplit(url if "://" in url else f"nats://{url}")
    return (parts.hostname or "").lower(), parts.port or 4222


def record_created_streams(patch: pytest.MonkeyPatch, servers: Iterable[str]) -> list[str]:
    """Record from now on each stream this process creates on ``servers``.

    ``JetStreamManager.add_stream`` creates every stream, including the ones
    behind key-value buckets and object stores. A stream that already existed
    is not recorded, so deleting the list never removes another run's
    streams on a shared server. Streams on another server, such as a
    temporary one a test started itself, end with that server.
    """

    from nats.js.api import StreamConfig
    from nats.js.errors import NotFoundError
    from nats.js.manager import JetStreamManager

    shared = {_server_address(server) for server in servers}
    created: list[str] = []
    add_stream = JetStreamManager.add_stream

    async def recording_add_stream(
        self: JetStreamManager, config: StreamConfig | None = None, **params: Any
    ) -> Any:
        name = (config or StreamConfig()).evolve(**params).name
        url = self._nc.connected_url
        if not name or url is None or _server_address(url.geturl()) not in shared:
            return await add_stream(self, config, **params)
        try:
            await self.stream_info(name)
        except NotFoundError:
            info = await add_stream(self, config, **params)
            created.append(name)
            return info
        return await add_stream(self, config, **params)

    patch.setattr(JetStreamManager, "add_stream", recording_add_stream)
    return created


def delete_streams(names: Iterable[str], config: NatsTransportConfig) -> None:
    """Delete ``names`` from the server ``config`` names; one already deleted is skipped.

    Runs on its own thread and event loop, so it works from a fixture's
    teardown whether or not the test used an event loop.
    """

    import nats
    from nats.js.errors import NotFoundError

    pending = list(dict.fromkeys(names))
    if not pending:
        return
    options: dict[str, Any] = {
        "servers": config.servers,
        "allow_reconnect": False,
        "connect_timeout": 5,
    }
    for option, value in (
        ("user", config.user),
        ("password", config.password),
        ("user_credentials", config.credentials_file),
    ):
        if value:
            options[option] = value

    async def delete() -> None:
        connection = await nats.connect(**options)
        try:
            jetstream = connection.jetstream()
            for name in pending:
                with suppress(NotFoundError):
                    await jetstream.delete_stream(name)
        finally:
            await connection.close()

    failures: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(delete())
        except BaseException as error:  # raised again on the caller's thread
            failures.append(error)

    thread = threading.Thread(target=run, name="delete-test-streams")
    thread.start()
    thread.join()
    if failures:
        raise RuntimeError(
            f"Could not delete the test's NATS streams {pending}: {failures[0]}"
        ) from failures[0]


@contextmanager
def deleting_created_streams() -> Iterator[list[str]]:
    """Delete, when the block ends, every stream created inside it on the shared live server.

    Live NATS tests share one long-lived server, where a team's work, presence
    and dead-letter streams, key-value buckets and object stores stay until
    deleted; a shared test server ran out of storage after about 1,500 of
    them. ``tests/conftest.py`` wraps every test marked ``nats`` in this, so a
    test removes what it created whether it passed or failed.
    """

    from tests.live_nats_client import nats_transport_config_from_env

    # Read before the test runs: a test may point NATS_URL at a temporary server.
    config = nats_transport_config_from_env()
    with pytest.MonkeyPatch.context() as patch:
        created = record_created_streams(patch, config.servers)
        try:
            yield created
        finally:
            delete_streams(created, config)
