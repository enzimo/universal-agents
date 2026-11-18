"""Every team's work and dead-letter streams on nats-server 2.15.1 (contract README, "Stream sizes", "Who sets up the streams").

installation_authority.queues.QueueTopology and the authority's start, each test on a
nats-server of its own, so a test chooses the server's storage and its partition mapping:

- every stream is created with its contract settings and the instance's limits, set aside
  whole when it is made, and a restart changes nothing;
- a changed limit updates the stream at the next start and keeps metadata the authority
  does not own; a team made with another partition count refuses the start;
- a store too small stops the start naming the stream and the setting, and a later start
  with lower limits finishes the team; no host can join meanwhile;
- a full work stream refuses new messages and keeps its oldest; the per-subject cap keeps
  room for control messages (FA-3 of PR #151, with four senders at the cap and full-size
  messages); a lowered cap that would drop queued messages refuses the start.

Each test starts its own nats-server (``LIVE_TEST_NATS_SERVER``, or nats-server on PATH).
The same rules against a stand-in are in tests/test_authority_queue_topology.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import nats
import pytest
from nats.js.errors import APIError

from custom_messages.wire.partitions import agent_partition
from custom_messages.wire.queues import QueueLimits, queue_stream_config, team_consumers, team_queue_streams
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.contract_files import transport
from docker.nats_service import mappings_block
from installation_authority.queues import QueueTopology, StreamCapWouldDrop, StreamCountMismatch, StreamStorageRefused
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.transport import QueueSettings
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import queue_limits
from tests.test_live_installation_authority import prepare
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
TEAM = "team_streams"
MIB = 1 << 20
FULL_MESSAGE = 263_168  # the largest payload a work stream takes (max_message_bytes), measured without headers


def server_config(folder: Path, *, partitions: int = 96, max_file_store: int | None = None) -> Path:
    """The account's mappings at ``partitions``, and a JetStream file store of ``max_file_store`` bytes."""

    text = mappings_block(partitions)
    if max_file_store is not None:
        text += f"jetstream {{ max_file_store: {max_file_store} }}\n"
    path = folder / "server.conf"
    path.write_text(text, encoding="utf-8")
    return path


def topology(nc: Any, limits: QueueLimits, partitions: int = 96) -> QueueTopology:
    return QueueTopology(nc, nc.jetstream(), transport=transport(), limits=limits, partitions=partitions)


async def account(nc: Any) -> dict:
    return json.loads((await nc.request("$JS.API.INFO", b"", timeout=5)).data)


def run(folder: Path, scenario: Any, **config: Any) -> None:
    async def connected(url: str) -> None:
        nc = await nats.connect(url, allow_reconnect=False)
        try:
            await scenario(nc)
        finally:
            await nc.close()

    with own_nats_server(nats_server(), folder, "-c", str(server_config(folder, **config))) as url:
        asyncio.run(connected(url))


def test_every_stream_is_created_with_its_settings_set_aside_whole_and_a_restart_changes_nothing(tmp_path, caplog):
    limits = queue_limits(pool=2 * MIB)

    async def scenario(nc: Any) -> None:
        js = nc.jetstream()
        with caplog.at_level("INFO"):
            await topology(nc, limits).ensure([TEAM])
        streams = team_queue_streams(transport(), TEAM, 96)
        assert len(streams) == 100
        for stream in streams:
            wanted = queue_stream_config(transport(), stream, limits)
            stored = (await js.stream_info(stream.name)).config
            for field, value in wanted.items():
                if field == "metadata":
                    assert stored.metadata["ua.partitions"] == "96"
                else:
                    assert getattr(stored, field) == value, (stream.name, field)
        for consumer in team_consumers(transport(), TEAM):
            stored = (await js.consumer_info(consumer.stream, consumer.name)).config
            assert (stored.filter_subject, stored.max_deliver, stored.ack_wait) == (
                consumer.config["filter_subject"], consumer.config["max_deliver"], consumer.config["ack_wait"])
        # NATS sets every limit aside when it creates the stream, before any message.
        assert (await account(nc))["reserved_storage"] == 99 * MIB + 2 * MIB
        created = {stream.name: (await js.stream_info(stream.name)).created for stream in streams}
        caplog.clear()
        with caplog.at_level("INFO"):
            await topology(nc, limits).ensure([TEAM])
        assert f"event=protocol1_queue_streams_ready teams={TEAM} created=0 updated=0" in caplog.text
        assert {stream.name: (await js.stream_info(stream.name)).created for stream in streams} == created

    run(tmp_path, scenario)


def test_a_changed_limit_updates_the_stream_at_the_next_start_and_keeps_metadata_it_does_not_own(tmp_path):
    pool = f"UA1_POOL_WORK_{TEAM}"

    async def scenario(nc: Any) -> None:
        js = nc.jetstream()
        await topology(nc, queue_limits(), partitions=4).ensure([TEAM])
        config = (await js.stream_info(pool)).config
        config.metadata = {**{key: value for key, value in config.metadata.items() if not key.startswith("_nats.")},
                           "owner": "operator"}
        await js.update_stream(config=config)
        await topology(nc, queue_limits(pool=3 * MIB), partitions=4).ensure([TEAM])
        stored = (await js.stream_info(pool)).config
        assert (stored.max_bytes, stored.metadata["owner"]) == (3 * MIB, "operator")
        assert (await account(nc))["reserved_storage"] == 7 * MIB + 3 * MIB  # four partitions, three more, the pool

    run(tmp_path, scenario, partitions=4)


def test_a_team_made_with_another_partition_count_refuses_the_start_and_changes_nothing(tmp_path):
    async def scenario(nc: Any) -> None:
        await topology(nc, queue_limits(), partitions=8).ensure([TEAM])
        before = await account(nc)
        with pytest.raises(StreamCountMismatch, match="created with partition count 8"):
            await topology(nc, queue_limits(pool=3 * MIB), partitions=4).ensure([TEAM])
        after = await account(nc)
        assert (after["streams"], after["reserved_storage"]) == (before["streams"], before["reserved_storage"])

    run(tmp_path, scenario, partitions=8)


def test_a_store_too_small_stops_the_start_naming_the_stream_and_a_later_start_with_lower_limits_finishes(tmp_path):
    async def scenario(nc: Any) -> None:
        js = nc.jetstream()
        with pytest.raises(StreamStorageRefused) as refused:
            await topology(nc, queue_limits()).ensure([TEAM])
        message = str(refused.value)
        assert "JetStream error 10047" in message and "(transport.queues.max_bytes.partition)" in message
        assert f"Team {TEAM}'s streams set aside 104,857,600 bytes at the current settings" in message
        assert "The streams made so far stay; the next start finishes the team." in message
        made = (await account(nc))["streams"]
        assert 0 < made < 100
        # The operator lowers the partition limit: the next start lowers what exists and makes the rest.
        await topology(nc, queue_limits(partition=MIB // 4)).ensure([TEAM])
        for stream in team_queue_streams(transport(), TEAM, 96):
            await js.stream_info(stream.name)
        assert (await account(nc))["reserved_storage"] == 96 * (MIB // 4) + 4 * MIB

    run(tmp_path, scenario, max_file_store=40 * MIB)


def test_no_host_can_join_while_the_streams_do_not_fit(tmp_path):
    """The authority refuses before it signs anything, so the bundle holds no certificate for the launcher to wait on."""

    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    before = layout.trust_bootstrap_path.read_bytes()

    async def scenario(nc: Any) -> None:
        authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                          host_keys.load_host_keys(layout, AUTHORITY_RUNTIME), partitions=96,
                                          queues=queue_limits())
        with pytest.raises(StreamStorageRefused):
            await authority.start(nc, teams=[TEAM])
        assert authority.live is None

    run(tmp_path, scenario, max_file_store=40 * MIB)
    assert layout.trust_bootstrap_path.read_bytes() == before and read_bundle(layout.trust_bootstrap_path).records == ()


def test_a_full_work_stream_refuses_new_messages_and_keeps_its_oldest(tmp_path):
    pool = f"UA1_POOL_WORK_{TEAM}"

    async def scenario(nc: Any) -> None:
        js = nc.jetstream()
        await topology(nc, queue_limits(), partitions=1).ensure([TEAM])
        subject = f"ua.v1.{TEAM}.lead-host-1.work.pool.researchers"
        stored = 0
        with pytest.raises(APIError) as full:
            for index in range(20):
                await js.publish(subject, bytes([65 + index]) * (100 * 1024))
                stored += 1
        assert full.value.err_code == 10077 and 0 < stored < 20, full.value
        assert (await js.get_msg(pool, 1)).data == b"A" * (100 * 1024)  # discard new: the oldest is kept

    run(tmp_path, scenario, partitions=1)


def test_the_per_subject_cap_keeps_room_for_control_messages_with_four_senders_at_the_cap(tmp_path):
    """FA-3: four senders each at the default cap with full-size messages for one agent leave room in its
    partition stream for its control queue, a neighbour's inbox and control queue, and a fifth sender."""

    count, agent = 4, "researcher-1"
    partition = agent_partition(TEAM, agent, count)
    neighbour = next(f"writer-{index}" for index in range(1000)
                     if agent_partition(TEAM, f"writer-{index}", count) == partition)
    queues = QueueSettings()  # the defaults: 16 MiB partition streams, a cap of 8
    limits = queue_limits(partition=queues.max_bytes.partition, cap=queues.max_messages_per_subject)

    async def scenario(nc: Any) -> None:
        js = nc.jetstream()
        await topology(nc, limits, partitions=count).ensure([TEAM])
        payload = b"x" * FULL_MESSAGE
        for sender in ("gateway-1", "lead-host-1", "team-services-1", "scheduler-1"):
            for _ in range(queues.max_messages_per_subject):
                await js.publish(f"ua.v1.{TEAM}.{sender}.work.agent.{agent}", payload)
        with pytest.raises(APIError) as capped:
            await js.publish(f"ua.v1.{TEAM}.gateway-1.work.agent.{agent}", payload)
        assert (capped.value.err_code, capped.value.description) == (10077, "maximum messages per subject exceeded")
        for subject in (f"work.control.{agent}", f"work.agent.{neighbour}", f"work.control.{neighbour}"):
            stored = await js.publish(f"ua.v1.{TEAM}.gateway-1.{subject}", payload)
            assert stored.stream == f"UA1_PART_{TEAM}~{partition}"
        for _ in range(queues.max_messages_per_subject):
            await js.publish(f"ua.v1.{TEAM}.worker-host-1.work.agent.{agent}", payload)

    run(tmp_path, scenario, partitions=count)


def test_a_lowered_cap_that_would_drop_queued_messages_refuses_the_start_and_drops_nothing(tmp_path):
    count, agent = 2, "researcher-1"
    stream = f"UA1_PART_{TEAM}~{agent_partition(TEAM, agent, count)}"

    async def scenario(nc: Any) -> None:
        js = nc.jetstream()
        await topology(nc, queue_limits(cap=8), partitions=count).ensure([TEAM])
        for _ in range(6):
            await js.publish(f"ua.v1.{TEAM}.gateway-1.work.agent.{agent}", b"task")
        with pytest.raises(StreamCapWouldDrop, match=f"stream {stream} holds 6 messages"):
            await topology(nc, queue_limits(cap=4), partitions=count).ensure([TEAM])
        info = await js.stream_info(stream)
        assert (info.state.messages, info.config.max_msgs_per_subject) == (6, 8)

    run(tmp_path, scenario, partitions=count)
