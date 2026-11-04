from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nats.js.errors import NotFoundError

from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.nats_topology import NatsStreamTopology, NatsTopologyMismatch
from custom_transport.subjects import NatsSubjectMapper


@pytest.mark.asyncio
async def test_consumers_validate_without_mutating_and_owner_surfaces_update_failure():
    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    configurations = {item.name: item for item in topology.specifications()}
    async def info(name):
        return SimpleNamespace(config=configurations[name])
    js = SimpleNamespace(stream_info=info, add_stream=AsyncMock(), update_stream=AsyncMock(),
                         delete_stream=AsyncMock(side_effect=NotFoundError()))
    await topology.validate(js)
    configurations[topology.mapper.work_stream_name()].num_replicas = 3
    with pytest.raises(NatsTopologyMismatch, match="num_replicas"):
        await topology.validate(js)
    js.add_stream.assert_not_called()
    js.update_stream.assert_not_called()
    js.update_stream.side_effect = RuntimeError("operator lacks update permission")
    with pytest.raises(RuntimeError, match="operator lacks"):
        await topology.reconcile(js, __import__("logging").getLogger(__name__))


@pytest.mark.asyncio
async def test_the_owner_deletes_the_retired_presence_stream_once(caplog):
    """The presence stream left in message contract step 7d; an upgraded installation still has it."""

    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    configurations = {item.name: item for item in topology.specifications()}

    async def info(name):
        return SimpleNamespace(config=configurations[name])

    deleted: list[str] = []

    async def delete_stream(name):
        if name in deleted:
            raise NotFoundError()
        deleted.append(name)

    js = SimpleNamespace(stream_info=info, add_stream=AsyncMock(), update_stream=AsyncMock(), delete_stream=delete_stream)
    with caplog.at_level(__import__("logging").INFO):
        await topology.reconcile(js, __import__("logging").getLogger(__name__))
        await topology.reconcile(js, __import__("logging").getLogger(__name__))

    assert deleted == ["TEST_PRESENCE"] == list(topology.retired_stream_names())
    assert "PRESENCE" not in " ".join(configurations)
    assert caplog.text.count("event=nats_retired_stream_deleted") == 1


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
