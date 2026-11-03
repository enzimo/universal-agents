"""The protocol 1 trust kernel: ids, NATS keys, key files, the bootstrap bundle and the kept trust records.

custom_transport.protocol1; the message contract's README, "Installation traffic" and
"Trust over time". Live NATS checks are in tests/test_live_protocol1_trust_kernel.py.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from custom_messages.wire.frame import Receiver, reply_audience, verify_frame
from custom_messages.wire.models import Frame
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1 import host_keys, nkeys
from custom_transport.protocol1.bootstrap import BootstrapBundle, BootstrapError, read_bundle, write_bundle
from custom_transport.protocol1.host_keys import HostKeys, KeyFileError
from custom_transport.protocol1.identity import HostIdentity, IdentityError
from custom_transport.protocol1.ids import new_message_id
from custom_transport.protocol1.live import LiveHost, request_subject, serving_subject
from custom_transport.protocol1.trust_state import PersistentTrustStore
from runtime_settings.instance_layout import InstanceLayout
from tests import message_contract_support as fixtures

MESSAGE_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


# -- ids ------------------------------------------------------------------------------------------

def test_message_ids_are_uuid7_ordered_by_their_millisecond() -> None:
    import re

    first, second = new_message_id(1_700_000_000_000), new_message_id(1_700_000_000_001)
    assert re.fullmatch(MESSAGE_ID, first) and re.fullmatch(MESSAGE_ID, second)
    assert uuid.UUID(first).version == 7 and first < second
    assert int(uuid.UUID(first).hex[:12], 16) == 1_700_000_000_000
    assert len({new_message_id(1) for _ in range(1000)}) == 1000


# -- NATS keys ------------------------------------------------------------------------------------

def test_nats_user_keys_round_trip_and_refuse_damage() -> None:
    seed = bytes(range(32))
    text = nkeys.user_seed(seed)
    assert text.startswith("SU") and nkeys.seed_bytes(text) == seed
    public = nkeys.user_public_key(bytes(range(32, 64)))
    assert public.startswith("U") and len(public) == 56 and nkeys.public_key_bytes(public) == bytes(range(32, 64))
    damaged = public[:-1] + ("A" if public[-1] != "A" else "B")
    with pytest.raises(ValueError):
        nkeys.public_key_bytes(damaged)
    with pytest.raises(ValueError):
        nkeys.seed_bytes(public)


@pytest.mark.skipif(shutil.which("nats-server") is None and not os.environ.get("LIVE_TEST_NATS_SERVER"),
                    reason="needs nats-server to check the key format")
def test_nats_server_accepts_the_generated_user_key(tmp_path: Path) -> None:
    """nats-server checks an NKey's prefix and checksum when it loads its configuration."""

    keys, _ = host_keys.ensure_host_keys(InstanceLayout.at(tmp_path), "worker-host-1")
    config = tmp_path / "nats.conf"
    config.write_text(f'authorization {{ users: [ {{ nkey: "{keys.nats_public_key}" }} ] }}\n')
    server = os.environ.get("LIVE_TEST_NATS_SERVER") or "nats-server"
    checked = subprocess.run([server, "-c", str(config), "-t"], capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    bad = tmp_path / "bad.conf"
    bad.write_text(f'authorization {{ users: [ {{ nkey: "{keys.nats_public_key[:-1]}A" }} ] }}\n')
    assert subprocess.run([server, "-c", str(bad), "-t"], capture_output=True, text=True, timeout=30).returncode != 0


# -- key files ------------------------------------------------------------------------------------

def test_host_keys_are_made_once_and_readable_only_by_their_owner(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    keys, created = host_keys.ensure_host_keys(layout, "lead_host-1")
    again, created_again = host_keys.ensure_host_keys(layout, "lead_host-1")
    assert created and not created_again and again.key_id == keys.key_id
    assert again.public_key_text == keys.public_key_text and again.nats_seed == keys.nats_seed
    # Key ids hold no underscore (contract key_id), runtime ids may.
    assert keys.key_id.startswith("lead-host-1.") and "_" not in keys.key_id
    path = layout.host_trust_dir("lead_host-1") / host_keys.HOST_KEYS_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o640)
    with pytest.raises(KeyFileError, match="readable only by its owner"):
        host_keys.load_host_keys(layout, "lead_host-1")


def test_a_key_another_process_wrote_first_is_kept(tmp_path: Path, monkeypatch) -> None:
    """Two processes starting at once never each keep a different key for one host."""

    layout = InstanceLayout.at(tmp_path)
    winner, _ = host_keys.ensure_host_keys(layout, "gateway")
    path = layout.host_trust_dir("gateway") / host_keys.HOST_KEYS_FILENAME
    # This process saw no file when it looked, then found the other process's file at publication.
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if self == path else real_exists(self))
    keys, created = host_keys.ensure_host_keys(layout, "gateway")
    assert not created and keys.key_id == winner.key_id
    assert not [name for name in os.listdir(path.parent) if name.startswith(".key-write-")]


def test_a_missing_or_foreign_key_file_is_refused(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    with pytest.raises(KeyFileError, match="launcher creates it"):
        host_keys.load_host_keys(layout, "worker-host-1")
    host_keys.ensure_host_keys(layout, "worker-host-1")
    other = layout.host_trust_dir("worker-host-2")
    other.mkdir(parents=True, mode=0o700)
    target = other / host_keys.HOST_KEYS_FILENAME
    target.write_bytes((layout.host_trust_dir("worker-host-1") / host_keys.HOST_KEYS_FILENAME).read_bytes())
    target.chmod(0o600)
    with pytest.raises(KeyFileError, match="belongs to 'worker-host-1'"):
        host_keys.load_host_keys(layout, "worker-host-2")


@pytest.mark.parametrize("runtime", ["../escape", "x-peer", "_reserved", "a/b", ""])
def test_a_host_key_folder_is_one_safe_name(tmp_path: Path, runtime: str) -> None:
    with pytest.raises(ValueError):
        InstanceLayout.at(tmp_path).host_trust_dir(runtime)


def test_the_authority_key_names_its_installation(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path)
    authority, created = host_keys.ensure_authority_keys(layout, "install_one")
    assert created and authority.installation == "install_one" and "_" not in authority.key_id
    assert host_keys.load_authority_keys(layout).key_id == authority.key_id
    assert layout.is_instance_private(layout.trust_authority_dir)


# -- the bootstrap bundle and kept trust records --------------------------------------------------

def example_bundle(*, revocations: bytes | None = None) -> BootstrapBundle:
    anchors = json.loads((fixtures.TRUST / "anchors.json").read_text())
    records = [json.loads(path.read_text()) for path in sorted((fixtures.TRUST / "certificates").glob("*.json"))]
    records.append(json.loads(revocations or (fixtures.TRUST / "revocations" / "install_example.json").read_bytes()))
    records += [json.loads(path.read_text()) for path in sorted((fixtures.TRUST / "sessions").glob("*.json"))]
    return BootstrapBundle(anchors["installation"], tuple((key["key_id"], key["public_key"]) for key in anchors["authority_keys"]),
                           tuple(records))


def test_the_bundle_round_trips_and_builds_a_trust_store(tmp_path: Path) -> None:
    bundle = example_bundle()
    write_bundle(tmp_path / "bootstrap.json", bundle)
    read = read_bundle(tmp_path / "bootstrap.json")
    assert read == bundle and stat.S_IMODE((tmp_path / "bootstrap.json").stat().st_mode) == 0o600
    store = read.trust_store(fixtures.NOW)
    assert store.host_certificate("install_example", "team-services-1", 2) is not None
    assert store.revocations_current("install_example", fixtures.NOW)


def test_a_stale_bundle_record_is_skipped_with_a_warning(caplog) -> None:
    bundle = example_bundle()
    tampered = dict(bundle.records[0], sequence=bundle.records[0]["sequence"] + 1)  # its signature no longer verifies
    store = bundle.with_records([tampered, *bundle.records[1:]]).trust_store(fixtures.NOW)
    assert "bootstrap_record_refused" in caplog.text
    assert store.revocations_current("install_example", fixtures.NOW)


def test_a_missing_or_damaged_bundle_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="launcher writes it"):
        read_bundle(tmp_path / "bootstrap.json")
    (tmp_path / "bootstrap.json").write_text(json.dumps({"version": 1, "installation": "i", "authority_keys": []}))
    with pytest.raises(BootstrapError, match="names no authority key"):
        read_bundle(tmp_path / "bootstrap.json")


def test_the_newest_revocations_record_survives_a_restart(tmp_path: Path) -> None:
    """README "Trust over time": receivers keep the newest record they verified, also across restarts."""

    later = fixtures.NOW + timedelta(days=3)
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert not trust.store.revocations_current("install_example", later)
    assert trust.add(fixtures.current_revocations(later, sequence=500), now=later)
    # Restarted from the same, older bundle: the newer record is still held.
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, later)
    assert reopened.store.revocations_current("install_example", later)
    # An older record written back is not taken over the newer one.
    assert not reopened.add(fixtures.current_revocations(later, sequence=200), now=later)


def test_a_newer_bundle_record_wins_over_an_older_kept_one(tmp_path: Path) -> None:
    later = fixtures.NOW + timedelta(days=3)
    PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW).add(
        fixtures.current_revocations(fixtures.NOW + timedelta(hours=2), sequence=150), now=fixtures.NOW + timedelta(hours=2))
    reopened = PersistentTrustStore.open(example_bundle(revocations=fixtures.current_revocations(later, sequence=600)),
                                         tmp_path, later)
    assert reopened.store.revocations_current("install_example", later)


def test_unreadable_kept_records_are_logged_and_the_bundle_still_serves(tmp_path: Path, caplog) -> None:
    (tmp_path / "trust-state.json").write_text("{not json")
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert "trust_state_unreadable" in caplog.text
    assert trust.store.revocations_current("install_example", fixtures.NOW)


def test_the_highest_block_list_sequence_survives_a_restart(tmp_path: Path) -> None:
    record = (fixtures.EXAMPLES / "state" / "valid" / "block_list" / "team.json").read_bytes()
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert trust.verify_block_list(record, now=fixtures.NOW) is not None
    assert trust.verify_block_list(record, now=fixtures.NOW) is None  # the same list again
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert reopened.verify_block_list(record, now=fixtures.NOW) is None
    older = json.loads(record)
    older["sequence"] -= 1
    from custom_messages.wire.trust import sign_state

    resigned = json.dumps(sign_state(older, fixtures.private_key(older["writer"]["key_id"]))).encode()
    with pytest.raises(FrameRefused) as refused:
        reopened.verify_block_list(resigned, now=fixtures.NOW)
    assert refused.value.code == "replayed"


# -- identity and signing -------------------------------------------------------------------------

def example_identity(runtime: str, key_id: str, epoch: int, sessions: dict[str, str] | None = None) -> HostIdentity:
    store = fixtures.trust_store()
    certificate = store.host_certificate("install_example", runtime, epoch)
    assert certificate is not None
    keys = HostKeys(runtime=runtime, key_id=key_id, application_key=fixtures.private_key(key_id), nats_seed=bytes(32))
    return HostIdentity(keys, certificate, sessions or {})


def test_an_identity_signs_only_for_its_certificate_and_its_agents() -> None:
    worker = example_identity("worker-host-1", "test-worker-key-1", 3, {"researcher-2": "s-researcher-2-1"})
    assert worker.issuer("team_example", "researcher-2")["session"] == "s-researcher-2-1"
    with pytest.raises(IdentityError, match="no session"):
        worker.issuer("team_example", "lead")
    with pytest.raises(IdentityError, match="does not serve"):
        worker.issuer("another_team")
    with pytest.raises(IdentityError, match="another host or key"):
        HostIdentity(worker.keys, fixtures.trust_store().host_certificate("install_example", "lead-host-1", 3))


def test_a_signed_request_and_its_reply_verify_as_the_contract_says() -> None:
    worker = LiveHost(None, example_identity("worker-host-1", "test-worker-key-1", 3),
                      PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent"), {}, {}), clock=lambda: fixtures.NOW)
    request = worker.sign("sessions.renew", {"request_id": "renew_1", "session": "s-researcher-2-1"},
                          {"installation": "install_example", "team": "team_example", "target": "team_services",
                           "id": "team_example"}, team="team_example")
    assert request["subject"] == "ua.v1.team_example.worker-host-1.svc.team"
    services_receiver = Receiver("install_example", "team_example", "team_services", "team_example", "team-services-1")
    verified = verify_frame(json.dumps(request).encode(), trust=fixtures.trust_store(), receiver=services_receiver,
                            arrived_on=request["subject"], now=fixtures.NOW, replay=fixtures.replay_holding())
    assert verified.frame.kind == "sessions.renew"
    # The team services' reply names the host, goes to its inbox, and the worker host accepts it.
    services = LiveHost(None, example_identity("team-services-1", "test-services-key-1", 2),
                        PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent"), {}, {}), clock=lambda: fixtures.NOW)
    session = json.loads((fixtures.TRUST / "sessions" / "researcher-2.json").read_text())
    reply = services.sign("sessions.renew.reply", {"outcome": "ok", "session": session},
                          reply_audience(verified.frame), team="team_example",
                          subject=f"_INBOX.worker-host-1.{verified.frame.message_id}", answering=verified.frame)
    assert reply["audience"]["target"] == "host"
    accepted = verify_frame(json.dumps(reply).encode(), trust=fixtures.trust_store(),
                            receiver=worker.reply_receiver(Frame.model_validate(request)), arrived_on=reply["subject"],
                            now=fixtures.NOW, replay=fixtures.replay_holding(), answering=Frame.model_validate(request))
    assert accepted.body.outcome == "ok"


def test_a_body_the_schema_refuses_is_never_signed() -> None:
    worker = LiveHost(None, example_identity("worker-host-1", "test-worker-key-1", 3),
                      PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent"), {}, {}), clock=lambda: fixtures.NOW)
    with pytest.raises(Exception, match="session"):
        worker.sign("sessions.renew", {"request_id": "renew_1"},
                    {"installation": "install_example", "team": "team_example", "target": "team_services",
                     "id": "team_example"}, team="team_example")


def test_subjects_follow_the_contracts_addresses() -> None:
    audience = {"installation": "install_example", "team": "team_example", "target": "agent", "id": "researcher-2"}
    assert request_subject("agent.status", audience, "lead-host-1") == "ua.v1.team_example.lead-host-1.ctl.agent.researcher-2"
    assert serving_subject("ctl.agent.{agent}", "team_example", "researcher-2") == "ua.v1.team_example.*.ctl.agent.researcher-2"
    install = {"installation": "install_example", "team": "_install", "target": "installation_service", "id": "authority"}
    assert request_subject("host.join", install, "new-host-1") == "ua.v1._install.new-host-1.svc"
    assert serving_subject("_install.{sender}.svc", "_install", "authority") == "ua.v1._install.*.svc"
