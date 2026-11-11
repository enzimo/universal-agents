"""Helpers shared by tests that run against a real NATS server."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from nats.aio.client import Client
from nats.js.api import StreamConfig, StreamInfo
from nats.js.errors import APIError, NotFoundError

# nats-py's own check of a stream name, which add_stream and stream_info run before sending.
from nats.js.manager import JetStreamManager, _validate_stream_name

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


#: Stream metadata key holding the token of the test that created the stream.
OWNER_METADATA_KEY = "ua.test.owner"
#: JetStream's error code when a stream with the name exists with another configuration
#: (JSStreamNameExistErr). Metadata takes part in that comparison.
STREAM_NAME_IN_USE = 10058
#: The longest a test's teardown spends deleting its streams, connecting included.
CLEANUP_TIMEOUT_SECONDS = 30.0

# Tokens of the OwnedStreams active in this process. A create that already carries one was
# marked by a recorder opened inside another, and keeps that mark; any other token was left
# in a configuration object reused from an earlier test, and is replaced.
_active_tokens: set[str] = set()


def shared_stream_names(config: NatsTransportConfig) -> frozenset[str]:
    """The streams on ``config``'s server that every run shares by design; no test may delete them.

    - The message contract's streams, key-value buckets (``KV_<name>``) and object stores
      (``OBJ_<name>``) whose names hold no ``{team}``: the server keeps one of each for every
      installation on it (``custom_messages.wire.buckets``).
    - The Website's attachment object store under its default name: every gateway on the server
      that keeps the default shares it.
    - With a subject prefix, the prefix's work and dead-letter streams, which every team using
      that prefix shares (``NatsStreamTopology``).

    A contract name without ``{team}`` counts as shared even if only one run used it: the worst a
    wrong entry here does is leave a stream behind, never delete one another run uses.
    """

    from custom_messages.wire.buckets import TEAM_PLACEHOLDER
    from custom_transport.protocol1.contract_files import transport
    from default_values import INTERACTIVE_APP_DEFAULTS

    contract = transport()
    names = {name for name in contract["streams"] if TEAM_PLACEHOLDER not in name}
    names |= {f"KV_{name}" for name in contract["key_value"] if TEAM_PLACEHOLDER not in name}
    names |= {f"OBJ_{name}" for name in contract["object_store"] if TEAM_PLACEHOLDER not in name}
    names.add(f"OBJ_{INTERACTIVE_APP_DEFAULTS.internal_website_attachment_nats_object_bucket.value}")
    if config.subject_prefix:
        from custom_transport.nats_topology import NatsStreamTopology
        from custom_transport.subjects import NatsSubjectMapper

        # The prefix replaces the team in every stream name, so any team id gives the same names.
        mapper = NatsSubjectMapper(team_id="any_team", subject_prefix=config.subject_prefix)
        names |= {str(spec.name) for spec in NatsStreamTopology(config, mapper).specifications()}
    return frozenset(names)


class OwnedStreams:
    """The streams one test created on the shared live server, as that server confirms them.

    Live tests share a long-lived server, so a stream's name does not prove which test
    created it: another run can create the same name between this test's look-up and its
    create, or create it again after this test deleted it, and a create whose reply was
    lost (a timeout, a cancelled task) may still have been carried out. The server decides
    instead:

    - every create this test sends carries the test's own random token in the stream's
      metadata. JetStream compares the whole configuration, metadata included, with a
      stream that already has the name, so the create succeeds only for a new stream or a
      repeat of this same create. Any other stream answers ``STREAM_NAME_IN_USE``, and the
      caller's create is then sent as the caller wrote it, so the caller gets the answer it
      would get without this recorder;
    - a successful create records the stream's generation (``StreamInfo.created``). An
      update drops metadata the caller did not send, but keeps the generation;
    - before an update of a name this test tried to create, a stream that still carries the
      token is recorded as well: a create retried after a lost reply looks the stream up
      and updates it (``InstallationAuthority._ensure_bucket``), which would drop the token;
    - at the end, a stream is deleted only when it carries the token or a recorded
      generation. A stream another client created, before or after, has neither, and stays.

    Streams every run on the server shares by design (``shared_stream_names``) belong to the
    server, not to a test: their creates are neither marked nor recorded.

    Nothing adds the token to a stream that exists already, so no other client's stream is
    ever claimed. One gap can delete another client's stream: between the last look-up and
    the delete, another client could delete this test's stream and create a new one with the
    same name. No participant deletes a stream it does not own, and JetStream has no
    conditional delete.

    Two gaps leave a stream this test created behind, and delete nothing; JetStream has no
    create that says who made the stream, so neither can be closed:

    - after ``STREAM_NAME_IN_USE``, the caller's unchanged create makes the stream when its
      holder deleted it in between; that stream carries no token;
    - a create whose reply was lost is confirmed only by this process's own later create or
      update. When another process updates the stream first, the token is gone and no
      generation was recorded.

    A side effect: while a stream carries the token, another client's create of it with the
    same settings also answers ``STREAM_NAME_IN_USE``. Product code looks a stream up before
    creating it (``NatsStreamTopology.reconcile``, ``_ensure_bucket``), and this process's own
    creates carry the same token, so only a separate process that looked the name up just
    before this test created it would see that.
    """

    def __init__(self, servers: Iterable[str], shared_names: Iterable[str] = ()) -> None:
        self.token = uuid.uuid4().hex
        # Names whose create was sent, in order; noted before sending, so a create whose
        # outcome is unknown is still checked at the end.
        self.tried: dict[str, None] = {}
        self.shared_names = frozenset(shared_names)
        self._generations: dict[str, set[datetime]] = {}
        self._watched = {_server_address(server) for server in servers}

    def owns(self, info: StreamInfo) -> bool:
        """Whether this test created the stream ``info`` describes."""

        metadata = info.config.metadata or {}
        if metadata.get(OWNER_METADATA_KEY) == self.token:
            return True
        return info.created is not None and info.created in self._generations.get(info.config.name or "", ())

    def _records(self, manager: JetStreamManager) -> bool:
        """Whether ``manager``'s requests go to the shared server this recorder watches."""

        url = manager._nc.connected_url
        return url is not None and _server_address(url.geturl()) in self._watched

    def _remember(self, info: StreamInfo) -> None:
        if info.created is not None and info.config.name:
            self._generations.setdefault(info.config.name, set()).add(info.created)

    def _marked(self, config: StreamConfig) -> StreamConfig:
        """``config`` carrying this test's token, on a copy: the caller's object is left as it was.

        ``StreamConfig.evolve`` shares the metadata dict, so the token goes into a new dict.
        A token of another active recorder (one opened inside this one) is kept, and then
        ``config`` itself is returned.
        """

        metadata = dict(config.metadata or {})
        if metadata.get(OWNER_METADATA_KEY) in _active_tokens:
            return config
        metadata[OWNER_METADATA_KEY] = self.token
        return config.evolve(metadata=metadata)

    def install(self, patch: pytest.MonkeyPatch) -> None:
        """Record from now on, until ``patch`` is undone, the creates this process sends.

        ``JetStreamManager.add_stream`` creates every stream, including those behind key-value
        buckets and object stores, and ``JetStreamContext`` inherits it. Requests to another
        server, such as a temporary one a test started itself, pass through unrecorded: their
        streams end with that server.
        """

        ownership = self
        add_stream = JetStreamManager.add_stream
        update_stream = JetStreamManager.update_stream

        async def recording_add_stream(
            self: JetStreamManager, config: StreamConfig | None = None, **params: Any
        ) -> StreamInfo:
            requested = (config or StreamConfig()).evolve(**params)
            name = requested.name
            if not name or name in ownership.shared_names or not ownership._records(self):
                return await add_stream(self, config, **params)
            # A name nats-py refuses never reaches the server, and the look-up at the end would
            # refuse it too: raise before noting it, as add_stream would.
            _validate_stream_name(name)
            ownership.tried[name] = None
            marked = ownership._marked(requested)
            try:
                info = await add_stream(self, marked)
            except APIError as error:
                # When the mark is an inner recorder's, that recorder sends the caller's create.
                if error.err_code != STREAM_NAME_IN_USE or marked is requested:
                    raise
                # Another stream holds the name: another client's, or this test's own after
                # an update dropped its token (its generation is recorded already).
                return await add_stream(self, config, **params)
            ownership._remember(info)
            return info

        async def recording_update_stream(
            self: JetStreamManager, config: StreamConfig | None = None, **params: Any
        ) -> StreamInfo:
            name = (config or StreamConfig()).evolve(**params).name
            if name in ownership.tried and ownership._records(self):
                with suppress(NotFoundError):
                    found = await self.stream_info(name)
                    if ownership.owns(found):
                        ownership._remember(found)
            return await update_stream(self, config, **params)

        patch.setattr(JetStreamManager, "add_stream", recording_add_stream)
        patch.setattr(JetStreamManager, "update_stream", recording_update_stream)

    async def delete_owned(self, jetstream: JetStreamManager) -> list[str]:
        """Delete every stream this test created that still exists; returns their names."""

        deleted: list[str] = []
        for name in self.tried:
            try:
                info = await jetstream.stream_info(name)
            except NotFoundError:
                continue  # deleted already, or never created
            if self.owns(info):
                with suppress(NotFoundError):
                    await jetstream.delete_stream(name)
                deleted.append(name)
        return deleted


def delete_owned_streams(ownership: OwnedStreams, config: NatsTransportConfig) -> list[str]:
    """Delete the streams ``ownership`` owns on the server ``config`` names; returns their names.

    Runs on its own thread and event loop, so it works from a fixture's teardown whether or
    not the test used an event loop. It logs in as the live transport does, and gives up after
    ``CLEANUP_TIMEOUT_SECONDS`` with an error naming the server and the streams: a server that
    is down or does not answer fails the teardown instead of holding it.
    """

    import nats

    from tests.live_nats_client import nats_connect_options

    if not ownership.tried:
        return []
    # One attempt and one retry half a second later. Otherwise nats-py retries a refused first
    # connection about 60 times, 2 s apart, whatever allow_reconnect says; 0 means no limit.
    options = {
        **nats_connect_options(config),
        "allow_reconnect": False,
        "max_reconnect_attempts": 1,
        "reconnect_time_wait": 0.5,
        "connect_timeout": 5,
    }

    async def delete() -> list[str]:
        connection = await nats.connect(**options)
        try:
            return await ownership.delete_owned(connection.jetstream())
        finally:
            await connection.close()

    results: list[list[str]] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(asyncio.run(asyncio.wait_for(delete(), CLEANUP_TIMEOUT_SECONDS)))
        except BaseException as error:  # raised again on the caller's thread
            failures.append(error)

    # A daemon thread, joined with a bound of its own: a connection whose close hangs after the
    # timeout can neither hold the teardown nor keep the test process from ending.
    thread = threading.Thread(target=run, name="delete-test-streams", daemon=True)
    thread.start()
    thread.join(CLEANUP_TIMEOUT_SECONDS + 5)
    if failures or thread.is_alive():
        error = failures[0] if failures else TimeoutError("the deletion did not end")
        raise RuntimeError(
            f"Could not delete the test's NATS streams among {list(ownership.tried)} on "
            f"{list(config.servers)} within {CLEANUP_TIMEOUT_SECONDS:g} s: {type(error).__name__}: {error}"
        ) from error
    return results[0]


@contextmanager
def deleting_created_streams(config: NatsTransportConfig | None = None) -> Iterator[OwnedStreams]:
    """Delete, when the block ends, every stream created inside it on the shared live server.

    Live NATS tests share one long-lived server, where a team's work, presence
    and dead-letter streams, key-value buckets and object stores stay until
    deleted; a shared test server ran out of storage after about 1,500 of
    them. ``tests/conftest.py`` wraps every test marked ``nats`` in this, so a
    test removes what it created whether it passed or failed. ``OwnedStreams``
    says how a stream is known to be this test's own; streams every run shares
    by design stay (``shared_stream_names``).

    ``config`` names the server and its login; by default the live server from the environment,
    read before the test runs, because a test may point NATS_URL at a temporary server.
    """

    if config is None:
        from tests.live_nats_client import nats_transport_config_from_env

        config = nats_transport_config_from_env()
    ownership = OwnedStreams(config.servers, shared_stream_names(config))
    _active_tokens.add(ownership.token)
    try:
        with pytest.MonkeyPatch.context() as patch:
            ownership.install(patch)
            yield ownership
    finally:
        _active_tokens.discard(ownership.token)
        delete_owned_streams(ownership, config)
