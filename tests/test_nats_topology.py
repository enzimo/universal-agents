import logging
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


@pytest.mark.asyncio
async def test_startup_never_inspects_or_deletes_retired_streams():
    topology = NatsStreamTopology(NatsTransportConfig(), NatsSubjectMapper(team_id="test"))
    configurations = {item.name: item for item in topology.specifications()}

    async def info(name):
        assert name in configurations, "Runtime must not request access to retired streams"
        return SimpleNamespace(config=configurations[name])

    js = SimpleNamespace(stream_info=info, update_stream=AsyncMock(), delete_stream=AsyncMock())
    await topology.reconcile(js, logging.getLogger(__name__))
    js.delete_stream.assert_not_called()


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


_OWNER_WITHOUT_RETIRED_ACCESS = """
authorization {
  users = [
    {user: "admin", password: "admin-secret"}
    {user: "owner", password: "owner-secret", permissions: {publish: {deny: [
      "$JS.API.STREAM.INFO.TEAM_UPGRADED_PRESENCE", "$JS.API.STREAM.DELETE.>"
    ]}}}
  ]
}
"""


@pytest.mark.asyncio
@pytest.mark.live
@pytest.mark.nats
async def test_runtime_starts_without_access_to_retired_streams(tmp_path, caplog):
    """Startup needs current topology rights only; retired cleanup belongs to the operator."""
    import nats

    from tests.own_nats_server import own_nats_server
    from tests.test_live_message_contract import nats_server

    (tmp_path / "server.conf").write_text(_OWNER_WITHOUT_RETIRED_ACCESS)
    mapper = NatsSubjectMapper(team_id="team_upgraded")
    presence = "TEAM_UPGRADED_PRESENCE"
    with own_nats_server(nats_server(), tmp_path, "-c", str(tmp_path / "server.conf")) as url:
        admin = await nats.connect(url, user="admin", password="admin-secret")
        try:
            for old_stream in (False, True):
                if old_stream:
                    await admin.jetstream().add_stream(name=presence, subjects=[f"{mapper.prefix}.presence.*"])
                config = NatsTransportConfig(servers=[url], user="owner", password="owner-secret")
                owner = NatsTransportAdapter(config=config, mapper=mapper)
                try:
                    with caplog.at_level(logging.INFO):
                        await owner.connect(reconcile_topology=True)
                        await owner.validate_streams()
                    assert "ermissions violation" not in caplog.text
                finally:
                    await owner.close()
            assert (await admin.jetstream().stream_info(presence)).config.name == presence
            await admin.jetstream().delete_stream(presence)
        finally:
            await admin.close()
