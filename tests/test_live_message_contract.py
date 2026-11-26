"""Protocol 1 permissions, transport settings and signed frames work together on an isolated broker.

These tests start their own nats-server in operator mode, as an installation's server runs
once every process has its own user (README, "NATS users and the account"):
- the installation's account JWT carries the partition mappings at the default count, so
  every agent's inbox, control queue and control address is stored or delivered in its
  partition, as the real server will;
- every host logs in with a user JWT holding its role's generated permissions, filled for the
  partitions it owns, the pools it runs and the edges it serves (README, "Filling the templates");
- streams and consumers have the settings of transport.yaml; frames are signed with public
  fixture keys.
No real operator credentials or live model are used (tests/nats_operator_support.py signs the
JWTs).

``LIVE_TEST_NATS_SERVER`` names the server binary and makes it required; CI sets
it. Without it the tests use nats-server from PATH and skip when there is none.
Either way the server must be at least transport.yaml's minimum version.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import time
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import nats
import pytest
from nats.js.api import AckPolicy, ConsumerConfig, DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError
from nats.nuid import NUID

from custom_messages.wire import frame as wire_frame
from custom_messages.wire.buckets import bucket_stream
from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT, KINDS, PARTITION_PROBE_SCOPE
from custom_messages.wire.partitions import (
    agent_partition,
    agent_record_key,
    arrival_subject,
    every_partition,
    partition_owner_key,
    signed_subject_of,
)
from custom_messages.wire.permissions import HostPlacement, host_permissions
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.bucket_reads import KeyChange, list_keys, watch_keys
from custom_transport.protocol1.mapping_check import check_partition_mapping
from installation_authority.queues import QueueTopology
from tests import message_contract_support as fixtures
from tests.live_nats_support import flush_fully
from tests.nats_operator_support import NatsUser, OperatorAccount
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import queue_limits

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
CONTRACT = Path(__file__).resolve().parents[1] / "contracts/messages/v1"
TRANSPORT = json.loads((CONTRACT / "generated/transport.json").read_text())
TEAM = "team_example"
# The installation's partition count: the contract's default, in the account's mappings and every filled grant.
COUNT = DEFAULT_PARTITION_COUNT
PARTITION_STREAM = TRANSPORT["consumers"]["agent_inbox"]["stream"]
POOL_STREAM = TRANSPORT["consumers"]["pool"]["stream"]
# Each team's edge deliveries, and its team services' inbox (where the copies wait), with TEAM's streams.
EDGE_STREAM = TRANSPORT["consumers"]["edge"]["stream"]
EDGE = EDGE_STREAM.format(team=TEAM)
TEAM_STREAM = TRANSPORT["consumers"]["team_services"]["stream"]
TEAM_WORK = TEAM_STREAM.format(team=TEAM)
RETENTION = {"workqueue": RetentionPolicy.WORK_QUEUE, "limits": RetentionPolicy.LIMITS}
STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}
DISCARD = {"new": DiscardPolicy.NEW, "old": DiscardPolicy.OLD}
# Which host owns which agents' partitions in these tests (README, "Partitions": a host owns
# whole partitions; it gets the partition of each agent named here).
HOSTS: dict[str, dict[str, Any]] = {
    "lead": {"role": "lead_host", "runtime": "lead-host-1", "owns": ["lead"], "pools": [], "edges": []},
    "worker": {"role": "worker_host", "runtime": "worker-host-1", "owns": ["researcher-2"], "pools": ["researcher"],
               "edges": []},
    # The same host after researcher-3's partition moved to it: a new credential naming both partitions.
    "worker_moved": {"role": "worker_host", "runtime": "worker-host-1", "owns": ["researcher-2", "researcher-3"],
                     "pools": ["researcher"], "edges": []},
    # The topology owner's {team} is every team of the installation (README, "Who sets up the streams").
    "authority": {"role": "installation_authority", "runtime": "authority-1", "owns": [], "pools": [], "edges": [],
                  "teams": ["team_example", "team_other"]},
    # The team services own no partition but withdraw from every partition stream of their teams.
    "services": {"role": "team_services", "runtime": "team-services-1", "owns": [], "pools": [], "edges": [],
                 "every_partition": True},
    "scheduler": {"role": "scheduler", "runtime": "scheduler-1", "owns": [], "pools": [], "edges": []},
    "gateway": {"role": "gateway", "runtime": "gateway-1", "owns": [], "pools": [], "edges": ["web"]},
    "edge": {"role": "edge_adapter", "runtime": "edge-adapter-1", "owns": [], "pools": [], "edges": ["a2a_partner"]},
    # A host that has not joined yet: the bootstrap bundle's pre-join permissions only.
    "joining": {"role": "joining_host", "runtime": "new-host-1", "owns": [], "pools": [], "edges": []},
    # A worker host of another team of the same installation.
    "other_worker": {"role": "worker_host", "runtime": "worker-host-9", "owns": ["writer-1"], "pools": [], "edges": [],
                     "team": "team_other"},
}
# The installation these hosts belong to, and the installation an agreement names (the edge adapter keeps its records).
INSTALLATION, PEER = "install_example", "install_peer"
# What a new host's bootstrap bundle carries besides the authority keys (README, "Installation traffic").
BOOTSTRAP_RECORDS = {"certificates/authority-1.json", "revocations/install_example.json"}


def part(agent: str, team: str = TEAM) -> int:
    return agent_partition(team, agent, COUNT)


def placement_of(user: str) -> HostPlacement:
    host = HOSTS[user]
    team = host.get("team", TEAM)
    teams = tuple(host.get("teams", [team]))
    partitions = (every_partition(teams, COUNT) if host.get("every_partition")
                  else {team: tuple(sorted({part(agent, team) for agent in host["owns"]}))})
    return HostPlacement(host["runtime"], teams, partitions=partitions, pools={team: tuple(host["pools"])},
                         edges={team: tuple(host["edges"])}, installation=INSTALLATION, peers=(PEER,))


def role_document(role: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((CONTRACT / "generated/permissions" / f"{role}.json").read_text())
    return document


def permissions(user: str) -> dict[str, Any]:
    """The host's generated permissions filled as membership code fills them (README, "Filling the templates")."""

    return host_permissions(role_document(HOSTS[user]["role"]), placement_of(user))


def consumer_name(template: str, team: str = TEAM, **ids: str) -> str:
    return str(TRANSPORT["consumers"][template]["name"]).format(team=team, **ids)


def consumer_stream(template: str, team: str = TEAM, partition: int = 0) -> str:
    return str(TRANSPORT["consumers"][template]["stream"]).format(team=team, partition=partition)


def agent_stream(agent: str, team: str = TEAM) -> str:
    """The partition stream that holds an agent's inbox and control queue."""

    return consumer_stream("agent_inbox", team, part(agent, team))


# Each agent's two queues (README, "An agent's two queues").
AGENT_QUEUES = ("agent_inbox", "agent_control")


def stream_config(name: str, team: str = TEAM, partition: Optional[int] = None) -> StreamConfig:
    """The stream exactly as transport.yaml describes it; a {team} stream for ``team``, a partition stream for ``partition``."""

    spec = TRANSPORT["streams"][name]
    return StreamConfig(
        name=name.format(team=team, partition=partition),
        subjects=[subject.format(team=team, partition=partition) for subject in spec["subjects"]],
        retention=RETENTION[spec["retention"]],
        storage=STORAGE[spec["storage"]],
        max_age=spec.get("max_age_seconds"),
        duplicate_window=spec.get("duplicate_window_seconds"),
        max_consumers=spec.get("max_consumers"),
        max_msg_size=spec.get("max_message_bytes"),
        max_msgs_per_subject=spec.get("max_msgs_per_subject", -1),
        # The byte limit comes from the instance's settings (README, "Stream sizes"); these
        # tests leave it unlimited, except the ones that fill a stream.
        discard=DISCARD[spec["discard"]],
    )


def consumer_config(template: str, team: str = TEAM, **ids: str) -> ConsumerConfig:
    """A pull consumer exactly as transport.yaml describes it, for concrete ids; an agent's in its partition."""

    spec = TRANSPORT["consumers"][template]
    assert spec["mode"] == "pull" and spec["ack"] == "explicit"
    fill = {"team": team, **ids}
    if "agent" in ids:
        fill["partition"] = str(part(ids["agent"], team))
    return ConsumerConfig(
        durable_name=spec["name"].format(**fill),
        filter_subject=spec["filter"].format(**fill),
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=spec["ack_wait_seconds"],
        max_deliver=spec["max_deliveries"],
    )


def headers(frame: dict) -> dict[str, str]:
    """The NATS header copies a sender sets (README, "NATS headers")."""

    copies = {"UA-Kind": frame["kind"], "UA-Id": frame["message_id"]}
    spec = KINDS.get(frame["kind"])
    if spec is not None and spec.delivery_class == "durable":
        copies["Nats-Msg-Id"] = frame["idempotency_key"]
    return copies


def arrived(frame: dict, message) -> dict:
    """The verification context of a frame as NATS delivered it, the arrival suffix taken off (README, "Partitions").

    The server's mapping must have put the frame in the partition the wire library computes,
    and the frame's exact arrival subject is not its signed one for a mapped address.
    """

    assert message.subject == arrival_subject(frame["subject"], COUNT), message.subject
    return {"arrived_on": signed_subject_of(message.subject, COUNT), "headers": message.headers or {}}


def server_version(executable: str) -> tuple[int, ...]:
    output = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10, check=True)
    found = re.search(r"v(\d+)\.(\d+)\.(\d+)", output.stdout + output.stderr)
    if found is None:
        pytest.fail(f"cannot read the version of {executable}: {output.stdout!r}")
    return tuple(int(part) for part in found.groups())


def nats_server() -> str:
    """The server binary: required when LIVE_TEST_NATS_SERVER names it, else optional from PATH."""

    configured = os.environ.get("LIVE_TEST_NATS_SERVER")
    if configured:
        if not (Path(configured).is_file() and os.access(configured, os.X_OK)):
            pytest.fail(f"LIVE_TEST_NATS_SERVER={configured} is not an executable file")
        executable = configured
    else:
        found = shutil.which("nats-server")
        if found is None:
            pytest.skip("isolated protocol permission checks need nats-server on PATH or LIVE_TEST_NATS_SERVER")
        executable = found
    minimum = tuple(int(part) for part in TRANSPORT["server"]["minimum_version"].split("."))
    if server_version(executable) < minimum:
        pytest.fail(f"{executable} is older than the contract's minimum NATS {TRANSPORT['server']['minimum_version']}")
    return executable


@dataclass
class Broker:
    """The isolated server, the installation's account on it and a user per host of HOSTS, plus an unlimited admin."""

    url: str
    account: OperatorAccount
    users: dict[str, NatsUser]


@pytest.fixture
def isolated_broker(tmp_path: Path):
    account = OperatorAccount(TRANSPORT["partitions"]["account_mappings"], COUNT)
    users = {name: account.user(name, permissions(name)) for name in HOSTS}
    # Test setup only: creates streams and buckets without being any of the contract's roles.
    users["admin"] = account.user("admin", None)
    settings = account.server_settings(tmp_path)
    config = tmp_path / "nats.json"
    config.write_text(json.dumps(settings))
    config.chmod(0o600)
    # own_nats_server sets the address, a port the server picks itself and the store folder.
    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        yield Broker(url, account, users)


class Client:
    """One connection and the asynchronous errors the server sent it."""

    def __init__(self, connection, errors: asyncio.Queue) -> None:
        self.nc = connection
        self.errors = errors

    async def refused(self, subject: str) -> None:
        """Wait for the server's permission error that names ``subject`` (nats-py lowercases it)."""

        error = str(await asyncio.wait_for(self.errors.get(), timeout=2)).lower()
        assert "permissions violation" in error and f'"{subject.lower()}"' in error, error

    async def publish_refused(self, subject: str, payload: bytes = b"{}") -> None:
        """A refused publish; the server names the subject after the account's mapping, partition included."""

        await self.nc.publish(subject, payload)
        await self.nc.flush()
        await self.refused(arrival_subject(subject, COUNT))

    async def subscribe_refused(self, subject: str) -> None:
        await self.nc.subscribe(subject)
        await self.nc.flush()
        await self.refused(subject)


async def subscribe_ready(client: Client, subject: str, cb) -> None:
    """Subscribe and wait until the server holds the subscription."""

    await client.nc.subscribe(subject, cb=cb)
    await flush_fully(client.nc)


async def connect(stack: AsyncExitStack, broker: Broker, user: str, login: Optional[NatsUser] = None) -> Client:
    """Log in as ``user`` with its own user JWT (or ``login``), with the host's reply inbox prefix."""

    runtime = HOSTS[user]["runtime"] if user in HOSTS else user
    # A host that has not joined asks for its join's reply under the bootstrap user's join prefix.
    prefix = f"_INBOX.{runtime}.join" if user in HOSTS and HOSTS[user]["role"] == "joining_host" else f"_INBOX.{runtime}"
    errors: asyncio.Queue = asyncio.Queue()
    connection = await nats.connect(
        broker.url, inbox_prefix=prefix.encode(), error_cb=errors.put, allow_reconnect=False,
        **(login or broker.users[user]).login(),
    )
    stack.push_async_callback(connection.close)
    return Client(connection, errors)


async def create_topology(admin: Client, agents: Iterable[str] = (), teams: Iterable[str] = (TEAM,),
                          all_partitions: bool = False) -> None:
    """What the topology owner does: the work streams, TEAM's partition streams and both queues' consumers for each agent.

    The partition streams are those of ``agents`` unless ``all_partitions``; a real
    installation has every one (creating 96 file streams takes a second or two).
    """

    js = admin.nc.jetstream()
    agents = list(agents)
    partitions = range(COUNT) if all_partitions else sorted({part(agent) for agent in agents})
    expected = ([stream_config(name, team) for team in teams for name in (EDGE_STREAM, TEAM_STREAM, POOL_STREAM)]
                + [stream_config(PARTITION_STREAM, TEAM, partition) for partition in partitions])
    for config in expected:
        info = await js.add_stream(config=config)
        for setting in ("subjects", "retention", "storage", "max_age", "duplicate_window", "max_consumers", "max_msg_size",
                        "discard"):
            assert getattr(info.config, setting) == getattr(config, setting), setting
    for agent in agents:
        for queue in AGENT_QUEUES:
            consumer = await js.add_consumer(agent_stream(agent), config=consumer_config(queue, agent=agent))
            for setting in ("filter_subject", "ack_policy", "ack_wait", "max_deliver"):
                assert getattr(consumer.config, setting) == getattr(consumer_config(queue, agent=agent), setting)


@pytest.mark.asyncio
async def test_a_host_reads_only_the_queues_of_its_own_partitions(isolated_broker) -> None:
    """README, "Grants for the agents a host runs" and "Credentials follow ownership".

    Queued work waits in its partition's stream for the host that owns the partition next.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        assert len({part("lead"), part("researcher-2"), part("researcher-3")}) == 3
        await create_topology(admin, ["lead", "researcher-2", "researcher-3"], teams=(TEAM, "team_other"))
        await admin.nc.jetstream().add_consumer(TEAM_WORK, config=consumer_config("team_services"))
        worker_js = worker.nc.jetstream()

        # A stop waits in the holder's control queue, in the holder's partition.
        frame = fixtures.example_frame("task.stop")
        stored = await lead.nc.jetstream().publish(frame["subject"], json.dumps(frame).encode(), headers=headers(frame))
        assert stored.stream == agent_stream("researcher-2")
        moved = frame["subject"].replace("researcher-2", "researcher-3")
        stored = await lead.nc.jetstream().publish(moved, b"queued before researcher-3's partition moved")
        assert stored.stream == agent_stream("researcher-3")
        own = consumer_name("agent_control", agent="researcher-2")
        await worker_js.consumer_info(agent_stream("researcher-2"), own)
        subscription = await worker_js.pull_subscribe_bind(stream=agent_stream("researcher-2"), consumer=own)
        delivered, = await subscription.fetch(1, timeout=2)
        checked = fixtures.verify(delivered.data, frame, arrived(frame, delivered))
        assert checked.body.task_id == frame["body"]["task_id"]
        await delivered.ack_sync(timeout=2)

        # Another host's partition, and a partition the host does not own yet, are out of reach.
        for agent in ("lead", "researcher-3"):
            for queue in AGENT_QUEUES:
                name = consumer_name(queue, agent=agent)
                await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{agent_stream(agent)}.{name}")
                await worker.publish_refused(f"$JS.API.CONSUMER.INFO.{agent_stream(agent)}.{name}")
        # Once researcher-3's partition moved here, the host's new credential reads the work queued before the move.
        after_move = await connect(stack, isolated_broker, "worker_moved")
        name = consumer_name("agent_control", agent="researcher-3")
        subscription = await after_move.nc.jetstream().pull_subscribe_bind(stream=agent_stream("researcher-3"), consumer=name)
        delivered, = await subscription.fetch(1, timeout=2)
        assert delivered.data == b"queued before researcher-3's partition moved"
        await delivered.ack_sync(timeout=2)
        for agent in ("researcher-2", "researcher-3"):
            for queue in AGENT_QUEUES:
                info = await admin.nc.jetstream().consumer_info(agent_stream(agent), consumer_name(queue, agent=agent))
                assert info.num_ack_pending == 0 and info.num_pending == 0

        # The same partition number of another team is another team's stream.
        other_team = consumer_stream("agent_inbox", "team_other", part("researcher-2"))
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{other_team}.agent~researcher-2")
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{other_team}.control~researcher-2")
        services = consumer_name("team_services")
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{TEAM_WORK}.{services}")
        await admin.nc.jetstream().publish(f"ua.v1.{TEAM}.lead-host-1.work.team", b"the team services' work")
        services_sub = await admin.nc.jetstream().pull_subscribe_bind(stream=TEAM_WORK, consumer=services)
        services_delivery, = await services_sub.fetch(1, timeout=2)
        await worker.publish_refused(services_delivery.reply, b"+ACK")
        assert (await admin.nc.jetstream().consumer_info(TEAM_WORK, services)).num_ack_pending == 1
        await services_delivery.ack_sync(timeout=2)
        assert worker.errors.empty() and lead.errors.empty() and after_move.errors.empty()


@pytest.mark.asyncio
async def test_the_server_stores_and_delivers_every_agent_in_the_partition_the_wire_library_computes(isolated_broker) -> None:
    """README, "Partitions": nats-server's partition(COUNT,1,3) is FNV-1a over the team then the agent.

    Each address the account maps arrives with one more token, the agent's partition, and
    the inbox and control queue are stored in that partition's stream.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        await create_topology(admin, all_partitions=True)
        js = admin.nc.jetstream()
        chosen = random.Random(97)
        alphabet = string.ascii_lowercase + string.digits + "-_"
        agents = ["lead", "researcher-2", "a", "z" * 32] + [
            chosen.choice(string.ascii_lowercase) + "".join(chosen.choices(alphabet, k=chosen.randint(0, 31)))
            for _ in range(150)]
        heard: asyncio.Queue = asyncio.Queue()
        await subscribe_ready(admin, f"ua.v1.{TEAM}.*.ctl.agent.>", heard.put)
        seen: set[int] = set()
        for agent in agents:
            for address in ("work.agent", "work.control"):
                stored = await js.publish(f"ua.v1.{TEAM}.lead-host-1.{address}.{agent}", agent.encode())
                assert stored.stream == agent_stream(agent), agent
                message = await js.get_msg(stored.stream, stored.seq)
                assert message.subject == f"ua.v1.{TEAM}.lead-host-1.{address}.{agent}.{part(agent)}"
            await admin.nc.publish(f"ua.v1.{TEAM}.lead-host-1.ctl.agent.{agent}", agent.encode())
            control = await asyncio.wait_for(heard.get(), timeout=2)
            assert control.subject == arrival_subject(f"ua.v1.{TEAM}.lead-host-1.ctl.agent.{agent}", COUNT)
            seen.add(part(agent))
        assert len(seen) > COUNT // 2  # the ids spread over the partitions
        # Unmapped addresses arrive as sent.
        stored = await js.publish(f"ua.v1.{TEAM}.lead-host-1.work.team", b"x")
        assert (await js.get_msg(stored.stream, stored.seq)).subject == f"ua.v1.{TEAM}.lead-host-1.work.team"
        assert admin.errors.empty()


@pytest.mark.asyncio
async def test_the_authority_checks_the_mapping_under_its_own_grants_and_no_other_host_may(isolated_broker) -> None:
    """README, "Partitions", the mapping check: the topology owner's grants hold its probe addresses, mapped or not."""

    runtime = HOSTS["authority"]["runtime"]
    async with AsyncExitStack() as stack:
        authority = await connect(stack, isolated_broker, "authority")
        await check_partition_mapping(authority.nc, sender=runtime, count=COUNT)
        assert authority.errors.empty()
        # Another host can neither probe as the authority nor hear its probes.
        lead = await connect(stack, isolated_broker, "lead")
        for address in ("ctl.agent", "work.agent", "work.control"):
            await lead.publish_refused(f"ua.v1.{PARTITION_PROBE_SCOPE}.{runtime}.{address}.probe-1")
        await lead.subscribe_refused(f"ua.v1.{PARTITION_PROBE_SCOPE}.{runtime}.>")


@pytest.mark.asyncio
async def test_a_sender_may_pick_the_partition_token_and_the_receiver_refuses_it(isolated_broker) -> None:
    """README, "Partitions", the arrival suffix: NATS checks grants after mapping, so it cannot stop this.

    The lead host publishes researcher-2's stop in the mapped form with the lead's partition.
    The server stores it in that partition's stream, where only the lead's consumers read, and
    the receiver's check refuses it; with the right partition it lands where the mapping puts it.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        await create_topology(admin, ["researcher-2", "lead"])
        right, wrong = part("researcher-2"), part("lead")
        assert right != wrong
        frame = fixtures.example_frame("task.stop")
        assert frame["subject"].endswith(".work.control.researcher-2")
        js = lead.nc.jetstream()
        stored = await js.publish(f"{frame['subject']}.{wrong}", json.dumps(frame).encode(), headers=headers(frame))
        assert stored.stream == agent_stream("lead") != agent_stream("researcher-2")
        message = await admin.nc.jetstream().get_msg(stored.stream, stored.seq)
        with pytest.raises(FrameRefused) as refused:
            signed_subject_of(message.subject, COUNT)
        assert (refused.value.code, refused.value.at) == ("subject_mismatch", "/subject")
        stored = await js.publish(f"{frame['subject']}.{right}", b"x")
        assert stored.stream == agent_stream("researcher-2")
        assert lead.errors.empty()


@pytest.mark.asyncio
async def test_workers_cannot_manage_streams_speak_for_others_or_read_others_traffic(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        worker = await connect(stack, isolated_broker, "worker")
        await create_topology(admin, ["researcher-2"])
        stream = agent_stream("researcher-2")
        own = consumer_name("agent_inbox", agent="researcher-2")
        control = consumer_name("agent_control", agent="researcher-2")
        for subject in (
            f"$JS.API.CONSUMER.CREATE.{stream}.{own}",
            f"$JS.API.CONSUMER.DURABLE.CREATE.{stream}.{own}",
            f"$JS.API.CONSUMER.DELETE.{stream}.{own}",
            f"$JS.API.CONSUMER.DELETE.{stream}.{control}",
            f"$JS.API.STREAM.INFO.{stream}",
            f"$JS.API.STREAM.MSG.GET.{stream}",
            f"$JS.API.STREAM.INFO.{TEAM_WORK}",
            f"$JS.API.STREAM.INFO.{EDGE}",
            "ua.v1.team_example.lead-host-1.work.agent.researcher-2",
            "ua.v1.team_example.lead-host-1.work.control.researcher-2",
            "ua.v1.team_example.lead-host-1.ctl.agent.researcher-2",
        ):
            await worker.publish_refused(subject)
        lead_partition = part("lead")
        for subject in (
            "_INBOX.lead-host-1.>",
            "ua.v1.team_example.lead-host-1.work.agent.researcher-2.*",
            "ua.v1.team_example.*.work.control.researcher-2.*",
            "ua.v1.team_example.*.work.>",
            "ua.v1.team_example.*.svc.team",
            f"ua.v1.team_other.*.ctl.agent.*.{part('researcher-2')}",
            f"ua.v1.team_example.*.ctl.agent.lead.{lead_partition}",
            f"ua.v1.team_example.*.ctl.agent.*.{lead_partition}",
            "ua.v1.team_example.*.ctl.agent.*.*",
            "ua.v1.team_example.*.ctl.agent.>",
        ):
            await worker.subscribe_refused(subject)
        assert worker.errors.empty()


@pytest.mark.asyncio
async def test_the_topology_owner_sets_up_streams_and_the_team_services_withdraw_queued_work(isolated_broker) -> None:
    """README, "Who sets up the streams" and "Withdrawing queued work", with each role's generated grants."""

    async with AsyncExitStack() as stack:
        authority = await connect(stack, isolated_broker, "authority")
        services = await connect(stack, isolated_broker, "services")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        # The topology owner's grants are account-wide: every partition stream of every team.
        await create_topology(authority, ["researcher-2"], all_partitions=True)
        owner = authority.nc.jetstream()
        await owner.update_stream(config=stream_config(TEAM_STREAM))
        assert await owner.delete_stream(consumer_stream("agent_inbox", TEAM, part("lead")))
        await owner.add_stream(config=stream_config(PARTITION_STREAM, TEAM, part("lead")))
        await owner.add_consumer(consumer_stream("pool"), config=consumer_config("pool", pool="researcher"))
        # Two tasks queued for researcher-2, one for its pool and one for an outside agent's edge; the
        # team services withdraw the second one and the outside agent's.
        kept, withdrawn = (fixtures.example_frame("task.request", name) for name in ("delegation", "delegation-for-person"))
        pooled = fixtures.example_frame("task.request", "to-pool")
        outside = fixtures.example_frame("task.request", "to-outside-agent")
        await owner.add_consumer(EDGE, config=consumer_config("edge", edge=outside["audience"]["id"]))
        for queued in (kept, withdrawn, pooled, outside):
            await lead.nc.jetstream().publish(queued["subject"], json.dumps(queued).encode(), headers=headers(queued))
        stream = agent_stream("researcher-2")
        work_subjects = f"ua.v1.{TEAM}.*.work.>"
        lookup = json.dumps({"seq": 1, "next_by_subj": work_subjects}).encode()
        await worker.publish_refused(f"$JS.API.STREAM.MSG.GET.{stream}", lookup)
        await worker.publish_refused(f"$JS.API.STREAM.MSG.DELETE.{stream}", json.dumps({"seq": 1}).encode())
        await lead.publish_refused(f"$JS.API.STREAM.CREATE.{stream}")
        await lead.publish_refused(f"$JS.API.STREAM.MSG.GET.{consumer_stream('pool')}", lookup)
        # The task's copy names only the task, so the team services read every stream the
        # withdrawal names for their team (every partition stream, the pool stream and the edge
        # stream) across its work subjects, and delete each verified task frame for that task.
        services_js, found = services.nc.jetstream(), []
        searched = [template.format(team=TEAM, partition=partition) for template in TRANSPORT["withdrawal"]["streams"]
                    for partition in (range(COUNT) if "{partition}" in template else (None,))]
        assert len(searched) == COUNT + 2 and EDGE in searched and TEAM_WORK not in searched
        targets = {(frame["body"]["task_id"], frame["message_id"]) for frame in (withdrawn, outside)}
        for name in searched:
            sequence = 0
            while True:
                try:
                    message = await services_js.get_msg(name, seq=sequence + 1, subject=work_subjects, next=True)
                except NotFoundError:  # past the stream's last message
                    break
                sequence = message.seq
                if message.headers and message.headers.get("UA-Kind") == "task.request":
                    document = json.loads(message.data)
                    context = {"arrived_on": signed_subject_of(message.subject, COUNT), "headers": dict(message.headers)}
                    task = fixtures.verify(message.data, document, context)
                    if (task.body.task_id, task.frame.message_id) in targets:
                        found.append((name, sequence))
        assert sorted(found) == sorted([(stream, 2), (EDGE, 1)])
        for name, sequence in found:
            assert await services_js.delete_msg(name, sequence)
        inbox = consumer_name("agent_inbox", agent="researcher-2")
        assert (await owner.consumer_info(stream, inbox)).num_pending == 1
        assert (await owner.stream_info(consumer_stream("pool"))).state.messages == 1
        assert (await owner.stream_info(EDGE)).state.messages == 0
        # Their own inbox's stream, where withdrawal would delete the tasks' copies, and another team's
        # partitions stay out of their reach.
        await services.publish_refused(f"$JS.API.STREAM.MSG.DELETE.{TEAM_WORK}", json.dumps({"seq": 1}).encode())
        await services.publish_refused(f"$JS.API.STREAM.MSG.GET.{TEAM_WORK}", json.dumps({"seq": 1}).encode())
        await services.publish_refused(f"$JS.API.STREAM.MSG.GET.{consumer_stream('agent_inbox', 'team_other', 0)}")
        assert await owner.delete_consumer(consumer_stream("pool"), consumer_name("pool", pool="researcher"))
        assert authority.errors.empty() and services.errors.empty() and lead.errors.empty() and worker.errors.empty()


@pytest.mark.asyncio
async def test_the_authority_sets_up_every_teams_streams_and_team_consumers_under_its_own_grants(isolated_broker) -> None:
    """README, "Who sets up the streams": what installation_authority.queues asks of the server, in operator mode."""

    team = "team_grants"
    async with AsyncExitStack() as stack:
        authority = await connect(stack, isolated_broker, "authority")
        nc = authority.nc
        # Creating every stream reads each one and the account's storage first.
        await QueueTopology(nc, nc.jetstream(), transport=TRANSPORT, limits=queue_limits(), partitions=COUNT).ensure([team])
        # A lowered cap reads the partition streams' subjects; an enlarged limit updates the stream.
        await QueueTopology(nc, nc.jetstream(), transport=TRANSPORT, limits=queue_limits(cap=4, pool=2 << 20),
                            partitions=COUNT).ensure([team])
        assert (await nc.jetstream().stream_info(f"UA1_POOL_WORK_{team}")).config.max_bytes == 2 << 20
        assert authority.errors.empty()
        # No other host sets up a stream.
        lead = await connect(stack, isolated_broker, "lead")
        await lead.publish_refused(f"$JS.API.STREAM.CREATE.UA1_PART_{team}~0")


@pytest.mark.asyncio
async def test_the_pool_controllers_host_reads_every_pool_of_its_team_and_no_other_host_does(isolated_broker) -> None:
    """README, "Worker pools" (A3): the lead host runs the worker pool controller, which reads each pool's backlog."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        js = admin.nc.jetstream()
        await create_topology(admin, teams=(TEAM, "team_other"))
        for team, pool in ((TEAM, "researcher"), (TEAM, "writer"), ("team_other", "researcher")):
            await js.add_consumer(consumer_stream("pool", team), config=consumer_config("pool", team, pool=pool))
        await js.publish(f"ua.v1.{TEAM}.lead-host-1.work.pool.writer", b"waiting for a writer")
        # The lead host runs no pool, yet reads every pool's information in its team.
        for pool, waiting in (("researcher", 0), ("writer", 1)):
            info = await lead.nc.jetstream().consumer_info(consumer_stream("pool"), consumer_name("pool", pool=pool))
            assert info.num_pending == waiting
        # It takes no pool's work, and reads no other team's pools.
        await lead.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{consumer_stream('pool')}.{consumer_name('pool', pool='writer')}")
        other = consumer_stream("pool", "team_other")
        await lead.publish_refused(f"$JS.API.CONSUMER.INFO.{other}.{consumer_name('pool', 'team_other', pool='researcher')}")
        # A worker host reads only the pools it runs.
        await worker.nc.jetstream().consumer_info(consumer_stream("pool"), consumer_name("pool", pool="researcher"))
        await worker.publish_refused(f"$JS.API.CONSUMER.INFO.{consumer_stream('pool')}.{consumer_name('pool', pool='writer')}")
        assert lead.errors.empty() and worker.errors.empty()


@pytest.mark.asyncio
async def test_a_custody_confirmation_reaches_a_busy_agent_past_the_tasks_waiting_in_its_inbox(isolated_broker) -> None:
    """README, "An agent's two queues": the host reads the control queue while new work waits in the inbox.

    In one queue, a host with no free slot stops pulling, so the confirmation its running
    task needs would wait behind the new tasks.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        services = await connect(stack, isolated_broker, "services")
        worker = await connect(stack, isolated_broker, "worker")
        await create_topology(admin, ["researcher-2"])
        stream = agent_stream("researcher-2")
        # Two new tasks for researcher-2, which has no free slot, so its host does not pull them.
        for name in ("delegation", "delegation-for-person"):
            task = fixtures.example_frame("task.request", name)
            await lead.nc.jetstream().publish(task["subject"], json.dumps(task).encode(), headers=headers(task))
        confirmed = fixtures.example_frame("custody.confirmed", "claim-with-token")
        stored = await services.nc.jetstream().publish(confirmed["subject"], json.dumps(confirmed).encode(),
                                                       headers=headers(confirmed))
        assert stored.stream == stream
        control = await worker.nc.jetstream().pull_subscribe_bind(
            stream=stream, consumer=consumer_name("agent_control", agent="researcher-2"))
        delivered, = await control.fetch(1, timeout=2)
        custody = fixtures.verify(delivered.data, confirmed, arrived(confirmed, delivered))
        assert (custody.frame.audience.id, custody.body.task_id) == ("researcher-2", confirmed["body"]["task_id"])
        await delivered.ack_sync(timeout=2)
        # The new tasks still wait in the inbox for a free slot; nothing was taken from it.
        inbox = await admin.nc.jetstream().consumer_info(stream, consumer_name("agent_inbox", agent="researcher-2"))
        assert (inbox.num_pending, inbox.num_ack_pending) == (2, 0)
        assert lead.errors.empty() and services.errors.empty() and worker.errors.empty()


@pytest.mark.asyncio
async def test_a_full_stream_refuses_new_messages_and_keeps_what_it_holds(isolated_broker) -> None:
    """README, "Stream sizes": a full work or dead-letter stream drops nothing queued or set aside."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        js = admin.nc.jetstream()
        first, second = b"x" * 600, b"y" * 600
        work = f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2"
        dead_letter = f"ua.v1.{TEAM}.worker-host-1.dlq.raw.{'e' * 64}"
        cases = ((stream_config(PARTITION_STREAM, TEAM, part("researcher-2")), work, work.replace("agent", "control")),
                 (stream_config("UA1_DLQ_{team}"), dead_letter, dead_letter.replace("e" * 64, "f" * 64)))
        for config, subject, other in cases:
            # Room for one of the two messages: the byte limit counts subjects and headers too.
            config.max_bytes = 1024
            name = (await js.add_stream(config=config)).config.name
            assert name is not None
            await js.publish(subject, first)
            with pytest.raises(nats.js.errors.APIError) as refused:
                await js.publish(other, second)
            assert refused.value.err_code == 10077, refused.value  # maximum bytes exceeded
            assert (await js.stream_info(name)).state.messages == 1
            assert (await js.get_msg(name, 1)).data == first


@pytest.mark.asyncio
async def test_every_streams_limit_is_set_aside_at_creation_and_lowering_it_drops_nothing(tmp_path: Path) -> None:
    """README, "Stream sizes": limits are reserved when a stream is created; a lowered one keeps what it holds."""

    mib = 1024 * 1024
    settings = {"jetstream": {**TRANSPORT["server"]["jetstream"], "max_file_store": 8 * mib}}
    config = tmp_path / "nats.json"
    config.write_text(json.dumps(settings))
    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        connection = await nats.connect(url, allow_reconnect=False)
        try:
            js = connection.jetstream()
            first = stream_config(POOL_STREAM)
            first.max_bytes = 5 * mib
            await js.add_stream(config=first)
            info = json.loads((await connection.request("$JS.API.INFO", b"", timeout=5)).data)
            assert info["reserved_storage"] == 5 * mib and info["storage"] == 0  # set aside, not yet used
            second = stream_config(POOL_STREAM, team="team_other")
            second.max_bytes = 5 * mib
            with pytest.raises(nats.js.errors.APIError) as refused:
                await js.add_stream(config=second)
            assert refused.value.err_code == 10047, refused.value  # insufficient storage resources

            subject = f"ua.v1.{TEAM}.lead-host-1.work.pool.researcher"
            for index in range(3):
                await js.publish(subject, bytes([65 + index]) * 600)
            first.max_bytes = 1024  # below the three messages it holds
            await js.update_stream(config=first)
            kept = await js.stream_info(first.name)
            assert (kept.config.max_bytes, kept.state.messages) == (1024, 3)
            with pytest.raises(nats.js.errors.APIError) as full:
                await js.publish(subject, b"D" * 600)
            assert full.value.err_code == 10077, full.value
            assert (await js.get_msg(first.name, 1)).data == b"A" * 600

            # Lowering gives the room back, so creating the refused stream again now succeeds:
            # a run stopped partway is finished by running it again with lower limits.
            info = json.loads((await connection.request("$JS.API.INFO", b"", timeout=5)).data)
            assert info["reserved_storage"] == 1024
            await js.add_stream(config=second)
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_a_host_may_hear_its_partitions_agents_and_answers_each_request_once(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        await worker.subscribe_refused(f"ua.v1.team_other.*.ctl.agent.*.{part('researcher-2')}")
        # Hosts listen only for their partitions' agents, so a request for an agent nobody runs fails at once.
        with pytest.raises(nats.errors.NoRespondersError):
            await lead.nc.request("ua.v1.team_example.lead-host-1.ctl.agent.researcher-3", b"{}", timeout=2)
        # The worker writes into the lead's inbox only to answer a request it received.
        await worker.publish_refused("_INBOX.lead-host-1.unsolicited")

        async def answer(message):
            await worker.nc.publish(message.reply, b"answer")
            await worker.nc.publish(message.reply, b"second answer")

        # Exactly the grant membership code fills for the partition the worker owns.
        await subscribe_ready(worker, f"ua.v1.team_example.*.ctl.agent.*.{part('researcher-2')}", answer)
        response = await lead.nc.request("ua.v1.team_example.lead-host-1.ctl.agent.researcher-2", b"{}", timeout=2)
        assert response.data == b"answer"
        await worker.refused(response.subject)  # allow_responses permits one answer per request
        assert worker.errors.empty() and lead.errors.empty()


@pytest.mark.asyncio
async def test_ids_that_once_shared_a_consumer_name_get_separate_consumers(isolated_broker) -> None:
    """team_example/web and team/example_web joined with _ named one consumer.

    Each team's edges now wait in the team's own stream, so the two never meet in one stream;
    the names still differ, and each consumer keeps its own filter.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        js = admin.nc.jetstream()
        pairs = [("team_example", "web"), ("team", "example_web")]
        spec = TRANSPORT["consumers"]["edge"]
        names = {spec["name"].format(team=team, edge=edge) for team, edge in pairs}
        assert len(names) == len(pairs)
        for team, edge in pairs:
            await js.add_stream(config=stream_config(EDGE_STREAM, team))
            await js.add_consumer(EDGE_STREAM.format(team=team), config=ConsumerConfig(
                durable_name=spec["name"].format(team=team, edge=edge),
                filter_subject=spec["filter"].format(team=team, edge=edge), ack_policy=AckPolicy.EXPLICIT,
            ))
        for team, edge in pairs:
            info = await js.consumer_info(EDGE_STREAM.format(team=team), spec["name"].format(team=team, edge=edge))
            assert info.config.filter_subject == spec["filter"].format(team=team, edge=edge)


@pytest.mark.asyncio
async def test_a_reassigned_task_is_delivered_again_and_its_retry_is_deduplicated(isolated_broker) -> None:
    """A handback cannot lose its replacement dispatch to the first dispatch's duplicate record.

    Nats-Msg-Id is the idempotency key, made from the task id and the dispatch's
    message id, so a new dispatch of the same task gets through and a retry of that
    dispatch does not.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        js = admin.nc.jetstream()
        pools = consumer_stream("pool")
        await js.add_stream(config=stream_config(POOL_STREAM))
        await js.add_consumer(pools, config=consumer_config("pool", pool="researcher"))
        pool = await js.pull_subscribe_bind(stream=pools, consumer=consumer_name("pool", pool="researcher"))
        original = fixtures.example_frame("task.request", "to-pool")
        replacement = fixtures.example_frame("task.request", "reassignment")
        assert original["body"]["task_id"] == replacement["body"]["task_id"]

        async def dispatch(frame: dict):
            return await lead.nc.jetstream().publish(frame["subject"], json.dumps(frame).encode(), headers=headers(frame))

        first = await dispatch(original)
        admitted, = await pool.fetch(1, timeout=2)
        fixtures.verify(admitted.data, original, arrived(original, admitted))
        await admitted.ack_sync(timeout=2)
        second = await dispatch(replacement)
        retry = await dispatch(replacement)
        assert not first.duplicate and not second.duplicate and second.seq > first.seq
        assert retry.duplicate and retry.seq == second.seq
        received, = await pool.fetch(1, timeout=2)
        assert fixtures.verify(received.data, replacement, arrived(replacement, received)).frame.message_id == replacement["message_id"]
        await received.ack_sync(timeout=2)
        assert (await js.stream_info(pools)).state.messages == 0
        assert lead.errors.empty()


@pytest.mark.asyncio
async def test_a_full_size_frame_is_stored_with_the_largest_headers_senders_may_set(isolated_broker) -> None:
    """README, "NATS headers": the stream's message limit leaves max_header_bytes for headers."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        await create_topology(admin, ["researcher-2"])
        js = admin.nc.jetstream()
        largest = {"Nats-Msg-Id": "task.stop:" + "k" * 118, "UA-Kind": "k" * 84, "UA-Id": "0" * 36,
                   "traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01", "tracestate": "t" * 512}
        subject = f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2"
        ack = await js.publish(subject, b"x" * TRANSPORT["limits"]["max_frame_bytes"], headers=largest)
        assert (await js.get_msg(agent_stream("researcher-2"), ack.seq)).headers["tracestate"] == "t" * 512
        too_large = TRANSPORT["streams"][PARTITION_STREAM]["max_message_bytes"] + 1
        with pytest.raises(nats.js.errors.APIError):
            await js.publish(subject, b"x" * too_large)


@pytest.mark.asyncio
async def test_installation_requests_reach_the_authority_and_dead_letters_keep_raw_copies(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        authority = await connect(stack, isolated_broker, "authority")
        worker = await connect(stack, isolated_broker, "worker")
        api = json.loads((CONTRACT / "generated/asyncapi.json").read_text())
        received: asyncio.Queue = asyncio.Queue()

        async def take(message):
            await received.put(message.data)

        # Joins and renewals share the installation address; the frame's kind tells them apart.
        await subscribe_ready(authority, "ua.v1._install.*.svc", take)
        for operation in ("join", "renew"):
            assert api["channels"]["install_svc"]["messages"].get(f"host_{operation}") is not None
            await worker.nc.publish(api["channels"]["install_svc"]["address"].format(sender="worker-host-1"), operation.encode())
            await worker.nc.flush()
            assert await asyncio.wait_for(received.get(), timeout=2) == operation.encode()

        js = admin.nc.jetstream()
        dead_letters = f"UA1_DLQ_{TEAM}"
        await js.add_stream(config=stream_config("UA1_DLQ_{team}"))
        raw = b"{a refused message stays byte-for-byte identical\x00"
        raw_subject = "ua.v1.team_example.worker-host-1.dlq.raw." + "c" * 64
        # Another copy first, so the copy's sequence in this stream is not 1; the publish is retried
        # after a lost acknowledgement, and the stream still keeps one copy under the subject.
        await worker.nc.jetstream().publish("ua.v1.team_example.worker-host-1.dlq.raw." + "d" * 64, b"another copy")
        for _attempt in range(2):
            await worker.nc.jetstream().publish(raw_subject, raw)
        notice = fixtures.example_frame("dlq.notice", "bad-signature")
        await worker.nc.jetstream().publish(notice["subject"], json.dumps(notice).encode(), headers=headers(notice))
        assert (await js.get_last_msg(dead_letters, raw_subject)).data == raw
        assert (await js.stream_info(dead_letters)).state.messages == 3  # the other copy, one copy of raw, the notice
        stored = await js.get_last_msg(dead_letters, notice["subject"])
        fixtures.verify(stored.data, notice)  # a stored frame is verified without its headers
        other = raw_subject.replace("worker-host-1", "another-host")
        await worker.publish_refused(other, raw)
        with pytest.raises(nats.js.errors.NotFoundError):
            await js.get_last_msg(dead_letters, other)
        # Only the team services read their team's notices and raw copies, and delete a copy.
        services = await connect(stack, isolated_broker, "services")
        dlq = TRANSPORT["consumers"]["dead_letters"]
        await js.add_consumer(dead_letters, config=ConsumerConfig(
            durable_name=dlq["name"].format(team=TEAM), filter_subject=dlq["filter"].format(team=TEAM),
            ack_policy=AckPolicy.EXPLICIT))
        pulled = await services.nc.jetstream().pull_subscribe_bind(dlq["name"].format(team=TEAM), stream=dead_letters)
        [message] = await pulled.fetch(1, timeout=2)
        assert message.subject == notice["subject"]
        await message.ack()
        # The team services find the copy by its subject and delete it by the sequence that read returns.
        found = await services.nc.jetstream().get_last_msg(dead_letters, raw_subject)
        assert found.data == raw and found.seq != notice["body"]["original"]["stream_sequence"]
        await services.nc.jetstream().delete_msg(dead_letters, found.seq)
        with pytest.raises(nats.js.errors.NotFoundError):
            await js.get_last_msg(dead_letters, raw_subject)
        await worker.publish_refused(f"$JS.API.STREAM.MSG.GET.{dead_letters}")
        assert worker.errors.empty() and authority.errors.empty() and services.errors.empty()


@pytest.mark.asyncio
async def test_a_receiver_answers_only_the_requesters_inbox(isolated_broker) -> None:
    """README, "Answering only the requester's inbox": a reply subject the requester chose is no write path.

    allow_responses lets the team services publish one answer to any reply subject, so a
    worker that names a JetStream API subject as its reply subject gets the stream deleted
    by a receiver that answers blindly. A receiver that answers through
    ``answer_subject`` leaves it alone and still answers a request from the worker's inbox.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        services = await connect(stack, isolated_broker, "services")
        worker = await connect(stack, isolated_broker, "worker")
        js = admin.nc.jetstream()
        refusal = {"UA-Refused": "invalid_body", "UA-Refused-At": "/body"}
        address = f"ua.v1.{TEAM}.worker-host-1.svc.team"

        async def blindly(message):
            await services.nc.publish(message.reply, b"", headers=refusal)

        # Without the rule, the answer to a request deletes a stream the worker may not touch.
        await js.add_stream(config=stream_config(TEAM_STREAM))
        blind = await services.nc.subscribe(f"ua.v1.{TEAM}.*.svc.team", cb=blindly)
        await flush_fully(services.nc)
        await worker.nc.publish(address, b"{}", reply=f"$JS.API.STREAM.DELETE.{TEAM_WORK}")
        await worker.nc.flush()

        async def stream_names() -> list[str]:
            return [info.config.name for info in await js.streams_info()]

        deadline = time.monotonic() + 2
        while TEAM_WORK in await stream_names() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert TEAM_WORK not in await stream_names()
        await blind.unsubscribe()

        dropped: list[tuple[str, str]] = []

        async def answer(message):
            subject = wire_frame.answer_subject(message.subject, message.reply)
            if subject is None:
                dropped.append((message.subject, message.reply))
                return
            await services.nc.publish(subject, b"", headers=refusal)

        await js.add_stream(config=stream_config(TEAM_STREAM))
        await subscribe_ready(services, f"ua.v1.{TEAM}.*.svc.team", answer)
        for reply in (f"$JS.API.STREAM.DELETE.{TEAM_WORK}", f"$KV.UA1_LOCKS.team_services.{TEAM}", "_INBOX.lead-host-1.x"):
            await worker.nc.publish(address, b"{}", reply=reply)
        await worker.nc.flush()
        answered = await worker.nc.request(address, b"{}", timeout=2)
        assert answered.headers == refusal and answered.data == b""
        assert [subject for subject, _reply in dropped] == [address] * 3
        assert (await js.stream_info(TEAM_WORK)).config.name == TEAM_WORK
        assert services.errors.empty() and worker.errors.empty()


async def create_buckets(owner: Client, teams: Iterable[str] = (TEAM, "team_other")) -> None:
    """What the topology owner does: every bucket as a stream with transport.yaml's settings, a bucket per team for each team."""

    js = owner.nc.jetstream()
    for bucket in TRANSPORT["key_value"]:
        for team in (teams if "{team}" in bucket else (None,)):
            settings = bucket_stream(TRANSPORT, bucket, team)
            info = await js.add_stream(config=StreamConfig(**{**settings, "storage": STORAGE[settings["storage"]]}))
            assert (info.config.allow_rollup_hdrs, info.config.deny_purge, info.config.allow_direct) == (False, True, True)


@pytest.mark.asyncio
async def test_hosts_write_only_their_own_records_and_read_every_bucket(isolated_broker) -> None:
    """README, "Key-value grants": the topology owner creates the buckets, each writer reaches only its own keys."""

    async with AsyncExitStack() as stack:
        authority = await connect(stack, isolated_broker, "authority")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        services = await connect(stack, isolated_broker, "services")
        await create_buckets(authority)
        hosts = await worker.nc.jetstream().key_value("UA1_HOSTS")
        watch = await hosts.watch("worker-host-1")
        await hosts.put("worker-host-1", b"liveness")

        async def first_value():
            async for entry in watch:
                if entry is not None and entry.value:
                    return entry.value

        assert await asyncio.wait_for(first_value(), timeout=2) == b"liveness"
        await watch.stop()
        # The product reads buckets through bucket_reads, which also asks for the stream's
        # information under the read's filter: a host's read grants cover that too.
        assert await list_keys(worker.nc.jetstream(), "UA1_HOSTS") == ["worker-host-1"]
        async with aclosing(watch_keys(worker.nc.jetstream(), "UA1_HOSTS", "worker-host-1")) as changes:
            assert await asyncio.wait_for(anext(changes), 2) == KeyChange("worker-host-1", b"liveness")
            assert await asyncio.wait_for(anext(changes), 2) is None
        # Agent records live under their partition; the host writes those of its own partitions only.
        agents = await worker.nc.jetstream().key_value("UA1_AGENTS")
        await agents.put(agent_record_key(TEAM, "researcher-2", COUNT), b"idle")
        lead_partition = part("lead")
        # Another host's liveness, an agent of another partition (also under the wrong partition's
        # key), a placement, a partition's ownership and the lock are refused.
        for subject in ("$KV.UA1_HOSTS.lead-host-1", f"$KV.UA1_AGENTS.{agent_record_key(TEAM, 'lead', COUNT)}",
                        f"$KV.UA1_AGENTS.{TEAM}.{lead_partition}.researcher-2", f"$KV.UA1_PLACEMENT.{TEAM}.researcher-2",
                        f"$KV.UA1_PLACEMENT.{partition_owner_key(TEAM, part('researcher-2'), COUNT)}",
                        f"$KV.UA1_LOCKS.team_services.{TEAM}", f"$KV.UA1_CERTS.session.{INSTALLATION}.{TEAM}.s-1"):
            await worker.publish_refused(subject)
        placement = await services.nc.jetstream().key_value("UA1_PLACEMENT")
        revision = await placement.create(f"{TEAM}.researcher-2", b"placed")
        await placement.update(f"{TEAM}.researcher-2", b"moved", last=revision)
        # The team services record each partition's owner (README, "Partitions", "Ownership").
        await placement.create(partition_owner_key(TEAM, part("researcher-2"), COUNT), b"worker-host-1")
        locks = await services.nc.jetstream().key_value("UA1_LOCKS")
        await locks.create(f"team_services.{TEAM}", b"team-services-1")
        with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
            await locks.create(f"team_services.{TEAM}", b"team-services-2")
        await services.publish_refused("$KV.UA1_PLACEMENT.another_team.researcher-2")
        assert (await (await worker.nc.jetstream().key_value("UA1_PLACEMENT")).get(f"{TEAM}.researcher-2")).value == b"moved"
        await lead.publish_refused(f"$KV.UA1_PLACEMENT.{partition_owner_key(TEAM, lead_partition, COUNT)}")

        # README, "Every bucket's stream settings": a writer of one key cannot erase the others.
        await (await lead.nc.jetstream().key_value("UA1_HOSTS")).put("lead-host-1", b"lead liveness")
        with pytest.raises(nats.js.errors.APIError, match="rollup"):
            await worker.nc.jetstream().publish("$KV.UA1_HOSTS.worker-host-1", b"x", headers={"Nats-Rollup": "all"})
        with pytest.raises(nats.js.errors.APIError, match="rollup"):
            await hosts.purge("worker-host-1")
        with pytest.raises(nats.js.errors.APIError, match="rollup"):
            await services.nc.jetstream().publish(f"$KV.UA1_LOCKS.team_services.{TEAM}", b"x", headers={"Nats-Rollup": "all"})
        assert (await hosts.get("lead-host-1")).value == b"lead liveness"
        assert (await locks.get(f"team_services.{TEAM}")).value == b"team-services-1"
        # A delete marker removes the writer's own record only.
        await hosts.delete("worker-host-1")
        with pytest.raises(nats.js.errors.KeyNotFoundError):
            await hosts.get("worker-host-1")
        assert (await hosts.get("lead-host-1")).value == b"lead liveness"
        assert authority.errors.empty() and worker.errors.empty() and services.errors.empty() and lead.errors.empty()


@pytest.mark.asyncio
async def test_bucket_readers_watch_through_unnamed_consumers_and_read_only_their_teams_words(isolated_broker) -> None:
    """README, "Key-value grants" and "One bucket per team for people's words"."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        authority = await connect(stack, isolated_broker, "authority")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        other = await connect(stack, isolated_broker, "other_worker")
        services = await connect(stack, isolated_broker, "services")
        edge = await connect(stack, isolated_broker, "edge")
        await create_buckets(authority)
        lead_hosts = await lead.nc.jetstream().key_value("UA1_HOSTS")
        watch = await lead_hosts.watch("worker-host-1")
        stack.push_async_callback(watch.stop)
        # A reader may not name a consumer: no durable consumer, and no way to change the lead's watch.
        for subject in ("$JS.API.CONSUMER.CREATE.KV_UA1_HOSTS.stay", "$JS.API.CONSUMER.CREATE.KV_UA1_HOSTS.stay.$KV.UA1_HOSTS.x",
                        "$JS.API.CONSUMER.DURABLE.CREATE.KV_UA1_HOSTS.stay", "$JS.API.CONSUMER.DELETE.KV_UA1_HOSTS.stay",
                        "$JS.API.STREAM.MSG.GET.KV_UA1_HOSTS"):
            await worker.publish_refused(subject)
        durable = {"stream_name": "KV_UA1_HOSTS", "config": {"durable_name": "stay", "ack_policy": "none"}}
        answer = json.loads((await worker.nc.request("$JS.API.CONSUMER.CREATE.KV_UA1_HOSTS", json.dumps(durable).encode(),
                                                     timeout=2)).data)
        assert "error" in answer, answer
        await (await worker.nc.jetstream().key_value("UA1_HOSTS")).put("worker-host-1", b"liveness")
        entry = await watch.updates(timeout=2)
        while entry is None or not entry.value:
            entry = await watch.updates(timeout=2)
        assert entry.value == b"liveness"

        # Each team's team view and block list are its own bucket; another team's host has no grant on them.
        view = await services.nc.jetstream().key_value(f"UA1_TEAM_VIEW_{TEAM}")
        await view.put(f"{TEAM}.objective.o1", b"a person's own words")
        await (await services.nc.jetstream().key_value(f"UA1_BLOCK_LIST_{TEAM}")).put(TEAM, b"blocks")
        assert (await (await worker.nc.jetstream().key_value(f"UA1_TEAM_VIEW_{TEAM}")).get(f"{TEAM}.objective.o1")).value \
            == b"a person's own words"
        for bucket in (f"UA1_TEAM_VIEW_{TEAM}", f"UA1_BLOCK_LIST_{TEAM}"):
            for subject in (f"$JS.API.DIRECT.GET.KV_{bucket}", f"$JS.API.DIRECT.GET.KV_{bucket}.$KV.{bucket}.{TEAM}",
                            f"$JS.API.CONSUMER.CREATE.KV_{bucket}", f"$JS.API.STREAM.INFO.KV_{bucket}"):
                await other.publish_refused(subject)
        # It reads its own team's bucket.
        with pytest.raises(nats.js.errors.KeyNotFoundError):
            await (await other.nc.jetstream().key_value("UA1_TEAM_VIEW_team_other")).get("team_other.objective.o9")
        # The product's listings (bucket_reads) work under each host's own login.
        assert await list_keys(worker.nc.jetstream(), f"UA1_TEAM_VIEW_{TEAM}", f"{TEAM}.") == [f"{TEAM}.objective.o1"]
        assert await list_keys(other.nc.jetstream(), "UA1_TEAM_VIEW_team_other") == []
        assert "worker-host-1" in await list_keys(lead.nc.jetstream(), "UA1_HOSTS")

        # The edge adapter keeps peers' copies only; the team services sign sessions of their own installation only.
        for subject in (f"$KV.UA1_CERTS.revocations.{INSTALLATION}", f"$KV.UA1_CERTS.host.{INSTALLATION}.lead-host-1.1",
                        f"$KV.UA1_CERTS.session.{INSTALLATION}.{TEAM}.s-1"):
            await edge.publish_refused(subject)
        await edge.nc.jetstream().publish(f"$KV.UA1_CERTS.revocations.{PEER}", b"a peer's revocations")
        await services.publish_refused(f"$KV.UA1_CERTS.session.{PEER}.{TEAM}.s-1")

        # The topology owner finds and deletes a consumer a host left behind.
        js = authority.nc.jetstream()
        names = [info.name for info in await js.consumers_info("KV_UA1_HOSTS")]
        assert names
        for name in names:
            assert await js.delete_consumer("KV_UA1_HOSTS", name)
        assert admin.errors.empty() and authority.errors.empty() and edge.errors.empty() and services.errors.empty()
        assert worker.errors.empty() and other.errors.empty() and lead.errors.empty()


@pytest.mark.asyncio
async def test_a_new_host_joins_with_only_its_pre_join_permissions_and_bootstrap_records(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        authority = await connect(stack, isolated_broker, "authority")
        joining = await connect(stack, isolated_broker, "joining")
        request = fixtures.example_frame("host.join", "full")

        async def answer(message):
            verified = fixtures.verify(message.data, request, arrived(request, message))
            reply = fixtures.example_frame("host.join.reply", "ok")
            reply.update(subject=message.reply, causation_id=verified.frame.message_id)
            await authority.nc.publish(message.reply, fixtures.resign(reply), headers=headers(reply))

        await subscribe_ready(authority, "ua.v1._install.*.svc", answer)
        response = await joining.nc.request(request["subject"], json.dumps(request).encode(), timeout=2, headers=headers(request))
        reply = json.loads(response.data)
        bootstrap = fixtures.trust_store(remove=[name for name in fixtures.default_records() if name not in BOOTSTRAP_RECORDS])
        verified = fixtures.verify(response.data, reply, arrived(reply, response), trust=bootstrap)
        assert verified.body.outcome == "ok" and verified.body.certificate.body.runtime == "new-host-1"
        await joining.publish_refused("ua.v1._install.worker-host-1.svc")
        await joining.publish_refused("ua.v1.team_example.new-host-1.work.team")
        await joining.subscribe_refused("_INBOX.worker-host-1.>")
        # Only its join's replies: a leftover bootstrap login hears none of the joined host's later replies.
        await joining.subscribe_refused("_INBOX.new-host-1.>")
        await joining.subscribe_refused("_INBOX.new-host-1.renew-reply")
        assert joining.errors.empty() and authority.errors.empty()


@pytest.mark.asyncio
async def test_a_host_owning_every_partition_beside_ten_pools_logs_in_with_the_longest_ids(isolated_broker) -> None:
    """README, "Credential size" (C25, C42): the largest credentials the authority issues at the default count work.

    A lead host owning all 96 partitions of its team beside 10 worker pools, the team services
    of one team, and the installation-wide gateway with the most teams that fit at three routes
    each, every id as long as the contract allows, fit max_credential_bytes, log in through the
    server's max_control_line and are allowed what their grants name.
    """

    from tests.test_message_contract import fits, gateway_credential_json

    longest = {name: fixtures.longest_id(name)
               for name in ("team_id", "agent_type", "runtime_id", "installation_id", "token_id")}
    gateway_teams = max(count for count in range(1, 65) if fits(gateway_credential_json(count, 3)))
    teams = tuple(f"t{index}".ljust(longest["team_id"], "x") for index in range(gateway_teams))
    pools = tuple(f"p{index}".ljust(longest["agent_type"], "y") for index in range(10))
    routes = tuple(f"e{index}".ljust(longest["token_id"], "z") for index in range(3))
    runtime, installation = "r" * longest["runtime_id"], "i" * longest["installation_id"]
    lead_host = HostPlacement(runtime, teams[:1], partitions=every_partition(teams[:1], COUNT), pools={teams[0]: pools},
                              installation=installation)
    team_services = HostPlacement(runtime, teams[:1], partitions=every_partition(teams[:1], COUNT),
                                  installation=installation)
    gateway = HostPlacement(runtime, teams, edges={team: routes for team in teams}, installation=installation)
    account: OperatorAccount = isolated_broker.account
    logins = {"lead": account.user("lead", host_permissions(role_document("lead_host"), lead_host)),
              "services": account.user("services", host_permissions(role_document("team_services"), team_services)),
              "gateway": account.user("gateway", host_permissions(role_document("gateway"), gateway))}
    limit = TRANSPORT["limits"]["max_credential_bytes"]
    for name, floor in (("lead", 40_000), ("services", 35_000), ("gateway", 55_000)):
        assert floor < len(logins[name].jwt) <= limit, (name, len(logins[name].jwt))

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        js = admin.nc.jetstream()
        team = teams[0]
        last = COUNT - 1
        await js.add_stream(config=stream_config(POOL_STREAM, team))
        await js.add_consumer(consumer_stream("pool", team), config=consumer_config("pool", team, pool=pools[-1]))
        await js.add_stream(config=stream_config(PARTITION_STREAM, team, last))
        agent = next(f"a{index}" for index in range(10_000) if agent_partition(team, f"a{index}", COUNT) == last)
        await js.add_consumer(consumer_stream("agent_inbox", team, last), config=consumer_config("agent_inbox", team, agent=agent))
        lead = await connect(stack, isolated_broker, runtime, login=logins["lead"])
        lead_js = lead.nc.jetstream()
        assert (await lead_js.consumer_info(consumer_stream("pool", team), consumer_name("pool", team, pool=pools[-1]))).num_pending == 0
        assert (await lead_js.consumer_info(consumer_stream("agent_inbox", team, last),
                                            consumer_name("agent_inbox", team, agent=agent))).num_pending == 0
        services = await connect(stack, isolated_broker, runtime, login=logins["services"])
        gateway_login = await connect(stack, isolated_broker, runtime, login=logins["gateway"])
        # The team services' new sweep grants work on the existing last partition stream at full login size.
        last_partition_stream = consumer_stream("agent_inbox", team, last)
        last_stream_info = json.loads(
            (await services.nc.request(f"$JS.API.STREAM.INFO.{last_partition_stream}", b"", timeout=2)).data
        )
        assert "error" not in last_stream_info and last_stream_info["config"]["name"] == last_partition_stream
        last_consumer_list = json.loads(
            (await services.nc.request(f"$JS.API.CONSUMER.LIST.{last_partition_stream}", b"", timeout=2)).data
        )
        assert "error" not in last_consumer_list and last_consumer_list["total"] == 1
        # A stream that does not exist answers 10059, which a refused request would not: the services'
        # next-to-last partition (MSG.GET, STREAM.INFO, CONSUMER.LIST), the services' DLQ stream
        # (STREAM.INFO), and the gateway's last route of its last team.
        prev_partition_stream = consumer_stream("agent_inbox", team, last - 1)
        dlq_stream = f"UA1_DLQ_{team}"
        for client, subject, payload in (
                (services, f"$JS.API.STREAM.MSG.GET.{prev_partition_stream}",
                 json.dumps({"last_by_subj": "x"}).encode()),
                (services, f"$JS.API.STREAM.INFO.{prev_partition_stream}", b""),
                (services, f"$JS.API.CONSUMER.LIST.{prev_partition_stream}", b""),
                (services, f"$JS.API.STREAM.INFO.{dlq_stream}", b""),
                (gateway_login, f"$JS.API.CONSUMER.INFO.{consumer_stream('edge', teams[-1])}."
                                f"{consumer_name('edge', teams[-1], edge=routes[-1])}", b"")):
            answer = json.loads((await client.nc.request(subject, payload, timeout=2)).data)
            assert answer["error"]["err_code"] == 10059, (subject, answer)  # stream not found
        assert lead.errors.empty() and services.errors.empty() and gateway_login.errors.empty()


@pytest.mark.asyncio
async def test_a_result_and_its_team_services_copy_both_arrive_and_retries_of_each_are_dropped(isolated_broker) -> None:
    """A result and its copy are stored once each, in their own streams, and a retry of either is dropped.

    The worker publishes its result to the lead and the copy to the team services
    with its own generated grants; each reader pulls with its own grants, from its
    own stream.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        worker = await connect(stack, isolated_broker, "worker")
        lead = await connect(stack, isolated_broker, "lead")
        services = await connect(stack, isolated_broker, "services")
        await create_topology(admin, ["lead"])
        await admin.nc.jetstream().add_consumer(TEAM_WORK, config=consumer_config("team_services"))
        original = fixtures.example_frame("task.result", "completed")
        copy_frame = fixtures.example_frame("task.result", "copy-to-team-services")

        async def publish(frame: dict):
            return await worker.nc.jetstream().publish(frame["subject"], json.dumps(frame).encode(), headers=headers(frame))

        sent = [await publish(original), await publish(copy_frame)]
        retries = [await publish(original), await publish(copy_frame)]
        assert not any(ack.duplicate for ack in sent) and all(ack.duplicate for ack in retries)
        for client, stream, consumer, frame in (
                (lead, agent_stream("lead"), consumer_name("agent_inbox", agent="lead"), original),
                (services, TEAM_WORK, consumer_name("team_services"), copy_frame)):
            inbox = await client.nc.jetstream().pull_subscribe_bind(stream=stream, consumer=consumer)
            received, = await inbox.fetch(1, timeout=2)
            verified = fixtures.verify(received.data, frame, arrived(frame, received))
            assert verified.frame.audience.target == frame["audience"]["target"]
            await received.ack_sync(timeout=2)
        for stream in (agent_stream("lead"), TEAM_WORK):
            assert (await admin.nc.jetstream().stream_info(stream)).state.messages == 0
        assert worker.errors.empty() and lead.errors.empty() and services.errors.empty()


@pytest.mark.asyncio
async def test_a_scheduled_run_reaches_its_worker_and_its_result_reaches_the_persons_gateway(isolated_broker) -> None:
    """The scheduler's task names the person's gateway route as its reply target (README, "Replies").

    Each host publishes and pulls with its own generated grants: the scheduler puts
    the run into the worker pool without its token, the worker claims it, the team
    services read the claim's copy and hand the token to that worker on its agent's control
    queue (custody.confirmed),
    the worker sends its result to the route the task names, and the gateway reads it
    with the delivery reference it needs.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        scheduler = await connect(stack, isolated_broker, "scheduler")
        services = await connect(stack, isolated_broker, "services")
        worker = await connect(stack, isolated_broker, "worker")
        gateway = await connect(stack, isolated_broker, "gateway")
        js = admin.nc.jetstream()
        await create_topology(admin, ["researcher-2"])
        pools = consumer_stream("pool")
        await js.add_consumer(pools, config=consumer_config("pool", pool="researcher"))
        await js.add_consumer(EDGE, config=consumer_config("edge", edge="web"))
        run = fixtures.example_frame("task.request", "scheduled-run")
        result = fixtures.example_frame("task.result", "scheduled-run-to-person")
        await scheduler.nc.jetstream().publish(run["subject"], json.dumps(run).encode(), headers=headers(run))
        pool = await worker.nc.jetstream().pull_subscribe_bind(stream=pools, consumer=consumer_name("pool", pool="researcher"))
        received, = await pool.fetch(1, timeout=2)
        task = fixtures.verify(received.data, run, arrived(run, received))
        assert task.authority is None  # every host running the pool reads its queue
        # The stream's own time for the frame, which the server sets: a receiver judges a
        # queued token by it (verify_frame(stored_at=), README "On behalf of a person").
        stored = received.metadata.timestamp
        assert stored.tzinfo is not None and abs(datetime.now(timezone.utc) - stored) < timedelta(minutes=1)
        await received.ack_sync(timeout=2)
        # The worker claims the run. A scheduled run's requester is the person's route, so the
        # claim goes only to its copy; the team services read it and answer with custody.
        claim = fixtures.example_frame("task.claimed", "scheduled-run-copy-to-team-services")
        assert claim["body"]["task_id"] == task.body.task_id
        await worker.nc.jetstream().publish(claim["subject"], json.dumps(claim).encode(), headers=headers(claim))
        await js.add_consumer(TEAM_WORK, config=consumer_config("team_services"))
        team_inbox = await services.nc.jetstream().pull_subscribe_bind(stream=TEAM_WORK,
                                                                       consumer=consumer_name("team_services"))
        delivered_claim, = await team_inbox.fetch(1, timeout=2)
        claimed = fixtures.verify(delivered_claim.data, claim, arrived(claim, delivered_claim))
        assert (claimed.frame.issuer.agent, claimed.body.task_id) == ("researcher-2", task.body.task_id)
        await delivered_claim.ack_sync(timeout=2)
        confirmed = fixtures.example_frame("custody.confirmed", "claim-with-token")
        await services.nc.jetstream().publish(confirmed["subject"], json.dumps(confirmed).encode(), headers=headers(confirmed))
        control = await worker.nc.jetstream().pull_subscribe_bind(stream=agent_stream("researcher-2"),
                                                                  consumer=consumer_name("agent_control", agent="researcher-2"))
        delivered, = await control.fetch(1, timeout=2)
        custody = fixtures.verify(delivered.data, confirmed, arrived(confirmed, delivered))
        assert custody.authority is not None and custody.authority.token.schedule_id == "sched_1"
        custody.authority.require_bound_to("root_task", task.body.work.root_task_id)
        await delivered.ack_sync(timeout=2)
        reply_to = task.body.work.reply_to
        assert (reply_to.target, reply_to.id) == ("edge", "web") and result["audience"]["id"] == reply_to.id
        await worker.nc.jetstream().publish(result["subject"], json.dumps(result).encode(), headers=headers(result))
        inbox = await gateway.nc.jetstream().pull_subscribe_bind(stream=EDGE, consumer=consumer_name("edge", edge="web"))
        delivered, = await inbox.fetch(1, timeout=2)
        answer = fixtures.verify(delivered.data, result, arrived(result, delivered))
        assert answer.body.delivery_ref == task.body.work.delivery_ref
        await delivered.ack_sync(timeout=2)
        assert scheduler.errors.empty() and services.errors.empty() and worker.errors.empty() and gateway.errors.empty()


@pytest.mark.asyncio
async def test_the_scheduler_signs_a_run_token_for_a_worker_that_asks(isolated_broker) -> None:
    """Only a scheduler signs a run's token, so it answers run_context.sign (README, "On behalf of a person").

    The scheduler listens at its own address, svc.scheduler; the team services may not
    listen there, and the token in the answer is the scheduler's.
    """

    async with AsyncExitStack() as stack:
        worker = await connect(stack, isolated_broker, "worker")
        scheduler = await connect(stack, isolated_broker, "scheduler")
        services = await connect(stack, isolated_broker, "services")
        request = fixtures.example_frame("run_context.sign", "full")

        async def respond(message):
            verified = fixtures.verify(message.data, request, arrived(request, message))
            reply = fixtures.example_frame("run_context.sign.reply", "ok")
            reply.update(subject=message.reply, causation_id=verified.frame.message_id)
            await scheduler.nc.publish(message.reply, fixtures.resign(reply), headers=headers(reply))

        await services.subscribe_refused(request["subject"])
        await subscribe_ready(scheduler, request["subject"], respond)
        response = await worker.nc.request(request["subject"], json.dumps(request).encode(), timeout=2, headers=headers(request))
        answer = json.loads(response.data)
        verified = fixtures.verify(response.data, answer, arrived(answer, response))
        token = verified.body.authority
        assert verified.certificate.roles == frozenset({"scheduler"}) and token.issuer.runtime == "scheduler-1"
        assert token.schedule_id == request["body"]["scheduled_job_id"] and verified.body.system_work is False
        assert worker.errors.empty() and scheduler.errors.empty()


@pytest.mark.asyncio
async def test_the_team_services_pass_a_queue_request_on_to_the_agent_that_holds_the_queue(isolated_broker) -> None:
    """The gateway asks the team services, which relay the command, token unchanged, to the holder (README, "Relays")."""

    async with AsyncExitStack() as stack:
        gateway = await connect(stack, isolated_broker, "gateway")
        services = await connect(stack, isolated_broker, "services")
        worker = await connect(stack, isolated_broker, "worker")
        relay = fixtures.example_frame("tasks.list", "relayed-to-the-holder")
        assert relay["authority"] == fixtures.example_frame("tasks.list", "full")["authority"]
        heard: asyncio.Queue = asyncio.Queue()
        await subscribe_ready(worker, f"ua.v1.{TEAM}.*.ctl.agent.*.{part('researcher-2')}", heard.put)

        # The gateway sends other agent controls to this address, so NATS lets it through;
        # the agent's host refuses the relayed kind from anyone but the team services.
        case = json.loads((CONTRACT / "examples/invalid/tasks-list-from-the-gateway-to-an-agent.json").read_text())
        straight = json.loads(case["frame_text"])
        await gateway.nc.publish(straight["subject"], case["frame_text"].encode(), headers=headers(straight))
        message = await asyncio.wait_for(heard.get(), timeout=2)
        with pytest.raises(FrameRefused) as refused:
            fixtures.verify(message.data, straight, arrived(straight, message))
        assert (refused.value.code, refused.value.at) == (case["refusal"], case["at"]) == ("wrong_sender", "/kind")

        async def respond():
            message = await asyncio.wait_for(heard.get(), timeout=2)
            verified = fixtures.verify(message.data, relay, arrived(relay, message))
            assert verified.authority is not None and verified.authority.certificate.runtime == "gateway-1"
            reply = fixtures.example_frame("tasks.list.reply", "from-the-holder")
            reply.update(subject=message.reply)
            await worker.nc.publish(message.reply, fixtures.resign(reply), headers=headers(reply))

        answering = asyncio.ensure_future(respond())
        response = await services.nc.request(relay["subject"], json.dumps(relay).encode(), timeout=2, headers=headers(relay))
        await answering
        answer = json.loads(response.data)
        verified = fixtures.verify(response.data, answer, arrived(answer, response))
        assert verified.frame.causation_id == relay["message_id"] and verified.frame.issuer.agent == "researcher-2"
        assert verified.body.outcome == "ok"
        assert services.errors.empty() and worker.errors.empty()


@pytest.mark.asyncio
async def test_an_outside_agents_adapter_cannot_put_work_into_a_worker_pool(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        edge = await connect(stack, isolated_broker, "edge")
        await edge.publish_refused(f"ua.v1.{TEAM}.edge-adapter-1.work.pool.researcher")


@pytest.mark.asyncio
async def test_signed_request_reply_uses_the_real_nats_inbox(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        request = fixtures.example_frame("agent.pause")

        async def respond(message):
            verified = fixtures.verify(message.data, request, arrived(request, message))
            reply = fixtures.example_frame("agent.pause.reply", "paused")
            reply.update(subject=message.reply, causation_id=verified.frame.message_id)
            await worker.nc.publish(message.reply, fixtures.resign(reply), headers=headers(reply))

        # The request arrives on its signed subject plus the agent's partition.
        await subscribe_ready(worker, arrival_subject(request["subject"], COUNT), respond)
        response = await lead.nc.request(request["subject"], json.dumps(request).encode(), timeout=2, headers=headers(request))
        answer = json.loads(response.data)
        verified = fixtures.verify(response.data, answer, arrived(answer, response))
        assert verified.frame.causation_id == request["message_id"] and verified.frame.issuer.agent == "researcher-2"
        assert verified.body.outcome == "ok" and verified.body.state == "paused"
        assert worker.errors.empty() and lead.errors.empty()


# Blobs (README, "Blobs"): the object store bucket, its stream settings and the steps the
# product takes itself, since the bucket allows no rollup headers.
BLOBS = TRANSPORT["object_store"]["UA1_BLOBS"]
BLOB_STREAM, CHUNKS, META = "OBJ_UA1_BLOBS", "$O.UA1_BLOBS.C.", "$O.UA1_BLOBS.M."


def blob_name(team: str, content: bytes) -> tuple[str, str]:
    """The object's name, {team}~{sha256}, and its metadata subject (the name in padded base64url)."""

    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
    name = BLOBS["object_name"].format(team=team, sha256=digest)
    return name, META + base64.urlsafe_b64encode(name.encode()).decode()


async def put_blob(writer: Client, team: str, content: bytes, request_id: str) -> str:
    """A writer's put: look the name up, publish the chunks under a new nuid, then one metadata message, no rollup."""

    js = writer.nc.jetstream()
    name, meta_subject = blob_name(team, content)
    try:
        await js.get_last_msg(BLOB_STREAM, meta_subject)
        raise AssertionError(f"{name} is already stored")
    except NotFoundError:
        pass
    nuid = NUID().next().decode()
    size = BLOBS["chunk_bytes"]
    chunks = [content[start:start + size] for start in range(0, len(content), size)]
    for chunk in chunks:
        await js.publish(CHUNKS + nuid, chunk)
    sha256 = hashlib.sha256(content).digest()
    info = {"name": name, "bucket": "UA1_BLOBS", "nuid": nuid, "size": len(content), "chunks": len(chunks),
            "digest": "SHA-256=" + base64.urlsafe_b64encode(sha256).decode(),
            "headers": {"UA-Blob-SHA256": [base64.urlsafe_b64encode(sha256).decode().rstrip("=")],
                        "UA-Blob-Team": [team], "UA-Blob-Request": [request_id],
                        "UA-Blob-Person": ["person_example"]},
            "options": {"max_chunk_size": size}}
    await js.publish(meta_subject, json.dumps(info).encode())
    return name


async def read_blob(reader: Client, team: str, sha256: str) -> Optional[bytes]:
    """A reader's get: the last metadata, then direct STREAM.MSG.GET chunk reads (S2, no consumer)."""

    from custom_transport.protocol1.blobs import BlobDeleted, get_blob

    try:
        return await get_blob(reader.nc.jetstream(), team, sha256)
    except BlobDeleted:
        return None


@pytest.mark.asyncio
async def test_blob_writers_put_readers_read_and_only_the_team_services_delete(isolated_broker) -> None:
    """README, "Blobs": writers, readers and the deleter each with their own generated grants.

    Readers fetch chunks directly through ``$JS.API.STREAM.MSG.GET.OBJ_UA1_BLOBS`` (S2)
    and hold no ``CONSUMER.CREATE`` or ``STREAM.NAMES`` grant on ``OBJ_UA1_BLOBS``.
    NATS's object store helpers cannot be used as they are: put and delete send rollup
    headers, which the bucket refuses, and get asks for every stream's name, which no
    reader may.
    """

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        worker = await connect(stack, isolated_broker, "worker")
        gateway = await connect(stack, isolated_broker, "gateway")
        edge = await connect(stack, isolated_broker, "edge")
        services = await connect(stack, isolated_broker, "services")
        settings = TRANSPORT["object_store_stream"]
        await admin.nc.jetstream().add_stream(config=StreamConfig(
            name=BLOB_STREAM, subjects=[CHUNKS + ">", META + ">"], storage=STORAGE[settings["storage"]],
            allow_rollup_hdrs=settings["allow_rollup_hdrs"], deny_delete=settings["deny_delete"],
            deny_purge=settings["deny_purge"], max_consumers=settings["max_consumers"],
            discard=DiscardPolicy.NEW))
        large = random.Random(3).randbytes(4 * 1024 * 1024 + 17)
        small = b"a short answer kept as a blob"
        large_name = await put_blob(worker, TEAM, large, "task_context_7f3a9c1e5b2d")
        await put_blob(gateway, TEAM, small, "task_context_7f3a9c1e5b2d")
        large_sha = large_name.split("~", 1)[1]
        assert await read_blob(edge, TEAM, large_sha) == large
        stored = (await admin.nc.jetstream().stream_info(BLOB_STREAM)).state.messages
        assert stored == -(-len(large) // BLOBS["chunk_bytes"]) + 1 + 2

        # nats-py's own get first asks the server which stream holds the chunks, which readers may not.
        bare = await edge.nc.jetstream(timeout=1).object_store("UA1_BLOBS")
        with pytest.raises(nats.errors.TimeoutError):
            await bare.get(large_name)
        await edge.refused("$JS.API.STREAM.NAMES")
        # S2: blob readers cannot create consumers on OBJ_UA1_BLOBS.
        await edge.publish_refused(f"$JS.API.CONSUMER.CREATE.{BLOB_STREAM}")
        await worker.publish_refused(f"$JS.API.CONSUMER.CREATE.{BLOB_STREAM}")
        # A reader cannot write; a writer cannot purge, nor erase other objects with a rollup.
        await edge.publish_refused(CHUNKS + NUID().next().decode())
        await edge.publish_refused(blob_name(TEAM, b"x")[1])
        await worker.publish_refused(f"$JS.API.STREAM.PURGE.{BLOB_STREAM}")
        with pytest.raises(nats.js.errors.APIError) as rollup:
            await worker.nc.jetstream().publish(blob_name(TEAM, small)[1], b"{}", headers={"Nats-Rollup": "all"})
        assert rollup.value.err_code == 10111, rollup.value  # rollup not permitted
        assert (await admin.nc.jetstream().stream_info(BLOB_STREAM)).state.messages == stored

        # The team services delete: a deleted marker with the owner headers, then a purge of the
        # chunks and of the name's earlier metadata.
        services_js = services.nc.jetstream()
        meta_subject = blob_name(TEAM, large)[1]
        info = json.loads((await services_js.get_last_msg(BLOB_STREAM, meta_subject)).data)
        await services_js.publish(meta_subject, json.dumps({**info, "deleted": True, "size": 0, "chunks": 0,
                                                            "digest": ""}).encode())
        await services_js.purge_stream(BLOB_STREAM, subject=CHUNKS + info["nuid"])
        await services_js.purge_stream(BLOB_STREAM, subject=meta_subject, keep=1)
        assert await read_blob(edge, TEAM, large_sha) is None
        assert (await admin.nc.jetstream().stream_info(BLOB_STREAM)).state.messages == 1 + 2
        assert await read_blob(edge, TEAM, blob_name(TEAM, small)[0].split("~", 1)[1]) == small
        assert worker.errors.empty() and gateway.errors.empty() and services.errors.empty() and edge.errors.empty()
