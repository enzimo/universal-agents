"""The launcher's start and restart of the real installation authority and team services on a real NATS server.

docker.message_hosts.MessageServices with ``assistant_cli run-installation-authority`` and
``assistant_cli run-team-services`` as real processes: the launcher goes on only once the
team services serve, a clean stop releases the team's lock so a restart serves at once, and
after an unclean stop the launcher waits until the stopped instance's lease runs out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import nats
import pytest
from nats.js.errors import KeyNotFoundError

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_messages.wire.values import parse_timestamp
from docker import message_hosts
from docker.launch_headless import message_hosts_to_approve, terminate_process
from env_secrets import PROCESS_INSTALLATION_AUTHORITY, PROCESS_TEAM_SERVICES, environment_for
from team_services.lock import LEASE, LOCKS_BUCKET, lock_key
from team_services.process import TEAM_SERVICES_RUNTIME
from team_services.serving import serving_process
from tests.test_live_message_contract import nats_server

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]
TEAM = "team_one"
REPO = Path(__file__).resolve().parents[1]


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


@pytest.fixture
def services(broker: str, tmp_path: Path):
    """What the launcher's main builds, for an instance whose NATS is ``broker``."""

    config = GatewayInstanceConfig.create(instance_name="services-test", instance_root=str(tmp_path / "instance"),
                                          team_id=TEAM, timezone="UTC")
    config.save()
    env = {key: value for key, value in os.environ.items() if not key.startswith("NATS_")}
    env["NATS_URL"] = broker
    started = message_hosts.MessageServices(
        config.layout,
        lambda: message_hosts_to_approve(GatewayInstanceConfig.load_for_process(config.config_path)),
        message_hosts.build_installation_authority_command(config.config_path, repo_root=REPO),
        environment_for(PROCESS_INSTALLATION_AUTHORITY, env),
        message_hosts.build_team_services_command(config.config_path, repo_root=REPO),
        environment_for(PROCESS_TEAM_SERVICES, env),
        cwd=REPO,
    )
    try:
        yield started
    finally:
        started.stop(terminate_process)


def lock_record(url: str) -> dict | None:
    """The team services' lock as NATS holds it; ``None`` when no instance holds it."""

    async def read() -> dict | None:
        connection = await nats.connect(url, allow_reconnect=False)
        try:
            bucket = await connection.jetstream().key_value(LOCKS_BUCKET)
            try:
                entry = await bucket.get(lock_key(TEAM))
            except KeyNotFoundError:
                return None
            return json.loads(entry.value) if entry.value else None
        finally:
            await connection.close()

    return asyncio.run(read())


def assert_serving(services: message_hosts.MessageServices, url: str) -> dict:
    assert services.team_services is not None and services.team_services.poll() is None
    assert serving_process(services.layout) == services.team_services.pid
    held = lock_record(url)
    assert held is not None and held["runtime"] == TEAM_SERVICES_RUNTIME
    return held


def test_a_clean_stop_releases_the_lock_so_a_restart_serves_at_once(services: message_hosts.MessageServices,
                                                                    broker: str) -> None:
    assert services.start() is True
    first = assert_serving(services, broker)

    services.stop(terminate_process)
    assert lock_record(broker) is None, "a cleanly stopped instance releases the team's lock"

    started = time.monotonic()
    assert services.start() is True  # as a restart of everything does
    assert time.monotonic() - started < LEASE.total_seconds(), "the new instance did not wait out a lease"
    assert assert_serving(services, broker)["instance"] != first["instance"]


def test_after_an_unclean_stop_the_launcher_waits_until_the_stopped_instances_lease_runs_out(
    services: message_hosts.MessageServices, broker: str, caplog
) -> None:
    assert services.start() is True
    assert_serving(services, broker)
    assert services.team_services is not None
    services.team_services.send_signal(signal.SIGKILL)  # an unclean stop: the lock stays held until its lease ends
    services.team_services.wait(timeout=5)
    held = lock_record(broker)
    assert held is not None
    lease_free_at = parse_timestamp(held["lease_ends_at"]).timestamp() + CLOCK_SKEW_SECONDS
    services.stop(terminate_process)

    with caplog.at_level(logging.INFO, logger="docker.message_hosts"):
        assert services.start() is True
    # The launcher went on only once the new instance served, which it can do only after the dead one's lease,
    # and said meanwhile what it waited for.
    assert time.time() >= lease_free_at
    assert "Still waiting for the team services" in caplog.text and "event=team_services_started" in caplog.text
    assert assert_serving(services, broker)["instance"] != held["instance"]
