"""An agent's partition and the keys that name partitions (contracts/messages/v1/README.md, "Partitions").

The live contract test checks that nats-server's partition(COUNT,1,3) stores each agent's
messages in the partition computed here; these tests pin the hash to published FNV-1a vectors.
"""

from __future__ import annotations

import pytest

import json
from pathlib import Path

from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT, MAX_PARTITION_COUNT, PARTITIONED_ADDRESSES
from custom_messages.wire.partitions import (
    agent_partition,
    agent_record_key,
    arrival_subject,
    check_partition_count,
    every_partition,
    partition_owner_key,
    signed_subject_of,
)
from custom_messages.wire.refusals import FrameRefused

TRANSPORT = json.loads((Path(__file__).resolve().parents[1]
                        / "contracts/messages/v1/generated/transport.json").read_text())

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


def test_the_mapped_addresses_are_the_ones_the_account_maps() -> None:
    mapped = {source.split(".", 4)[4].replace("*", "{agent}") for source in TRANSPORT["partitions"]["account_mappings"]}
    assert mapped == set(PARTITIONED_ADDRESSES) == {"work.agent.{agent}", "work.control.{agent}", "ctl.agent.{agent}"}


@pytest.mark.parametrize("address", ["work.agent", "work.control", "ctl.agent"])
@pytest.mark.parametrize("count", [7, DEFAULT_PARTITION_COUNT])
def test_a_mapped_address_arrives_with_its_agents_partition_and_verifies_as_signed(address: str, count: int) -> None:
    signed = f"ua.v1.team_example.lead-host-1.{address}.researcher-2"
    arrived = arrival_subject(signed, count)
    assert arrived == f"{signed}.{agent_partition('team_example', 'researcher-2', count)}"
    assert signed_subject_of(arrived, count) == signed


@pytest.mark.parametrize("subject", [
    "ua.v1.team_example.lead-host-1.work.pool.researcher", "ua.v1.team_example.lead-host-1.work.team",
    "ua.v1.team_example.gateway-1.work.edge.web", "ua.v1._install.new-host-1.svc", "_INBOX.lead-host-1.x.y",
    "ua.v1.team_example.lead-host-1.svc.team",
])
def test_every_other_subject_arrives_as_signed(subject: str) -> None:
    assert arrival_subject(subject) == subject == signed_subject_of(subject)


@pytest.mark.parametrize("suffix", ["", ".7", ".8.8", ".x", ".08"])
def test_a_missing_extra_or_other_partition_token_is_refused(suffix: str) -> None:
    """README, "Partitions": the arrival suffix is the agent's partition at the installation's count, once."""

    signed = "ua.v1.team_example.lead-host-1.work.control.researcher-2"
    assert agent_partition("team_example", "researcher-2") == 8
    with pytest.raises(FrameRefused) as refused:
        signed_subject_of(signed + suffix)
    assert (refused.value.code, refused.value.at) == ("subject_mismatch", "/subject")
    # The partition at another count is another token (3 at 7, 8 at 96), so a receiver at the wrong count refuses too.
    assert signed_subject_of(arrival_subject(signed, 7), 7) == signed
    with pytest.raises(FrameRefused) as refused:
        signed_subject_of(arrival_subject(signed, 7))
    assert (refused.value.code, refused.value.at) == ("subject_mismatch", "/subject")
