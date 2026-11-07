"""What the launcher does for the message hosts before any service starts.

The message contract's hosts sign their messages with keys of their own, and an
installation authority vouches for them (``installation_authority``,
``custom_transport.protocol1``). Before it starts any host, the launcher:

1. keeps the installation's id, which the authority's key file holds, or picks a new
   random one when there is no key file: on the first start, and when the key was lost,
   since nothing the old key signed can be checked any more;
2. creates any missing key: the authority's, and each host's it starts;
3. writes the list of hosts the authority approves without asking anyone, with their
   public keys (``expected-hosts.json``; empty until the launcher starts message hosts),
   and the bootstrap bundle's authority keys;
4. starts the installation authority once NATS and the team's message streams are ready,
   and waits until the bundle carries its certificate and a fresh revocations record, so
   every later host can join (a join that comes before the authority serves waits for
   it). The authority is stopped again when it is not ready in time or the wait is
   interrupted.

Keys are files only their owner can read; each process is given only its own folder
(``runtime_settings.instance_layout``). A process the launcher stops listing loses
automatic approval at its next join or renewal. The trust folder and the NATS data are
backed up together (user docs, "Message host trust").
"""

from __future__ import annotations

import logging
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

from custom_transport.protocol1.bootstrap import BootstrapBundle, BootstrapError, read_bundle, write_bundle
from custom_transport.protocol1.host_keys import AUTHORITY_KEY_FILENAME, ensure_authority_keys, ensure_host_keys, load_authority_keys
from installation_authority.expected_hosts import ExpectedHost, write_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME
from runtime_settings.instance_layout import InstanceLayout

LOGGER = logging.getLogger(__name__)
# How long the authority may take to set up the topology and sign its records.
AUTHORITY_READY_TIMEOUT_SECONDS = 60.0


def _installation_id(layout: InstanceLayout) -> str:
    """The installation's id: kept by the authority's key, chosen at random when there is none.

    A bundle without its key is not reused: a new key under the old id could check none
    of the records the old key signed, so every host joins a new installation instead.
    """

    if (layout.trust_authority_dir / AUTHORITY_KEY_FILENAME).exists():
        return load_authority_keys(layout).installation
    installation = f"ua-{secrets.token_hex(6)}"
    try:
        previous: Optional[str] = read_bundle(layout.trust_bootstrap_path).installation
    except BootstrapError:
        previous = None
    if previous is not None:
        LOGGER.warning("The installation authority's key is missing; the instance starts a new installation and "
                       "every message host joins it again. event=installation_authority_key_missing previous=%s "
                       "installation=%s", previous, installation)
    return installation


def prepare_message_host_trust(layout: InstanceLayout, hosts: Mapping[str, ExpectedHost] | None = None) -> str:
    """Keys, the expected hosts and the bundle's authority keys; returns the installation id."""

    layout.trust_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    installation = _installation_id(layout)
    authority, created = ensure_authority_keys(layout, installation)
    if created:
        LOGGER.info("Created the installation authority's key. event=installation_authority_key_created installation=%s",
                    installation)
    ensure_host_keys(layout, AUTHORITY_RUNTIME)
    write_expected_hosts(layout.trust_expected_hosts_path, dict(hosts or {}))
    keys = ((authority.key_id, authority.public_key_text),)
    try:
        existing: Optional[BootstrapBundle] = read_bundle(layout.trust_bootstrap_path)
    except BootstrapError:
        existing = None
    if existing is None or existing.authority_keys != keys or existing.installation != installation:
        write_bundle(layout.trust_bootstrap_path,
                     BootstrapBundle(installation, keys, existing.records if existing and existing.installation == installation
                                     else ()))
    return installation


def build_installation_authority_command(config_path: Path, *, repo_root: Path,
                                         python_executable: str | None = None) -> list[str]:
    return [python_executable or sys.executable, "-u", str(repo_root / "assistant_cli.py"), "run-installation-authority",
            "--config", str(config_path)]


def _revocations_sequence(layout: InstanceLayout) -> int:
    try:
        records = read_bundle(layout.trust_bootstrap_path).records
    except BootstrapError:
        return 0
    return max((int(record.get("sequence", 0)) for record in records if record.get("record") == "revocations"), default=0)


def _ready(layout: InstanceLayout, sequence_before: int) -> bool:
    try:
        records = read_bundle(layout.trust_bootstrap_path).records
    except BootstrapError:
        return False
    signed_now = any(record.get("record") == "revocations" and int(record.get("sequence", 0)) > sequence_before
                     for record in records)
    has_certificate = any(record.get("record") == "certificate" and record.get("body", {}).get("runtime") == AUTHORITY_RUNTIME
                          for record in records)
    return signed_now and has_certificate


def start_installation_authority(layout: InstanceLayout, command: list[str], env: Mapping[str, str], *, cwd: Path,
                                 timeout_seconds: float = AUTHORITY_READY_TIMEOUT_SECONDS) -> subprocess.Popen[bytes]:
    """Start the authority and wait until it signed its records; raise ``RuntimeError`` if it does not.

    The authority is stopped (and waited for) when it is not ready in time and when the
    wait ends any other way, Ctrl+C included, so no authority outlives a failed start.
    """

    sequence_before = _revocations_sequence(layout)
    LOGGER.info("Starting the installation authority: %s", " ".join(command))
    process = subprocess.Popen(command, cwd=cwd, env=dict(env))
    try:
        deadline = time.monotonic() + timeout_seconds
        while not _ready(layout, sequence_before):
            code = process.poll()
            if code is not None:
                raise RuntimeError(f"The installation authority exited with code {code} before it was ready.")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"The installation authority was not ready within {timeout_seconds:.0f} seconds.")
            time.sleep(0.2)
    except BaseException:
        _stop(process)
        raise
    LOGGER.info("The installation authority is ready. event=installation_authority_started")
    return process


def _stop(process: subprocess.Popen[bytes], *, timeout_seconds: float = 10.0) -> None:
    """Stop the authority: ask, wait, and kill it when it does not stop in time."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        LOGGER.warning("The installation authority did not stop after %.0f seconds; killing it.", timeout_seconds)
        process.kill()
        process.wait(timeout=5.0)
