"""The partition mapping check, against a stand-in server (contract README, "Partitions", "The mapping check").

custom_transport.protocol1.mapping_check and the installation authority's start. The same
check on real servers, the bundled one's configuration and an operator-mode account, is in
tests/test_live_partition_mapping.py and tests/test_live_message_contract.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

import pytest

from custom_messages.wire.kinds import MAX_PARTITION_COUNT, PARTITION_PROBE_SCOPE, PARTITIONED_ADDRESSES
from custom_messages.wire.partitions import agent_partition, mapping_source
from custom_transport.protocol1 import mapping_check
from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
from custom_transport.protocol1.mapping_check import (
    MIN_PROBES,
    NO_ANSWER,
    OTHER_COUNT,
    UNMAPPED,
    PartitionMappingError,
    check_partition_mapping,
    probe_agents,
)
from installation_authority.ledger import ISSUED_FILENAME
from installation_authority.service import AUTHORITY_RUNTIME, PartitionMappingRefused
from tests.test_installation_authority_rules import authority_at

ROOT = f"ua.v1.{PARTITION_PROBE_SCOPE}.authority."
LIVE_CONTROL, INBOX, CONTROL_QUEUE = "ctl.agent.{agent}", "work.agent.{agent}", "work.control.{agent}"
assert set(PARTITIONED_ADDRESSES) == {LIVE_CONTROL, INBOX, CONTROL_QUEUE}
ELSEWHERE = "elsewhere"  # a line whose target is mistyped: its probes go to a subject the check does not hear


def fnv_partition(team: str, agent: str, count: int) -> int:
    """nats-server's partition(count,1,3) for any count, also above the contract's maximum (which agent_partition refuses)."""

    value = 0x811C9DC5
    for byte in (team + agent).encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return value % count


class StandInServer:
    """One connection to a server that maps every line at ``maps_at`` (``None``: no mapping), or delivers nothing.

    ``lines`` gives single addresses another count, ``None`` (left out) or ``ELSEWHERE``.
    """

    def __init__(self, maps_at: Optional[int], *, lines: Optional[dict[str, Any]] = None, delivers: bool = True,
                 foreign: tuple[str, ...] = ()) -> None:
        self.maps_at, self.lines, self.delivers = maps_at, dict(lines or {}), delivers
        self.queued: list[str] = list(foreign)  # another check's probes, delivered first
        self.subscriptions: list[tuple[str, Any]] = []
        self.unsubscribed = 0

    async def subscribe(self, subject: str, cb: Any) -> Any:
        self.subscriptions.append((subject, cb))

        async def unsubscribe() -> None:
            self.unsubscribed += 1

        return SimpleNamespace(unsubscribe=unsubscribe)

    async def publish(self, subject: str, data: bytes) -> None:
        assert data == b"" and subject.startswith(ROOT)
        words = subject.removeprefix(ROOT).split(".")
        (address,) = [item for item in PARTITIONED_ADDRESSES if item.split(".")[:2] == words[:2]]
        maps_at = self.lines.get(address, self.maps_at)
        if maps_at is None:
            self.queued.append(subject)
        elif maps_at == ELSEWHERE:
            self.queued.append(ROOT + "lost." + ".".join(words))
        else:
            self.queued.append(f"{subject}.{fnv_partition(PARTITION_PROBE_SCOPE, words[-1], maps_at)}")

    async def flush(self) -> None:
        if not self.delivers:
            return
        for subject in self.queued:
            for pattern, cb in self.subscriptions:
                if subject.startswith(pattern.removesuffix(">")):
                    await cb(SimpleNamespace(subject=subject))
        self.queued.clear()


def checked(server: StandInServer, count: int = 96, **options: Any) -> None:
    with patch.object(mapping_check.secrets, "token_hex", return_value="abcd1234"):
        asyncio.run(check_partition_mapping(server, sender="authority", count=count, **options))


def refused(server: StandInServer, count: int = 96, **options: Any) -> PartitionMappingError:
    with pytest.raises(PartitionMappingError) as caught:
        checked(server, count, **options)
    return caught.value


@pytest.mark.parametrize("count", [1, 2, 7, 64, 96, 128])
def test_the_probes_tell_every_other_count_apart_and_number_at_least_the_minimum(count: int) -> None:
    agents = probe_agents(count, "abcd1234")
    assert len(agents) == MIN_PROBES and agents == probe_agents(count, "abcd1234")
    for other in set(range(1, MAX_PARTITION_COUNT + 1)) - {count}:
        assert any(agent_partition(PARTITION_PROBE_SCOPE, agent, other) != agent_partition(PARTITION_PROBE_SCOPE, agent, count)
                   for agent in agents), other


@pytest.mark.parametrize("server_count", [192, 288, 960])
def test_a_count_above_the_maximum_is_caught_by_the_minimum_of_probes(server_count: int) -> None:
    """An operator's typo can be any number; a multiple of the count is the worst case (each probe keeps its token
    with chance 96/server_count). Without the minimum, 192 passed about one check in eleven (C19)."""

    for index in range(2000):
        agents = probe_agents(96, f"{index:08x}")
        assert any(fnv_partition(PARTITION_PROBE_SCOPE, agent, server_count) != agent_partition(
            PARTITION_PROBE_SCOPE, agent, 96) for agent in agents), index


def test_a_server_mapping_at_the_installations_count_passes_and_keeps_no_subscription(caplog) -> None:
    server = StandInServer(96)
    with caplog.at_level("INFO"):
        checked(server)
    assert [subject for subject, _cb in server.subscriptions] == [ROOT + ">"] and server.unsubscribed == 1
    assert f"event=partition_mapping_checked count=96 probes={MIN_PROBES} lines=3" in caplog.text


def test_another_checks_probes_on_the_same_server_are_not_this_ones() -> None:
    """Two authorities, or a test run beside a running instance, may check at once."""

    checked(StandInServer(96, foreign=(ROOT + "ctl.agent.probe-other-0", ROOT + "work.agent.probe-other-1.5",
                                       ROOT + "work.control.probe-abcd1234-0.5.5")))


def test_a_server_without_the_mappings_is_named_line_by_line_with_the_lines_to_add() -> None:
    error = refused(StandInServer(None), count=7)
    assert error.problems == {mapping_source(address): UNMAPPED for address in PARTITIONED_ADDRESSES}
    message = str(error)
    assert message.startswith("The NATS server does not map agents' addresses at transport.queues.partitions 7: ")
    for address in PARTITIONED_ADDRESSES:
        assert f"the line for {mapping_source(address)} is missing: a probe to {ROOT}" in message
    assert '"ua.v1.*.*.ctl.agent.*": "ua.v1.{{wildcard(1)}}.{{wildcard(2)}}.ctl.agent.{{wildcard(3)}}.' \
           '{{partition(7,1,3)}}"' in message
    # G3 of pull request 152's review: the lines first, inside the login's account; the script only for a source
    # checkout (the published image keeps no .py files), and what its top-level block serves.
    assert ("Add these mapping lines inside the account the instance logs in to (in the server's configuration file, "
            "the account's mappings block): ") in message
    assert message.endswith(". In a source checkout, python scripts/write_nats_mapping_config.py FILE --partitions 7 "
                            "writes them as a top-level mappings block, which serves a server whose logins are in no "
                            "named account.")
    assert "set it back" not in message


def test_a_server_mapping_with_another_count_is_named_with_that_count() -> None:
    error = refused(StandInServer(64))
    assert error.problems == {mapping_source(address): OTHER_COUNT for address in PARTITIONED_ADDRESSES}
    for address in PARTITIONED_ADDRESSES:
        assert f"the line for {mapping_source(address)} maps with partition count 64" in str(error)
    assert "if transport.queues.partitions was changed, set it back" in str(error)
    assert "{{partition(96,1,3)}}" in str(error)


def test_a_count_the_probes_cannot_name_is_called_another_count() -> None:
    """The probes name counts up to the contract's maximum; above it, the refusal says only that the count differs."""

    error = refused(StandInServer(192))
    assert set(error.problems.values()) == {OTHER_COUNT}
    assert f"the line for {mapping_source(INBOX)} maps with another partition count" in str(error)


def test_each_line_is_judged_by_itself() -> None:
    """Live control right, the inbox at another count, the control queue left out (C18)."""

    error = refused(StandInServer(96, lines={INBOX: 64, CONTROL_QUEUE: None}))
    assert error.problems == {mapping_source(INBOX): OTHER_COUNT, mapping_source(CONTROL_QUEUE): UNMAPPED}
    message = str(error)
    assert f"the line for {mapping_source(INBOX)} maps with partition count 64" in message
    assert f"the line for {mapping_source(CONTROL_QUEUE)} is missing" in message
    assert f"the line for {mapping_source(LIVE_CONTROL)}" not in message


def test_a_line_that_sends_its_probes_elsewhere_is_named() -> None:
    error = refused(StandInServer(96, lines={CONTROL_QUEUE: ELSEWHERE}), wait_seconds=0.05)
    assert error.problems == {mapping_source(CONTROL_QUEUE): NO_ANSWER}
    assert (f"the line for {mapping_source(CONTROL_QUEUE)} sent {MIN_PROBES} of {MIN_PROBES} probes to "
            f"{ROOT}work.control.<agent> somewhere the check does not hear") in str(error)


def test_nothing_coming_back_names_the_rights_the_login_needs_or_a_lost_connection() -> None:
    server = StandInServer(96, delivers=False)
    error = refused(server, wait_seconds=0.05)
    assert error.problems == {mapping_source(address): NO_ANSWER for address in PARTITIONED_ADDRESSES}
    assert f"allowed to publish and subscribe to {ROOT}>, or the connection to the server was lost" in str(error)
    assert server.unsubscribed == 1


class _MapsNothing(StandInServer):
    """A server without the mappings, whose JetStream the authority must not reach before refusing."""

    def __init__(self) -> None:
        super().__init__(None)

    def jetstream(self) -> Any:
        raise AssertionError("the authority used JetStream before refusing")


def test_the_authority_refuses_to_start_on_a_server_without_the_mappings_before_anything_changes(tmp_path) -> None:
    authority = authority_at(tmp_path, partitions=96)
    path = authority.layout.trust_bootstrap_path
    write_bundle(path, BootstrapBundle("install_one", ((authority.authority.key_id, authority.authority.public_key_text),),
                                       partitions=96))
    before = path.read_bytes()

    with pytest.raises(PartitionMappingRefused, match="does not map agents' addresses at transport.queues.partitions 96"):
        asyncio.run(authority.start(_MapsNothing(), teams=["team_one"]))

    assert path.read_bytes() == before
    assert not (authority.layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME).exists()
