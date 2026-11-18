"""Each placed agent's consumers follow its placements on nats-server 2.15.1 (contract README, "Who sets up the streams").

installation_authority.placements with the installation authority, the team services and
a worker host running, as in tests/test_live_team_services.py:

- placing an agent through the team services creates its inbox and control consumers in
  its partition's stream with the contract's settings, and closing its session removes
  them;
- consumers whose queues hold work stay when the placement ends, logged, and go once the
  work is taken; a message past its last delivery keeps them too and is not delivered again;
- an authority that follows the placements again catches up on what changed meanwhile.

Each test starts its own nats-server (``LIVE_TEST_NATS_SERVER``, or nats-server on PATH),
with the partition mappings at the default count. The same rules against stand-ins are in
tests/test_authority_placed_consumers.py.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest
from nats.js.errors import NotFoundError

from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT
from custom_messages.wire.partitions import agent_partition
from custom_messages.wire.queues import agent_consumers
from custom_transport.protocol1.contract_files import transport
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import team_services_profile
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import TEST_QUEUE_LIMITS
from tests.test_live_message_contract import nats_server
from tests.test_live_team_services import TEAM, WORKER, close_session, connect, host, open_session, team_services

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
STREAM_OF = {agent: f"UA1_PART_{TEAM}~{agent_partition(TEAM, agent, DEFAULT_PARTITION_COUNT)}"
             for agent in ("researcher-1", "researcher-2")}
# The contract's limit on deliveries of one message (transport.yaml, agent_inbox).
MAX_DELIVERIES = 20


@pytest.fixture
def broker(tmp_path: Path):
    with own_nats_server(nats_server(), tmp_path) as url:
        yield url


async def authority_with_placements(stack: AsyncExitStack, url: str, layout: InstanceLayout) -> InstallationAuthority:
    """What the launcher does, then a running authority (``installation`` in test_live_team_services, keeping it)."""

    profiles = (team_services_profile(TEAM), WORKER)
    hosts = {profile.runtime: message_hosts.expected_host(layout, profile) for profile in profiles}
    message_hosts.prepare_message_host_trust(layout, hosts, partitions=DEFAULT_PARTITION_COUNT)
    authority = InstallationAuthority(layout, load_authority_keys(layout), load_host_keys(layout, AUTHORITY_RUNTIME),
                                      partitions=DEFAULT_PARTITION_COUNT, queues=TEST_QUEUE_LIMITS)
    await authority.start(await connect(stack, url, AUTHORITY_RUNTIME), teams=[TEAM])
    return authority


async def follow(authority: InstallationAuthority, stop: asyncio.Event) -> asyncio.Task[None]:
    """Run ``follow_placements`` until ``stop``; return once it caught up."""

    previous = authority.placements
    task = asyncio.create_task(authority.follow_placements(stop))
    await eventually(lambda: _caught_up(authority, previous))
    return task


async def _caught_up(authority: InstallationAuthority, previous: Any) -> bool:
    return authority.placements is not None and authority.placements is not previous and authority.placements.ready.is_set()


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("not within the time allowed")
        await asyncio.sleep(0.05)


async def consumers_of(js: Any, agent: str) -> set[str]:
    """Which of the agent's two consumers exist."""

    found = set()
    for consumer in agent_consumers(transport(), TEAM, agent, DEFAULT_PARTITION_COUNT):
        try:
            await js.consumer_info(consumer.stream, consumer.name)
        except NotFoundError:
            continue
        found.add(consumer.name)
    return found


def both(agent: str) -> set[str]:
    return {f"agent~{agent}", f"control~{agent}"}


@pytest.mark.asyncio
async def test_placing_an_agent_creates_its_consumers_and_closing_its_session_removes_them(broker: str,
                                                                                         tmp_path: Path, caplog) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    caplog.set_level("INFO")
    async with AsyncExitStack() as stack:
        authority = await authority_with_placements(stack, broker, layout)
        following = await follow(authority, stop)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        js = (await connect(stack, broker, "admin")).jetstream()

        opened = await open_session(worker, "researcher-1")
        assert opened["outcome"] == "ok"
        await eventually(lambda: _have(js, "researcher-1", both("researcher-1")))
        for consumer in agent_consumers(transport(), TEAM, "researcher-1", DEFAULT_PARTITION_COUNT):
            assert consumer.stream == STREAM_OF["researcher-1"]
            stored = (await js.consumer_info(consumer.stream, consumer.name)).config
            assert (stored.filter_subject, stored.max_deliver, stored.ack_wait) == (
                consumer.config["filter_subject"], MAX_DELIVERIES, consumer.config["ack_wait"])
        # A message to the agent's inbox address is mapped to its partition and waits on its consumer.
        await js.publish(f"ua.v1.{TEAM}.gateway-1.work.agent.researcher-1", b"a task")
        assert (await js.consumer_info(STREAM_OF["researcher-1"], "agent~researcher-1")).num_pending == 1
        inbox = await js.pull_subscribe_bind("agent~researcher-1", STREAM_OF["researcher-1"])
        for message in await inbox.fetch(1, timeout=5):
            await message.ack_sync()

        closed = await close_session(worker, opened["session"]["body"]["session"])
        assert closed["outcome"] == "ok"
        await eventually(lambda: _have(js, "researcher-1", set()))
        assert f"event=agent_consumers_removed team={TEAM} agent=researcher-1" in caplog.text
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, following), timeout=10)


async def _have(js: Any, agent: str, wanted: set[str]) -> bool:
    return await consumers_of(js, agent) == wanted


@pytest.mark.asyncio
async def test_consumers_holding_work_stay_when_the_placement_ends_and_go_once_it_is_taken(broker: str,
                                                                                          tmp_path: Path, caplog) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    caplog.set_level("INFO")
    async with AsyncExitStack() as stack:
        authority = await authority_with_placements(stack, broker, layout)
        following = await follow(authority, stop)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        js = (await connect(stack, broker, "admin")).jetstream()
        opened = await open_session(worker, "researcher-1")
        await eventually(lambda: _have(js, "researcher-1", both("researcher-1")))
        await js.publish(f"ua.v1.{TEAM}.gateway-1.work.agent.researcher-1", b"a task")

        await close_session(worker, opened["session"]["body"]["session"])
        await eventually(lambda: _logged(caplog, "event=agent_consumers_kept"))
        assert (f"event=agent_consumers_kept team={TEAM} agent=researcher-1 pending=1 ack_pending=0 stored=1"
                in caplog.text)
        assert await consumers_of(js, "researcher-1") == both("researcher-1")
        # The work is taken: the next check removes them.
        inbox = await js.pull_subscribe_bind("agent~researcher-1", STREAM_OF["researcher-1"])
        for message in await inbox.fetch(1, timeout=5):
            await message.ack_sync()
        await eventually(lambda: _drained(js, "researcher-1"))
        assert authority.placements is not None
        await authority.placements.recheck()
        assert await consumers_of(js, "researcher-1") == set()
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, following), timeout=10)


@pytest.mark.asyncio
async def test_a_message_past_its_last_delivery_keeps_the_consumers_and_is_not_delivered_again(
        broker: str, tmp_path: Path, caplog) -> None:
    """C03 of the step 2a plan review: the consumer reports nothing pending, the stream still holds the message."""

    layout = InstanceLayout.at(tmp_path / "instance")
    stop = asyncio.Event()
    caplog.set_level("INFO")
    async with AsyncExitStack() as stack:
        authority = await authority_with_placements(stack, broker, layout)
        following = await follow(authority, stop)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        js = (await connect(stack, broker, "admin")).jetstream()
        opened = await open_session(worker, "researcher-1")
        await eventually(lambda: _have(js, "researcher-1", both("researcher-1")))
        # NATS holds a message refused on its last allowed delivery as delivered and unacknowledged
        # until the acknowledgement wait (60 s) has ended and a reader asks for more (measured on
        # 2.15.1); for this test only, that wait is 1 s.
        config = (await js.consumer_info(STREAM_OF["researcher-1"], "agent~researcher-1")).config
        config.ack_wait = 1.0
        await js.add_consumer(STREAM_OF["researcher-1"], config=config)
        await js.publish(f"ua.v1.{TEAM}.gateway-1.work.agent.researcher-1", b"a task its agent cannot read")
        inbox = await js.pull_subscribe_bind("agent~researcher-1", STREAM_OF["researcher-1"])
        for _ in range(MAX_DELIVERIES):
            for message in await inbox.fetch(1, timeout=5):
                await message.nak()
        with pytest.raises(asyncio.TimeoutError):  # no twenty-first delivery
            await inbox.fetch(1, timeout=2)
        await eventually(lambda: _drained(js, "researcher-1"))  # past its deliveries: neither waiting nor unacknowledged

        await close_session(worker, opened["session"]["body"]["session"])
        await eventually(lambda: _logged(caplog, "event=agent_consumers_kept"))
        assert (f"event=agent_consumers_kept team={TEAM} agent=researcher-1 pending=0 ack_pending=0 stored=1"
                in caplog.text)
        assert authority.placements is not None
        await authority.placements.recheck()
        assert await consumers_of(js, "researcher-1") == both("researcher-1")
        with pytest.raises(asyncio.TimeoutError):
            await inbox.fetch(1, timeout=1)
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, following), timeout=10)


@pytest.mark.asyncio
async def test_an_authority_following_again_catches_up_on_placements_that_changed_meanwhile(
        broker: str, tmp_path: Path, caplog) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    stop, stop_following = asyncio.Event(), asyncio.Event()
    caplog.set_level("INFO")
    async with AsyncExitStack() as stack:
        authority = await authority_with_placements(stack, broker, layout)
        following = await follow(authority, stop_following)
        serving = await team_services(stack, broker, layout, stop)
        worker = await host(stack, broker, layout, WORKER)
        js = (await connect(stack, broker, "admin")).jetstream()
        first = await open_session(worker, "researcher-1")
        await eventually(lambda: _have(js, "researcher-1", both("researcher-1")))

        stop_following.set()  # the authority stops following, as when it is down
        await asyncio.wait_for(following, timeout=10)
        await open_session(worker, "researcher-2")
        await close_session(worker, first["session"]["body"]["session"])
        assert await consumers_of(js, "researcher-2") == set()

        following = await follow(authority, stop)
        assert await consumers_of(js, "researcher-2") == both("researcher-2")
        assert await consumers_of(js, "researcher-1") == set()
        assert f"event=placed_consumers_caught_up team={TEAM} placed=1 ended=1" in caplog.text
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, following), timeout=10)


async def _logged(caplog: Any, text: str) -> bool:
    return text in caplog.text


async def _drained(js: Any, agent: str) -> bool:
    info = await js.consumer_info(STREAM_OF[agent], f"agent~{agent}")
    return (info.num_pending, info.num_ack_pending) == (0, 0)
