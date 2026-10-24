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
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import nats
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig

from custom_messages.wire.frame import Receiver, TrustedKey, b64url_decode, sign_frame, verify_frame

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
CONTRACT = Path(__file__).resolve().parents[1] / "contracts/messages/v1"
KEYS = json.loads((CONTRACT / "examples/keys.json").read_text())["keys"]
TRANSPORT = json.loads((CONTRACT / "generated/transport.json").read_text())
NOW = datetime(2026, 10, 22, 4, 30, 1, tzinfo=timezone.utc)
TEAM = "team_example"
RETENTION = {"workqueue": RetentionPolicy.WORK_QUEUE, "limits": RetentionPolicy.LIMITS}
STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}
# Which host runs which agents in these tests.
HOSTS = {
    "lead": {"role": "lead_host", "runtime": "lead-host-1", "agents": ["lead"]},
    "worker": {"role": "worker_host", "runtime": "worker-host-1", "agents": ["researcher-2"]},
}


def expand(template: str, bindings: dict[str, list[str]]) -> list[str]:
    """Fill each {placeholder} with every value bound to it; identities never become wildcards."""

    subjects = [template]
    for name, values in bindings.items():
        subjects = [subject.replace("{" + name + "}", value) for subject in subjects for value in values] \
            if "{" + name + "}" in template else subjects
    leftover = [subject for subject in subjects if re.search(r"\{[a-z_]+\}", subject)]
    assert not leftover, f"unbound placeholders in {leftover}"
    return subjects


def permissions(user: str) -> dict:
    host = HOSTS[user]
    document = json.loads((CONTRACT / "generated/permissions" / f"{host['role']}.json").read_text())
    bindings = {"team": [TEAM], "self": [host["runtime"]], "agent": host["agents"]}
    granted = {action: sorted({subject for template in document[action] for subject in expand(template, bindings)})
               for action in ("publish", "subscribe")}
    if "allow_responses" in document:
        granted["allow_responses"] = document["allow_responses"]
    return granted


def consumer_name(template: str, **ids: str) -> str:
    return TRANSPORT["consumers"][template]["name"].format(team=TEAM, **ids)


def stream_config(name: str) -> StreamConfig:
    """The stream exactly as transport.yaml describes it."""

    spec = TRANSPORT["streams"][name]
    return StreamConfig(
        name=name,
        subjects=spec["subjects"],
        retention=RETENTION[spec["retention"]],
        storage=STORAGE[spec["storage"]],
        max_age=spec.get("max_age_seconds"),
        duplicate_window=spec.get("duplicate_window_seconds"),
        max_consumers=spec.get("max_consumers"),
        max_msg_size=spec.get("max_message_bytes"),
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


def example(kind: str, name: str = "basic") -> dict:
    return json.loads((CONTRACT / "examples/valid" / kind / f"{name}.json").read_text())


def signed(frame: dict) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(b64url_decode(KEYS[frame["issuer"]["key_id"]]["seed"]))
    return json.dumps(sign_frame(frame, key)).encode()


def trusted_keys() -> dict[str, TrustedKey]:
    return {key_id: TrustedKey(
        Ed25519PrivateKey.from_private_bytes(b64url_decode(spec["seed"])).public_key(),
        spec["team"], spec["runtime"],
    ) for key_id, spec in KEYS.items()}


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


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def isolated_broker(tmp_path: Path):
    executable = nats_server()
    port = free_port()
    passwords = {name: secrets.token_urlsafe(24) for name in ("admin", *HOSTS)}
    users = [{"user": "admin", "password": passwords["admin"]}]
    users += [{"user": name, "password": passwords[name], "permissions": permissions(name)} for name in HOSTS]
    settings = {
        "host": "127.0.0.1", "port": port,
        "jetstream": {"store_dir": str(tmp_path / "store"), **TRANSPORT["server"]["jetstream"]},
        "authorization": {"users": users},
    }
    config = tmp_path / "nats.json"
    config.write_text(json.dumps(settings))
    config.chmod(0o600)
    with (tmp_path / "nats.log").open("w") as log:
        process = subprocess.Popen([executable, "-c", str(config)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    pytest.fail(f"isolated NATS exited during startup: {(tmp_path / 'nats.log').read_text()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("isolated NATS did not start within five seconds")
                    time.sleep(0.02)
            yield f"nats://127.0.0.1:{port}", passwords
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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
    """Subscribe and wait until the server holds the subscription.

    nats-py 2.16.0 queues SUB in its outgoing buffer for a writer task, but
    flush() writes its PING straight to the socket. One PONG therefore does not
    prove the server has the subscription, and a request sent from another
    connection right after can get "no responders" (about 3% of runs under
    load). While the first flush waits, the writer task sends the queued SUB, so
    the second PING follows it on the wire.
    """

    await client.nc.subscribe(subject, cb=cb)
    await client.nc.flush()
    await client.nc.flush()


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


async def create_topology(admin: Client, agents: Iterable[str]) -> None:
    """What the topology owner does: the work stream and one inbox consumer per agent."""

    js = admin.nc.jetstream()
    expected = stream_config("UA1_WORK")
    info = await js.add_stream(config=expected)
    for setting in ("subjects", "retention", "storage", "max_age", "duplicate_window", "max_consumers", "max_msg_size"):
        assert getattr(info.config, setting) == getattr(expected, setting), setting
    for agent in agents:
        consumer = await js.add_consumer("UA1_WORK", config=consumer_config("agent_inbox", agent=agent))
        for setting in ("filter_subject", "ack_policy", "ack_wait", "max_deliver"):
            assert getattr(consumer.config, setting) == getattr(consumer_config("agent_inbox", agent=agent), setting)


@pytest.mark.asyncio
async def test_generated_grants_allow_pull_ack_and_refuse_other_consumers(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        await create_topology(admin, ["researcher-2", "researcher-3"])
        own, other = consumer_name("agent_inbox", agent="researcher-2"), consumer_name("agent_inbox", agent="researcher-3")

        frame = example("task.stop")
        await lead.nc.jetstream().publish(frame["subject"], signed(frame))
        worker_js = worker.nc.jetstream()
        await worker_js.consumer_info("UA1_WORK", own)
        subscription = await worker_js.pull_subscribe_bind(stream="UA1_WORK", consumer=own)
        delivered, = await subscription.fetch(1, timeout=2)
        checked = verify_frame(delivered.data, keys=trusted_keys(), arrived_on=delivered.subject,
                               receiver=Receiver(**frame["audience"]), now=NOW)
        assert checked.body.task_id == frame["body"]["task_id"]
        await delivered.ack_sync(timeout=2)
        info = await admin.nc.jetstream().consumer_info("UA1_WORK", own)
        assert info.num_ack_pending == 0 and info.num_pending == 0

        await worker.publish_refused(f"$JS.API.CONSUMER.MSG.NEXT.UA1_WORK.{other}")
        await admin.nc.jetstream().publish("ua.v1.team_example.lead-host-1.work.agent.researcher-3", b"other consumer's work")
        other_sub = await admin.nc.jetstream().pull_subscribe_bind(stream="UA1_WORK", consumer=other)
        other_delivery, = await other_sub.fetch(1, timeout=2)
        await worker.publish_refused(other_delivery.reply, b"+ACK")
        assert (await admin.nc.jetstream().consumer_info("UA1_WORK", other)).num_ack_pending == 1
        await other_delivery.ack_sync(timeout=2)
        assert worker.errors.empty() and lead.errors.empty()


@pytest.mark.asyncio
async def test_workers_cannot_manage_streams_speak_for_others_or_read_others_traffic(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        worker = await connect(stack, isolated_broker, "worker")
        await create_topology(admin, ["researcher-2"])
        own = consumer_name("agent_inbox", agent="researcher-2")
        for subject in (
            f"$JS.API.CONSUMER.CREATE.UA1_WORK.{own}",
            f"$JS.API.CONSUMER.DURABLE.CREATE.UA1_WORK.{own}",
            f"$JS.API.CONSUMER.DELETE.UA1_WORK.{own}",
            "$JS.API.STREAM.INFO.UA1_WORK",
            "$JS.API.STREAM.MSG.GET.UA1_WORK",
            "ua.v1.team_example.lead-host-1.work.agent.researcher-2",
            "ua.v1.team_example.lead-host-1.ctl.agent.researcher-2.pause",
        ):
            await worker.publish_refused(subject)
        for subject in (
            "_INBOX.lead-host-1.>",
            "ua.v1.team_example.lead-host-1.work.agent.researcher-2",
            "ua.v1.team_example.*.work.>",
        ):
            await worker.subscribe_refused(subject)
        assert worker.errors.empty()


@pytest.mark.asyncio
async def test_a_host_gets_only_its_own_agents_requests_and_answers_only_requests(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        await worker.subscribe_refused("ua.v1.team_example.*.ctl.agent.researcher-3.pause")
        await worker.subscribe_refused("ua.v1.team_example.*.ctl.agent.*.pause")
        # Nobody else may receive researcher-3's control requests, so the lead learns at once.
        with pytest.raises(nats.errors.NoRespondersError):
            await lead.nc.request("ua.v1.team_example.lead-host-1.ctl.agent.researcher-3.pause", b"{}", timeout=2)
        # The worker writes into the lead's inbox only to answer a request it received.
        await worker.publish_refused("_INBOX.lead-host-1.unsolicited")

        async def answer(message):
            await worker.nc.publish(message.reply, b"answer")
            await worker.nc.publish(message.reply, b"second answer")

        await subscribe_ready(worker, "ua.v1.team_example.*.ctl.agent.researcher-2.pause", answer)
        response = await lead.nc.request("ua.v1.team_example.lead-host-1.ctl.agent.researcher-2.pause", b"{}", timeout=2)
        assert response.data == b"answer"
        await worker.refused(response.subject)  # allow_responses permits one answer per request
        assert worker.errors.empty() and lead.errors.empty()


@pytest.mark.asyncio
async def test_ids_that_once_shared_a_consumer_name_get_separate_consumers(isolated_broker) -> None:
    """team_example/researcher-2 and team/example_researcher-2 joined with _ named one consumer."""

    async with AsyncExitStack() as stack:
        admin = await connect(stack, isolated_broker, "admin")
        js = admin.nc.jetstream()
        await js.add_stream(config=stream_config("UA1_WORK"))
        pairs = [("team_example", "researcher-2"), ("team", "example_researcher-2")]
        spec = TRANSPORT["consumers"]["agent_inbox"]
        for team, agent in pairs:
            await js.add_consumer("UA1_WORK", config=ConsumerConfig(
                durable_name=spec["name"].format(team=team, agent=agent),
                filter_subject=spec["filter"].format(team=team, agent=agent), ack_policy=AckPolicy.EXPLICIT,
            ))
        for team, agent in pairs:
            info = await js.consumer_info("UA1_WORK", spec["name"].format(team=team, agent=agent))
            assert info.config.filter_subject == spec["filter"].format(team=team, agent=agent)


@pytest.mark.asyncio
async def test_signed_request_reply_uses_the_real_nats_inbox(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        lead = await connect(stack, isolated_broker, "lead")
        worker = await connect(stack, isolated_broker, "worker")
        request = example("agent.pause")

        async def respond(message):
            verified = verify_frame(message.data, keys=trusted_keys(), arrived_on=message.subject,
                                    receiver=Receiver(**request["audience"]), now=NOW)
            reply = example("agent.pause.reply", "paused")
            reply.update(subject=message.reply, causation_id=verified.frame.message_id)
            await worker.nc.publish(message.reply, signed(reply))

        await subscribe_ready(worker, request["subject"], respond)
        response = await lead.nc.request(request["subject"], signed(request), timeout=2)
        verified = verify_frame(response.data, keys=trusted_keys(), arrived_on=response.subject,
                                receiver=Receiver("team_example", "agent", "lead", "lead-host-1"), now=NOW)
        assert verified.frame.causation_id == request["message_id"]
        assert verified.body.outcome == "ok" and verified.body.state == "paused"
        assert worker.errors.empty() and lead.errors.empty()
