"""Start an owned test service on an explicit, restart-stable loopback port."""

import argparse
import logging
import os
import socket
import subprocess
from pathlib import Path

LABEL = "com.universalagents.ci.job"
LOG = logging.getLogger("ua.ci.service")


def docker(*args):
    return subprocess.run(
        ["rtk", "proxy", "docker", *args], text=True, capture_output=True, timeout=180
    )


def start(service):
    job_id = os.environ["UA_CI_ID"]
    name = os.environ[
        "NATS_DOCKER_CONTAINER" if service == "nats" else "UA_CI_POSTGRES"
    ]
    container_port = 4222 if service == "nats" else 5432
    image_args = (
        ["nats:2.15.1-alpine", "--jetstream"]
        if service == "nats"
        else [
            "--env",
            "POSTGRES_DB=ua_live_tests",
            "--env",
            "POSTGRES_PASSWORD=postgres",
            "--env",
            "POSTGRES_USER=postgres",
            "postgres:16-alpine",
        ]
    )
    (Path(os.environ["UA_CI_CACHE"]) / "docker-used").touch()
    for attempt in range(5):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        # Docker-assigned ephemeral ports change on restart. An explicit port
        # is persisted in HostConfig. Retry only a race with another binder.
        result = docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{LABEL}={job_id}",
            "--publish",
            f"127.0.0.1:{port}:{container_port}",
            *image_args,
        )
        if result.returncode == 0:
            break
        if not any(
            message in result.stderr.lower()
            for message in (
                "address already in use",
                "port is already allocated",
            )
        ):
            raise RuntimeError(result.stderr)
        owner = docker(
            "inspect", "--format", '{{index .Config.Labels "' + LABEL + '"}}', name
        )
        if owner.returncode or owner.stdout.strip() != job_id:
            raise RuntimeError(
                "Port allocation failed without a verifiably owned container"
            )
        removed = docker("rm", "--force", "--volumes", name)
        if removed.returncode:
            raise RuntimeError(removed.stderr)
        LOG.warning(
            "Port %s was claimed before Docker bound it; retry %s", port, attempt + 1
        )
    else:
        raise RuntimeError(
            "Unable to allocate a loopback service port after five attempts"
        )
    if service == "nats":
        endpoint = f"nats://127.0.0.1:{port}"
        values = {"NATS_URL": endpoint, "NATS_SERVERS": endpoint}
    else:
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/ua_live_tests"
        values = {"POSTGRES_DSN": dsn, "LIVE_TEST_POSTGRES_DSN": dsn}
    with Path(os.environ["GITHUB_ENV"]).open("a") as stream:
        stream.writelines(f"{key}={value}\n" for key, value in values.items())
    LOG.info("Started %s on loopback port %s", name, port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("nats", "postgres"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    start(args.service)
