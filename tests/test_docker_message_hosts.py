"""The launcher's preparation of the message hosts' keys, and how it starts the installation authority
and the team services.

docker.message_hosts; the live runs of the real processes are in
tests/test_live_installation_authority.py and tests/test_live_launcher_message_services.py.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from custom_transport.protocol1.bootstrap import read_bundle, write_bundle
from custom_transport.protocol1.host_keys import HOST_KEYS_FILENAME, load_authority_keys, load_host_keys
from docker import message_hosts
from installation_authority.expected_hosts import ExpectedHost, read_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME
from runtime_settings.instance_layout import InstanceLayout
from team_services import serving


def test_the_first_start_makes_the_keys_the_bundle_and_the_list(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    installation = message_hosts.prepare_message_host_trust(layout)
    assert installation.startswith("ua-")
    authority = load_authority_keys(layout)
    assert authority.installation == installation
    assert load_host_keys(layout, AUTHORITY_RUNTIME).runtime == AUTHORITY_RUNTIME
    bundle = read_bundle(layout.trust_bootstrap_path)
    assert (bundle.installation, bundle.authority_keys) == (installation, ((authority.key_id, authority.public_key_text),))
    assert read_expected_hosts(layout.trust_expected_hosts_path) == {}
    assert stat.S_IMODE(layout.trust_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((layout.host_trust_dir(AUTHORITY_RUNTIME) / HOST_KEYS_FILENAME).stat().st_mode) == 0o600


def test_later_starts_keep_the_installation_its_keys_and_the_authoritys_records(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    installation = message_hosts.prepare_message_host_trust(layout)
    key_id = load_authority_keys(layout).key_id
    record = {"record": "revocations", "sequence": 7}
    write_bundle(layout.trust_bootstrap_path, read_bundle(layout.trust_bootstrap_path).with_records([record]))
    host = ExpectedHost("team-services", "team-services.0123", "AAAA", frozenset({"team_services"}), frozenset({"team_one"}))
    assert message_hosts.prepare_message_host_trust(layout, {"team-services": host}) == installation
    assert load_authority_keys(layout).key_id == key_id
    assert read_bundle(layout.trust_bootstrap_path).records == (record,)
    assert set(read_expected_hosts(layout.trust_expected_hosts_path)) == {"team-services"}
    # A host the launcher no longer starts loses automatic approval at the next start.
    message_hosts.prepare_message_host_trust(layout)
    assert read_expected_hosts(layout.trust_expected_hosts_path) == {}


def test_a_lost_bundle_is_rebuilt_for_the_same_installation(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    installation = message_hosts.prepare_message_host_trust(layout)
    layout.trust_bootstrap_path.unlink()
    assert message_hosts.prepare_message_host_trust(layout) == installation
    assert read_bundle(layout.trust_bootstrap_path).installation == installation


def ready_bundle_writer(layout: InstanceLayout, sequence: int) -> list[str]:
    """A stand-in authority that writes a certificate and a newer revocations record, then waits."""

    script = (
        "import json, sys, time\n"
        f"path = {str(layout.trust_bootstrap_path)!r}\n"
        "bundle = json.load(open(path))\n"
        f"bundle['records'] = [{{'record': 'certificate', 'body': {{'runtime': '{AUTHORITY_RUNTIME}'}}}}, "
        f"{{'record': 'revocations', 'sequence': {sequence}}}]\n"
        "json.dump(bundle, open(path, 'w'))\n"
        "time.sleep(30)\n"
    )
    return [sys.executable, "-c", script]


def test_the_launcher_waits_until_the_authority_signed_its_records(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    message_hosts.prepare_message_host_trust(layout)
    process = message_hosts.start_installation_authority(layout, ready_bundle_writer(layout, 5), {}, cwd=tmp_path,
                                                         timeout_seconds=10)
    try:
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_an_authority_that_exits_or_stays_silent_stops_the_launch(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    message_hosts.prepare_message_host_trust(layout)
    with pytest.raises(RuntimeError, match="exited with code 3"):
        message_hosts.start_installation_authority(layout, [sys.executable, "-c", "raise SystemExit(3)"], {},
                                                   cwd=tmp_path, timeout_seconds=10)
    with pytest.raises(RuntimeError, match="did not get ready within"):
        message_hosts.start_installation_authority(layout, [sys.executable, "-c", "import time; time.sleep(30)"], {},
                                                   cwd=tmp_path, timeout_seconds=0.5)
    # A record from an earlier start does not count as ready: the revocations must be signed now.
    write_bundle(layout.trust_bootstrap_path, read_bundle(layout.trust_bootstrap_path).with_records(
        [{"record": "certificate", "body": {"runtime": AUTHORITY_RUNTIME}}, {"record": "revocations", "sequence": 9}]))
    with pytest.raises(RuntimeError, match="did not get ready within"):
        message_hosts.start_installation_authority(layout, ready_bundle_writer(layout, 9), {}, cwd=tmp_path,
                                                   timeout_seconds=1)
    assert json.loads(layout.trust_bootstrap_path.read_text())["records"][1]["sequence"] == 9


# --- the team services: the launcher waits until they serve ---------------------------------


def team_services_stand_in(layout: InstanceLayout, *, serve_after: float | None, pid_offset: int = 0,
                           exit_code: int | None = None) -> list[str]:
    """A stand-in for the team services: after ``serve_after`` seconds it says it serves, as
    team_services.serving does, naming its process (or another one with ``pid_offset``), then waits."""

    script = "import json, os, sys, time\n"
    if exit_code is not None:
        script += f"sys.exit({exit_code})\n"
    if serve_after is not None:
        script += (f"time.sleep({serve_after})\n"
                   f"path = {str(layout.team_services_serving_path)!r}\n"
                   "os.makedirs(os.path.dirname(path), exist_ok=True)\n"
                   f"json.dump({{'pid': os.getpid() + {pid_offset}, 'instance': 'stand-in'}}, open(path + '.new', 'w'))\n"
                   "os.replace(path + '.new', path)\n")
    script += "time.sleep(30)\n"
    return [sys.executable, "-c", script]


def stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    process.wait(timeout=5)


def test_the_launcher_waits_until_the_team_services_serve(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    started = time.monotonic()
    process = message_hosts.start_team_services(layout, team_services_stand_in(layout, serve_after=0.5), {},
                                                cwd=tmp_path, timeout_seconds=10)
    try:
        assert time.monotonic() - started >= 0.5, "it returned before they served"
        assert process.poll() is None
        assert serving.serving_process(layout) == process.pid
    finally:
        stop(process)


def test_while_another_instances_lease_holds_the_team_services_back_the_launcher_keeps_waiting_and_says_so(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """After an unclean stop the stopped instance's lease holds the new one back (about 40 seconds, here 2)."""

    layout = InstanceLayout.at(tmp_path)
    monkeypatch.setattr(message_hosts, "WAIT_REPORT_SECONDS", 0.5)
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="docker.message_hosts"):
        process = message_hosts.start_team_services(layout, team_services_stand_in(layout, serve_after=2), {},
                                                    cwd=tmp_path, timeout_seconds=10)
    try:
        assert time.monotonic() - started >= 2
        assert serving.serving_process(layout) == process.pid
        waits = [record.getMessage() for record in caplog.records if "event=message_service_waiting" in record.getMessage()]
        assert waits and all("Still waiting for the team services" in wait and "lease" in wait for wait in waits)
        assert "event=team_services_started" in caplog.text
    finally:
        stop(process)


def test_the_bounded_wait_covers_a_stopped_instances_lease() -> None:
    from custom_messages.wire.kinds import CLOCK_SKEW_SECONDS
    from team_services.lock import LEASE

    assert message_hosts.TEAM_SERVICES_READY_TIMEOUT_SECONDS > LEASE.total_seconds() + CLOCK_SKEW_SECONDS + 30


def test_a_serving_file_of_an_earlier_or_another_process_does_not_count(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    # An earlier run's file, even one naming a process id the new process may get again after a restart.
    layout.team_services_serving_path.parent.mkdir(parents=True)
    layout.team_services_serving_path.write_text(json.dumps({"pid": os.getpid(), "instance": "earlier"}))
    with pytest.raises(RuntimeError, match="team services did not get ready within 1 seconds: .*lease"):
        message_hosts.start_team_services(layout, team_services_stand_in(layout, serve_after=None), {}, cwd=tmp_path,
                                          timeout_seconds=1)
    assert not layout.team_services_serving_path.exists(), "the launcher removed the earlier file"
    with pytest.raises(RuntimeError, match="did not get ready within"):
        message_hosts.start_team_services(layout, team_services_stand_in(layout, serve_after=0, pid_offset=1), {},
                                          cwd=tmp_path, timeout_seconds=1)


def test_team_services_that_exit_before_serving_stop_the_launch(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    with pytest.raises(RuntimeError, match="team services exited with code 4 before getting ready"):
        message_hosts.start_team_services(layout, team_services_stand_in(layout, serve_after=None, exit_code=4), {},
                                          cwd=tmp_path, timeout_seconds=10)


def test_a_stop_during_the_wait_returns_the_team_services_still_starting(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    process = message_hosts.start_team_services(layout, team_services_stand_in(layout, serve_after=None), {},
                                                cwd=tmp_path, timeout_seconds=10, stop_requested=lambda: True)
    try:
        assert process.poll() is None
    finally:
        stop(process)


def test_the_serving_file_names_the_process_and_a_damaged_one_is_an_error(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    layout = InstanceLayout.at(tmp_path)
    assert serving.serving_process(layout) is None
    serving.mark_serving(layout, "instance-1", datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert serving.serving_process(layout) == os.getpid()
    assert json.loads(layout.team_services_serving_path.read_text())["instance"] == "instance-1"
    serving.forget_serving(layout)
    assert serving.serving_process(layout) is None
    layout.team_services_serving_path.write_text("{}")
    with pytest.raises(ValueError, match="does not name a process"):
        serving.serving_process(layout)


# --- both, at launch and on a restart of everything ------------------------------------------


class FakeProcess:
    def __init__(self, name: str, events: list[str]):
        self.name, self.events, self.code = name, events, None
        self.pid = 1

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.code = 0


def test_message_services_start_in_order_with_the_hosts_written_first_and_stop_in_reverse(
    tmp_path: Path, monkeypatch
) -> None:
    layout = InstanceLayout.at(tmp_path)
    events: list[str] = []
    limits = iter([2, 5])

    def hosts() -> dict[str, ExpectedHost]:
        limit = next(limits)
        events.append(f"hosts:{limit}")
        return {"lead-host": ExpectedHost("lead-host", "lead-host.0123", "AAAA", frozenset({"lead_host"}),
                                          frozenset({"team_one"}), frozenset({"researcher"}), limit)}

    def start(name: str):
        def started(layout_, command, env, *, cwd, stop_requested):
            listed = read_expected_hosts(layout_.trust_expected_hosts_path)["lead-host"].session_limit
            events.append(f"start:{name}:{env['ROLE']}:hosts={listed}")
            return FakeProcess(name, events)
        return started

    monkeypatch.setattr(message_hosts, "start_installation_authority", start("authority"))
    monkeypatch.setattr(message_hosts, "start_team_services", start("team-services"))
    services = message_hosts.MessageServices(layout, hosts, ["authority"], {"ROLE": "authority"}, ["team-services"],
                                             {"ROLE": "team-services"}, cwd=tmp_path)

    assert services.start() is True
    assert [name for name, _ in services.processes()] == ["installation authority", "team services"]
    services.stop(lambda process, _name: process.terminate())
    assert services.processes() == ()
    # Again, as a restart of everything does: the hosts are read and written again first, and the
    # environments get their copies of saved settings recomputed.
    assert services.start(lambda env, process: {**env, "ROLE": f"{env['ROLE']}+{process}"}) is True
    assert events == [
        "hosts:2", "start:authority:authority:hosts=2", "start:team-services:team-services:hosts=2",
        "stop:team-services", "stop:authority",
        "hosts:5", "start:authority:authority+installation authority:hosts=5",
        "start:team-services:team-services+team services:hosts=5",
    ]


def test_a_stop_while_the_authority_starts_does_not_start_the_team_services(tmp_path: Path, monkeypatch) -> None:
    layout = InstanceLayout.at(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(message_hosts, "start_installation_authority",
                        lambda *_args, **_kwargs: FakeProcess("authority", events))
    monkeypatch.setattr(message_hosts, "start_team_services", lambda *_args, **_kwargs: pytest.fail("not started"))
    services = message_hosts.MessageServices(layout, dict, ["authority"], {}, ["team-services"], {}, cwd=tmp_path)
    assert services.start(stop_requested=lambda: True) is False
    assert [name for name, _ in services.processes()] == ["installation authority"], "stopped with the others"
