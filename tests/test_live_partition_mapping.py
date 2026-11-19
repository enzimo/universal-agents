"""The partition mapping on real servers (contract README, "Partitions").

- A server started from the bundled server's generated configuration (``docker.nats_service``)
  stores an agent's inbox and control publishes in the partition stream that
  ``custom_messages.wire.partitions`` computes, pending on the consumers the contract names,
  and delivers its live control requests with the partition token.
- The mapping check (``custom_transport.protocol1.mapping_check``) passes on that server,
  names a server without the mappings and one that maps with another count, and judges each
  of an operator's hand-written lines by itself.

Each test starts its own nats-server (``LIVE_TEST_NATS_SERVER``, or nats-server on PATH).
The installation authority's grants for the check, under an operator-mode account, are in
tests/test_live_message_contract.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import nats
import pytest

from custom_messages.wire.kinds import PARTITIONED_ADDRESSES
from custom_messages.wire.partitions import account_mappings, agent_partition, arrival_subject, mapping_line, mapping_source
from custom_messages.wire.queues import agent_consumers, team_queue_streams
from custom_transport.protocol1.contract_files import transport
from custom_transport.protocol1.mapping_check import OTHER_COUNT, UNMAPPED, PartitionMappingError, check_partition_mapping
from docker.nats_service import write_local_nats_server_config
from tests.own_nats_server import own_nats_server
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
TEAM, AGENT, SENDER = "team_map", "researcher-7", "team-services-1"
USER, PASSWORD = "ua-runtime", "mapping-test-password"


def bundled_server_config(folder: Path, partitions: int) -> Path:
    """What the launcher writes for the bundled server, in ``folder``."""

    return write_local_nats_server_config(folder, user=USER, password=PASSWORD, partitions=partitions)


async def connected(url: str):
    return await nats.connect(url, user=USER, password=PASSWORD, allow_reconnect=False)


def test_the_bundled_servers_configuration_puts_an_agents_queues_and_live_control_in_its_partition(tmp_path: Path) -> None:
    count = 7  # not the default: the configuration carries the installation's count
    partition = agent_partition(TEAM, AGENT, count)
    stream = next(item for item in team_queue_streams(transport(), TEAM, count) if item.partition == partition)
    consumers = {consumer.name: consumer for consumer in agent_consumers(transport(), TEAM, AGENT, count)}
    assert stream.name == f"UA1_PART_{TEAM}~{partition}" and set(consumers) == {f"agent~{AGENT}", f"control~{AGENT}"}

    async def scenario(url: str) -> None:
        nc = await connected(url)
        try:
            js = nc.jetstream()
            # What the topology owner creates (checkpoint 5 creates every stream with its settings).
            subjects = [f"ua.v1.{TEAM}.*.work.agent.*.{partition}", f"ua.v1.{TEAM}.*.work.control.*.{partition}"]
            await js.add_stream(name=stream.name, subjects=subjects)
            for consumer in consumers.values():
                assert consumer.stream == stream.name
                await js.add_consumer(stream.name, durable_name=consumer.name,
                                      filter_subject=consumer.config["filter_subject"])
            inbox = f"ua.v1.{TEAM}.{SENDER}.work.agent.{AGENT}"
            control = f"ua.v1.{TEAM}.{SENDER}.work.control.{AGENT}"
            # Senders address the agent by id; the server adds the partition.
            assert (await js.publish(inbox, b"task")).stream == stream.name
            assert (await js.publish(control, b"stop")).stream == stream.name
            for name, subject in ((f"agent~{AGENT}", inbox), (f"control~{AGENT}", control)):
                assert (await js.consumer_info(stream.name, name)).num_pending == 1
                message = await js.get_last_msg(stream.name, arrival_subject(subject, count))
                assert message.subject == f"{subject}.{partition}"
            # Live control arrives with the token the host subscribes with (custom_transport.protocol1.live).
            arrived: asyncio.Queue = asyncio.Queue()

            async def on_message(message) -> None:
                await arrived.put(message.subject)

            await nc.subscribe(f"ua.v1.{TEAM}.*.ctl.agent.{AGENT}.{partition}", cb=on_message)
            await nc.flush()
            await nc.publish(f"ua.v1.{TEAM}.{SENDER}.ctl.agent.{AGENT}", b"status")
            assert await asyncio.wait_for(arrived.get(), 2) == f"ua.v1.{TEAM}.{SENDER}.ctl.agent.{AGENT}.{partition}"
            # And the authority's check passes at the configured count.
            await check_partition_mapping(nc, sender="authority", count=count)
        finally:
            await nc.close()

    config = bundled_server_config(tmp_path, count)
    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        asyncio.run(scenario(url))


@pytest.mark.parametrize(("server_maps_at", "reason"), [(None, UNMAPPED), (64, OTHER_COUNT)])
def test_the_check_names_a_server_without_the_mappings_or_with_another_count(tmp_path: Path, server_maps_at,
                                                                              reason) -> None:
    async def scenario(url: str) -> None:
        nc = await nats.connect(url, allow_reconnect=False)
        try:
            with pytest.raises(PartitionMappingError) as refused:
                await check_partition_mapping(nc, sender="authority", count=96)
            assert refused.value.problems == {mapping_source(address): reason for address in PARTITIONED_ADDRESSES}
        finally:
            await nc.close()

    with own_nats_server(nats_server(), tmp_path, partitions=server_maps_at) as url:
        asyncio.run(scenario(url))


def test_the_check_judges_each_mapping_line_by_itself(tmp_path: Path) -> None:
    """An operator's hand-written lines: live control right, the inbox at another count, the control queue left out.

    Before the check probed every line, this server passed, and an inbox publish was stored in
    another partition's stream with nothing pending on the agent's consumer (C18).
    """

    inbox, control_queue, live_control = (mapping_source(address) for address in
                                          ("work.agent.{agent}", "work.control.{agent}", "ctl.agent.{agent}"))
    lines = {live_control: account_mappings(96)[live_control], inbox: account_mappings(64)[inbox]}
    config = tmp_path / "hand-written.conf"
    config.write_text("mappings {\n" + "".join(f"  {mapping_line(*line)}\n" for line in lines.items()) + "}\n",
                      encoding="utf-8")

    async def scenario(url: str) -> None:
        nc = await nats.connect(url, allow_reconnect=False)
        try:
            with pytest.raises(PartitionMappingError) as refused:
                await check_partition_mapping(nc, sender="authority", count=96, wait_seconds=1.0)
            assert refused.value.problems == {inbox: OTHER_COUNT, control_queue: UNMAPPED}
            assert f"the line for {inbox} maps with partition count 64" in str(refused.value)
        finally:
            await nc.close()

    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        asyncio.run(scenario(url))
