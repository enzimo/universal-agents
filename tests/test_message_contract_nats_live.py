"""Exercise generated protocol 1 subjects and permissions on an authenticated broker."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import nats
import pytest

CONTRACT = Path(__file__).resolve().parents[1] / "contracts/messages/v1"
pytestmark = [pytest.mark.live, pytest.mark.nats]


@pytest.fixture
def contract_broker(tmp_path: Path):
    binary = shutil.which("nats-server")
    if binary is None:
        pytest.skip("nats-server 2.15.1 or later is required")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)", version)
    if match is None or tuple(map(int, match.groups())) < (2, 15, 1):
        pytest.skip("nats-server 2.15.1 or later is required")
    passwords = {role: secrets.token_hex(24) for role in ("worker_host", "installation_authority", "operator")}
    users = []
    for role, runtime in (("worker_host", "worker-host-1"), ("installation_authority", "authority-1")):
        document = json.loads((CONTRACT / f"generated/permissions/{role}.json").read_text())
        permissions = {operation: [subject.format(team="team_example", self=runtime) for subject in document[operation]]
                       for operation in ("publish", "subscribe")}
        users.append({"user": role, "password": passwords[role], "permissions": permissions})
    users.append({"user": "operator", "password": passwords["operator"]})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "nats.conf"
    config.write_text(json.dumps({"listen": f"127.0.0.1:{port}", "authorization": {"users": users},
                                 "jetstream": {"store_dir": str(tmp_path / "jetstream")}}))
    config.chmod(0o600)
    # Credentials stay in a private file, never in process arguments or output.
    process = subprocess.Popen([binary, "--config", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None:
                pytest.fail("the isolated protocol contract broker exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("the isolated protocol contract broker did not start within 5 seconds")
                time.sleep(0.02)
        yield f"nats://127.0.0.1:{port}", passwords
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_generated_contract_allows_join_and_retains_raw_dead_letters(contract_broker) -> None:
    async def exercise() -> None:
        url, passwords = contract_broker
        clients = []
        errors = asyncio.Queue()

        async def error_cb(error):
            await errors.put(error)

        async def connect(role, runtime):
            client = await nats.connect(url, user=role, password=passwords[role],
                                        inbox_prefix=f"_INBOX.{runtime}".encode(), error_cb=error_cb,
                                        allow_reconnect=False, connect_timeout=2)
            clients.append(client)
            return client

        try:
            authority = await connect("installation_authority", "authority-1")
            worker = await connect("worker_host", "worker-host-1")
            operator = await connect("operator", "operator-1")
            api = json.loads((CONTRACT / "generated/asyncapi.json").read_text())
            permissions = json.loads((CONTRACT / "generated/permissions/installation_authority.json").read_text())
            for operation in ("join", "renew"):
                pattern = next(subject for subject in permissions["subscribe"] if subject.endswith(f".host.{operation}"))
                subscription = await authority.subscribe(pattern)
                await authority.flush()
                subject = api["channels"][f"host_{operation}"]["address"].format(sender="worker-host-1")
                await worker.publish(subject, operation.encode())
                await worker.flush()
                assert (await subscription.next_msg(timeout=1)).data == operation.encode()

            transport = json.loads((CONTRACT / "generated/transport.json").read_text())
            stream = transport["classes"]["dead_letter"]["stream"]
            await operator.jetstream().add_stream(name=stream, subjects=transport["streams"][stream]["subjects"])
            raw = b"{a refused message must remain byte-for-byte identical\x00"
            copy = "c" * 64
            raw_subject = f"ua.v1.team_example.worker-host-1.dlq.raw.{copy}"
            await worker.jetstream().publish(raw_subject, raw, timeout=1)
            notice_subject = f"ua.v1.team_example.worker-host-1.dlq.notice.{copy}"
            notice = (CONTRACT / "examples/valid/dlq.notice/bad-signature.json").read_bytes()
            await worker.jetstream().publish(notice_subject, notice, timeout=1)
            assert (await operator.jetstream().get_last_msg(stream, raw_subject)).data == raw
            assert (await operator.jetstream().get_last_msg(stream, notice_subject)).data == notice

            other_subject = raw_subject.replace("worker-host-1", "another-host")
            await worker.publish(other_subject, raw)
            await worker.flush()
            denied = await asyncio.wait_for(errors.get(), timeout=1)
            assert "permissions violation" in str(denied).lower()
            assert other_subject in str(denied)
            with pytest.raises(nats.js.errors.NotFoundError):
                await operator.jetstream().get_last_msg(stream, other_subject)
        finally:
            for client in reversed(clients):
                await client.close()

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
