"""What the launcher does for the message hosts before any service starts.

The message contract's hosts sign their messages with keys of their own, and an
installation authority vouches for them (``installation_authority``,
``custom_transport.protocol1``). Before it starts any host, the launcher:

1. keeps the installation's id (from the authority's key file or the bootstrap bundle,
   or a new random one on the first start);
2. creates any missing key: the authority's, and each host's it starts;
3. writes the list of hosts the authority approves without asking anyone, with their
   public keys (``expected-hosts.json``), and the bootstrap bundle's authority keys;
4. starts the installation authority right after NATS and waits until the bundle carries
   its certificate and a fresh revocations record, so every later host can join;
5. starts the team services and waits until they serve (``team_services.serving``), so the
   team lead, the startup agents and the gateway find them answering. After an unclean
   stop that takes until the dead instance's lease on the team's lock runs out (about 40
   seconds), and the launcher says every 10 seconds what it still waits for.

``MessageServices`` does steps 3 to 5 at launch and again on a restart of everything (the
configuration editor's restart): it writes the expected hosts from the saved configuration
as it is then, so a raised limit takes effect, and the restarted authority and team
services read the saved settings again.

Keys are files only their owner can read; each process is given only its own folder
(``runtime_settings.instance_layout``). A process the launcher stops listing loses
automatic approval at the next start.
"""

from __future__ import annotations

import logging
import secrets
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
from custom_transport.protocol1.bootstrap import BootstrapBundle, BootstrapError, read_bundle, write_bundle
from custom_transport.protocol1.host_keys import AUTHORITY_KEY_FILENAME, ensure_authority_keys, ensure_host_keys, load_authority_keys
from custom_transport.protocol1.joining import HostProfile
from installation_authority.expected_hosts import ExpectedHost, write_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME
from runtime_settings.instance_layout import InstanceLayout
from team_services.lock import LEASE
from team_services.serving import forget_serving, serving_process

LOGGER = logging.getLogger(__name__)
# How long the authority may take to set up the topology and sign its records.
AUTHORITY_READY_TIMEOUT_SECONDS = 60.0
# How long the team services may take to serve: after an unclean stop, the dead instance's lease
# on the team's lock and the clock tolerance (team_services.lock), plus as long as the authority
# may take, for starting and joining.
TEAM_SERVICES_READY_TIMEOUT_SECONDS = LEASE.total_seconds() + CLOCK_SKEW_SECONDS + AUTHORITY_READY_TIMEOUT_SECONDS
# How often the launcher says what it still waits for.
WAIT_REPORT_SECONDS = 10.0
AUTHORITY_NAME = "installation authority"
TEAM_SERVICES_NAME = "team services"
_TEAM_SERVICES_WAIT = (f"they join and take the team's lock, which another instance's lease holds back, after an "
                       f"unclean stop for up to {LEASE.total_seconds() + CLOCK_SKEW_SECONDS:.0f} seconds (their log "
                       f"says event=team_services_standby while they wait)")


def _installation_id(layout: InstanceLayout) -> str:
    """The installation's id: kept by the authority's key and the bundle, chosen once at random."""

    if (layout.trust_authority_dir / AUTHORITY_KEY_FILENAME).exists():
        return load_authority_keys(layout).installation
    try:
        return read_bundle(layout.trust_bootstrap_path).installation
    except BootstrapError:
        return f"ua-{secrets.token_hex(6)}"


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


def expected_host(layout: InstanceLayout, profile: HostProfile, *,
                  agent_types_from_catalog: bool = False) -> ExpectedHost:
    """The approval entry of a host the launcher starts, with its key made when missing.

    ``agent_types_from_catalog``: the host may also ask for every agent type in the instance's
    catalog as the authority reads it at the join (the team lead's process).
    """

    keys, _ = ensure_host_keys(layout, profile.runtime)
    return ExpectedHost(profile.runtime, keys.key_id, keys.public_key_text, frozenset(profile.roles),
                        frozenset(profile.teams), frozenset(profile.agent_types), profile.session_limit,
                        agent_types_from_catalog)


def build_team_services_command(config_path: Path, *, repo_root: Path, python_executable: str | None = None) -> list[str]:
    return [python_executable or sys.executable, "-u", str(repo_root / "assistant_cli.py"), "run-team-services",
            "--config", str(config_path)]


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


def _never() -> bool:
    return False


def _stop_unready(process: subprocess.Popen[bytes]) -> None:
    """Stop a process that did not get ready in time, so nothing is left running."""

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_until_ready(process: subprocess.Popen[bytes], ready: Callable[[], bool], *, name: str,
                      timeout_seconds: float, stop_requested: Callable[[], bool], waiting_for: str) -> bool:
    """Wait until ``ready``; ``False`` when the launcher stops first.

    Raises ``RuntimeError``, with the process stopped, when it exits or is not ready in time.
    """

    started = time.monotonic()
    report_at = started + WAIT_REPORT_SECONDS
    while not ready():
        if stop_requested():
            return False
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"The {name} exited with code {code} before getting ready.")
        now = time.monotonic()
        if now >= started + timeout_seconds:
            _stop_unready(process)
            raise RuntimeError(f"The {name} did not get ready within {timeout_seconds:.0f} seconds: {waiting_for}.")
        if now >= report_at:
            LOGGER.info("Still waiting for the %s: %s. event=message_service_waiting process=%s seconds=%.0f "
                        "timeout_seconds=%.0f", name, waiting_for, name.replace(" ", "_"), now - started,
                        timeout_seconds)
            report_at = now + WAIT_REPORT_SECONDS
        time.sleep(0.2)
    return True


def start_installation_authority(layout: InstanceLayout, command: list[str], env: Mapping[str, str], *, cwd: Path,
                                 timeout_seconds: float = AUTHORITY_READY_TIMEOUT_SECONDS,
                                 stop_requested: Callable[[], bool] = _never) -> subprocess.Popen[bytes]:
    """Start the authority and wait until it signed its records; raise ``RuntimeError`` if it does not.

    When ``stop_requested`` says the launcher stops, this returns the authority still starting.
    """

    sequence_before = _revocations_sequence(layout)
    LOGGER.info("Starting the installation authority: %s", " ".join(command))
    process = subprocess.Popen(command, cwd=cwd, env=dict(env))
    if _wait_until_ready(process, lambda: _ready(layout, sequence_before), name=AUTHORITY_NAME,
                         timeout_seconds=timeout_seconds, stop_requested=stop_requested,
                         waiting_for="it sets up the message topology and signs its certificate and revocations"):
        LOGGER.info("The installation authority is ready. event=installation_authority_started")
    return process


def start_team_services(layout: InstanceLayout, command: list[str], env: Mapping[str, str], *, cwd: Path,
                        timeout_seconds: float = TEAM_SERVICES_READY_TIMEOUT_SECONDS,
                        stop_requested: Callable[[], bool] = _never) -> subprocess.Popen[bytes]:
    """Start the team services and wait until they serve; raise ``RuntimeError`` if they do not.

    They join, take the team's lock and serve on their own; the file they write then
    (``team_services.serving``) must name this process. When ``stop_requested`` says the
    launcher stops, this returns them still starting.
    """

    forget_serving(layout)
    LOGGER.info("Starting the team services: %s", " ".join(command))
    process = subprocess.Popen(command, cwd=cwd, env=dict(env))
    started = time.monotonic()
    if _wait_until_ready(process, lambda: serving_process(layout) == process.pid, name=TEAM_SERVICES_NAME,
                         timeout_seconds=timeout_seconds, stop_requested=stop_requested,
                         waiting_for=_TEAM_SERVICES_WAIT):
        LOGGER.info("The team services are serving. event=team_services_started seconds=%.1f",
                    time.monotonic() - started)
    return process


def _unchanged(env: Mapping[str, str], _process: str) -> dict[str, str]:
    return dict(env)


@dataclass
class MessageServices:
    """The installation authority and the team services, which every other message host needs.

    ``start`` writes the trust folder with ``hosts()``, the hosts the launcher starts as
    the saved configuration lists them now, then starts the authority and the team services
    in that order, each once the one before is ready. A restart of everything stops the
    other processes, then ``stop`` stops the team services and the authority, and ``start``
    runs again with each environment's copies of saved settings recomputed
    (``environment``), before the other processes start again (plan 13B.2).
    """

    layout: InstanceLayout
    hosts: Callable[[], Mapping[str, ExpectedHost]]
    authority_command: list[str]
    authority_env: Mapping[str, str]
    team_services_command: list[str]
    team_services_env: Mapping[str, str]
    cwd: Path
    authority: Optional[subprocess.Popen[bytes]] = None
    team_services: Optional[subprocess.Popen[bytes]] = None

    def start(self, environment: Callable[[Mapping[str, str], str], dict[str, str]] = _unchanged,
              stop_requested: Callable[[], bool] = _never) -> bool:
        """Start both, each ready before the next; ``False`` when the launcher stops first.

        Raises ``RuntimeError`` when one exits or is not ready in time, and ``OSError`` or
        ``ValueError`` when the trust folder or the saved configuration cannot be read.
        """

        prepare_message_host_trust(self.layout, self.hosts())
        self.authority = start_installation_authority(self.layout, self.authority_command,
                                                      environment(self.authority_env, AUTHORITY_NAME), cwd=self.cwd,
                                                      stop_requested=stop_requested)
        if stop_requested():
            return False
        self.team_services = start_team_services(self.layout, self.team_services_command,
                                                 environment(self.team_services_env, TEAM_SERVICES_NAME),
                                                 cwd=self.cwd, stop_requested=stop_requested)
        return not stop_requested()

    def processes(self) -> tuple[tuple[str, subprocess.Popen[bytes]], ...]:
        return tuple((name, process) for name, process in ((AUTHORITY_NAME, self.authority),
                                                           (TEAM_SERVICES_NAME, self.team_services))
                     if process is not None)

    def stop(self, terminate: Callable[[subprocess.Popen[bytes], str], None]) -> None:
        """Stop the team services, then the authority; a clean stop releases the team's lock at once."""

        processes = self.processes()
        self.authority = self.team_services = None
        with ExitStack() as stops:  # every stop is tried; the last registered runs first
            for name, process in processes:
                stops.callback(terminate, process, name)
