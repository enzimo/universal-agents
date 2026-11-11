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
from pydantic import ValidationError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import Receiver, reply_audience, verify_frame
from custom_messages.wire.models import Frame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import RETENTION, SKEW, sign_state
from custom_transport.protocol1 import host_keys, nkeys, private_files
from custom_transport.protocol1.bootstrap import BootstrapBundle, BootstrapError, read_bundle, write_bundle
from custom_transport.protocol1.host_keys import HostKeys, KeyFileError
from custom_transport.protocol1.identity import HostIdentity, IdentityError
from custom_transport.protocol1.ids import new_message_id
from custom_transport.protocol1.live import LiveHost, request_subject, serving_subject
from custom_transport.protocol1 import trust_state
from custom_transport.protocol1.trust_state import DAMAGED_MARK, TRUST_STATE_FILENAME, PersistentTrustStore
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
    # Another last character breaks the checksum; "A" itself would leave about one key in 32 unchanged.
    damaged = keys.nats_public_key[:-1] + ("A" if keys.nats_public_key[-1] != "A" else "B")
    assert damaged != keys.nats_public_key
    bad.write_text(f'authorization {{ users: [ {{ nkey: "{damaged}" }} ] }}\n')
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
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Every folder it made is owner-only, not just the host's own (plan 13B.3).
    for folder in (layout.trust_dir, layout.trust_dir / "hosts", path.parent):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    assert not [name for name in os.listdir(path.parent) if name.startswith(private_files.TEMPORARY_PREFIX)]
    path.chmod(0o640)
    with pytest.raises(KeyFileError, match="readable only by its owner"):
        host_keys.load_host_keys(layout, "lead_host-1")


def test_a_key_object_never_prints_its_secrets(tmp_path: Path) -> None:
    keys, _ = host_keys.ensure_host_keys(InstanceLayout.at(tmp_path), "lead-host-1")
    authority, _ = host_keys.ensure_authority_keys(InstanceLayout.at(tmp_path), "install_one")
    assert "nats_seed" not in repr(keys) and repr(keys.nats_seed) not in repr(keys)
    assert "application_key" not in repr(keys) and "private_key" not in repr(authority)
    assert keys.key_id in repr(keys) and authority.key_id in repr(authority)


def _open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("damage", ["folder", "link", "not_json", "unknown_version", "wrong_key_id"])
def test_a_damaged_key_file_is_refused_with_a_key_file_error(tmp_path: Path, damage: str) -> None:
    layout = InstanceLayout.at(tmp_path)
    host_keys.ensure_host_keys(layout, "worker-host-1")
    path = layout.host_trust_dir("worker-host-1") / host_keys.HOST_KEYS_FILENAME
    document = json.loads(path.read_text())
    path.unlink()
    if damage == "folder":
        path.mkdir()
    elif damage == "link":
        (tmp_path / "elsewhere.json").write_text(json.dumps(document))
        (tmp_path / "elsewhere.json").chmod(0o600)
        path.symlink_to(tmp_path / "elsewhere.json")
    else:
        if damage == "unknown_version":
            document["version"] = 99
        elif damage == "wrong_key_id":
            document["application_key"]["key_id"] = "worker-host-1.0000000000000000"
        path.write_text("{not json" if damage == "not_json" else json.dumps(document))
        path.chmod(0o600)
    before = _open_descriptors()
    expected = {"folder": "regular file", "link": "regular file", "not_json": "damaged",
                "unknown_version": "unknown format", "wrong_key_id": "another key id"}[damage]
    with pytest.raises(KeyFileError, match=expected):
        host_keys.load_host_keys(layout, "worker-host-1")
    assert _open_descriptors() == before


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
    assert not [name for name in os.listdir(path.parent) if name.startswith(private_files.TEMPORARY_PREFIX)]


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
    path = layout.trust_authority_dir / host_keys.AUTHORITY_KEY_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # The same installation gets the same key; another installation's key is refused, not handed out.
    again, created_again = host_keys.ensure_authority_keys(layout, "install_one")
    assert again.key_id == authority.key_id and not created_again
    with pytest.raises(KeyFileError, match="belongs to installation 'install_one', not 'install_two'"):
        host_keys.ensure_authority_keys(layout, "install_two")


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


def revocations(now, *, sequence: int, entries: list[dict] | None = None, installation: str = "install_example") -> bytes:
    """A revocations record of ``installation`` current at ``now`` (written an hour before), with ``entries`` if given."""

    if installation == "install_example":
        document = json.loads(fixtures.current_revocations(now, sequence=sequence))
    else:
        document = json.loads((fixtures.TRUST / "revocations" / f"{installation}.json").read_text())
        written = now - timedelta(hours=1)
        document["sequence"], document["issued_at"] = sequence, format_timestamp(written)
        document["body"]["expires_at"] = format_timestamp(written + timedelta(days=1))
    if entries is not None:
        document["body"]["entries"] = entries
    return json.dumps(sign_state(document, fixtures.private_key(document["writer"]["key_id"]))).encode()


def revoked_entry(certificate_id: str, runtime: str, key_id: str, *, key_expires_at) -> dict:
    return {"certificate_id": certificate_id, "runtime": runtime, "key_id": key_id,
            "key_expires_at": format_timestamp(key_expires_at), "membership_epoch": 1,
            "revoked_at": format_timestamp(fixtures.NOW - timedelta(days=1)), "reason": "compromised"}


def kept_file(folder: Path) -> dict:
    return json.loads((folder / TRUST_STATE_FILENAME).read_text())


def kept_sequences(folder: Path, installation: str = "install_example") -> list[int]:
    return [record["sequence"] for record in kept_file(folder)["revocations"].get(installation, [])]


LATER = fixtures.NOW + timedelta(days=3)
# The examples' revoked entry: listed by every record these tests sign unless they say otherwise.
OLD_ENTRY = json.loads((fixtures.TRUST / "revocations" / "install_example.json").read_text())["body"]["entries"][0]


def test_the_newest_revocations_record_survives_a_restart(tmp_path: Path) -> None:
    """README "Trust over time": receivers keep the newest record they verified, also across restarts."""

    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert not trust.store.revocations_current("install_example", LATER)
    assert trust.add(revocations(LATER, sequence=500), now=LATER)
    assert stat.S_IMODE((tmp_path / TRUST_STATE_FILENAME).stat().st_mode) == 0o600
    # Restarted from the same, older bundle: the newer record is still held.
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert reopened.store.revocations_current("install_example", LATER)
    # An older record written back is not taken over the newer one.
    assert not reopened.add(revocations(LATER, sequence=200), now=LATER)
    assert kept_sequences(tmp_path) == [500]


def test_a_record_first_read_from_the_bundle_is_kept_too(tmp_path: Path) -> None:
    """The record the store holds is kept, wherever it came from; a later, older bundle cannot bring back an older one."""

    PersistentTrustStore.open(example_bundle(revocations=revocations(LATER, sequence=600)), tmp_path, LATER)
    assert kept_sequences(tmp_path) == [600]
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert reopened.store.revocations_current("install_example", LATER)
    assert not reopened.add(revocations(LATER, sequence=550), now=LATER)


def test_a_newer_bundle_record_wins_over_an_older_kept_one(tmp_path: Path) -> None:
    PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW).add(
        revocations(fixtures.NOW + timedelta(hours=2), sequence=150), now=fixtures.NOW + timedelta(hours=2))
    reopened = PersistentTrustStore.open(example_bundle(revocations=revocations(LATER, sequence=600)), tmp_path, LATER)
    assert reopened.store.revocations_current("install_example", LATER)
    assert kept_sequences(tmp_path) == [600]


def test_reading_a_block_list_keeps_the_revocations_record(tmp_path: Path) -> None:
    """A save for another reason writes the revocations record too (it used to write an empty map over it)."""

    record = (fixtures.EXAMPLES / "state" / "valid" / "block_list" / "team.json").read_bytes()
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert trust.add(revocations(LATER, sequence=500), now=LATER)
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    reopened.verify_block_list(record, team="team_example", now=LATER)
    assert kept_sequences(tmp_path) == [500]
    assert PersistentTrustStore.open(example_bundle(), tmp_path, LATER).store.revocations_current("install_example", LATER)


def test_an_entry_a_newer_record_left_out_early_is_still_revoked_after_a_restart(tmp_path: Path) -> None:
    """README "Trust over time": an entry left out before nothing its key signed can be accepted is kept.

    The older record that lists it is kept beside the newer one, also when the newer
    one comes from the bundle, so the restarted store rebuilds the same list.
    """

    entry = revoked_entry("cert_stolen", "worker-host-7", "test-stolen-key-1", key_expires_at=LATER + timedelta(days=20))
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert trust.add(revocations(LATER, sequence=300, entries=[OLD_ENTRY, entry]), now=LATER)
    assert trust.add(revocations(LATER, sequence=400, entries=[OLD_ENTRY]), now=LATER)
    assert trust.store.key_revoked("install_example", "worker-host-7", "test-stolen-key-1")
    assert kept_sequences(tmp_path) == [300, 400]
    reopened = PersistentTrustStore.open(example_bundle(revocations=revocations(LATER, sequence=500, entries=[OLD_ENTRY])),
                                         tmp_path, LATER)
    assert reopened.store.key_revoked("install_example", "worker-host-7", "test-stolen-key-1")
    assert kept_sequences(tmp_path) == [300, 500]
    # Once nothing that key signed can be accepted, the older record is let go. The store
    # judges an entry at the newer record's issue time, an hour before the clock here.
    much_later = LATER + timedelta(days=20) + RETENTION + SKEW + timedelta(hours=2)
    gone = PersistentTrustStore.open(example_bundle(revocations=revocations(much_later, sequence=900, entries=[OLD_ENTRY])),
                                     tmp_path, much_later)
    assert not gone.store.key_revoked("install_example", "worker-host-7", "test-stolen-key-1")
    assert kept_sequences(tmp_path) == [900]


def test_another_installations_kept_record_waits_for_its_agreement(tmp_path: Path, caplog) -> None:
    """A peer's record verifies only once its agreement is read; until then it stays kept and comes first."""

    agreement = (fixtures.TRUST / "agreements" / "install_peer.json").read_bytes()
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert trust.add(agreement, now=fixtures.NOW)
    assert trust.add(revocations(fixtures.NOW, sequence=70, installation="install_peer"), now=fixtures.NOW)
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert "kept_revocations_waiting" in caplog.text and not reopened.store.revocations_current("install_peer", fixtures.NOW)
    # A save for another reason keeps the waiting record.
    reopened.verify_block_list((fixtures.EXAMPLES / "state" / "valid" / "block_list" / "team.json").read_bytes(),
                               team="team_example", now=fixtures.NOW)
    assert kept_sequences(tmp_path, "install_peer") == [70]
    # The agreement arrives: the kept record is taken at once, before an older copy from the bucket.
    assert reopened.add(agreement, now=fixtures.NOW)
    assert reopened.store.revocations_current("install_peer", fixtures.NOW)
    assert not reopened.add(revocations(fixtures.NOW, sequence=60, installation="install_peer"), now=fixtures.NOW)


def test_a_kept_record_dated_ahead_of_a_clock_that_went_back_waits_until_it_verifies(tmp_path: Path) -> None:
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert trust.add(revocations(LATER, sequence=500), now=LATER)
    behind = LATER - timedelta(hours=3)
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, behind)
    # Kept beside the bundle's older record, which is all this store could check.
    assert kept_sequences(tmp_path) == [1, 500]
    # The next record this host adds, once the clock is right again, comes after the kept one.
    assert not reopened.add(revocations(LATER, sequence=450), now=LATER)
    assert reopened.store.revocations_current("install_example", LATER)
    assert kept_sequences(tmp_path) == [500]


def test_two_store_objects_on_one_folder_keep_each_others_records(tmp_path: Path) -> None:
    """A second copy of a host starting while the first still runs never drops what the first kept."""

    record = (fixtures.EXAMPLES / "state" / "valid" / "block_list" / "team.json").read_bytes()
    first = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    second = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert first.add(revocations(LATER, sequence=500), now=LATER)
    second.verify_block_list(record, team="team_example", now=fixtures.NOW)
    # The second still holds only the bundle's record; it writes the first's beside it.
    assert kept_sequences(tmp_path) == [1, 500] and kept_file(tmp_path)["block_lists"]["team_example"][0] == 412
    # The second takes the first's record as its own once it checks it.
    assert not second.add(revocations(LATER, sequence=450), now=LATER)
    assert second.store.revocations_current("install_example", LATER)
    assert kept_sequences(tmp_path) == [500]


def test_a_failed_save_is_tried_again_at_the_next_change(tmp_path: Path, monkeypatch, caplog) -> None:
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    real_write = trust_state.write_private_file

    def full_disk(path, content, **options):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(trust_state, "write_private_file", full_disk)
    assert trust.add(revocations(LATER, sequence=500), now=LATER)
    assert "trust_state_save_failed" in caplog.text and kept_sequences(tmp_path) == [1]
    monkeypatch.setattr(trust_state, "write_private_file", real_write)
    # The same record again: the store already holds it, and the save it missed happens now.
    assert not trust.add(revocations(LATER, sequence=500), now=LATER)
    assert kept_sequences(tmp_path) == [500]
    assert not [name for name in os.listdir(tmp_path) if name.startswith(private_files.TEMPORARY_PREFIX)]


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    "null",
    json.dumps({"version": 1, "revocations": {}, "block_list": {}}),  # another format version
    json.dumps({"version": 2, "revocations": [], "block_lists": {}}),
    json.dumps({"version": 2, "revocations": {"install_example": [{"record": "certificate"}]}, "block_lists": {}}),
    json.dumps({"version": 2, "revocations": {}, "block_lists": {"team_example": [412]}}),
])
def test_an_unreadable_kept_file_is_set_aside_and_the_bundle_still_serves(tmp_path: Path, caplog, content: str) -> None:
    (tmp_path / TRUST_STATE_FILENAME).write_text(content)
    (tmp_path / TRUST_STATE_FILENAME).chmod(0o600)
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert "trust_state_unreadable" in caplog.text
    assert trust.store.revocations_current("install_example", fixtures.NOW)
    aside = [path for path in tmp_path.iterdir() if path.name.startswith(TRUST_STATE_FILENAME + DAMAGED_MARK)]
    assert len(aside) == 1 and aside[0].read_text() == content
    # The next save writes a new file; the damaged one stays for whoever looks into it.
    assert trust.add(revocations(LATER, sequence=500), now=LATER)
    assert kept_sequences(tmp_path) == [500] and aside[0].exists()


def test_a_kept_file_others_can_read_is_set_aside(tmp_path: Path, caplog) -> None:
    PersistentTrustStore.open(example_bundle(), tmp_path, LATER).add(revocations(LATER, sequence=500), now=LATER)
    (tmp_path / TRUST_STATE_FILENAME).chmod(0o644)
    PersistentTrustStore.open(example_bundle(), tmp_path, LATER)
    assert "trust_state_unreadable" in caplog.text and "readable by others" in caplog.text


def test_a_block_list_is_returned_whenever_it_is_current_and_never_older(tmp_path: Path) -> None:
    """The highest sequence survives a restart; the same list again, also after a restart, is still the list."""

    record = (fixtures.EXAMPLES / "state" / "valid" / "block_list" / "team.json").read_bytes()
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert trust.verify_block_list(record, team="team_example", now=fixtures.NOW).document.sequence == 412
    assert trust.verify_block_list(record, team="team_example", now=fixtures.NOW).document.sequence == 412
    reopened = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    assert reopened.verify_block_list(record, team="team_example", now=fixtures.NOW).document.sequence == 412
    older = json.loads(record)
    older["sequence"] -= 1
    resigned = json.dumps(sign_state(older, fixtures.private_key(older["writer"]["key_id"]))).encode()
    with pytest.raises(FrameRefused) as refused:
        reopened.verify_block_list(resigned, team="team_example", now=fixtures.NOW)
    assert refused.value.code == "replayed"


def test_a_block_list_read_as_another_teams_or_not_a_record_is_refused(tmp_path: Path) -> None:
    record = (fixtures.EXAMPLES / "state" / "valid" / "block_list" / "team.json").read_bytes()
    trust = PersistentTrustStore.open(example_bundle(), tmp_path, fixtures.NOW)
    with pytest.raises(FrameRefused) as refused:
        trust.verify_block_list(record, team="team_other", now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/key")
    for data in (b"{not json", b"[]", b"null"):
        with pytest.raises(FrameRefused):
            trust.verify_block_list(data, team="team_example", now=fixtures.NOW)
    assert kept_file(tmp_path)["block_lists"] == {}


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
                      PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent")), clock=lambda: fixtures.NOW)
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
                        PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent")), clock=lambda: fixtures.NOW)
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
                      PersistentTrustStore(fixtures.trust_store(), Path("/nonexistent")), clock=lambda: fixtures.NOW)
    with pytest.raises(ValidationError, match="session"):
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
