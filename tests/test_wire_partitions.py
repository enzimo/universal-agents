"""An agent's partition and the keys that name partitions (contracts/messages/v1/README.md, "Partitions").

The live contract test checks that nats-server's partition(COUNT,1,3) stores each agent's
messages in the partition computed here; these tests pin the hash to published FNV-1a vectors.
"""

from __future__ import annotations

import pytest

from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT, MAX_PARTITION_COUNT
from custom_messages.wire.partitions import (
    agent_partition,
    agent_record_key,
    check_partition_count,
    every_partition,
    partition_owner_key,
)

# FNV-1a 32-bit of the concatenated bytes (the published test vectors for "", "a" and "foobar").
FNV1A_32 = {"": 0x811C9DC5, "a": 0xE40C292C, "foobar": 0xBF9CF968}


@pytest.mark.parametrize(("team", "agent", "text"), [("foo", "bar", "foobar"), ("fo", "obar", "foobar"),
                                                     ("a", "", "a"), ("", "", "")])
def test_the_partition_is_fnv1a_over_the_team_then_the_agent(team: str, agent: str, text: str) -> None:
    for count in (1, 7, DEFAULT_PARTITION_COUNT, MAX_PARTITION_COUNT):
        assert agent_partition(team, agent, count) == FNV1A_32[text] % count


def test_the_default_count_is_the_contracts_and_the_hash_reads_utf8_bytes() -> None:
    assert agent_partition("foo", "bar") == FNV1A_32["foobar"] % DEFAULT_PARTITION_COUNT
    # NATS hashes a subject token's bytes: "é" is two bytes, C3 A9, not one character.
    value = 0x811C9DC5
    for byte in b"t\xc3\xa9":
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    assert agent_partition("t", "é", MAX_PARTITION_COUNT) == value % MAX_PARTITION_COUNT


def test_keys_name_the_partition() -> None:
    assert agent_record_key("team_example", "lead") == "team_example.55.lead"
    assert agent_record_key("team_example", "lead", 8) == f"team_example.{agent_partition('team_example', 'lead', 8)}.lead"
    assert partition_owner_key("team_example", 55) == "team_example.partition.55"
    for partition in (-1, DEFAULT_PARTITION_COUNT, True, "3"):
        with pytest.raises(ValueError, match="a partition is"):
            partition_owner_key("team_example", partition)  # type: ignore[arg-type]


def test_counts_outside_what_an_installation_may_choose_are_refused() -> None:
    assert check_partition_count(MAX_PARTITION_COUNT) == MAX_PARTITION_COUNT
    for count in (0, MAX_PARTITION_COUNT + 1, 96.0, True):
        with pytest.raises(ValueError, match="partition count"):
            agent_partition("team_example", "lead", count)  # type: ignore[arg-type]


def test_every_partition_covers_each_team_whole() -> None:
    assert every_partition(("a", "b"), 4) == {"a": (0, 1, 2, 3), "b": (0, 1, 2, 3)}
    assert len(every_partition(("a",))["a"]) == DEFAULT_PARTITION_COUNT
