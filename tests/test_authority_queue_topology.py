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
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import ConsumerConfig, StreamConfig
from nats.js.errors import APIError, NotFoundError, ServiceUnavailableError

from custom_messages.wire.partitions import agent_partition
from custom_messages.wire.queues import (
    QueueLimits,
    agent_consumers,
    queue_stream_config,
    team_consumers,
    team_queue_streams,
)
from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
from custom_transport.protocol1.contract_files import transport
from installation_authority.ledger import ISSUED_FILENAME
from installation_authority import queues as queues_module
from installation_authority.queues import (
    AgentBacklog,
    QueueTopology,
    StreamAccountRefused,
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
# The buckets' streams: the installation's five and two per team.
SHARED_BUCKETS = [f"KV_{bucket}" for bucket in transport()["key_value"] if "{team}" not in bucket]
TEAM_BUCKETS = [f"KV_{bucket.replace('{team}', TEAM)}" for bucket in transport()["key_value"] if "{team}" in bucket]


class StandInJetStream:
    """A JetStream that keeps stream and consumer configurations, sets limits aside and may run out of room.

    ``room`` is the server's storage: past it, and for any change at all while what is set
    aside already exceeds it, 10047 (as nats-server 2.15.1 refuses after restarting with less
    storage). ``account_limit``, ``account_streams`` and ``account_consumers`` are the account's
    ``max_storage``, ``max_streams`` and ``max_consumers`` (-1: none), ``tiers`` its limits per replica tier instead
    (``{"R1": (max_storage, max_streams)}``). ``subjects`` gives a stream's stored messages per
    subject.
    """

    def __init__(self, *, room: Optional[int] = None, account_limit: int = -1, account_streams: int = -1,
                 account_consumers: int = -1, tiers: Optional[dict[str, tuple[int, int]]] = None) -> None:
        self.room, self.account_limit, self.account_streams, self.tiers = room, account_limit, account_streams, tiers
        self.account_consumers = account_consumers
        self.streams: dict[str, StreamConfig] = {}
        self.consumers: dict[tuple[str, str], ConsumerConfig] = {}
        self.subjects: dict[str, dict[str, int]] = {}
        # A consumer's (num_pending, num_ack_pending), as its information reports them.
        self.backlogs: dict[tuple[str, str], tuple[int, int]] = {}
        self.calls: list[tuple[str, str]] = []

    @property
    def reserved(self) -> int:
        return sum(max(config.max_bytes or 0, 0) for config in self.streams.values())

    async def stream_info(self, name: str, subjects_filter: Optional[str] = None) -> Any:
        self.calls.append(("info" if subjects_filter is None else "subjects", name))
        if name not in self.streams:
            raise NotFoundError(code=404, err_code=10059, description="stream not found")
        stored = self.subjects.get(name, {})
        matching = {subject: count for subject, count in stored.items()
                    if subjects_filter is not None and _matches(subjects_filter, subject)}
        return SimpleNamespace(config=self.streams[name],
                               state=SimpleNamespace(subjects=matching if subjects_filter else None))

    def _fits(self, config: StreamConfig) -> None:
        before = max(self.streams[config.name].max_bytes or 0, 0) if config.name in self.streams else 0
        after = self.reserved - before + max(config.max_bytes or 0, 0)
        if self.room is not None and (self.reserved > self.room or after > self.room):
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
        pending, ack_pending = self.backlogs.get((stream, name), (0, 0))
        return SimpleNamespace(config=self.consumers[(stream, name)], num_pending=pending, num_ack_pending=ack_pending)

    async def add_consumer(self, stream: str, config: ConsumerConfig) -> Any:
        self.calls.append(("consumer", config.name))
        self.consumers[(stream, config.name)] = config
        return SimpleNamespace(config=config)

    async def delete_consumer(self, stream: str, name: str) -> bool:
        self.calls.append(("delete consumer", name))
        if (stream, name) not in self.consumers:
            raise NotFoundError(code=404, err_code=10014, description="consumer not found")
        del self.consumers[(stream, name)]
        return True

    async def consumers_info(self, stream: str, offset: Optional[int] = None) -> list[Any]:
        self.calls.append(("list consumers", stream))
        names = sorted(name for held, name in self.consumers if held == stream)
        page = names[offset or 0:(offset or 0) + queues_module.CONSUMER_LIST_PAGE]
        return [SimpleNamespace(name=name) for name in page]


def _matches(pattern: str, subject: str) -> bool:
    """NATS subject matching: ``*`` takes one token, ``>`` the rest."""

    wanted, tokens = pattern.split("."), subject.split(".")
    for index, token in enumerate(wanted):
        if token == ">":
            return len(tokens) > index
        if index >= len(tokens) or token not in ("*", tokens[index]):
            return False
    return len(tokens) == len(wanted)


class StandInConnection:
    """The connection's account information request, from the stand-in JetStream, shaped as nats-server 2.15.1's.

    What streams set aside is reported once per stream, whatever their copies; an account with
    tiers reports top-level limits of 0 and each tier on its own.
    """

    def __init__(self, js: StandInJetStream) -> None:
        self.js = js
        self.requests = 0

    async def request(self, subject: str, data: bytes, timeout: float) -> Any:
        assert subject == "$JS.API.INFO"
        self.requests += 1
        js = self.js
        info: dict[str, Any] = {"reserved_storage": js.reserved, "streams": len(js.streams),
                                "limits": {"max_storage": js.account_limit, "max_streams": js.account_streams,
                                           "max_consumers": js.account_consumers}}
        if js.tiers is not None:
            info["limits"] = {"max_storage": 0, "max_streams": 0}
            info["tiers"] = {}
            for tier, (storage, streams) in js.tiers.items():
                held = [config for config in js.streams.values() if f"R{config.num_replicas or 1}" == tier]
                info["tiers"][tier] = {"reserved_storage": sum(max(config.max_bytes or 0, 0) for config in held),
                                       "streams": len(held), "limits": {"max_storage": storage, "max_streams": streams}}
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
    assert caplog.text.count("event=protocol1_bucket_created") == len(SHARED_BUCKETS + TEAM_BUCKETS)
    created = len(STREAMS + SHARED_BUCKETS + TEAM_BUCKETS)
    assert f"event=protocol1_queue_streams_ready teams={TEAM} created={created} updated=0" in caplog.text


def test_a_restart_with_the_same_settings_changes_nothing() -> None:
    js = StandInJetStream()
    ensured(js)
    js.calls.clear()
    connection = StandInConnection(js)
    asyncio.run(QueueTopology(connection, js, transport=transport(), limits=TEST_QUEUE_LIMITS,
                              partitions=COUNT).ensure([TEAM]))
    assert {call for call, _name in js.calls} == {"info"}
    assert connection.requests == 0, "no account request either"


def test_the_buckets_streams_are_made_with_the_contracts_restrictions_and_updated_only_when_they_differ(caplog) -> None:
    js = StandInJetStream()
    ensured(js, queue_limits(replicas=3))
    certs = js.streams["KV_UA1_CERTS"]
    assert (certs.allow_rollup_hdrs, certs.deny_delete, certs.deny_purge, certs.num_replicas) == (False, True, True, 3)
    assert set(SHARED_BUCKETS + TEAM_BUCKETS) <= set(js.streams)
    js.streams["KV_UA1_CERTS"] = replace(certs, metadata={"owner": "a test", "_nats.level": "1"})
    js.calls.clear()
    with caplog.at_level("INFO"):
        ensured(js)  # one copy now
    updated = [name for call, name in js.calls if call == "update"]
    assert set(updated) == set(STREAMS + SHARED_BUCKETS + TEAM_BUCKETS)
    assert js.streams["KV_UA1_CERTS"].num_replicas == 1 and js.streams["KV_UA1_CERTS"].metadata["owner"] == "a test"
    assert "event=protocol1_bucket_updated stream=KV_UA1_CERTS changed=num_replicas:3->1" in caplog.text


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
    assert message.startswith(f"Stream {STREAMS[5]} does not fit the storage limit of the NATS account, 5,242,880 "
                              "bytes:")
    assert (f"Team {TEAM}'s streams set aside 6,291,456 bytes at the current settings ({COUNT} partition streams of "
            "1,048,576 bytes, and transport.queues.max_bytes.dead_letter 1,048,576") in message
    assert "the NATS account sets aside 0 bytes now of its limit of 5,242,880." in message
    assert "On the bundled NATS server" not in message
    assert "Nothing was changed." in message and not js.streams


def test_the_servers_storage_refusal_names_the_stream_and_setting_and_the_next_start_finishes_the_team() -> None:
    js = StandInJetStream(room=4 << 20)
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js)
    message = str(refused.value)
    assert message.startswith(f"NATS has no room to set aside 1,048,576 bytes for stream {STREAMS[4]} "
                              f"(transport.queues.max_bytes.{team_queue_streams(transport(), TEAM, COUNT)[4].kind})")
    assert "JetStream error 10047, insufficient storage resources available" in message
    assert "the NATS account sets aside 4,194,304 bytes now (it has no limit of its own; the server's storage " \
           "applies)" in message
    # 4 MiB set aside and 2 MiB more to come: the bundled server needs a third more free disk.
    assert ("On the bundled NATS server JetStream gets three quarters of the disk's free space, measured each time "
            "NATS starts, so for 6,291,456 bytes set aside the disk needs about 8,388,608 bytes free at every start."
            in message)
    assert "If NATS started with less room than its streams had already set aside, it refuses every change" in message
    assert "The streams made so far stay; once the limits fit, the next start finishes the team." in message
    assert list(js.streams) == STREAMS[:4]
    # The operator lowers the limits; the next start lowers what exists and creates the rest.
    ensured(js, queue_limits(**{kind: 1 << 19 for kind in KINDS}))
    assert [name for name in js.streams if not name.startswith("KV_")] == STREAMS
    assert js.reserved == len(STREAMS) * (1 << 19)


def test_after_nats_restarts_with_less_room_than_is_set_aside_the_same_settings_still_serve() -> None:
    """C21: a server with less storage than its streams set aside refuses every change, a lowered limit too."""

    js = StandInJetStream()
    ensured(js)
    js.room = 3 << 20  # NATS restarted on a fuller disk: 6 MiB set aside, 3 MiB of storage
    ensured(js)  # nothing differs, so nothing is asked of NATS

    with pytest.raises(StreamStorageRefused) as lowered:
        ensured(js, queue_limits(partition=1 << 19))
    message = str(lowered.value)
    assert message.startswith(f"NATS refused to lower the limit of stream UA1_PART_{TEAM}~0: JetStream error 10047")
    assert "NATS refuses every new or changed stream while its streams set aside more than the storage it has" \
        in message
    assert "Lowering a limit cannot help then." in message
    assert "for 6,291,456 bytes set aside the disk needs about 8,388,608 bytes free at every start." in message
    assert "Free disk space and restart the instance" in message

    with pytest.raises(StreamStorageRefused) as new_team:
        ensured(js, teams=(TEAM, "team_b"))
    first = team_queue_streams(transport(), "team_b", COUNT)[0].name
    assert str(new_team.value).startswith(f"NATS has no room to set aside 1,048,576 bytes for stream {first}")

    js.room = 64 << 20  # disk space freed and the instance restarted
    ensured(js, queue_limits(partition=1 << 19))
    assert js.streams[f"UA1_PART_{TEAM}~0"].max_bytes == 1 << 19


def test_a_bucket_the_server_refuses_for_storage_gets_the_same_refusal() -> None:
    js = StandInJetStream()
    ensured(js)
    bucket = f"UA1_BLOCK_LIST_{TEAM}"
    del js.streams[f"KV_{bucket}"]
    js.room = 3 << 20  # less storage than the 6 MiB set aside: even a stream without a limit is refused
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js)
    assert str(refused.value).startswith(f"NATS refused to create bucket {bucket}: JetStream error 10047")
    assert "Lowering a limit cannot help then." in str(refused.value)


def test_an_account_with_tiers_is_checked_against_the_tier_of_the_replica_count() -> None:
    """C22 (a): the top-level limits of a tiered account are 0 and say nothing."""

    js = StandInJetStream(tiers={"R1": (64 << 20, -1)})
    ensured(js)
    assert len(js.streams) == len(STREAMS + SHARED_BUCKETS + TEAM_BUCKETS)

    js = StandInJetStream(tiers={"R1": (5 << 20, -1), "R3": (64 << 20, -1)})
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js)
    assert str(refused.value).startswith(f"Stream {STREAMS[5]} does not fit the storage limit of the NATS account's "
                                         "tier R1, 5,242,880 bytes:")
    assert not js.streams


def test_an_account_with_tiers_but_none_for_the_replica_count_refuses_before_any_change() -> None:
    js = StandInJetStream(tiers={"R1": (64 << 20, -1)})
    with pytest.raises(StreamAccountRefused) as refused:
        ensured(js, queue_limits(replicas=3))
    assert str(refused.value) == (
        "The NATS account keeps its JetStream limits per replica tier (R1) and has none for R3, the transport.replicas "
        "setting of 3. Set transport.replicas to a replica count the account has a tier for, or ask the account's "
        "operator for limits in tier R3. Nothing was changed.")
    assert not js.streams


@pytest.mark.parametrize("tiered", [False, True])
def test_more_streams_than_the_account_allows_refuse_before_any_change(tiered: bool) -> None:
    """C22 (b): a team needs 100 streams at the default count, so the stream limit is the one most likely met."""

    wanted = len(STREAMS + SHARED_BUCKETS + TEAM_BUCKETS)
    js = StandInJetStream(tiers={"R1": (-1, wanted - 1)}) if tiered else StandInJetStream(account_streams=wanted - 1)
    with pytest.raises(StreamAccountRefused) as refused:
        ensured(js)
    where = "The NATS account's tier R1" if tiered else "The NATS account"
    assert str(refused.value) == (
        f"{where} allows {wanted - 1} streams and holds 0; this start would add {wanted}. A team needs 8 streams at "
        f"partition count {COUNT} ({COUNT} partition streams, 4 more and 2 buckets), and the installation 5 buckets of "
        f"its own. The partition count is fixed when the installation is created, so raise the account's stream limit "
        f"to at least {wanted}. Nothing was changed.")
    assert not js.streams
    js.account_streams = wanted
    if tiered:
        js.tiers = {"R1": (-1, wanted)}
    ensured(js)


def test_a_flat_account_counts_every_copy_of_a_stream() -> None:
    """Measured on a three-server cluster: a flat account counts each copy, but reports what is set aside once."""

    js = StandInJetStream(account_limit=10 << 20)  # six streams of 1 MiB: they fit once, not three times
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js, queue_limits(replicas=3))
    message = str(refused.value)
    assert message.startswith(f"Stream {STREAMS[3]} does not fit the storage limit of the NATS account, 10,485,760 "
                              "bytes:")  # the fourth: 12 MiB at three copies
    assert "the NATS account sets aside 0 bytes now, which with 3 copies of each stream counts as 0, of its limit" \
        in message
    assert "or the other transport.queues.max_bytes entries, transport.replicas, or raise" in message
    ensured(js, queue_limits(replicas=1))
    js.calls.clear()
    with pytest.raises(StreamStorageRefused) as raised:  # three copies of what exists: 18 MiB
        ensured(js, queue_limits(replicas=3))
    assert "sets aside 6,291,456 bytes now, which with 3 copies of each stream counts as 18,874,368" in str(raised.value)
    assert "more than its storage limit" not in str(raised.value)
    assert not [call for call in js.calls if call[0] in {"create", "update"}]


def test_an_account_already_over_its_limit_refuses_before_any_change_and_says_lowering_cannot_help() -> None:
    js = StandInJetStream()
    ensured(js)
    js.account_limit = 3 << 20  # lowered below the 6 MiB its streams set aside
    js.calls.clear()
    with pytest.raises(StreamStorageRefused) as refused:
        ensured(js, queue_limits(partition=1 << 19))
    assert str(refused.value) == (
        "The NATS account sets aside 6,291,456 bytes, more than its storage limit of 3,145,728: its limit was lowered "
        "below what its streams had set aside. NATS refuses every new or changed stream until it is raised, a lowered "
        "limit too, so lowering transport.queues.max_bytes cannot help. Raise the account's storage limit. Nothing was "
        "changed.")
    assert not [call for call in js.calls if call[0] in {"create", "update"}]


def test_the_servers_stream_limit_refusal_has_its_own_words() -> None:
    class AtStreamLimit(StandInJetStream):
        async def add_stream(self, config: StreamConfig) -> Any:
            if len(self.streams) >= 3:
                raise APIError(code=400, err_code=10027, description="maximum number of streams reached")
            return await super().add_stream(config)

    with pytest.raises(StreamAccountRefused) as refused:
        ensured(AtStreamLimit())
    assert str(refused.value).startswith(
        f"NATS refused to create stream {STREAMS[3]}: the NATS account allows no more streams (JetStream error 10027, "
        "maximum number of streams reached). A team needs 8 streams")
    assert "Raise the account's stream limit." in str(refused.value)
    assert "transport.replicas" not in str(refused.value)


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
    assert len(js.streams) == 2 * len(STREAMS + TEAM_BUCKETS) + len(SHARED_BUCKETS) and len(js.consumers) == 4


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
    js.calls.clear()

    with pytest.raises(StreamCountMismatch, match="created with partition count 4"):
        asyncio.run(authority.start(_MappedServerWithTeamAtFour(js), teams=[TEAM]))

    assert path.read_bytes() == before
    assert not (authority.layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME).exists()
    assert not [call for call in js.calls if call[0] in {"create", "update"}], "no stream or bucket changed"


# The first stream a start creates, in the contract's order: the team's dead-letter stream.
FIRST = queue_stream_config(transport(), team_queue_streams(transport(), TEAM, COUNT)[0], TEST_QUEUE_LIMITS)


def _refusing_streams(error: Exception) -> StandInJetStream:
    class Refusing(StandInJetStream):
        async def add_stream(self, config: StreamConfig) -> Any:
            raise error

    return Refusing()


@pytest.mark.parametrize(("error", "kind", "says"), [
    # More copies than the servers hold: one server (10074), a cluster too small (10005, measured on 2.15.1).
    (APIError(code=500, err_code=10074, description="replicas > 1 not supported in non-clustered mode"), StreamRefused,
     "the servers JetStream runs on cannot hold 3 copies of each stream (JetStream error 10074, replicas > 1 not "
     "supported in non-clustered mode). Set transport.replicas to at most the number of servers"),
    (APIError(code=400, err_code=10005, description="no suitable peers for placement"), StreamRefused,
     "cannot hold 3 copies of each stream (JetStream error 10005, no suitable peers for placement). Set "
     "transport.replicas"),
    (APIError(code=400, err_code=10065, description="subjects overlap with an existing stream"), StreamRefused,
     f"another stream in the NATS account takes some of its subjects ({', '.join(FIRST['subjects'])}) (JetStream "
     "error 10065, subjects overlap with an existing stream). The instance needs these subjects to itself: remove "
     "the other stream or narrow its subjects"),
    (APIError(code=400, err_code=10026, description="maximum consumers limit reached"), StreamAccountRefused,
     "the NATS account allows fewer consumers per stream than the instance's streams take (JetStream error 10026, "
     f"maximum consumers limit reached); stream {STREAMS[0]} takes up to {FIRST['max_consumers']}. Raise the "
     "account's consumer limit to at least 4,096, or remove it."),
])
def test_each_server_refusal_names_the_setting_or_limit_that_fixes_it(error, kind, says) -> None:
    """FA-3 of pull request 152's review: one table; a setting is named only for the errors it fixes."""

    with pytest.raises(kind) as refused:
        ensured(_refusing_streams(error), queue_limits(replicas=3))
    message = str(refused.value)
    assert message.startswith(f"NATS refused to create stream {STREAMS[0]}: ") and says in message
    assert message.endswith("The streams made so far stay; once the cause is fixed, the next start finishes the team.")
    assert ("transport.replicas" in message) == (error.err_code in queues_module.REPLICA_ERRORS)


def test_a_refusal_the_table_does_not_know_is_shown_as_the_server_sent_it_naming_no_setting() -> None:
    error = APIError(code=500, err_code=10052, description="stream configuration update can not change storage type")
    with pytest.raises(StreamRefused) as refused:
        ensured(_refusing_streams(error))
    assert str(refused.value) == (
        f"NATS refused to create stream {STREAMS[0]}: JetStream error 10052, stream configuration update can not "
        "change storage type. The streams made so far stay; once the cause is fixed, the next start finishes the team.")


def test_an_account_allowing_fewer_consumers_per_stream_than_a_stream_takes_refuses_before_any_change() -> None:
    """FA-1: nats-server 2.15.1 refuses (10026) a stream whose consumer limit is above the account's."""

    js = StandInJetStream(account_consumers=2_000)
    with pytest.raises(StreamAccountRefused) as refused:
        ensured(js)
    assert str(refused.value) == (
        f"The NATS account allows 2,000 consumers per stream, and bucket {SHARED_BUCKETS[0][3:]} takes up to 4,096: "
        "NATS refuses a stream whose consumer limit is above the account's. The contract sets how many consumers each "
        "stream takes, so raise the account's consumer limit to at least 4,096, or remove it. Nothing was changed.")
    assert not [call for call in js.calls if call[0] in {"create", "update"}]
    ensured(StandInJetStream(account_consumers=4_096))  # the contract's largest fits


class _Unanswered(StandInJetStream):
    """A JetStream that answers no request of ``kind`` ("info", "subjects", "consumer info" or "consumer")."""

    def __init__(self, kind: str, error: Exception) -> None:
        super().__init__()
        self.kind, self.error = kind, error

    async def stream_info(self, name: str, subjects_filter: Optional[str] = None) -> Any:
        if self.kind == "info":
            raise self.error
        return await super().stream_info(name, subjects_filter)

    async def consumer_info(self, stream: str, name: str) -> Any:
        if self.kind == "consumer info":
            raise self.error
        return await super().consumer_info(stream, name)

    async def add_consumer(self, stream: str, config: ConsumerConfig) -> Any:
        if self.kind == "consumer":
            raise self.error
        return await super().add_consumer(stream, config)


@pytest.mark.parametrize(("kind", "error", "says"), [
    ("info", NatsTimeoutError(), f"NATS did not answer the request to read stream {STREAMS[0]} (TimeoutError): its "
     "JetStream may be starting, or a cluster choosing a leader. Start again once NATS answers. Nothing was changed."),
    ("info", ServiceUnavailableError(code=503, err_code=10008, description="JetStream system temporarily unavailable"),
     f"NATS refused to read stream {STREAMS[0]}: JetStream error 10008, JetStream system temporarily unavailable. "
     "Nothing was changed."),
    ("consumer info", NatsTimeoutError(), "NATS did not answer the request to create or update consumer "),
    ("consumer", APIError(code=400, err_code=10026, description="maximum consumers limit reached"),
     "the NATS account allows fewer consumers per stream than the instance's streams take (JetStream error 10026, "
     "maximum consumers limit reached). Raise the account's consumer limit to at least 4,096, or remove it. The "
     "streams made so far stay"),
])
def test_errors_reading_streams_or_making_team_consumers_refuse_the_start_naming_what_failed(kind, error, says) -> None:
    """FA-4 and CRIT-2: the launcher shows the reason instead of a traceback and "exited with code 1"."""

    with pytest.raises((StreamRefused, StreamAccountRefused)) as refused:
        ensured(_Unanswered(kind, error))
    assert says in str(refused.value)
    assert "transport." not in str(refused.value)


def test_an_unanswered_account_information_request_refuses_the_start_before_any_change() -> None:
    class Silent(StandInConnection):
        async def request(self, subject: str, data: bytes, timeout: float) -> Any:
            raise NatsTimeoutError()

    js = StandInJetStream()
    topology = QueueTopology(Silent(js), js, transport=transport(), limits=TEST_QUEUE_LIMITS, partitions=COUNT)
    with pytest.raises(StreamRefused, match="NATS did not answer the request to read the NATS account's JetStream "
                                            "information \\(TimeoutError\\).* Nothing was changed.$"):
        asyncio.run(topology.ensure([TEAM]))
    assert not js.streams


# -- agents' consumers (installation_authority.placements decides when) -------------------------


def topology_of(js: StandInJetStream, count: int = COUNT) -> QueueTopology:
    return QueueTopology(StandInConnection(js), js, transport=transport(), limits=TEST_QUEUE_LIMITS, partitions=count)


def test_an_agents_two_consumers_are_made_in_its_partitions_stream_with_the_contracts_settings() -> None:
    js = StandInJetStream()
    ensured(js)
    asyncio.run(topology_of(js).ensure_agent_consumers(TEAM, "researcher-1"))
    stream = f"UA1_PART_{TEAM}~{agent_partition(TEAM, 'researcher-1', COUNT)}"
    for consumer in agent_consumers(transport(), TEAM, "researcher-1", COUNT):
        stored = js.consumers[(stream, consumer.name)]
        assert consumer.stream == stream and stored.filter_subject == consumer.config["filter_subject"]
        assert (stored.durable_name, stored.max_deliver, stored.ack_wait) == (
            consumer.name, consumer.config["max_deliver"], consumer.config["ack_wait"])
    assert {name for _stream, name in js.consumers} >= {"agent~researcher-1", "control~researcher-1"}


def test_an_agents_backlog_counts_waiting_unacknowledged_and_stored_messages_on_its_own_subjects() -> None:
    js = StandInJetStream()
    ensured(js)
    topology = topology_of(js)
    asyncio.run(topology.ensure_agent_consumers(TEAM, "researcher-1"))
    assert asyncio.run(topology.agent_backlog(TEAM, "researcher-1")) == AgentBacklog(0, 0, 0)
    partition = agent_partition(TEAM, "researcher-1", COUNT)
    stream = f"UA1_PART_{TEAM}~{partition}"
    js.backlogs[(stream, "agent~researcher-1")] = (2, 1)
    js.subjects[stream] = {f"ua.v1.{TEAM}.gateway-1.work.agent.researcher-1.{partition}": 3,
                           f"ua.v1.{TEAM}.sched.work.control.researcher-1.{partition}": 1,
                           f"ua.v1.{TEAM}.gateway-1.work.agent.researcher-10.{partition}": 5}  # a neighbour's
    assert asyncio.run(topology.agent_backlog(TEAM, "researcher-1")) == AgentBacklog(2, 1, 4)
    # A message past its last delivery is neither waiting nor unacknowledged, but still stored.
    js.backlogs.clear()
    js.subjects[stream] = {f"ua.v1.{TEAM}.gateway-1.work.agent.researcher-1.{partition}": 1}
    backlog = asyncio.run(topology.agent_backlog(TEAM, "researcher-1"))
    assert backlog == AgentBacklog(0, 0, 1) and not backlog.empty


def test_an_agents_consumers_are_deleted_and_listed_through_every_page(monkeypatch) -> None:
    js = StandInJetStream()
    ensured(js, count=1)
    topology = topology_of(js, count=1)
    agents = [f"researcher-{number}" for number in range(5)]
    for agent in agents:
        asyncio.run(topology.ensure_agent_consumers(TEAM, agent))
    monkeypatch.setattr(queues_module, "CONSUMER_LIST_PAGE", 3)  # ten consumers in the one partition stream: four pages
    assert asyncio.run(topology.agents_with_consumers(TEAM)) == set(agents)
    assert asyncio.run(topology.delete_agent_consumers(TEAM, "researcher-0")) == 2
    assert asyncio.run(topology.delete_agent_consumers(TEAM, "researcher-0")) == 0
    assert asyncio.run(topology.agents_with_consumers(TEAM)) == set(agents[1:])
