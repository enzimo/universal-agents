"""Live NATS tests remove the streams they created on the shared server, and only those
(tests.live_nats_support.OwnedStreams).

Each test runs on a nats-server of its own, which stands in for the shared server: a second
connection plays another run, creating and updating streams the way another process would,
past this process's recorders. Requests are intercepted at ``JetStreamManager._api_request``
to place another run's create at an exact moment, or to lose the reply of a create the server
carried out.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import nats
import nats.errors
import pytest
import pytest_asyncio
from nats.aio.client import Client
from nats.js.api import ObjectStoreConfig, StorageType, StreamConfig
from nats.js.errors import NotFoundError
from nats.js.manager import JetStreamManager

from custom_transport.nats_adapter import NatsTransportConfig
from tests.live_nats_support import OWNER_METADATA_KEY, OwnedStreams, deleting_created_streams
from tests.own_nats_server import own_nats_server
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.live, pytest.mark.nats, pytest.mark.asyncio]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    with own_nats_server(nats_server(), tmp_path) as url:
        yield url


@pytest_asyncio.fixture
async def connections(server: str) -> AsyncIterator[tuple[Client, Client]]:
    """This test's connection and another run's."""

    mine = await nats.connect(server, allow_reconnect=False)
    another_run = await nats.connect(server, allow_reconnect=False)
    try:
        yield mine, another_run
    finally:
        await another_run.close()
        await mine.close()


def _recording(server: str) -> AbstractContextManager[OwnedStreams]:
    return deleting_created_streams(NatsTransportConfig(servers=[server]))


def _stream(name: str, **settings: Any) -> StreamConfig:
    return StreamConfig(name=name, subjects=[f"{name.lower()}.>"], storage=StorageType.MEMORY, **settings)


async def _as_another_run(connection: Client, action: str, config: StreamConfig) -> None:
    """Send a stream ``action`` (CREATE, UPDATE) the way another process does, past this process's recorders."""

    reply = await connection.request(
        f"$JS.API.STREAM.{action}.{config.name}", json.dumps(config.as_dict()).encode(), timeout=5
    )
    answer = json.loads(reply.data)
    assert "error" not in answer, answer


async def _stream_names(connection: Client) -> set[str]:
    return {str(info.config.name) for info in await connection.jetstream().streams_info()}


async def _messages(connection: Client, name: str) -> int:
    return (await connection.jetstream().stream_info(name)).state.messages


def _intercept_first_create(
    monkeypatch: pytest.MonkeyPatch,
    connection: Client,
    name: str,
    *,
    before: Callable[[], Awaitable[None]] | None = None,
    lose_reply: bool = False,
) -> None:
    """On ``connection``'s first create of ``name``: run ``before`` first, or lose the server's reply."""

    send = JetStreamManager._api_request
    first = [True]

    async def api_request(self: JetStreamManager, subject: str, req: bytes = b"", timeout: float = 5) -> Any:
        intercepted = self._nc is connection and subject == f"$JS.API.STREAM.CREATE.{name}" and bool(first)
        if intercepted:
            first.clear()
            if before is not None:
                await before()
        response = await send(self, subject, req, timeout=timeout)
        if intercepted and lose_reply:
            raise nats.errors.TimeoutError  # the server created the stream; its answer never arrived
        return response

    monkeypatch.setattr(JetStreamManager, "_api_request", api_request)


async def test_a_failing_test_removes_what_it_created_and_nothing_that_was_there_before(
    server: str, connections: tuple[Client, Client]
) -> None:
    mine, another_run = connections
    jetstream = mine.jetstream()
    another_runs = _stream("OTHER")
    await _as_another_run(another_run, "CREATE", another_runs)

    with pytest.raises(RuntimeError, match="the test failed"):
        with _recording(server):
            await jetstream.add_stream(another_runs)  # creating a stream that exists is not creating it
            await jetstream.add_stream(_stream("OWN"))
            await jetstream.create_key_value(bucket="cleanup", storage=StorageType.MEMORY)
            await jetstream.create_object_store(
                bucket="cleanup", config=ObjectStoreConfig(storage=StorageType.MEMORY)
            )
            raise RuntimeError("the test failed")

    assert await _stream_names(mine) == {"OTHER"}


async def test_a_stream_another_run_creates_between_the_look_up_and_the_create_stays(
    server: str, connections: tuple[Client, Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is free when this test looks it up; another run creates it before this test's create arrives."""

    mine, another_run = connections
    config = _stream("RACED")

    async def another_run_creates_it() -> None:
        await _as_another_run(another_run, "CREATE", config)
        await another_run.jetstream().publish("raced.one", b"another run's message")

    _intercept_first_create(monkeypatch, mine, "RACED", before=another_run_creates_it)
    with _recording(server):
        with pytest.raises(NotFoundError):
            await mine.jetstream().stream_info("RACED")
        answered = await mine.jetstream().add_stream(config)  # the same settings: answered as without the recorder
        assert answered.config.name == "RACED"

    assert await _messages(mine, "RACED") == 1


async def test_a_stream_another_run_creates_again_after_this_test_deleted_it_stays(
    server: str, connections: tuple[Client, Client]
) -> None:
    mine, another_run = connections
    config = _stream("AGAIN")
    with _recording(server):
        await mine.jetstream().add_stream(config)
        await mine.jetstream().delete_stream("AGAIN")
        await _as_another_run(another_run, "CREATE", config)
        await another_run.jetstream().publish("again.one", b"another run's message")

    assert await _messages(mine, "AGAIN") == 1


@pytest.mark.parametrize("afterwards", ["nothing", "the same create again", "a look-up and an update"])
async def test_a_create_whose_reply_was_lost_is_deleted(
    server: str, connections: tuple[Client, Client], monkeypatch: pytest.MonkeyPatch, afterwards: str
) -> None:
    mine, _ = connections
    jetstream = mine.jetstream()
    config = _stream("LOST")
    _intercept_first_create(monkeypatch, mine, "LOST", lose_reply=True)
    with _recording(server):
        with pytest.raises(nats.errors.TimeoutError):
            await jetstream.add_stream(config)
        if afterwards == "the same create again":
            await jetstream.add_stream(config)
        elif afterwards == "a look-up and an update":
            # What InstallationAuthority._ensure_bucket does when it runs again. The update
            # drops the token, so only the generation recorded before it proves ownership.
            await jetstream.stream_info("LOST")
            await jetstream.update_stream(config)
            assert OWNER_METADATA_KEY not in ((await jetstream.stream_info("LOST")).config.metadata or {})
        assert "LOST" in await _stream_names(mine)

    assert "LOST" not in await _stream_names(mine)


async def test_a_stream_whose_token_another_process_dropped_is_still_deleted(
    server: str, connections: tuple[Client, Client]
) -> None:
    """A child process updating the stream drops the token; the generation recorded at the create remains."""

    mine, another_run = connections
    config = _stream("UPDATED")
    with _recording(server):
        await mine.jetstream().add_stream(config)
        await _as_another_run(another_run, "UPDATE", config)
        assert OWNER_METADATA_KEY not in ((await mine.jetstream().stream_info("UPDATED")).config.metadata or {})

    assert "UPDATED" not in await _stream_names(mine)


async def test_the_callers_configuration_is_left_as_it_was(
    server: str, connections: tuple[Client, Client]
) -> None:
    mine, another_run = connections
    jetstream = mine.jetstream()
    labels = {"purpose": "a test's own label"}
    labelled = _stream("LABELLED", metadata=labels)
    taken = _stream("TAKEN")
    reused = _stream("REUSED", metadata={OWNER_METADATA_KEY: "an earlier test's token"})
    await _as_another_run(another_run, "CREATE", taken)

    with _recording(server) as ownership:
        created = await jetstream.add_stream(labelled)
        await jetstream.add_stream(taken)  # held by another run: the caller's create is sent unchanged
        replaced = await jetstream.add_stream(reused)

    assert labelled.metadata is labels and labels == {"purpose": "a test's own label"}
    assert taken.metadata is None
    assert reused.metadata == {OWNER_METADATA_KEY: "an earlier test's token"}
    assert created.config.metadata is not None
    assert created.config.metadata["purpose"] == "a test's own label"
    assert created.config.metadata[OWNER_METADATA_KEY] == ownership.token
    assert replaced.config.metadata is not None
    assert replaced.config.metadata[OWNER_METADATA_KEY] == ownership.token
    assert await _stream_names(mine) == {"TAKEN"}


async def test_a_recorder_opened_inside_another_keeps_its_own_mark(
    server: str, connections: tuple[Client, Client]
) -> None:
    mine, _ = connections
    jetstream = mine.jetstream()
    with _recording(server) as outer:
        with _recording(server) as inner:
            info = await jetstream.add_stream(_stream("INNER"))
            assert info.config.metadata is not None
            assert info.config.metadata[OWNER_METADATA_KEY] == inner.token
        assert "INNER" not in await _stream_names(mine)
        info = await jetstream.add_stream(_stream("OUTER"))
        assert info.config.metadata is not None
        assert info.config.metadata[OWNER_METADATA_KEY] == outer.token

    assert await _stream_names(mine) == set()
