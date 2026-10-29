"""The previous release's transfer files still read here, and older ones are refused clearly.

``fixtures/team_transfer/previous-sections.json`` holds section documents that
the previous release (v0.9.2) wrote into a transfer file and this version still
reads. ``access``, ``settings``, ``knowledge`` and ``schedules`` were exported
from a small team by the v0.9.2 tag's own stores, services and exporters (fake
values); this version reads ``schedules`` (version 3) through its version 4
step and ``settings`` (version 4) through its version 5 step. ``control``,
``operator_secrets`` and ``broker_credentials`` are v0.8.5's documents: their
formats did not change since (one frozen fingerprint each in
``section-contracts.json``), so they are also v0.9.2's format. They stay frozen, independently of this release's exporters. Each must read,
through its conversion step when its section changed since
(``SECTION_UPGRADES``), and validate with this version's record models. When a
release ships, replace the fixture with that release's documents and delete
the steps from older versions.

A file from an earlier release is refused before anything is written, with a
message that names the exporting release and the release to go through.
"""

import base64
import json
from pathlib import Path

import pytest

from team_transfer.access import AccessSection
from team_transfer.container import TransferFileError, TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.format import SectionDocumentV1
from team_transfer.knowledge import KnowledgeSection
from team_transfer.people import PeopleSection
from team_transfer.registry import TRANSFER_SECTIONS
from team_transfer.sections import SECTION_UPGRADES, readable_version_problem, upgrade_records
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

FIXTURES = (
    Path(__file__).parent / "fixtures" / "team_transfer" / "previous-sections.json"
)
PASSPHRASE = "correct horse battery staple"


def test_previous_release_sections_read_and_validate_with_current_record_models():
    # The broker registers its own conversion steps; load them like the broker does.
    import secure_tool_broker.credential_transfer_api  # noqa: F401

    sections = {section.name: section for section in TRANSFER_SECTIONS}
    fixtures = json.loads(FIXTURES.read_text())
    for name, version, target in SECTION_UPGRADES:
        # The schedule section changed three times while retaining the frozen
        # source fixture; versions 4 and 5 were exported by stable releases
        # (v0.9.6, and v0.9.7 to v0.9.8).
        allowed = {fixtures[name]["schema_version"]}
        if name == "schedules":
            allowed.update({4, 5})
        # Likewise the settings section: the last stable release (v0.9.8)
        # exports version 6, and this release reads it.
        if name == "settings":
            allowed.add(6)
        assert version in allowed, f"the conversion step from {name} version {version} has no supported release fixture"
        assert target == sections[name].schema_version
    for name, raw in fixtures.items():
        section = sections[name]
        document = SectionDocumentV1.model_validate(raw)
        converted = upgrade_records(document, section.schema_version)
        if name == "broker_credentials":
            section.record_model.model_validate({"records": converted})
        else:
            for record in converted:
                section.record_model.model_validate(record)
        if name == "access":
            # The block names an operation by its catalog id, which this version still has.
            [block] = [record for record in converted if record["kind"] == "grant"]
            assert block["document"]["target"]["id"] == "fn:tool_loading#find_tools"
        if name == "schedules":
            [schedule] = [
                section.record_model.model_validate(record)
                for record in converted
                if record["kind"] == "schedule"
            ]
            # The Markdown parses into this version's ScheduledMessage.
            assert schedule.schedule.document["schedule_id"] == "sched_previous"


def test_previous_release_credentials_are_read_inside_the_broker():
    from secure_tool_broker.credential_transfer_api import (
        CredentialImportRequestV1,
        decrypt_records,
    )
    from team_transfer.crypto import canonical_json, encrypt_with_key

    raw = json.loads(FIXTURES.read_text())["broker_credentials"]
    key = bytes(range(32))
    request = CredentialImportRequestV1(
        team_id="team_a",
        encryption_key_b64=base64.b64encode(key).decode(),
        ciphertext_b64=base64.b64encode(
            encrypt_with_key(canonical_json(raw), key)
        ).decode(),
    )
    assert decrypt_records(request) == raw["records"]


def test_the_refusal_says_which_versions_this_version_reads_and_what_to_do(monkeypatch):
    older = readable_version_problem("notes", 1, 3, exported_by="0.8.4")
    assert older == (
        "This file's notes section is version 1, but this version of Universal Agents reads version 3 only. "
        "The file was exported by an older release (0.8.4): import it into a newer release that can read it "
        "and export the team again there. Repeat with newer releases until this release can read the file. "
        "See the 'Moving a team to a new version' guide for the required releases."
    )
    newer = readable_version_problem("notes", 4, 3)
    assert "reads version 3 only. The file was exported by a newer release: import it with that release" in newer

    monkeypatch.setitem(SECTION_UPGRADES, ("notes", 2, 3), lambda records: records)
    assert readable_version_problem("notes", 2, 3) == ""
    assert "reads versions 2 and 3." in readable_version_problem("notes", 1, 3)


def test_only_explicit_source_versions_are_read_when_a_section_changes_twice(monkeypatch):
    monkeypatch.setitem(SECTION_UPGRADES, ("notes", 2, 4), lambda records: records)
    previous = SectionDocumentV1(section="notes", schema_version=2, records=[{"value": "kept"}])
    assert upgrade_records(previous, 4) == [{"value": "kept"}]
    assert "reads versions 2 and 4." in readable_version_problem("notes", 3, 4)


@pytest.mark.parametrize("source_version", [3, 4])
def test_supported_schedule_sections_start_without_an_explicit_timing_revision(source_version):
    from team_transfer.schedules import SchedulesSection

    raw = json.loads(FIXTURES.read_text())["schedules"]
    raw["schema_version"] = source_version
    converted = upgrade_records(SectionDocumentV1.model_validate(raw), SchedulesSection.schema_version)
    [schedule] = [
        SchedulesSection.record_model.model_validate(record).schedule
        for record in converted if record["kind"] == "schedule"
    ]
    assert schedule.document["timing_revision"] == 0


def _run_cli(*argv: str) -> None:
    import assistant_cli
    from team_transfer.cli import run_team_command

    run_team_command(assistant_cli.build_parser().parse_args(["team", *argv]))


@pytest.mark.parametrize("source_release", ["0.8.5", "0.9.0"])
def test_an_unsupported_export_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_release: str
) -> None:
    import team_transfer.service as service
    from storage.gateway_state_repository import create_gateway_state_repository

    class SettingsAsV085(SettingsSection):
        schema_version = 3

    class AccessAsV085(AccessSection):
        schema_version = 2

    class KnowledgeAsV090(KnowledgeSection):
        schema_version = 1

    # v0.9.0 accepts v0.8.5, but its knowledge version is still too old here.
    # A single intermediate export therefore cannot be promised to work.
    source_sections, expected_problem = {
        "0.8.5": (
            (SettingsAsV085(), PeopleSection(), AccessAsV085()),
            "This file's settings section is version 3, but this version of Universal Agents reads versions 5, 6 and 7.",
        ),
        "0.9.0": (
            (SettingsSection(), PeopleSection(), AccessSection(), KnowledgeAsV090()),
            "This file's knowledge section is version 1, but this version of Universal Agents reads version 2 only.",
        ),
    }[source_release]
    monkeypatch.setattr(service, "application_version", lambda: source_release)
    config_path, config = new_instance(tmp_path, "old", "team_old")
    people = create_gateway_state_repository(config.state_storage_settings)
    people.create_gateway_user(canonical_user_id="usr_owner", display_name="Owner", timezone="UTC")
    people.create_team_membership(canonical_user_id="usr_owner", team_id="team_old", role="owner")
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path), out, PASSPHRASE, owner_person_id="usr_owner",
        sections=source_sections,
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE)
    fresh, review = tmp_path / "fresh", tmp_path / "review"

    for command in (
        ("import", "--in", str(out), "--instance-root", str(fresh), "--dry-run", "--passphrase-file", str(secret)),
        ("review", "--in", str(out), "--instance-root", str(fresh), "--review-dir", str(review), "--passphrase-file", str(secret)),
    ):
        with pytest.raises(SystemExit) as refused:
            _run_cli(*command)
        message = str(refused.value)
        assert message.startswith(expected_problem)
        assert f"exported by an older release ({source_release})" in message
        assert "Repeat with newer releases until this release can read the file." in message
        assert "See the 'Moving a team to a new version' guide for the required releases." in message

    # The CLI requires a saved review before writing. Exercise the write-capable
    # service directly to prove an unsupported file never creates the target.
    with TransferReader.open(out, PASSPHRASE) as reader:
        with pytest.raises(TransferFileError) as refused:
            import_team(reader, fresh, dry_run=False)
    assert str(refused.value).startswith(expected_problem)
    assert not fresh.exists() and not review.exists()
