"""The launcher's preparation of the message hosts' keys, and how it starts the installation authority.

docker.message_hosts; the live run of the real process is in
tests/test_live_installation_authority.py.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from custom_transport.protocol1.bootstrap import read_bundle, write_bundle
from custom_transport.protocol1.host_keys import HOST_KEYS_FILENAME, load_authority_keys, load_host_keys
from docker import message_hosts
from installation_authority.expected_hosts import ExpectedHost, read_expected_hosts
from installation_authority.service import AUTHORITY_RUNTIME
from runtime_settings.instance_layout import InstanceLayout


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
    with pytest.raises(RuntimeError, match="not ready within"):
        message_hosts.start_installation_authority(layout, [sys.executable, "-c", "import time; time.sleep(30)"], {},
                                                   cwd=tmp_path, timeout_seconds=0.5)
    # A record from an earlier start does not count as ready: the revocations must be signed now.
    write_bundle(layout.trust_bootstrap_path, read_bundle(layout.trust_bootstrap_path).with_records(
        [{"record": "certificate", "body": {"runtime": AUTHORITY_RUNTIME}}, {"record": "revocations", "sequence": 9}]))
    with pytest.raises(RuntimeError, match="not ready within"):
        message_hosts.start_installation_authority(layout, ready_bundle_writer(layout, 9), {}, cwd=tmp_path,
                                                   timeout_seconds=1)
    assert json.loads(layout.trust_bootstrap_path.read_text())["records"][1]["sequence"] == 9


def test_a_lost_authority_key_starts_a_new_installation(tmp_path: Path, caplog) -> None:
    """A new key could check nothing the old one signed, so the bundle's id is not reused: every host joins anew."""

    layout = InstanceLayout.at(tmp_path)
    old = message_hosts.prepare_message_host_trust(layout)
    write_bundle(layout.trust_bootstrap_path, read_bundle(layout.trust_bootstrap_path).with_records(
        [{"record": "revocations", "sequence": 7}]))
    (layout.trust_authority_dir / "authority-key.json").unlink()
    new = message_hosts.prepare_message_host_trust(layout)
    assert new != old and new.startswith("ua-")
    assert load_authority_keys(layout).installation == new
    bundle = read_bundle(layout.trust_bootstrap_path)
    assert bundle.installation == new and bundle.records == ()
    assert "event=installation_authority_key_missing" in caplog.text


def _recording_popen(monkeypatch) -> list[subprocess.Popen[bytes]]:
    started: list[subprocess.Popen[bytes]] = []
    real = subprocess.Popen

    def popen(*args, **kwargs):
        process = real(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(message_hosts.subprocess, "Popen", popen)
    return started


def test_an_authority_that_is_not_ready_in_time_is_stopped(tmp_path: Path, monkeypatch) -> None:
    layout = InstanceLayout.at(tmp_path)
    message_hosts.prepare_message_host_trust(layout)
    started = _recording_popen(monkeypatch)
    with pytest.raises(RuntimeError, match="not ready within"):
        message_hosts.start_installation_authority(layout, [sys.executable, "-c", "import time; time.sleep(30)"], {},
                                                   cwd=tmp_path, timeout_seconds=0.5)
    assert started[0].poll() is not None


def test_an_interrupted_wait_stops_the_authority(tmp_path: Path, monkeypatch) -> None:
    layout = InstanceLayout.at(tmp_path)
    message_hosts.prepare_message_host_trust(layout)
    started = _recording_popen(monkeypatch)

    def interrupt(*_args) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(message_hosts, "_ready", interrupt)
    with pytest.raises(KeyboardInterrupt):
        message_hosts.start_installation_authority(layout, [sys.executable, "-c", "import time; time.sleep(30)"], {},
                                                   cwd=tmp_path, timeout_seconds=10)
    assert started[0].poll() is not None
