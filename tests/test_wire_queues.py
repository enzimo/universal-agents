"""A team's protocol 1 streams and consumers filled from the contract (custom_messages.wire.queues).

The live tests (installation authority, checkpoint streams) create these on nats-server; these
check that the contract's templates are filled for one team, with the instance's limits, and
that an agent's consumers sit where the account's partition mapping stores its messages.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from nats.js.api import ConsumerConfig, StreamConfig

from custom_messages.wire.partitions import agent_partition, arrival_subject
from custom_messages.wire.queues import (
    PARTITION_COUNT_METADATA,
    QueueLimits,
    agent_consumers,
    consumer_agent,
    queue_stream_config,
    team_consumers,
    team_queue_streams,
)

TRANSPORT = json.loads((Path(__file__).resolve().parents[1]
                        / "contracts/messages/v1/generated/transport.json").read_text())
MIB = 1024 * 1024
LIMITS = QueueLimits(
    partitions=7,
    max_bytes={"partition": 16 * MIB, "pool": 128 * MIB, "edge": 64 * MIB, "team_services": 256 * MIB,
               "dead_letter": 32 * MIB},
    max_messages_per_subject=8,
    replicas=3,
)


def _streams(team: str, count: int) -> dict:
    return {stream.name: stream for stream in team_queue_streams(TRANSPORT, team, count)}


def _matches(pattern: str, subject: str) -> bool:
    """NATS subject matching: * is one token, > the rest."""

    want, have = pattern.split("."), subject.split(".")
    for index, token in enumerate(want):
        if token == ">":
            return len(have) > index
        if index >= len(have) or token not in ("*", have[index]):
            return False
    return len(want) == len(have)


def test_a_team_has_one_partition_stream_per_partition_and_the_four_others() -> None:
    streams = team_queue_streams(TRANSPORT, "team_a", 7)
    partitions = [stream for stream in streams if stream.partition is not None]

    assert sorted(stream.name for stream in streams) == sorted([
        *(f"UA1_PART_team_a~{partition}" for partition in range(7)),
        "UA1_POOL_WORK_team_a", "UA1_EDGE_WORK_team_a", "UA1_TEAM_WORK_team_a", "UA1_DLQ_team_a"])
    assert [(stream.name, stream.partition) for stream in partitions] == [
        (f"UA1_PART_team_a~{partition}", partition) for partition in range(7)]
    assert {stream.name: (stream.kind, stream.setting) for stream in streams if stream.partition in (None, 6)} == {
        "UA1_PART_team_a~6": ("partition", "transport.queues.max_bytes.partition"),
        "UA1_POOL_WORK_team_a": ("pool", "transport.queues.max_bytes.pool"),
        "UA1_EDGE_WORK_team_a": ("edge", "transport.queues.max_bytes.edge"),
        "UA1_TEAM_WORK_team_a": ("team_services", "transport.queues.max_bytes.team_services"),
        "UA1_DLQ_team_a": ("dead_letter", "transport.queues.max_bytes.dead_letter")}
    assert len(team_queue_streams(TRANSPORT, "team_a", 96)) == 100


def test_a_partition_stream_has_the_contracts_settings_the_limits_and_its_count() -> None:
    stream = _streams("team_a", 7)["UA1_PART_team_a~3"]

    config = queue_stream_config(TRANSPORT, stream, LIMITS)

    assert config == {
        "name": "UA1_PART_team_a~3",
        "subjects": ["ua.v1.team_a.*.work.agent.*.3", "ua.v1.team_a.*.work.control.*.3"],
        "retention": "workqueue", "storage": "file", "max_age": 604860, "duplicate_window": 120,
        "max_consumers": 1024, "max_msg_size": 263168, "discard": "new",
        "max_bytes": 16 * MIB, "max_msgs_per_subject": 8, "discard_new_per_subject": True,
        "num_replicas": 3, "metadata": {PARTITION_COUNT_METADATA: "7"},
    }
    sent = StreamConfig(**config).as_dict()  # nats-py takes every field; times go to the server in nanoseconds
    assert (sent["max_age"], sent["duplicate_window"]) == (604860 * 10**9, 120 * 10**9)


def test_the_dead_letter_stream_keeps_its_fixed_cap_and_replaces_rather_than_refuses_per_subject() -> None:
    dead_letters = _streams("team_a", 7)["UA1_DLQ_team_a"]

    config = queue_stream_config(TRANSPORT, dead_letters, LIMITS)

    assert config == {
        "name": "UA1_DLQ_team_a", "subjects": ["ua.v1.team_a.*.dlq.>"], "retention": "limits", "storage": "file",
        "max_age": 2592060, "max_msg_size": 263168, "max_consumers": 16, "max_msgs_per_subject": 1,
        "discard": "new", "max_bytes": 32 * MIB, "num_replicas": 3,
    }
    StreamConfig(**config).as_dict()


def test_every_stream_takes_its_kinds_limit_and_only_partition_streams_take_the_cap() -> None:
    for stream in team_queue_streams(TRANSPORT, "team_b", 7):
        config = queue_stream_config(TRANSPORT, stream, LIMITS)
        assert config["max_bytes"] == LIMITS.max_bytes[stream.kind]
        assert config["discard"] == "new"
        assert config.get("discard_new_per_subject", False) is (stream.partition is not None)
        assert all(subject.startswith("ua.v1.team_b.") for subject in config["subjects"])


def test_a_contract_field_this_version_cannot_apply_is_refused_rather_than_left_out() -> None:
    transport = copy.deepcopy(TRANSPORT)
    transport["streams"]["UA1_POOL_WORK_{team}"]["max_msgs"] = 1000
    pool = next(stream for stream in team_queue_streams(transport, "team_a", 7) if stream.kind == "pool")

    with pytest.raises(ValueError, match=r"UA1_POOL_WORK_\{team\} has contract fields .*\['max_msgs'\]"):
        queue_stream_config(transport, pool, LIMITS)


def test_a_missing_size_kind_names_its_setting() -> None:
    limits = QueueLimits(partitions=7, max_bytes={"partition": 16 * MIB}, max_messages_per_subject=8)
    edge = next(stream for stream in team_queue_streams(TRANSPORT, "team_a", 7) if stream.kind == "edge")

    with pytest.raises(ValueError, match="UA1_EDGE_WORK_team_a: transport.queues.max_bytes.edge is missing"):
        queue_stream_config(TRANSPORT, edge, limits)


@pytest.mark.parametrize("wrong", [
    {"partitions": 0}, {"partitions": 129}, {"max_messages_per_subject": 0}, {"replicas": True},
    {"max_bytes": {"partition": 0}},
])
def test_limits_nats_would_refuse_are_refused_first(wrong: dict) -> None:
    values = {"partitions": 7, "max_bytes": {"partition": MIB}, "max_messages_per_subject": 1, **wrong}
    with pytest.raises(ValueError):
        QueueLimits(**values)


@pytest.mark.parametrize("team", ["team.a", "team_*", "team>", "_probe", "t" * 25, ""])
def test_a_team_id_that_is_not_one_subject_token_is_refused(team: str) -> None:
    with pytest.raises(ValueError, match="is not a protocol 1 team id"):
        team_queue_streams(TRANSPORT, team, 7)
    with pytest.raises(ValueError, match="is not a protocol 1 team id"):
        team_consumers(TRANSPORT, team)


def test_the_team_services_and_dead_letter_consumers_exist_once_per_team() -> None:
    consumers = team_consumers(TRANSPORT, "team_a")

    assert sorted((consumer.stream, consumer.config) for consumer in consumers) == sorted([
        ("UA1_TEAM_WORK_team_a", {"name": "team~team_a", "durable_name": "team~team_a",
                                  "filter_subject": "ua.v1.team_a.*.work.team", "ack_policy": "explicit",
                                  "ack_wait": 60, "max_deliver": 20}),
        ("UA1_DLQ_team_a", {"name": "dlq~team_a", "durable_name": "dlq~team_a",
                            "filter_subject": "ua.v1.team_a.*.dlq.notice.*", "ack_policy": "explicit",
                            "ack_wait": 60, "max_deliver": 20}),
    ], key=str)
    for consumer in consumers:
        ConsumerConfig(**consumer.config).as_dict()


@pytest.mark.parametrize("count", [1, 7, 96, 128])
def test_an_agents_consumers_read_where_the_account_mapping_stores_its_messages(count: int) -> None:
    for agent in ("researcher-2", "a", "A" * 32, "lead_0"):
        partition = agent_partition("team_a", agent, count)
        consumers = {consumer.name: consumer for consumer in agent_consumers(TRANSPORT, "team_a", agent, count)}
        assert set(consumers) == {f"agent~{agent}", f"control~{agent}"}
        inbox, control = consumers[f"agent~{agent}"], consumers[f"control~{agent}"]
        stream = _streams("team_a", count)[f"UA1_PART_team_a~{partition}"]
        subjects = queue_stream_config(TRANSPORT, stream, QueueLimits(
            partitions=count, max_bytes={"partition": MIB}, max_messages_per_subject=1))["subjects"]

        assert (inbox.stream, control.stream) == (stream.name, stream.name) == (f"UA1_PART_team_a~{partition}",) * 2
        for consumer, address, other in ((inbox, "work.agent", "work.control"), (control, "work.control", "work.agent")):
            stored = arrival_subject(f"ua.v1.team_a.gateway-1.{address}.{agent}", count)
            assert _matches(consumer.config["filter_subject"], stored)
            assert any(_matches(subject, stored) for subject in subjects)
            assert not _matches(consumer.config["filter_subject"], arrival_subject(
                f"ua.v1.team_a.gateway-1.{other}.{agent}", count))
            assert not _matches(consumer.config["filter_subject"], arrival_subject(
                f"ua.v1.team_b.gateway-1.{address}.{agent}", count))


@pytest.mark.parametrize("agent", ["agent.x", "x*", "", "a" * 33])
def test_an_agent_id_that_is_not_one_subject_token_is_refused(agent: str) -> None:
    with pytest.raises(ValueError, match="is not a protocol 1 agent id"):
        agent_consumers(TRANSPORT, "team_a", agent, 7)


@pytest.mark.parametrize(("name", "agent"), [
    ("agent~researcher-2", "researcher-2"), ("control~researcher-2", "researcher-2"), ("control~A", "A"),
    ("agent~", None), ("control~", None), ("team~team_a", None), ("dlq~team_a", None), ("pool~team_a~writers", None),
    ("agent~a~b", None), ("agents~x", None), ("x" + "agent~y", None), ("agent~" + "a" * 33, None),
])
def test_an_agent_consumers_name_gives_its_agent_and_any_other_name_none(name: str, agent: str | None) -> None:
    assert consumer_agent(TRANSPORT, name) == agent


def test_every_agent_consumer_name_reads_back_to_its_agent() -> None:
    for consumer in agent_consumers(TRANSPORT, "team_a", "writer-9", 96):
        assert consumer_agent(TRANSPORT, consumer.name) == "writer-9"
