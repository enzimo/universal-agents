"""Protocol 1 permissions and signed frames work together on an isolated broker.

The broker needs a different authorization file from the running team's broker.
These tests therefore start their own nats-server, with generated role grants
and public fixture signing keys. No operator credentials or live model are used.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import socket
import subprocess
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

import nats
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nats.js.api import ConsumerConfig, RetentionPolicy, StorageType, StreamConfig

from custom_messages.wire.frame import Receiver, TrustedKey, b64url_decode, sign_frame, verify_frame

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
CONTRACT = Path(__file__).resolve().parents[1] / "contracts/messages/v1"
KEYS = json.loads((CONTRACT / "examples/keys.json").read_text())["keys"]
NOW = datetime(2026, 10, 22, 4, 30, 1, tzinfo=timezone.utc)
CONSUMER = "agent_team_example_researcher-2"
OTHER_CONSUMER = "agent_team_example_researcher-3"


def permissions(role: str, runtime: str, agent: str) -> dict:
    document = json.loads((CONTRACT / "generated/permissions" / f"{role}.json").read_text())
    bindings = {"team": "team_example", "self": runtime, "agent": agent}
    return {action: [subject.format(**bindings) for subject in document[action]]
            for action in ("publish", "subscribe")}


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


@pytest.fixture
def isolated_broker(tmp_path: Path):
    executable = shutil.which("nats-server")
    if executable is None:
        pytest.skip("isolated protocol permission checks require nats-server on PATH")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    passwords = {name: secrets.token_urlsafe(24) for name in ("admin", "lead", "worker")}
    settings = {
        "host": "127.0.0.1", "port": port,
        "jetstream": {"store_dir": str(tmp_path / "store")},
        "authorization": {"users": [
            {"user": "admin", "password": passwords["admin"]},
            {"user": "lead", "password": passwords["lead"],
             "permissions": permissions("lead_host", "lead-host-1", "lead")},
            {"user": "worker", "password": passwords["worker"],
             "permissions": permissions("worker_host", "worker-host-1", "researcher-2")},
        ]},
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
                    pytest.fail("isolated NATS exited during startup")
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


async def connection(stack: AsyncExitStack, broker, user: str, errors: asyncio.Queue):
    url, passwords = broker
    runtime = "lead-host-1" if user == "lead" else "worker-host-1"
    client = await nats.connect(
        url, user=user, password=passwords[user], inbox_prefix=f"_INBOX.{runtime}".encode(),
        error_cb=errors.put, allow_reconnect=False,
    )
    stack.push_async_callback(client.close)
    return client


async def denied(client, errors: asyncio.Queue, subject: str, payload: bytes = b"{}") -> None:
    await client.publish(subject, payload, reply="_INBOX.worker-host-1.denied")
    await client.flush()
    error = await asyncio.wait_for(errors.get(), timeout=2)
    assert "permissions violation" in str(error)


@pytest.mark.asyncio
async def test_generated_grants_allow_pull_ack_and_refuse_other_consumers(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        errors = asyncio.Queue()
        admin = await connection(stack, isolated_broker, "admin", errors)
        lead = await connection(stack, isolated_broker, "lead", errors)
        worker = await connection(stack, isolated_broker, "worker", errors)
        js = admin.jetstream()
        await js.add_stream(config=StreamConfig(
            name="UA1_WORK", subjects=["ua.v1.*.*.work.>"],
            retention=RetentionPolicy.WORK_QUEUE, storage=StorageType.FILE,
        ))
        for agent, name in (("researcher-2", CONSUMER), ("researcher-3", OTHER_CONSUMER)):
            await js.add_consumer("UA1_WORK", config=ConsumerConfig(
                durable_name=name, filter_subject=f"ua.v1.team_example.*.work.agent.{agent}", ack_policy="explicit",
            ))

        frame = example("task.stop")
        await lead.jetstream().publish(frame["subject"], signed(frame))
        worker_js = worker.jetstream()
        await worker_js.consumer_info("UA1_WORK", CONSUMER)
        subscription = await worker_js.pull_subscribe_bind(stream="UA1_WORK", consumer=CONSUMER)
        delivered, = await subscription.fetch(1, timeout=2)
        checked = verify_frame(delivered.data, keys=trusted_keys(), arrived_on=delivered.subject,
                               receiver=Receiver(**frame["audience"]), now=NOW)
        assert checked.body.task_id == frame["body"]["task_id"]
        await delivered.ack_sync(timeout=2)
        info = await js.consumer_info("UA1_WORK", CONSUMER)
        assert info.num_ack_pending == 0 and info.num_pending == 0
        assert errors.empty()

        await denied(worker, errors, f"$JS.API.CONSUMER.MSG.NEXT.UA1_WORK.{OTHER_CONSUMER}")
        await js.publish("ua.v1.team_example.lead-host-1.work.agent.researcher-3", b"other consumer's work")
        other = await js.pull_subscribe_bind(stream="UA1_WORK", consumer=OTHER_CONSUMER)
        other_delivery, = await other.fetch(1, timeout=2)
        await denied(worker, errors, other_delivery.reply, b"+ACK")
        assert (await js.consumer_info("UA1_WORK", OTHER_CONSUMER)).num_ack_pending == 1
        await other_delivery.ack_sync(timeout=2)
        assert errors.empty()


@pytest.mark.asyncio
async def test_signed_request_reply_uses_the_real_nats_inbox(isolated_broker) -> None:
    async with AsyncExitStack() as stack:
        errors = asyncio.Queue()
        lead = await connection(stack, isolated_broker, "lead", errors)
        worker = await connection(stack, isolated_broker, "worker", errors)
        request = example("agent.pause")

        async def respond(message):
            verified = verify_frame(message.data, keys=trusted_keys(), arrived_on=message.subject,
                                    receiver=Receiver(**request["audience"]), now=NOW)
            reply = example("agent.pause.reply", "paused")
            reply.update(subject=message.reply, causation_id=verified.frame.message_id)
            await worker.publish(message.reply, signed(reply))

        await worker.subscribe(request["subject"], cb=respond)
        await worker.flush()
        response = await lead.request(request["subject"], signed(request), timeout=2)
        verified = verify_frame(response.data, keys=trusted_keys(), arrived_on=response.subject,
                                receiver=Receiver("team_example", "agent", "lead", "lead-host-1"), now=NOW)
        assert verified.frame.causation_id == request["message_id"]
        assert verified.body.outcome == "ok" and verified.body.state == "paused"
        assert errors.empty()
