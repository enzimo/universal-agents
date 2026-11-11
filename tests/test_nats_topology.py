import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import APIError, NotFoundError

from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.nats_topology import NatsStreamTopology, NatsTopologyMismatch
from custom_transport.subjects import NatsSubjectMapper


@pytest.mark.asyncio
async def test_consumers_validate_without_mutating_and_owner_surfaces_update_failure():
    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    configurations = {item.name: item for item in topology.specifications()}
    async def info(name):
        if name not in configurations:
            raise NotFoundError()
        return SimpleNamespace(config=configurations[name])
    js = SimpleNamespace(stream_info=info, add_stream=AsyncMock(), update_stream=AsyncMock(),
                         delete_stream=AsyncMock())
    await topology.validate(js)
    configurations[topology.mapper.work_stream_name()].num_replicas = 3
    with pytest.raises(NatsTopologyMismatch, match="num_replicas"):
        await topology.validate(js)
    js.add_stream.assert_not_called()
    js.update_stream.assert_not_called()
    js.update_stream.side_effect = RuntimeError("operator lacks update permission")
    with pytest.raises(RuntimeError, match="operator lacks"):
        await topology.reconcile(js, __import__("logging").getLogger(__name__))


class _Broker:
    """Streams as a JetStream API stand-in sees them; ``failure`` is what the server answers a request with."""

    def __init__(self, topology, *, old_stream: bool, info_failure=None, delete_failure=None):
        self.streams = {item.name: item for item in topology.specifications()}
        self.retired = list(topology.retired_stream_names()) if old_stream else []
        self.info_failure, self.delete_failure = info_failure, delete_failure
        self.deleted: list[str] = []
        self.looked_up: list[str] = []

    async def stream_info(self, name):
        self.looked_up.append(name)
        if name in self.retired and self.info_failure is not None:
            raise self.info_failure
        if name in self.retired:
            return SimpleNamespace(config=SimpleNamespace(name=name))
        if name not in self.streams:
            raise NotFoundError()
        return SimpleNamespace(config=self.streams[name])

    async def delete_stream(self, name):
        if self.delete_failure is not None:
            raise self.delete_failure
        self.retired.remove(name)
        self.deleted.append(name)

    async def add_stream(self, config):
        self.streams[config.name] = config

    async def update_stream(self, config):
        self.streams[config.name] = config


@pytest.mark.asyncio
async def test_a_fresh_install_looks_the_retired_stream_up_and_sends_no_delete(caplog):
    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    broker = _Broker(topology, old_stream=False)
    broker.streams.clear()

    with caplog.at_level(logging.INFO):
        await topology.reconcile(broker, logging.getLogger(__name__))

    assert broker.deleted == [] and "TEST_PRESENCE" in broker.looked_up
    assert set(broker.streams) == {topology.mapper.work_stream_name(), topology.mapper.dead_letter_stream_name()}
    assert "nats_retired_stream" not in caplog.text


@pytest.mark.asyncio
async def test_the_owner_deletes_the_retired_presence_stream_once(caplog):
    """The presence stream left in message contract step 7d; an upgraded installation still has it."""

    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    broker = _Broker(topology, old_stream=True)

    with caplog.at_level(logging.INFO):
        await topology.reconcile(broker, logging.getLogger(__name__))
        await topology.reconcile(broker, logging.getLogger(__name__))

    assert broker.deleted == ["TEST_PRESENCE"] == list(topology.retired_stream_names())
    assert "PRESENCE" not in " ".join(item.name for item in topology.specifications())
    assert caplog.text.count("event=nats_retired_stream_deleted stream=TEST_PRESENCE") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("info_failure", "delete_failure"),
    [
        (None, APIError(code=403, description="not authorized")),  # the server refuses the delete
        (None, NatsTimeoutError()),  # a login without the right: the request goes unanswered
        (NatsTimeoutError(), None),  # nor may it look the stream up
    ],
    ids=["delete refused", "delete timed out", "lookup timed out"],
)
async def test_a_stream_the_owner_cannot_delete_is_reported_and_the_start_goes_on(
    caplog, info_failure, delete_failure
):
    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    broker = _Broker(topology, old_stream=True, info_failure=info_failure, delete_failure=delete_failure)

    with caplog.at_level(logging.INFO):
        await topology.reconcile(broker, logging.getLogger(__name__))

    assert broker.retired == ["TEST_PRESENCE"]
    [warning] = [record for record in caplog.records if "nats_retired_stream_not_deleted" in record.getMessage()]
    assert warning.levelno == logging.WARNING
    message = warning.getMessage()
    assert "stream=TEST_PRESENCE" in message and "`nats stream rm TEST_PRESENCE`" in message
    assert f"error_type={type(info_failure or delete_failure).__name__}" in message
    # The rest of the start went on: the topology was checked as usual.
    await topology.validate(broker)


@pytest.mark.asyncio
async def test_missing_topology_requires_owner():
    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    js = SimpleNamespace(stream_info=AsyncMock(side_effect=NotFoundError()))
    with pytest.raises(NatsTopologyMismatch, match="start the topology owner"):
        await topology.validate(js)


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_real_broker_rejects_disagreeing_consumer_without_changing_retention():
    from dataclasses import replace
    from id_generation import new_id
    from live_nats_client import nats_transport_config_from_env

    config = nats_transport_config_from_env()
    mapper = NatsSubjectMapper(team_id=new_id("topology"))
    owner = NatsTransportAdapter(config=config, mapper=mapper)
    consumer = NatsTransportAdapter(config=replace(config, work_max_age_seconds=60), mapper=mapper)
    await owner.connect(reconcile_topology=True)
    try:
        with pytest.raises(NatsTopologyMismatch, match="max_age"):
            await consumer.connect()
        assert not consumer.connected
        await owner.validate_streams()
    finally:
        await consumer.close()
        await owner.close()


_OWNER_WITHOUT_DELETE = """
authorization {
  users = [
    {user: "admin", password: "admin-secret"}
    {user: "owner", password: "owner-secret", permissions: {publish: {deny: ["$JS.API.STREAM.DELETE.>"]}}}
  ]
}
"""


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_real_broker_owner_without_the_delete_right_starts_with_and_without_the_old_stream(tmp_path, caplog):
    """An external NATS login that may create and update streams but not delete them (user_docs_source/docker.md).

    The owner starts on its own server: (a) with no old presence stream it sends no delete and
    starts at once; (b) with one it logs what operators see, the client's permissions error
    and the warning naming the stream, and still starts; (c) a login that may delete streams
    removes it.
    """

    import time

    import nats

    from tests.own_nats_server import own_nats_server
    from tests.test_live_message_contract import nats_server

    (tmp_path / "server.conf").write_text(_OWNER_WITHOUT_DELETE)
    mapper = NatsSubjectMapper(team_id="team_upgraded")
    [presence] = NatsStreamTopology(NatsTransportConfig(), mapper).retired_stream_names()

    def owner(url: str, user: str, password: str) -> NatsTransportAdapter:
        config = NatsTransportConfig(servers=[url], user=user, password=password)
        return NatsTransportAdapter(config=config, mapper=mapper, logger=logging.getLogger("test.topology.owner"))

    with own_nats_server(nats_server(), tmp_path, "-c", str(tmp_path / "server.conf")) as url:
        admin = await nats.connect(url, user="admin", password="admin-secret")
        try:
            # (a) A fresh install: no delete is sent, so nothing waits and nothing is refused.
            started = time.monotonic()
            with caplog.at_level(logging.INFO):
                fresh = owner(url, "owner", "owner-secret")
                await fresh.connect(reconcile_topology=True)
                await fresh.close()
            assert time.monotonic() - started < 3
            assert "nats_retired_stream" not in caplog.text and "ermissions violation" not in caplog.text
            assert "STREAM.DELETE" not in (tmp_path / "nats.log").read_text()
            caplog.clear()

            # (b) An instance upgraded from 0.9 still has its presence stream.
            await admin.jetstream().add_stream(name=presence, subjects=[f"{mapper.prefix}.presence.*"])
            with caplog.at_level(logging.INFO):
                upgraded = owner(url, "owner", "owner-secret")
                await upgraded.connect(reconcile_topology=True)
                await upgraded.validate_streams()
                await upgraded.close()
            lines = [record.getMessage() for record in caplog.records]
            assert any(f"event=nats_retired_stream_not_deleted stream={presence}" in line for line in lines), lines
            # nats-py reports the server's refusal through the client's error callback, lowercased.
            refusal = f'NATS client error: nats: permissions violation for publish to "$js.api.stream.delete.{presence.lower()}"'
            assert refusal in lines, lines
            assert (await admin.jetstream().stream_info(presence)).config.name == presence
            caplog.clear()

            # (c) A login that may delete streams removes it, once.
            with caplog.at_level(logging.INFO):
                cleaning = owner(url, "admin", "admin-secret")
                await cleaning.connect(reconcile_topology=True)
                await cleaning.close()
            assert f"event=nats_retired_stream_deleted stream={presence}" in caplog.text
            with pytest.raises(NotFoundError):
                await admin.jetstream().stream_info(presence)
        finally:
            await admin.close()
