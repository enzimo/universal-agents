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

from custom_messages.wire.kinds import MAX_PARTITION_COUNT, PARTITION_PROBE_SCOPE
from custom_messages.wire.partitions import agent_partition, arrival_subject
from custom_transport.protocol1 import mapping_check
from custom_transport.protocol1.bootstrap import BootstrapBundle, write_bundle
from custom_transport.protocol1.mapping_check import (
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

PREFIX = f"ua.v1.{PARTITION_PROBE_SCOPE}.authority.ctl.agent."


class StandInServer:
    """One connection to a server that maps at ``maps_at`` (``None``: no mapping), or delivers nothing."""

    def __init__(self, maps_at: Optional[int], *, delivers: bool = True, foreign: tuple[str, ...] = ()) -> None:
        self.maps_at, self.delivers = maps_at, delivers
        self.queued: list[str] = list(foreign)  # another check's probes, delivered first
        self.subscriptions: list[tuple[str, Any]] = []
        self.unsubscribed = 0

    async def subscribe(self, subject: str, cb: Any) -> Any:
        self.subscriptions.append((subject, cb))

        async def unsubscribe() -> None:
            self.unsubscribed += 1

        return SimpleNamespace(unsubscribe=unsubscribe)

    async def publish(self, subject: str, data: bytes) -> None:
        assert data == b""
        self.queued.append(subject if self.maps_at is None else arrival_subject(subject, self.maps_at))

    async def flush(self) -> None:
        if not self.delivers:
            return
        for subject in self.queued:
            for pattern, cb in self.subscriptions:
                if subject.startswith(pattern.removesuffix(">")):
                    await cb(SimpleNamespace(subject=subject))
        self.queued.clear()


def checked(server: StandInServer, count: int = 96, **options: Any) -> None:
    asyncio.run(check_partition_mapping(server, sender="authority", count=count, **options))


@pytest.mark.parametrize("count", [1, 2, 7, 64, 96, 128])
def test_the_probes_tell_every_other_count_apart(count: int) -> None:
    agents = probe_agents(count, "abcd1234")
    assert 1 <= len(agents) <= 8 and agents == probe_agents(count, "abcd1234")
    for other in set(range(1, MAX_PARTITION_COUNT + 1)) - {count}:
        assert any(agent_partition(PARTITION_PROBE_SCOPE, agent, other) != agent_partition(PARTITION_PROBE_SCOPE, agent, count)
                   for agent in agents), other


def test_a_server_mapping_at_the_installations_count_passes_and_keeps_no_subscription(caplog) -> None:
    server = StandInServer(96)
    with caplog.at_level("INFO"):
        checked(server)
    assert [subject for subject, _cb in server.subscriptions] == [PREFIX + ">"] and server.unsubscribed == 1
    assert "event=partition_mapping_checked count=96" in caplog.text


def test_another_checks_probes_on_the_same_server_are_not_this_ones() -> None:
    """Two authorities, or a test run beside a running instance, may check at once."""

    checked(StandInServer(96, foreign=(PREFIX + "probe-other-0", PREFIX + "probe-other-1.5.5")))


def test_a_server_without_the_mappings_is_named() -> None:
    with pytest.raises(PartitionMappingError) as refused:
        checked(StandInServer(None), count=7)
    assert refused.value.reason == UNMAPPED
    message = str(refused.value)
    assert "does not apply the partition mappings" in message and "at transport.queues.partitions 7" in message
    assert "ua.v1.*.*.ctl.agent.* -> ua.v1.{{wildcard(1)}}.{{wildcard(2)}}.ctl.agent.{{wildcard(3)}}." \
           "{{partition(7,1,3)}}" in message


def test_a_server_mapping_with_another_count_is_named_with_that_count_when_the_probes_tell() -> None:
    with patch.object(mapping_check.secrets, "token_hex", return_value="abcd1234"):
        agents = probe_agents(96, "abcd1234")
        matching = [other for other in range(1, MAX_PARTITION_COUNT + 1)
                    if all(agent_partition(PARTITION_PROBE_SCOPE, agent, other) == agent_partition(
                        PARTITION_PROBE_SCOPE, agent, 64) for agent in agents)]
        with pytest.raises(PartitionMappingError) as refused:
            checked(StandInServer(64))
    assert refused.value.reason == OTHER_COUNT
    shown = "partition count 64" if matching == [64] else "another partition count"
    assert f"maps agents' addresses with {shown}, but transport.queues.partitions is 96" in str(refused.value)
    assert "set the setting back" in str(refused.value) and "{{partition(96,1,3)}}" in str(refused.value)


def test_nothing_coming_back_names_the_rights_the_login_needs() -> None:
    server = StandInServer(96, delivers=False)
    with pytest.raises(PartitionMappingError) as refused:
        checked(server, wait_seconds=0.05)
    assert refused.value.reason == NO_ANSWER
    assert f"allowed to publish and subscribe to {PREFIX}>" in str(refused.value)
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

    with pytest.raises(PartitionMappingRefused, match="does not apply the partition mappings"):
        asyncio.run(authority.start(_MapsNothing(), teams=["team_one"]))

    assert path.read_bytes() == before
    assert not (authority.layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME).exists()
