"""Every team's work and dead-letter streams, against a stand-in JetStream (contract README, "Stream sizes").

installation_authority.queues.QueueTopology and the authority's start. The same on a real
nats-server 2.15.1, with its storage refusals and the per-subject cap, is in
tests/test_live_queue_streams.py.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import APIError, NotFoundError

from custom_messages.wire.queues import QueueLimits, queue_stream_config, team_consumers, team_queue_streams
from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
from custom_transport.protocol1.contract_files import transport
from installation_authority.ledger import ISSUED_FILENAME
from installation_authority.queues import (
    QueueTopology,
    StreamCapWouldDrop,
    StreamCountMismatch,
    StreamRefused,
    StreamStorageRefused,
)
from installation_authority.service import AUTHORITY_RUNTIME
from tests.queue_limits_support import TEST_QUEUE_LIMITS, queue_limits
from tests.test_installation_authority_rules import authority_at
from tests.test_partition_mapping_check import StandInServer

TEAM, COUNT = "team_a", 2
KINDS = ("partition", "pool", "edge", "team_services", "dead_letter")
STREAMS = [stream.name for stream in team_queue_streams(transport(), TEAM, COUNT)]  # 2 partitions and 4 more


class StandInJetStream:
    """A JetStream that keeps stream and consumer configurations, sets limits aside and may run out of room.

    ``room`` is the server's storage (10047 past it); ``account_limit`` the account's
    ``limits.max_storage`` (-1: none). ``subjects`` gives a stream's stored messages per subject.
    """

    def __init__(self, *, room: Optional[int] = None, account_limit: int = -1) -> None:
        self.room, self.account_limit = room, account_limit
        self.streams: dict[str, StreamConfig] = {}
        self.consumers: dict[tuple[str, str], ConsumerConfig] = {}
        self.subjects: dict[str, dict[str, int]] = {}
        self.calls: list[tuple[str, str]] = []

    @property
    def reserved(self) -> int:
        return sum(max(config.max_bytes or 0, 0) for config in self.streams.values())

    async def stream_info(self, name: str, subjects_filter: Optional[str] = None) -> Any:
        self.calls.append(("info" if subjects_filter is None else "subjects", name))
        if name not in self.streams:
            raise NotFoundError(code=404, err_code=10059, description="stream not found")
        stored = self.subjects.get(name, {})
        return SimpleNamespace(config=self.streams[name],
                               state=SimpleNamespace(subjects=dict(stored) if subjects_filter else None))

    def _fits(self, config: StreamConfig) -> None:
        before = max(self.streams[config.name].max_bytes or 0, 0) if config.name in self.streams else 0
        if self.room is not None and self.reserved - before + (config.max_bytes or 0) > self.room:
            raise APIError(code=500, err_code=10047, description="insufficient storage resources available")

    async def add_stream(self, config: StreamConfig) -> Any:
        self.calls.append(("create", config.name))
        assert config.name not in self.streams
        self._fits(config)
        self.streams[config.name] = replace(config, metadata={**(config.metadata or {}), "_nats.level": "1"})
        return SimpleNamespace(config=config)

    async def update_stream(self, config: StreamConfig) -> Any:
        self.calls.append(("update", config.name))
        assert config.name in self.streams and not any(key.startswith("_nats.") for key in config.metadata or {})
        self._fits(config)
        self.streams[config.name] = replace(config, metadata={**(config.metadata or {}), "_nats.level": "1"})
        return SimpleNamespace(config=config)

    async def consumer_info(self, stream: str, name: str) -> Any:
        if (stream, name) not in self.consumers:
            raise NotFoundError(code=404, err_code=10014, description="consumer not found")
        return SimpleNamespace(config=self.consumers[(stream, name)])

    async def add_consumer(self, stream: str, config: ConsumerConfig) -> Any:
        self.calls.append(("consumer", config.name))
        self.consumers[(stream, config.name)] = config
        return SimpleNamespace(config=config)


class StandInConnection:
    """The connection's account information request, from the stand-in JetStream."""

    def __init__(self, js: StandInJetStream) -> None:
        self.js = js

    async def request(self, subject: str, data: bytes, timeout: float) -> Any:
        assert subject == "$JS.API.INFO"
        info = {"reserved_storage": self.js.reserved, "limits": {"max_storage": self.js.account_limit}}
        return SimpleNamespace(data=json.dumps(info).encode())


def ensured(js: StandInJetStream, limits: QueueLimits = TEST_QUEUE_LIMITS, *, teams: tuple[str, ...] = (TEAM,),
            count: int = COUNT) -> None:
    topology = QueueTopology(StandInConnection(js), js, transport=transport(), limits=limits, partitions=count)
    asyncio.run(topology.ensure(teams))


def test_every_stream_and_team_consumer_is_created_with_the_contracts_settings(caplog) -> None:
    js = StandInJetStream()
    limits = queue_limits(partition=2 << 20, pool=3 << 20, replicas=3)
    with caplog.at_level("INFO"):
        ensured(js, limits)
    for stream in team_queue_streams(transport(), TEAM, COUNT):
        wanted = queue_stream_config(transport(), stream, limits)
        stored = js.streams[stream.name]
        assert {field: getattr(stored, field) for field in wanted if field != "metadata"} == {
            field: value for field, value in wanted.items() if field != "metadata"}
        assert stored.num_replicas == 3 and stored.max_bytes == limits.max_bytes[stream.kind]
    assert js.streams[f"UA1_PART_{TEAM}~1"].metadata["ua.partitions"] == str(COUNT)
    assert set(js.consumers) == {(consumer.stream, consumer.name) for consumer in team_consumers(transport(), TEAM)}
    assert caplog.text.count("event=protocol1_queue_stream_created") == len(STREAMS)
    assert f"event=protocol1_queue_streams_ready teams={TEAM} created={len(STREAMS)} updated=0" in caplog.text


def test_a_restart_with_the_same_settings_changes_nothing() -> None:
    js = StandInJetStream()
    ensured(js)
    js.calls.clear()
    ensured(js)
    assert {call for call, _name in js.calls} == {"info"}


def test_a_changed_limit_updates_the_stream_at_the_next_start_and_keeps_metadata_it_does_not_own(caplog) -> None:
    js = StandInJetStream()
    ensured(js)
    pool = f"UA1_POOL_WORK_{TEAM}"
    js.streams[pool] = replace(js.streams[pool], metadata={"owner": "someone", "_nats.level": "1"})
    js.calls.clear()
    with caplog.at_level("INFO"):
        ensured(js, queue_limits(pool=5 << 20))
    assert [call for call in js.calls if call[0] == "update"] == [("update", pool)]
    assert js.streams[pool].max_bytes == 5 << 20 and js.streams[pool].metadata["owner"] == "someone"
    assert f"event=protocol1_queue_stream_updated stream={pool} changed=max_bytes:{1 << 20}->{5 << 20}" in caplog.text


def test_lowered_streams_are_changed_first_so_their_room_serves_the_others() -> None:
    js = StandInJetStream(room=len(STREAMS) * (1 << 20))
    ensured(js)
    js.calls.clear()
    ensured(js, queue_limits(partition=1 << 19, pool=2 << 20))
    updates = [name for call, name in js.calls if call == "update"]
    assert updates == [f"UA1_PART_{TEAM}~0", f"UA1_PART_{TEAM}~1", f"UA1_POOL_WORK_{TEAM}"]


def test_a_team_made_with_another_partition_count_refuses_the_start_and_changes_nothing() -> None:
    js = StandInJetStream()
    ensured(js, count=4)
    js.calls.clear()
    with pytest.raises(StreamCountMismatch) as refused:
        ensured(js, queue_limits(pool=5 << 20), count=COUNT)
    assert (f"Team {TEAM}'s partition streams were created with partition count 4 (stream UA1_PART_{TEAM}~0), but "
            f"transport.queues.partitions is {COUNT}") in str(refused.value)
    assert "set transport.queues.partitions back to 4. Nothing was changed." in str(refused.value)
    assert {call for call, _name in js.calls} == {"info"}


def test_a_partition_stream_without_a_recorded_count_refuses_the_start() -> None:
    js = StandInJetStream()
    ensured(js)
    name = f"UA1_PART_{TEAM}~1"
    js.streams[name] = replace(js.streams[name], metadata={"_nats.level": "1"})
    with pytest.raises(StreamCountMismatch, match=f"Stream {name} records no partition count"):
        ensured(js)


def test_a_lowered_cap_that_would_drop_queued_messages_refuses_the_start() -> None:
    js = StandInJetStream()
    ensured(js)
    name = f"UA1_PART_{TEAM}~1"
    js.subjects[name] = {f"ua.v1.{TEAM}.gateway-1.work.agent.lead.1": 6, f"ua.v1.{TEAM}.sched.work.agent.lead.1": 3}
    js.calls.clear()
    with pytest.raises(StreamCapWouldDrop) as refused:
        ensured(js, queue_limits(cap=4))
    assert (f"Lowering transport.queues.max_messages_per_subject to 4 would drop queued messages: stream {name} holds 6 "
            f"messages on ua.v1.{TEAM}.gateway-1.work.agent.lead.1, and 1 of its subjects hold more than 4") \
        in str(refused.value)
    assert "Keep transport.queues.max_messages_per_subject at 8 until those queues drain" in str(refused.value)
    assert not [call for call in js.calls if call[0] in {"create", "update"}]
    # Once those queues hold no more than the new cap, it is lowered.
    js.subjects[name] = {f"ua.v1.{TEAM}.gateway-1.work.agent.lead.1": 4}
    ensured(js, queue_limits(cap=4))
    assert js.streams[name].max_msgs_per_subject == 4


def test_limits_that_do_not_fit_the_accounts_storage_limit_refuse_before_any_change() -> None:
    js = StandInJetStream(account_limit=5 << 20)
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js)
    message = str(refused.value)
    # Six 1 MiB streams in the contract's order; the sixth is the first that does not fit.
    assert message.startswith(f"Stream {STREAMS[5]} does not fit the NATS account's storage limit of 5,242,880 bytes")
    assert (f"Team {TEAM}'s streams set aside 6,291,456 bytes at the current settings ({COUNT} partition streams of "
            "1,048,576 bytes, and transport.queues.max_bytes.dead_letter 1,048,576") in message
    assert "the account sets aside 0 bytes now of its limit of 5,242,880." in message
    assert "Nothing was changed." in message and not js.streams


def test_the_servers_storage_refusal_names_the_stream_and_setting_and_the_next_start_finishes_the_team() -> None:
    js = StandInJetStream(room=4 << 20)
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js)
    message = str(refused.value)
    assert message.startswith(f"NATS has no room to set aside 1,048,576 bytes for stream {STREAMS[4]} "
                              f"(transport.queues.max_bytes.{team_queue_streams(transport(), TEAM, COUNT)[4].kind})")
    assert "JetStream error 10047, insufficient storage resources available" in message
    assert "the account sets aside 4,194,304 bytes now (it has no limit of its own; the server's storage applies)" \
        in message
    assert "The streams made so far stay; the next start finishes the team." in message
    assert list(js.streams) == STREAMS[:4]
    # The operator lowers the limits; the next start lowers what exists and creates the rest.
    ensured(js, queue_limits(**{kind: 1 << 19 for kind in KINDS}))
    assert list(js.streams) == STREAMS and js.reserved == len(STREAMS) * (1 << 19)


def test_a_changed_team_consumer_is_updated() -> None:
    js = StandInJetStream()
    ensured(js)
    consumer = team_consumers(transport(), TEAM)[0]
    key = (consumer.stream, consumer.name)
    js.consumers[key] = replace(js.consumers[key], max_deliver=3)
    js.calls.clear()
    ensured(js)
    assert js.calls.count(("consumer", consumer.name)) == 1 and js.consumers[key].max_deliver == 20


def test_every_team_is_set_up() -> None:
    js = StandInJetStream()
    ensured(js, teams=("team_b", TEAM))
    assert len(js.streams) == 2 * len(STREAMS) and len(js.consumers) == 4


class _MappedServerWithTeamAtFour(StandInServer):
    """A server that maps at 96, whose team streams were made at another count."""

    def __init__(self, js: StandInJetStream) -> None:
        super().__init__(96)
        self.js = js

    def jetstream(self) -> StandInJetStream:
        return self.js

    async def request(self, subject: str, data: bytes, timeout: float) -> Any:
        return await StandInConnection(self.js).request(subject, data, timeout)


def test_the_authority_refuses_a_team_made_with_another_count_before_its_own_records_change(tmp_path) -> None:
    js = StandInJetStream()
    ensured(js, count=4)
    authority = authority_at(tmp_path, partitions=96)
    path = authority.layout.trust_bootstrap_path
    write_bundle(path, BootstrapBundle("install_one", ((authority.authority.key_id, authority.authority.public_key_text),),
                                       partitions=96))
    before = path.read_bytes()

    with pytest.raises(StreamCountMismatch, match="created with partition count 4"):
        asyncio.run(authority.start(_MappedServerWithTeamAtFour(js), teams=[TEAM]))

    assert path.read_bytes() == before
    assert not (authority.layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME).exists()
    assert not any(name.startswith("KV_") for name in js.streams)


def test_another_refusal_of_the_server_names_the_stream_and_what_to_check() -> None:
    class RefusingReplicas(StandInJetStream):
        async def add_stream(self, config: StreamConfig) -> Any:
            raise APIError(code=500, err_code=10074, description="replicas > 1 not supported in non-clustered mode")

    with pytest.raises(StreamRefused) as refused:
        ensured(RefusingReplicas(), queue_limits(replicas=3))
    assert str(refused.value).startswith(f"NATS refused to create stream {STREAMS[0]}: JetStream error 10074, replicas "
                                         "> 1 not supported in non-clustered mode. Check transport.replicas")
