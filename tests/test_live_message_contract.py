"""Protocol 1 permissions, transport settings and signed frames work together on an isolated broker.

The broker needs a different authorization file from the running team's broker, so
these tests start their own nats-server with:
- the generated role permissions, expanded for the identities each host serves;
- the server, stream and consumer settings from transport.yaml;
- public fixture signing keys.
No operator credentials or live model are used.

``LIVE_TEST_NATS_SERVER`` names the server binary and makes it required; CI sets
it. Without it the tests use nats-server from PATH and skip when there is none.
Either way the server must be at least transport.yaml's minimum version.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import shutil
import subprocess
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Iterable

import nats
import pytest
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

from custom_messages.wire import frame as wire_frame
from custom_messages.wire.buckets import bucket_stream
from custom_messages.wire.kinds import KINDS
from custom_messages.wire.permissions import HostPlacement, host_permissions
from custom_messages.wire.refusals import FrameRefused
from tests import message_contract_support as fixtures
from tests.live_nats_support import flush_fully
from tests.own_nats_server import own_nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
CONTRACT = Path(__file__).resolve().parents[1] / "contracts/messages/v1"
TRANSPORT = json.loads((CONTRACT / "generated/transport.json").read_text())
TEAM = "team_example"
RETENTION = {"workqueue": RetentionPolicy.WORK_QUEUE, "limits": RetentionPolicy.LIMITS}
STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}
# Which host runs which agents in these tests.
HOSTS = {
    "lead": {"role": "lead_host", "runtime": "lead-host-1", "agents": ["lead"], "pools": [], "edges": []},
    "worker": {"role": "worker_host", "runtime": "worker-host-1", "agents": ["researcher-2"], "pools": ["researcher"], "edges": []},
    # The same host after researcher-3 moved to it: a new credential naming both agents.
    "worker_moved": {"role": "worker_host", "runtime": "worker-host-1", "agents": ["researcher-2", "researcher-3"],
                     "pools": ["researcher"], "edges": []},
    # The topology owner's {team} is every team of the installation (README, "Who sets up the streams").
    "authority": {"role": "installation_authority", "runtime": "authority-1", "agents": [], "pools": [], "edges": [],
                  "teams": ["team_example", "team_other"]},
    "services": {"role": "team_services", "runtime": "team-services-1", "agents": [], "pools": [], "edges": []},
    "scheduler": {"role": "scheduler", "runtime": "scheduler-1", "agents": [], "pools": [], "edges": []},
    "gateway": {"role": "gateway", "runtime": "gateway-1", "agents": [], "pools": [], "edges": ["web"]},
    "edge": {"role": "edge_adapter", "runtime": "edge-adapter-1", "agents": [], "pools": [], "edges": ["a2a_partner"]},
    # A host that has not joined yet: the bootstrap bundle's pre-join permissions only.
    "joining": {"role": "joining_host", "runtime": "new-host-1", "agents": [], "pools": [], "edges": []},
    # A worker host of another team of the same installation.
    "other_worker": {"role": "worker_host", "runtime": "worker-host-9", "agents": ["writer-1"], "pools": [], "edges": [],
                     "team": "team_other"},
}
# The installation these hosts belong to, and the installation an agreement names (the edge adapter keeps its records).
INSTALLATION, PEER = "install_example", "install_peer"
# What a new host's bootstrap bundle carries besides the authority keys (README, "Installation traffic").
BOOTSTRAP_RECORDS = {"certificates/authority-1.json", "revocations/install_example.json"}


def permissions(user: str) -> dict:
    """The host's generated permissions filled as membership code fills them (README, "Filling the templates")."""

    host = HOSTS[user]
    team = host.get("team", TEAM)
    document = json.loads((CONTRACT / "generated/permissions" / f"{host['role']}.json").read_text())
    placement = HostPlacement(host["runtime"], tuple(host.get("teams", [team])), agents={team: tuple(host["agents"])},
                              pools={team: tuple(host["pools"])}, edges={team: tuple(host["edges"])},
                              installation=INSTALLATION, peers=(PEER,))
    return host_permissions(document, placement)


def consumer_name(template: str, **ids: str) -> str:
    return TRANSPORT["consumers"][template]["name"].format(team=TEAM, **ids)


def consumer_stream(template: str, team: str = TEAM) -> str:
    return TRANSPORT["consumers"][template]["stream"].format(team=team)


# The team's agent inboxes, agent control queues and worker pools; each host reads only its own
# agents' and pools' consumers.
AGENT_STREAM = consumer_stream("agent_inbox")
# Each agent's two queues (README, "An agent's two queues").
AGENT_QUEUES = ("agent_inbox", "agent_control")


def stream_config(name: str, team: str = TEAM) -> StreamConfig:
    """The stream exactly as transport.yaml describes it; a {team} stream for ``team``."""

    spec = TRANSPORT["streams"][name]
    return StreamConfig(
        name=name.format(team=team),
        subjects=[subject.format(team=team) for subject in spec["subjects"]],
        retention=RETENTION[spec["retention"]],
        storage=STORAGE[spec["storage"]],
        max_age=spec.get("max_age_seconds"),
        duplicate_window=spec.get("duplicate_window_seconds"),
        max_consumers=spec.get("max_consumers"),
        max_msg_size=spec.get("max_message_bytes"),
        max_msgs_per_subject=spec.get("max_msgs_per_subject", -1),
    )


def consumer_config(template: str, **ids: str) -> ConsumerConfig:
    """A pull consumer exactly as transport.yaml describes it, for concrete ids."""

    spec = TRANSPORT["consumers"][template]
    assert spec["mode"] == "pull" and spec["ack"] == "explicit"
    return ConsumerConfig(
        durable_name=consumer_name(template, **ids),
        filter_subject=spec["filter"].format(team=TEAM, **ids),
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
    """The verification context of a frame as NATS delivered it."""

    return {"arrived_on": message.subject, "headers": message.headers or {}}


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


@pytest.fixture
def isolated_broker(tmp_path: Path):
    passwords = {name: secrets.token_urlsafe(24) for name in ("admin", *HOSTS)}
    users = [{"user": "admin", "password": passwords["admin"]}]
    users += [{"user": name, "password": passwords[name], "permissions": permissions(name)} for name in HOSTS]
    # own_nats_server sets the address, a port the server picks itself and the store folder.
    settings = {"jetstream": dict(TRANSPORT["server"]["jetstream"]), "authorization": {"users": users}}
    config = tmp_path / "nats.json"
    config.write_text(json.dumps(settings))
    config.chmod(0o600)
    with own_nats_server(nats_server(), tmp_path, "-c", str(config)) as url:
        yield url, passwords


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
        await self.nc.publish(subject, payload)
        await self.nc.flush()
        await self.refused(subject)

    async def subscribe_refused(self, subject: str) -> None:
        await self.nc.subscribe(subject)
        await self.nc.flush()
        await self.refused(subject)


async def subscribe_ready(client: Client, subject: str, cb) -> None:
    """Subscribe and wait until the server holds the subscription."""

    await client.nc.subscribe(subject, cb=cb)
    await flush_fully(client.nc)


async def connect(stack: AsyncExitStack, broker, user: str) -> Client:
    url, passwords = broker
    runtime = HOSTS[user]["runtime"] if user in HOSTS else "admin"
    errors: asyncio.Queue = asyncio.Queue()
    connection = await nats.connect(
        url, user=user, password=passwords[user], inbox_prefix=f"_INBOX.{runtime}".encode(),
        error_cb=errors.put, allow_reconnect=False,
    )
    stack.push_async_callback(connection.close)
    return Client(connection, errors)


async def create_topology(admin: Client, agents: Iterable[str] = (), teams: Iterable[str] = (TEAM,)) -> None:
    """What the topology owner does: the work streams and both queues' consumers for each agent of TEAM."""

    js = admin.nc.jetstream()
    expected = [stream_config("UA1_WORK")] + [stream_config("UA1_AGENT_WORK_{team}", team) for team in teams]
    for config in expected:
        info = await js.add_stream(config=config)
        for setting in ("subjects", "retention", "storage", "max_age", "duplicate_window", "max_consumers", "max_msg_size"):
            assert getattr(info.config, setting) == getattr(config, setting), setting
    for agent in agents:
        for queue in AGENT_QUEUES:
            consumer = await js.add_consumer(AGENT_STREAM, config=consumer_config(queue, agent=agent))
            for setting in ("filter_subject", "ack_policy", "ack_wait", "max_deliver"):
                assert getattr(consumer.config, setting) == getattr(consumer_config(queue, agent=agent), setting)


@pytest.mark.asyncio
async def test_a_host_reads_only_the_queues_of_the_agents_it_runs(isolated_broker) -> None:
    """README, "Grants for the agents a host runs": queued work waits for the host the agent moves to."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        await create_topology(admin, ["lead", "researcher-2", "researcher-3"], teams=(TEAM, "team_other"))
        await admin.nc.jetstream().add_consumer("UA1_WORK", config=consumer_config("team_services"))
        worker_js = worker.nc.jetstream()

        # A stop waits in the holder's control queue.
        frame = fixtures.example_frame("task.stop")
        await lead.nc.jetstream().publish(frame["subject"], json.dumps(frame).encode(), headers=headers(frame))
        moved = frame["subject"].replace("researcher-2", "researcher-3")
        await lead.nc.jetstream().publish(moved, b"queued before researcher-3 moved to the worker host")
        own = consumer_name("agent_control", agent="researcher-2")
        await worker_js.consumer_info(AGENT_STREAM, own)
        subscription = await worker_js.pull_subscribe_bind(stream=AGENT_STREAM, consumer=own)
        delivered, = await subscription.fetch(1, timeout=2)
        checked = fixtures.verify(delivered.data, frame, arrived(frame, delivered))
        assert checked.body.task_id == frame["body"]["task_id"]
        await delivered.ack_sync(timeout=2)

        # Another host's agent, and an agent not yet placed here, are out of reach.
        for agent in ("lead", "researcher-3"):
            for queue in AGENT_QUEUES:
                await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{AGENT_STREAM}.{consumer_name(queue, agent=agent)}")
                await worker.publish_refused(f"$JS.API.CONSUMER.INFO.{AGENT_STREAM}.{consumer_name(queue, agent=agent)}")
        # Once researcher-3 moved here, the host's new credential reads the work queued before the move.
        after_move = await connect(stack, isolated_broker, "worker_moved")
        name = consumer_name("agent_control", agent="researcher-3")
        subscription = await after_move.nc.jetstream().pull_subscribe_bind(stream=AGENT_STREAM, consumer=name)
        delivered, = await subscription.fetch(1, timeout=2)
        assert delivered.data == b"queued before researcher-3 moved to the worker host"
        await delivered.ack_sync(timeout=2)
        for agent in ("researcher-2", "researcher-3"):
            for queue in AGENT_QUEUES:
                info = await admin.nc.jetstream().consumer_info(AGENT_STREAM, consumer_name(queue, agent=agent))
                assert info.num_ack_pending == 0 and info.num_pending == 0

        other_team = consumer_stream("agent_inbox", "team_other")
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{other_team}.agent~team_other~researcher-2")
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.{other_team}.control~team_other~researcher-2")
        services = consumer_name("team_services")
        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.UA1_WORK.{services}")
        await admin.nc.jetstream().publish(f"ua.v1.{TEAM}.lead-host-1.work.team", b"the team services' work")
        services_sub = await admin.nc.jetstream().pull_subscribe_bind(stream="UA1_WORK", consumer=services)
        services_delivery, = await services_sub.fetch(1, timeout=2)
        await worker.publish_refused(services_delivery.reply, b"+ACK")
        assert (await admin.nc.jetstream().consumer_info("UA1_WORK", services)).num_ack_pending == 1
        await services_delivery.ack_sync(timeout=2)
        assert worker.errors.empty() and lead.errors.empty() and after_move.errors.empty()


@pytest.mark.asyncio
async def test_workers_cannot_manage_streams_speak_for_others_or_read_others_traffic(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        worker = await connect(stack, isolated_broker, "worker")
        await create_topology(admin, ["researcher-2"])
        own = consumer_name("agent_inbox", agent="researcher-2")
        control = consumer_name("agent_control", agent="researcher-2")
        for subject in (
            f"$JS.API.CONSUMER.CREATE.{AGENT_STREAM}.{own}",
            f"$JS.API.CONSUMER.DURABLE.CREATE.{AGENT_STREAM}.{own}",
            f"$JS.API.CONSUMER.DELETE.{AGENT_STREAM}.{own}",
            f"$JS.API.CONSUMER.DELETE.{AGENT_STREAM}.{control}",
            f"$JS.API.STREAM.INFO.{AGENT_STREAM}",
            f"$JS.API.STREAM.MSG.GET.{AGENT_STREAM}",
            "$JS.API.STREAM.INFO.UA1_WORK",
            "ua.v1.team_example.lead-host-1.work.agent.researcher-2",
            "ua.v1.team_example.lead-host-1.work.control.researcher-2",
            "ua.v1.team_example.lead-host-1.ctl.agent.researcher-2",
        ):
            await worker.publish_refused(subject)
        for subject in (
            "_INBOX.lead-host-1.>",
            "ua.v1.team_example.lead-host-1.work.agent.researcher-2",
            "ua.v1.team_example.*.work.control.researcher-2",
            "ua.v1.team_example.*.work.>",
            "ua.v1.team_example.*.svc.team",
            "ua.v1.team_other.*.ctl.agent.researcher-2",
            "ua.v1.team_example.*.ctl.agent.lead",
            "ua.v1.team_example.*.ctl.agent.*",
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
        await create_topology(authority, ["researcher-2"])
        owner = authority.nc.jetstream()
        await owner.update_stream(config=stream_config("UA1_WORK"))
        await owner.add_consumer(AGENT_STREAM, config=consumer_config("pool", pool="researcher"))
        # Two tasks queued for researcher-2; the team services withdraw the second one.
        kept, withdrawn = (fixtures.example_frame("task.request", name) for name in ("delegation", "delegation-for-person"))
        for queued in (kept, withdrawn):
            await lead.nc.jetstream().publish(queued["subject"], json.dumps(queued).encode(), headers=headers(queued))
        work_subjects = f"ua.v1.{TEAM}.*.work.>"
        lookup = json.dumps({"seq": 1, "next_by_subj": work_subjects}).encode()
        await worker.publish_refused(f"$JS.API.STREAM.MSG.GET.{AGENT_STREAM}", lookup)
        await worker.publish_refused(f"$JS.API.STREAM.MSG.DELETE.{AGENT_STREAM}", json.dumps({"seq": 1}).encode())
        await lead.publish_refused(f"$JS.API.STREAM.CREATE.{AGENT_STREAM}")
        # The task's copy names only the task, so the team services read their own team's
        # stream across its work subjects and delete each verified task frame for that task.
        services_js, sequence, found = services.nc.jetstream(), 0, []
        while True:
            try:
                stored = await services_js.get_msg(AGENT_STREAM, seq=sequence + 1, subject=work_subjects, next=True)
            except NotFoundError:  # past the stream's last message
                break
            sequence = stored.seq
            if stored.headers and stored.headers.get("UA-Kind") == "task.request":
                document = json.loads(stored.data)
                task = fixtures.verify(stored.data, document, {"arrived_on": stored.subject, "headers": dict(stored.headers)})
                if task.body.task_id == withdrawn["body"]["task_id"]:
                    found.append(sequence)
        assert len(found) == 1 and await services_js.delete_msg(AGENT_STREAM, found[0])
        inbox = consumer_name("agent_inbox", agent="researcher-2")
        assert (await owner.consumer_info(AGENT_STREAM, inbox)).num_pending == 1
        # The shared UA1_WORK, which holds other teams' messages, stays out of their reach.
        await services.publish_refused("$JS.API.STREAM.MSG.DELETE.UA1_WORK", json.dumps({"seq": 1}).encode())
        await services.publish_refused("$JS.API.STREAM.MSG.GET.UA1_WORK", json.dumps({"seq": 1}).encode())
        assert await owner.delete_consumer(AGENT_STREAM, consumer_name("pool", pool="researcher"))
        assert authority.errors.empty() and services.errors.empty() and lead.errors.empty() and worker.errors.empty()


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
        # Two new tasks for researcher-2, which has no free slot, so its host does not pull them.
        for name in ("delegation", "delegation-for-person"):
            task = fixtures.example_frame("task.request", name)
            await lead.nc.jetstream().publish(task["subject"], json.dumps(task).encode(), headers=headers(task))
        confirmed = fixtures.example_frame("custody.confirmed", "claim-with-token")
        stored = await services.nc.jetstream().publish(confirmed["subject"], json.dumps(confirmed).encode(),
                                                       headers=headers(confirmed))
        assert stored.stream == AGENT_STREAM
        control = await worker.nc.jetstream().pull_subscribe_bind(
            stream=AGENT_STREAM, consumer=consumer_name("agent_control", agent="researcher-2"))
        delivered, = await control.fetch(1, timeout=2)
        custody = fixtures.verify(delivered.data, confirmed, arrived(confirmed, delivered))
        assert (custody.frame.audience.id, custody.body.task_id) == ("researcher-2", confirmed["body"]["task_id"])
        await delivered.ack_sync(timeout=2)
        # The new tasks still wait in the inbox for a free slot; nothing was taken from it.
        inbox = await admin.nc.jetstream().consumer_info(AGENT_STREAM, consumer_name("agent_inbox", agent="researcher-2"))
        assert (inbox.num_pending, inbox.num_ack_pending) == (2, 0)
        assert lead.errors.empty() and services.errors.empty() and worker.errors.empty()


@pytest.mark.asyncio
async def test_a_host_may_hear_its_teams_agents_and_answers_each_request_once(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        await worker.subscribe_refused("ua.v1.team_other.*.ctl.agent.*")
        # Hosts listen only for the agents they run, so a request for an agent nobody runs fails at once.
        with pytest.raises(nats.errors.NoRespondersError):
            await lead.nc.request("ua.v1.team_example.lead-host-1.ctl.agent.researcher-3", b"{}", timeout=2)
        # The worker writes into the lead's inbox only to answer a request it received.
        await worker.publish_refused("_INBOX.lead-host-1.unsolicited")

        async def answer(message):
            await worker.nc.publish(message.reply, b"answer")
            await worker.nc.publish(message.reply, b"second answer")

        await subscribe_ready(worker, "ua.v1.team_example.*.ctl.agent.researcher-2", answer)
        response = await lead.nc.request("ua.v1.team_example.lead-host-1.ctl.agent.researcher-2", b"{}", timeout=2)
        assert response.data == b"answer"
        await worker.refused(response.subject)  # allow_responses permits one answer per request
        assert worker.errors.empty() and lead.errors.empty()


@pytest.mark.asyncio
async def test_ids_that_once_shared_a_consumer_name_get_separate_consumers(isolated_broker) -> None:
    """team_example/web and team/example_web joined with _ named one consumer."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        js = admin.nc.jetstream()
        await js.add_stream(config=stream_config("UA1_WORK"))
        pairs = [("team_example", "web"), ("team", "example_web")]
        spec = TRANSPORT["consumers"]["edge"]
        for team, edge in pairs:
            await js.add_consumer("UA1_WORK", config=ConsumerConfig(
                durable_name=spec["name"].format(team=team, edge=edge),
                filter_subject=spec["filter"].format(team=team, edge=edge), ack_policy=AckPolicy.EXPLICIT,
            ))
        for team, edge in pairs:
            info = await js.consumer_info("UA1_WORK", spec["name"].format(team=team, edge=edge))
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
        await js.add_stream(config=stream_config("UA1_AGENT_WORK_{team}"))
        await js.add_consumer(AGENT_STREAM, config=consumer_config("pool", pool="researcher"))
        pool = await js.pull_subscribe_bind(stream=AGENT_STREAM, consumer=consumer_name("pool", pool="researcher"))
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
        assert (await js.stream_info(AGENT_STREAM)).state.messages == 0
        assert lead.errors.empty()


@pytest.mark.asyncio
async def test_a_full_size_frame_is_stored_with_the_largest_headers_senders_may_set(isolated_broker) -> None:
    """README, "NATS headers": the stream's message limit leaves max_header_bytes for headers."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        await create_topology(admin)
        js = admin.nc.jetstream()
        largest = {"Nats-Msg-Id": "task.stop:" + "k" * 118, "UA-Kind": "k" * 84, "UA-Id": "0" * 36,
                   "traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01", "tracestate": "t" * 512}
        subject = f"ua.v1.{TEAM}.lead-host-1.work.agent.researcher-2"
        ack = await js.publish(subject, b"x" * TRANSPORT["limits"]["max_frame_bytes"], headers=largest)
        assert (await js.get_msg(AGENT_STREAM, ack.seq)).headers["tracestate"] == "t" * 512
        too_large = TRANSPORT["streams"]["UA1_AGENT_WORK_{team}"]["max_message_bytes"] + 1
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
        await js.add_stream(config=stream_config("UA1_WORK"))
        blind = await services.nc.subscribe(f"ua.v1.{TEAM}.*.svc.team", cb=blindly)
        await flush_fully(services.nc)
        await worker.nc.publish(address, b"{}", reply="$JS.API.STREAM.DELETE.UA1_WORK")
        await worker.nc.flush()
        async def stream_names() -> list[str]:
            return [info.config.name for info in await js.streams_info()]

        deadline = time.monotonic() + 2
        while "UA1_WORK" in await stream_names() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert "UA1_WORK" not in await stream_names()
        await blind.unsubscribe()

        dropped: list[tuple[str, str]] = []

        async def answer(message):
            subject = wire_frame.answer_subject(message.subject, message.reply)
            if subject is None:
                dropped.append((message.subject, message.reply))
                return
            await services.nc.publish(subject, b"", headers=refusal)

        await js.add_stream(config=stream_config("UA1_WORK"))
        await subscribe_ready(services, f"ua.v1.{TEAM}.*.svc.team", answer)
        for reply in ("$JS.API.STREAM.DELETE.UA1_WORK", f"$KV.UA1_LOCKS.team_services.{TEAM}", "_INBOX.lead-host-1.x"):
            await worker.nc.publish(address, b"{}", reply=reply)
        await worker.nc.flush()
        answered = await worker.nc.request(address, b"{}", timeout=2)
        assert answered.headers == refusal and answered.data == b""
        assert [subject for subject, _reply in dropped] == [address] * 3
        assert (await js.stream_info("UA1_WORK")).config.name == "UA1_WORK"
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
        agents = await worker.nc.jetstream().key_value("UA1_AGENTS")
        await agents.put(f"{TEAM}.researcher-2", b"idle")
        # Another host's liveness, an agent it does not run, a placement and the lock are refused.
        for subject in ("$KV.UA1_HOSTS.lead-host-1", f"$KV.UA1_AGENTS.{TEAM}.lead", f"$KV.UA1_PLACEMENT.{TEAM}.researcher-2",
                        f"$KV.UA1_LOCKS.team_services.{TEAM}", f"$KV.UA1_CERTS.session.{INSTALLATION}.{TEAM}.s-1"):
            await worker.publish_refused(subject)
        placement = await services.nc.jetstream().key_value("UA1_PLACEMENT")
        revision = await placement.create(f"{TEAM}.researcher-2", b"placed")
        await placement.update(f"{TEAM}.researcher-2", b"moved", last=revision)
        locks = await services.nc.jetstream().key_value("UA1_LOCKS")
        await locks.create(f"team_services.{TEAM}", b"team-services-1")
        with pytest.raises(nats.js.errors.KeyWrongLastSequenceError):
            await locks.create(f"team_services.{TEAM}", b"team-services-2")
        await services.publish_refused("$KV.UA1_PLACEMENT.another_team.researcher-2")
        assert (await (await worker.nc.jetstream().key_value("UA1_PLACEMENT")).get(f"{TEAM}.researcher-2")).value == b"moved"

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
        assert authority.errors.empty() and worker.errors.empty() and services.errors.empty()


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
        assert joining.errors.empty() and authority.errors.empty()


@pytest.mark.asyncio
async def test_a_result_and_its_team_services_copy_both_arrive_and_retries_of_each_are_dropped(isolated_broker) -> None:
    """A copy carries its own key: a stream drops repeats of one Nats-Msg-Id across all its subjects.

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
        await admin.nc.jetstream().add_consumer("UA1_WORK", config=consumer_config("team_services"))
        original = fixtures.example_frame("task.result", "completed")
        copy_frame = fixtures.example_frame("task.result", "copy-to-team-services")

        async def publish(frame: dict):
            return await worker.nc.jetstream().publish(frame["subject"], json.dumps(frame).encode(), headers=headers(frame))

        sent = [await publish(original), await publish(copy_frame)]
        retries = [await publish(original), await publish(copy_frame)]
        assert not any(ack.duplicate for ack in sent) and all(ack.duplicate for ack in retries)
        for client, stream, consumer, frame in (
                (lead, AGENT_STREAM, consumer_name("agent_inbox", agent="lead"), original),
                (services, "UA1_WORK", consumer_name("team_services"), copy_frame)):
            inbox = await client.nc.jetstream().pull_subscribe_bind(stream=stream, consumer=consumer)
            received, = await inbox.fetch(1, timeout=2)
            verified = fixtures.verify(received.data, frame, arrived(frame, received))
            assert verified.frame.audience.target == frame["audience"]["target"]
            await received.ack_sync(timeout=2)
        for stream in (AGENT_STREAM, "UA1_WORK"):
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
        await js.add_consumer(AGENT_STREAM, config=consumer_config("pool", pool="researcher"))
        await js.add_consumer("UA1_WORK", config=consumer_config("edge", edge="web"))
        run = fixtures.example_frame("task.request", "scheduled-run")
        result = fixtures.example_frame("task.result", "scheduled-run-to-person")
        await scheduler.nc.jetstream().publish(run["subject"], json.dumps(run).encode(), headers=headers(run))
        pool = await worker.nc.jetstream().pull_subscribe_bind(stream=AGENT_STREAM, consumer=consumer_name("pool", pool="researcher"))
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
        await js.add_consumer("UA1_WORK", config=consumer_config("team_services"))
        team_inbox = await services.nc.jetstream().pull_subscribe_bind(stream="UA1_WORK",
                                                                       consumer=consumer_name("team_services"))
        delivered_claim, = await team_inbox.fetch(1, timeout=2)
        claimed = fixtures.verify(delivered_claim.data, claim, arrived(claim, delivered_claim))
        assert (claimed.frame.issuer.agent, claimed.body.task_id) == ("researcher-2", task.body.task_id)
        await delivered_claim.ack_sync(timeout=2)
        confirmed = fixtures.example_frame("custody.confirmed", "claim-with-token")
        await services.nc.jetstream().publish(confirmed["subject"], json.dumps(confirmed).encode(), headers=headers(confirmed))
        control = await worker.nc.jetstream().pull_subscribe_bind(stream=AGENT_STREAM,
                                                                  consumer=consumer_name("agent_control", agent="researcher-2"))
        delivered, = await control.fetch(1, timeout=2)
        custody = fixtures.verify(delivered.data, confirmed, arrived(confirmed, delivered))
        assert custody.authority is not None and custody.authority.token.schedule_id == "sched_1"
        custody.authority.require_bound_to("root_task", task.body.work.root_task_id)
        await delivered.ack_sync(timeout=2)
        reply_to = task.body.work.reply_to
        assert (reply_to.target, reply_to.id) == ("edge", "web") and result["audience"]["id"] == reply_to.id
        await worker.nc.jetstream().publish(result["subject"], json.dumps(result).encode(), headers=headers(result))
        inbox = await gateway.nc.jetstream().pull_subscribe_bind(stream="UA1_WORK", consumer=consumer_name("edge", edge="web"))
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
        await subscribe_ready(worker, f"ua.v1.{TEAM}.*.ctl.agent.researcher-2", heard.put)

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

        await subscribe_ready(worker, request["subject"], respond)
        response = await lead.nc.request(request["subject"], json.dumps(request).encode(), timeout=2, headers=headers(request))
        answer = json.loads(response.data)
        verified = fixtures.verify(response.data, answer, arrived(answer, response))
        assert verified.frame.causation_id == request["message_id"] and verified.frame.issuer.agent == "researcher-2"
        assert verified.body.outcome == "ok" and verified.body.state == "paused"
        assert worker.errors.empty() and lead.errors.empty()
