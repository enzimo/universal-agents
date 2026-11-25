"""Every team's work and dead-letter streams on nats-server 2.15.1 (contract README, "Stream sizes", "Who sets up the streams").

installation_authority.queues.QueueTopology and the authority's start, each test on a
nats-server of its own, so a test chooses the server's storage and its partition mapping:

- every stream is created with its contract settings and the instance's limits, set aside
  whole when it is made, and a restart changes nothing;
- a changed limit updates the stream at the next start and keeps metadata the authority
  does not own; a team made with another partition count refuses the start;
- a store too small stops the start naming the stream and the setting, and a later start
  with lower limits finishes the team; no host can join meanwhile; a start lowers limits
  before it raises others, so room moved between them fits a nearly full store;
- after NATS restarts with less storage than its streams set aside (the bundled server
  measures its storage from the disk's free space at every start), the same settings still
  serve and a changed limit is refused saying that lowering cannot help;
- an account whose limits are kept per replica tier is checked against its tier, and one
  that allows too few streams, or fewer consumers per stream than the contract's streams
  take, refuses the start before any change;
- another stream on the instance's subjects stops the start naming that conflict, not a
  setting, and an account without JetStream stops it saying to enable JetStream there;
- a full work stream refuses new messages and keeps its oldest; the per-subject cap keeps
  room for control messages (FA-3 of PR #151, with four senders at the cap and full-size
  messages); a lowered cap that would drop queued messages refuses the start.

Each test starts its own nats-server (``LIVE_TEST_NATS_SERVER``, or nats-server on PATH).
The same rules against a stand-in are in tests/test_authority_queue_topology.py.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import nats
import pytest
from nats.js.errors import APIError

from custom_messages.wire.partitions import agent_partition
from custom_messages.wire.queues import (
    QueueLimits,
    object_store_stream_config,
    object_store_streams,
    queue_stream_config,
    team_consumers,
    team_queue_streams,
)
from custom_transport.jetstream_account import reported_bytes, request_account_info
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.contract_files import transport
from docker.nats_service import mappings_block
from installation_authority.queues import (
    QueueTopology,
    StreamAccountRefused,
    StreamCapWouldDrop,
    StreamCountMismatch,
    StreamRefused,
    StreamStorageRefused,
)
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.transport import QueueSettings
from tests.nats_operator_support import OperatorAccount
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import queue_limits
from tests.test_live_installation_authority import TEAM as AUTHORITY_TEAM
from tests.test_live_installation_authority import prepare, start_authority, stop_authority
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
    return await request_account_info(nc)


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
        for stream in object_store_streams(transport()):
            wanted = object_store_stream_config(transport(), stream, limits)
            stored = (await js.stream_info(stream.name)).config
            for field, value in wanted.items():
                assert getattr(stored, field) == value, (stream.name, field)
        for consumer in team_consumers(transport(), TEAM):
            stored = (await js.consumer_info(consumer.stream, consumer.name)).config
            assert (stored.filter_subject, stored.max_deliver, stored.ack_wait) == (
                consumer.config["filter_subject"], consumer.config["max_deliver"], consumer.config["ack_wait"])
        # NATS sets every limit aside when it creates the stream, before any message (100 team streams + OBJ_UA1_BLOBS).
        assert (await account(nc))["reserved_storage"] == 99 * MIB + 2 * MIB + MIB
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
        assert (await account(nc))["reserved_storage"] == 7 * MIB + 3 * MIB + MIB  # 4 parts, 3 more, pool, blobs

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
        assert "The streams made so far stay; once the limits fit, the next start finishes the team." in message
        made = (await account(nc))["streams"]
        assert 0 < made < 100
        # The operator lowers the partition limit: the next start lowers what exists and makes the rest.
        await topology(nc, queue_limits(partition=MIB // 4)).ensure([TEAM])
        for stream in team_queue_streams(transport(), TEAM, 96):
            await js.stream_info(stream.name)
        assert (await account(nc))["reserved_storage"] == 96 * (MIB // 4) + 4 * MIB + MIB

    run(tmp_path, scenario, max_file_store=40 * MIB)


def test_room_a_lowered_limit_gives_back_serves_a_raised_one_at_the_same_start(tmp_path):
    """EA-2 of pull request 152's review: a start lowers limits before it creates or enlarges streams.

    A nearly full store of 20 MiB holds four partition streams of 1 MiB, a pool of 8 MiB and three
    more streams of 1 MiB (15 MiB set aside). The next start moves room from the pool to the
    partition streams (3 MiB each, a pool of 1 MiB: 16 MiB). Enlarging the partition streams
    first would set aside 21 MiB by the third of them, and NATS would refuse it (10047).
    """

    async def scenario(nc: Any) -> None:
        await topology(nc, queue_limits(pool=8 * MIB), partitions=4).ensure([TEAM])
        await topology(nc, queue_limits(partition=3 * MIB, pool=MIB), partitions=4).ensure([TEAM])
        assert (await account(nc))["reserved_storage"] == 4 * 3 * MIB + MIB + 3 * MIB + MIB

    run(tmp_path, scenario, partitions=4, max_file_store=20 * MIB)


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


def test_after_nats_restarts_with_less_storage_than_is_set_aside_the_same_settings_still_serve(tmp_path):
    """C21 of the step 2a run: the bundled server's storage is three quarters of the disk's free space at each start.

    A server whose storage is now below what its streams set aside keeps serving them and
    takes new messages, but refuses every new or changed stream, a lowered limit too. So a
    start that asks for no change serves, and a changed limit stops the start saying so.
    ``max_file_store`` stands in for the disk.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    prepare(layout)
    stored = tmp_path / "nats"
    stored.mkdir()

    def authority_starts(room: int, **options: Any) -> None:
        async def started(url: str) -> None:
            async with AsyncExitStack() as stack:
                await stop_authority(await start_authority(stack, url, layout, **options))

        with own_nats_server(nats_server(), stored, "-c", str(server_config(stored, max_file_store=room))) as url:
            asyncio.run(started(url))

    authority_starts(300 * MIB)  # 100 streams of 1 MiB set aside
    authority_starts(60 * MIB)  # the disk filled up: the same settings ask NATS for no change
    with pytest.raises(StreamStorageRefused) as refused:
        authority_starts(60 * MIB, queues=queue_limits(partition=MIB // 4))  # 28 MiB in all would fit 60 MiB
    message = str(refused.value)
    assert message.startswith(f"NATS refused to lower the limit of stream UA1_PART_{AUTHORITY_TEAM}~0: JetStream error "
                              "10047, insufficient storage resources available.")
    assert "Lowering a limit cannot help then." in message
    assert "for 105,906,176 bytes set aside the disk needs about 141,208,235 bytes free at every start." in message
    assert "Free disk space and restart the instance" in message
    authority_starts(300 * MIB, queues=queue_limits(partition=MIB // 4))  # disk space freed, NATS restarted


def _tiered_account(folder: Path, tiers: dict[str, dict[str, int]]) -> tuple[Path, OperatorAccount]:
    """An operator-mode server's configuration whose installation account keeps its limits per replica tier."""

    operator = OperatorAccount(transport()["partitions"]["account_mappings"], 96, jetstream_tiers=tiers)
    config = folder / "nats.json"
    config.write_text(json.dumps(operator.server_settings(folder)))
    config.chmod(0o600)
    return config, operator


def test_an_account_with_limits_per_replica_tier_is_checked_against_its_tier(tmp_path):
    """C22: the reply's top-level limits are 0 for such an account; the tier of the replica count holds them."""

    config, operator = _tiered_account(tmp_path, {"R1": {"disk_storage": 4096 * MIB, "streams": 120}})

    async def scenario(url: str) -> None:
        nc = await nats.connect(url, allow_reconnect=False, **operator.user("authority", None).login())
        try:
            with pytest.raises(StreamAccountRefused, match="per replica tier \\(R1\\) and has none for R3"):
                await topology(nc, queue_limits(replicas=3)).ensure([TEAM])
            assert (await account(nc))["streams"] == 0
            await topology(nc, queue_limits()).ensure([TEAM])  # 100 streams, 7 buckets, 1 object store
            info = await account(nc)
            assert info["limits"]["max_storage"] == 0 and info["tiers"]["R1"]["streams"] == 108
            # The top-level figure is the tiers' total, so the Status page reads it on either kind of account.
            assert reported_bytes(info, "reserved_storage") == info["tiers"]["R1"]["reserved_storage"] == 101 * MIB
            with pytest.raises(StreamAccountRefused) as refused:
                await topology(nc, queue_limits()).ensure([TEAM, "team_more"])
            assert str(refused.value).startswith(
                "The NATS account's tier R1 allows 120 streams and holds 108; this start would add 102. A team needs "
                "102 streams at partition count 96 (96 partition streams, 4 more and 2 buckets)")
            assert (await account(nc))["streams"] == 108, "nothing was made"
        finally:
            await nc.close()

    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        asyncio.run(scenario(url))


def test_an_account_allowing_fewer_consumers_per_stream_than_the_contract_refuses_the_start_before_any_change(tmp_path):
    """FA-1 and CRIT-1 of pull request 152's review: NATS refuses (10026) a stream whose own consumer limit is above
    the account's, and the buckets' streams take 4,096 consumers."""

    config, operator = _tiered_account(tmp_path, {"R1": {"disk_storage": 4096 * MIB, "consumer": 2000}})

    async def scenario(url: str) -> None:
        nc = await nats.connect(url, allow_reconnect=False, **operator.user("authority", None).login())
        try:
            with pytest.raises(StreamAccountRefused) as refused:
                await topology(nc, queue_limits()).ensure([TEAM])
            message = str(refused.value)
            assert message.startswith("The NATS account's tier R1 allows 2,000 consumers per stream, and bucket ")
            assert message.endswith("raise the account's consumer limit to at least 4,096, or remove it. Nothing was "
                                    "changed.")
            assert (await account(nc))["tiers"]["R1"]["streams"] == 0, "nothing was made"
            # What the check stands in front of: the server's own refusal of such a stream.
            with pytest.raises(APIError) as server:
                await nc.jetstream().add_stream(name="WIDE", subjects=["wide.>"], max_consumers=4096)
            assert server.value.err_code == 10026
        finally:
            await nc.close()

    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        asyncio.run(scenario(url))


def test_another_stream_on_the_instances_subjects_stops_the_start_naming_that_conflict_not_a_setting(tmp_path):
    """FA-3 of pull request 152's review: before, every refusal but 10027 and 10047 said to check transport.replicas."""

    async def scenario(nc: Any) -> None:
        await nc.jetstream().add_stream(name="AUDIT_EVERYTHING", subjects=["ua.>"])
        with pytest.raises(StreamRefused) as refused:
            await topology(nc, queue_limits(), partitions=1).ensure([TEAM])
        message = str(refused.value)
        assert message.startswith(f"NATS refused to create stream UA1_DLQ_{TEAM}: another stream in the NATS account "
                                  f"takes some of its subjects (ua.v1.{TEAM}.")
        assert "(JetStream error 10065, subjects overlap with an existing stream)" in message
        assert "transport." not in message

    run(tmp_path, scenario, partitions=1)


def test_an_account_without_jetstream_stops_the_start_saying_to_enable_it_there(tmp_path):
    """C37: on a server with JetStream, an account without it is refused with 10039 (measured on 2.15.1)."""

    config = tmp_path / "accounts.conf"
    config.write_text("accounts {\n"
                      "  STREAMS: { jetstream: enabled, users: [{user: streams, password: streams}] }\n"
                      "  BARE: { users: [{user: bare, password: bare}] }\n"
                      "}\n", encoding="utf-8")

    async def scenario(url: str) -> None:
        nc = await nats.connect(url, allow_reconnect=False, user="bare", password="bare")
        try:
            with pytest.raises(StreamRefused) as refused:
                await topology(nc, queue_limits(), partitions=1).ensure([TEAM])
        finally:
            await nc.close()
        assert str(refused.value) == (
            f"NATS refused to read stream UA1_DLQ_{TEAM}: JetStream is not enabled for the NATS account the instance "
            "logs in to (JetStream error 10039, JetStream not enabled for account). Enable JetStream for that account "
            "(jetstream: enabled in the account's block of the server's configuration file), then start again. "
            "Nothing was changed.")

    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        asyncio.run(scenario(url))


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
