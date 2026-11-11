"""Live NATS tests remove the streams they created on the shared server, and only those
(tests.live_nats_support.OwnedStreams).

Each test runs on a nats-server of its own, which stands in for the shared server: a second
connection plays another run, creating and updating streams the way another process would,
past this process's recorders. Requests are intercepted at ``JetStreamManager._api_request``
to place another run's create at an exact moment, or to lose the reply of a create the server
carried out.

Streams every run shares by design are created by the real installation authority and
transport adapter. The cleanup's own connection is checked against a server that refuses it,
one that never answers, and one that requires a login.
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, AsyncExitStack
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

from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import team_services_profile
from tests import live_nats_support
from tests.live_nats_client import nats_connect_options, nats_transport_config_from_env
from tests.live_nats_support import (
    OWNER_METADATA_KEY,
    OwnedStreams,
    delete_owned_streams,
    deleting_created_streams,
    shared_stream_names,
)
from tests.own_nats_server import own_nats_server
from tests.test_live_message_contract import nats_server
from tests.test_live_team_services import TEAM, installation

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


@pytest.mark.parametrize("afterwards", ["nothing", "a look-up and an update"])
async def test_a_stream_another_run_creates_between_the_look_up_and_the_create_stays(
    server: str, connections: tuple[Client, Client], monkeypatch: pytest.MonkeyPatch, afterwards: str
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
        if afterwards == "a look-up and an update":
            # What InstallationAuthority._ensure_bucket does when it runs again. The name is
            # one this test tried, but the stream it updates is another run's: not claimed.
            await mine.jetstream().stream_info("RACED")
            await mine.jetstream().update_stream(config)

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


async def test_a_name_nats_py_refuses_is_not_kept_for_the_end(
    server: str, connections: tuple[Client, Client]
) -> None:
    """nats-py refuses the name before sending; the end of the block has nothing to look up."""

    mine, _ = connections
    with _recording(server) as ownership:
        with pytest.raises(ValueError, match="invalid stream name"):
            await mine.jetstream().add_stream(_stream("TWO WORDS"))

    assert ownership.tried == {}


#: The contract's buckets without a team: one per server, which every installation on it uses.
INSTALLATION_WIDE_BUCKETS = {"KV_UA1_AGENTS", "KV_UA1_CERTS", "KV_UA1_HOSTS", "KV_UA1_LOCKS", "KV_UA1_PLACEMENT"}


async def test_the_shared_names_are_the_contracts_names_without_a_team_and_the_websites_default() -> None:
    names = shared_stream_names(NatsTransportConfig(servers=["nats://127.0.0.1:4222"]))

    assert names == INSTALLATION_WIDE_BUCKETS | {"UA1_WORK", "OBJ_UA1_BLOBS", "OBJ_ua_attachments"}


async def test_buckets_every_installation_shares_are_neither_marked_nor_deleted(
    server: str, connections: tuple[Client, Client], tmp_path: Path
) -> None:
    """The installation authority creates the contract's buckets: the team's are this test's, the rest the server's."""

    mine, _ = connections
    with _recording(server) as ownership:
        async with AsyncExitStack() as stack:
            await installation(stack, server, InstanceLayout.at(tmp_path / "instance"), team_services_profile(TEAM))

    assert set(ownership.tried) == {f"KV_UA1_BLOCK_LIST_{TEAM}", f"KV_UA1_TEAM_VIEW_{TEAM}"}
    assert await _stream_names(mine) == INSTALLATION_WIDE_BUCKETS
    for name in INSTALLATION_WIDE_BUCKETS:
        assert OWNER_METADATA_KEY not in ((await mine.jetstream().stream_info(name)).config.metadata or {})


async def test_streams_every_team_shares_under_a_subject_prefix_are_neither_marked_nor_deleted(
    server: str, connections: tuple[Client, Client]
) -> None:
    mine, _ = connections
    config = NatsTransportConfig(servers=[server], subject_prefix="ua.shared", storage="memory")
    adapter = NatsTransportAdapter(config=config, mapper=NatsSubjectMapper(team_id="team_a", subject_prefix="ua.shared"))
    with deleting_created_streams(config) as ownership:
        await adapter.connect(reconcile_topology=True)
        await adapter.close()

    created = {"UA_SHARED_WORK", "UA_SHARED_DEAD_LETTER"}
    assert ownership.tried == {}
    assert created <= ownership.shared_names
    assert await _stream_names(mine) == created


def _left_behind(url: str) -> OwnedStreams:
    ownership = OwnedStreams([url])
    ownership.tried["LEFT_BEHIND"] = None
    return ownership


async def test_the_cleanup_fails_at_once_naming_the_server_the_streams_and_the_refusal_when_the_server_refuses() -> None:
    with socket.socket() as refusing:  # bound, never listening: every connection is refused
        refusing.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{refusing.getsockname()[1]}"
        url = f"nats://alice:example-password@{address}"  # a login in the URL, as NATS_URL may carry
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="LEFT_BEHIND") as raised:
            delete_owned_streams(_left_behind(url), NatsTransportConfig(servers=[url]))

    assert time.monotonic() - started < 5  # not the two minutes of nats-py's default retries
    message = str(raised.value)
    assert f"on {address}: ConnectionRefusedError" in message  # the cause, not nats-py's NoServersError
    assert "example-password" not in message and "alice" not in message
    assert "within" not in message  # the time limit did not stop it


async def test_the_cleanup_gives_up_on_a_server_that_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_nats_support, "CLEANUP_TIMEOUT_SECONDS", 1.0)
    with socket.socket() as silent:  # accepts connections and never says a word
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        url = f"nats://127.0.0.1:{silent.getsockname()[1]}"
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=r"LEFT_BEHIND.* on 127\.0\.0\.1:\d+: not finished within 1 s$"):
            delete_owned_streams(_left_behind(url), NatsTransportConfig(servers=[url]))

    assert time.monotonic() - started < 3


async def test_the_cleanup_logs_in_as_the_live_transport_does_and_names_a_refused_login(tmp_path: Path) -> None:
    with own_nats_server(nats_server(), tmp_path, "--user", "team", "--pass", "team-pass") as url:
        config = NatsTransportConfig(servers=[url], user="team", password="team-pass")
        connection = await nats.connect(**nats_connect_options(config), allow_reconnect=False)
        try:
            with deleting_created_streams(config):
                await connection.jetstream().add_stream(_stream("GUARDED"))
            assert await _stream_names(connection) == set()
        finally:
            await connection.close()

        # A refusal after the TCP connect: nats-py raises it as itself, not as NoServersError.
        refused = NatsTransportConfig(servers=[url], user="team", password="wrong-password")
        with pytest.raises(RuntimeError, match="LEFT_BEHIND") as raised:
            delete_owned_streams(_left_behind(url), refused)

    message = str(raised.value)
    assert "Authorization Violation" in message
    assert "wrong-password" not in message
    cause = raised.value.__cause__
    assert cause is not None and cause.__cause__ is not cause  # not chained to itself


async def test_the_connect_options_carry_the_credentials_file() -> None:
    """A server that accepts only a credentials file refuses a connection without it (not run here: it needs an operator setup)."""

    config = NatsTransportConfig(servers=["nats://a:4222", "nats://b:4222"], credentials_file="/run/secrets/team.creds")

    assert nats_connect_options(config) == {
        "servers": ["nats://a:4222", "nats://b:4222"],
        "user_credentials": "/run/secrets/team.creds",
    }


async def test_protocol1_team_refuses_to_start_in_a_test_not_marked_nats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its streams are deleted only by the marker, so without it they would stay on the shared server."""

    from tests.protocol1_team_support import protocol1_team

    monkeypatch.setattr(live_nats_support, "_active_tokens", set())  # no recorder, as without the marker
    with pytest.raises(RuntimeError, match="mark the test nats"):
        async with protocol1_team(tmp_path, "team_unmarked", ("TeamArchitect",)):
            pass


async def test_every_test_marked_nats_runs_inside_a_recorder_for_the_live_server(
    delete_streams_a_live_nats_test_created: OwnedStreams | None,
) -> None:
    ownership = delete_streams_a_live_nats_test_created

    assert isinstance(ownership, OwnedStreams)
    assert ownership.token in live_nats_support._active_tokens
    assert JetStreamManager.add_stream.__name__ == "recording_add_stream"
    assert ownership.shared_names == shared_stream_names(nats_transport_config_from_env())
