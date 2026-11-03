"""The installation authority on a real NATS server: topology, joins, renewals and restarts.

installation_authority.service with custom_transport.protocol1.joining; the message
contract's README "Installation traffic" and "Trust over time".
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from contextlib import AsyncExitStack
from pathlib import Path

import nats
import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_transport.protocol1 import host_keys
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import HostProfile, JoinRefused, join
from custom_transport.protocol1.live import LiveHost, utc_now
from custom_transport.protocol1.trust_state import PersistentTrustStore
from docker import message_hosts
from env_secrets import PROCESS_INSTALLATION_AUTHORITY, environment_for
from installation_authority.expected_hosts import ExpectedHost, write_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority
from runtime_settings.instance_layout import InstanceLayout
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
INSTALLATION, TEAM = "install_one", "team_one"
WORKER = HostProfile("worker-host-1", (TEAM,), ("worker_host",), ("researcher",), 4)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture
def broker(tmp_path: Path):
    port = free_port()
    with (tmp_path / "nats.log").open("w") as log:
        process = subprocess.Popen([nats_server(), "-js", "-sd", str(tmp_path / "store"), "-a", "127.0.0.1", "-p", str(port)],
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("NATS did not start within five seconds")
                    time.sleep(0.02)
            yield f"nats://127.0.0.1:{port}"
        finally:
            process.terminate()
            process.wait(timeout=5)


def prepare(layout: InstanceLayout) -> host_keys.HostKeys:
    """What the launcher does before it starts the authority: keys, the expected hosts and the bundle."""

    authority, _ = host_keys.ensure_authority_keys(layout, INSTALLATION)
    host_keys.ensure_host_keys(layout, AUTHORITY_RUNTIME)
    worker, _ = host_keys.ensure_host_keys(layout, WORKER.runtime)
    write_expected_hosts(layout.trust_expected_hosts_path, {WORKER.runtime: ExpectedHost(
        WORKER.runtime, worker.key_id, worker.public_key_text, frozenset(WORKER.roles), frozenset(WORKER.teams),
        frozenset(WORKER.agent_types), WORKER.session_limit)})
    write_bundle(layout.trust_bootstrap_path, BootstrapBundle(INSTALLATION, ((authority.key_id, authority.public_key_text),)))
    return worker


async def connect(stack: AsyncExitStack, url: str, runtime: str):
    connection = await nats.connect(url, inbox_prefix=f"_INBOX.{runtime}".encode(), allow_reconnect=False)
    stack.push_async_callback(connection.close)
    return connection


async def start_authority(stack: AsyncExitStack, url: str, layout: InstanceLayout) -> InstallationAuthority:
    authority = InstallationAuthority(layout, host_keys.load_authority_keys(layout),
                                      host_keys.load_host_keys(layout, AUTHORITY_RUNTIME))
    await authority.start(await connect(stack, url, AUTHORITY_RUNTIME), teams=[TEAM])
    return authority


def host_trust(layout: InstanceLayout, runtime: str) -> PersistentTrustStore:
    return PersistentTrustStore.open(read_bundle(layout.trust_bootstrap_path), layout.host_trust_dir(runtime), utc_now())


@pytest.mark.asyncio
async def test_a_listed_host_joins_and_others_are_refused(broker: str, tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        await start_authority(stack, broker, layout)
        # The bundle now carries the authority's certificate and a current revocations record.
        records = {record["record"] for record in read_bundle(layout.trust_bootstrap_path).records}
        assert records == {"certificate", "revocations"}
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        assert (certificate.runtime, certificate.membership_epoch, certificate.roles) == ("worker-host-1", 1,
                                                                                          frozenset({"worker_host"}))
        assert certificate.teams == frozenset({TEAM}) and certificate.session_limit == 4
        # Asking again with the same key hands back the same certificate.
        assert (await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), WORKER)).membership_epoch == 1
        # A host nobody listed, a listed host asking for more, and another key for a listed runtime.
        stranger_keys, _ = host_keys.ensure_host_keys(layout, "stranger-1")
        stranger = HostProfile("stranger-1", (TEAM,), ("worker_host",))
        with pytest.raises(JoinRefused) as refused:
            await join(await connect(stack, broker, "stranger-1"), stranger_keys, host_trust(layout, "stranger-1"), stranger)
        assert refused.value.outcome == "denied" and "launcher" in refused.value.message
        greedy = HostProfile(WORKER.runtime, (TEAM,), ("worker_host", "lead_host"), ("researcher",), 4)
        with pytest.raises(JoinRefused) as refused:
            await join(worker_nc, worker_keys, host_trust(layout, WORKER.runtime), greedy)
        assert refused.value.outcome == "denied"


@pytest.mark.asyncio
async def test_a_renewal_raises_the_epoch_and_the_authority_keeps_its_records_across_a_restart(broker: str,
                                                                                               tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    worker_keys = prepare(layout)
    async with AsyncExitStack() as stack:
        first = await start_authority(stack, broker, layout)
        worker_nc = await connect(stack, broker, WORKER.runtime)
        trust = host_trust(layout, WORKER.runtime)
        certificate = await join(worker_nc, worker_keys, trust, WORKER)
        worker = LiveHost(worker_nc, HostIdentity(worker_keys, certificate), trust)
        reply = await worker.request("host.renew", {
            "renew_id": "renew-1", "certificate_id": certificate.certificate_id, "membership_epoch": 1,
            "profile_sha256": WORKER.digest(), "contract_sha256": json.loads(
                (Path(__file__).resolve().parents[1] / "contracts/messages/v1/generated/compatibility.json").read_text())[
                "contract_sha256"]},
            {"installation": INSTALLATION, "team": "_install", "target": "installation_service", "id": "authority"},
            team="_install")
        renewed = reply.raw["body"]["certificate"]["body"]
        assert renewed["membership_epoch"] == 2 and renewed["replaces"] == certificate.certificate_id
        sequence = first._revocations["sequence"]
        own_epoch = first._own["body"]["membership_epoch"]
        # A restarted authority keeps its own certificate and signs revocations with a higher sequence.
        second = await start_authority(stack, broker, layout)
        assert second._own["body"]["membership_epoch"] == own_epoch
        assert second._revocations["sequence"] > sequence
        js = worker_nc.jetstream()
        hosts = await js.stream_info("KV_UA1_HOSTS")
        assert hosts.config.max_age == 60
        dead_letters = await js.consumer_info(f"UA1_DLQ_{TEAM}", f"dlq~{TEAM}")
        assert dead_letters.config.filter_subject == f"ua.v1.{TEAM}.*.dlq.notice.*"


@pytest.mark.asyncio
async def test_the_launcher_starts_the_real_authority_and_a_listed_host_joins(broker: str, tmp_path: Path,
                                                                              monkeypatch) -> None:
    """docker.message_hosts with ``assistant_cli run-installation-authority`` as a real process."""

    config = GatewayInstanceConfig.create(instance_name="authority-test", instance_root=str(tmp_path / "instance"),
                                          team_id=TEAM, timezone="UTC")
    config.save()
    layout = config.layout
    worker_keys, _ = host_keys.ensure_host_keys(layout, WORKER.runtime)
    expected = {WORKER.runtime: ExpectedHost(WORKER.runtime, worker_keys.key_id, worker_keys.public_key_text,
                                             frozenset(WORKER.roles), frozenset(WORKER.teams), frozenset(WORKER.agent_types),
                                             WORKER.session_limit)}
    installation = message_hosts.prepare_message_host_trust(layout, expected)
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    env["NATS_URL"] = broker
    repo = Path(__file__).resolve().parents[1]
    process = message_hosts.start_installation_authority(
        layout, message_hosts.build_installation_authority_command(config.config_path, repo_root=repo),
        environment_for(PROCESS_INSTALLATION_AUTHORITY, env), cwd=repo, timeout_seconds=60)
    try:
        async with AsyncExitStack() as stack:
            trust = host_trust(layout, WORKER.runtime)
            assert trust.store.installation == installation
            certificate = await join(await connect(stack, broker, WORKER.runtime), worker_keys, trust, WORKER)
            assert certificate.installation == installation and certificate.membership_epoch == 1
    finally:
        process.terminate()
        assert process.wait(timeout=10) == 0
