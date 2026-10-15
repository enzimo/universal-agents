"""The team transfer file and the import machinery (``team_transfer``), before any real section.

Sections here are small stand-ins; each real section has its own tests. These
tests prove the parts every section relies on: the file is private and never
overwritten, a wrong passphrase or any edit is refused, member names cannot
escape, only a fresh instance root is imported into, an unfinished import
blocks the runtime, one-version-back conversion works and nothing older or
newer is read, and the command line runs end to end.
"""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from team_transfer import sections as transfer_sections
from team_transfer.container import TransferFileError, TransferReader, check_member_name
from team_transfer.crypto import TransferKeyError, decrypt, new_key_derivation
from team_transfer.format import MANIFEST_MEMBER, SectionDocumentV1
from team_transfer.sections import (
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    encrypt_section,
)
from team_transfer.service import (
    TransferError,
    export_team,
    import_team,
    require_finished_import,
)
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
# The cheapest Argon2id cost libsodium allows, so tests stay fast.
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}


class NotesSection:
    """A plain section: records, one carried file, and a refusal rule."""

    name = "notes"
    schema_version = 2
    encrypted = False
    after_start = False

    def __init__(self, notes: list[str], refuse: tuple[str, ...] = ()) -> None:
        self.notes = notes
        self.refuse = refuse
        self.applied: list[list[PlannedRecord]] = []

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        files.add("docs/readme.txt", b"carried file")
        return SectionExport(records=[{"key": note, "team": source.team_id} for note in self.notes])

    def plan(self, records: list[dict[str, Any]], context: ImportContext) -> list[PlannedRecord]:
        return [
            PlannedRecord(self.name, record["key"], RecordOutcome.REFUSED, "not allowed here")
            if record["key"] in self.refuse
            else PlannedRecord(self.name, record["key"], RecordOutcome.IMPORT)
            for record in records
        ]

    def apply(self, records: list[dict[str, Any]], plan: list[PlannedRecord], context: ImportContext) -> None:
        self.applied.append(plan)
        imported = [r.key for r in plan if r.outcome is not RecordOutcome.REFUSED]
        context.layout.root.mkdir(parents=True, exist_ok=True)
        (context.layout.root / "notes.json").write_text(json.dumps(imported))
        (context.layout.root / "readme.txt").write_bytes(context.file(self.name, "docs/readme.txt"))


class SecretsSection:
    name = "secrets"
    schema_version = 1
    encrypted = True
    after_start = True

    def __init__(self) -> None:
        self.planned = False

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        records = [{"tool": "cli:github", "value": "s3cret-value"}]
        return SectionExport(records=records, ciphertext=encrypt_section(self.name, self.schema_version, records, files.keys))

    def plan(self, records: list[dict[str, Any]], context: ImportContext) -> list[PlannedRecord]:
        self.planned = True
        return []

    def apply(self, records: list[dict[str, Any]], plan: list[PlannedRecord], context: ImportContext) -> None:
        raise AssertionError("Encrypted sections are imported by import-credentials, not team import.")


def _source() -> ExportSource:
    return ExportSource(config=None, layout=None, team_id="team_a", instance_name="alpha")  # type: ignore[arg-type]


def _export(tmp_path: Path, *sections: Any, name: str = "team.uatransfer") -> Path:
    out = tmp_path / name
    export_team(
        _source(),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=sections,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    return out


def _rewrite(path: Path, change) -> None:
    """Rewrite the archive with ``change(name, data) -> data | None`` (None drops the entry)."""

    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    path.unlink()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            changed = change(name, data)
            if changed is not None:
                archive.writestr(name, changed)


def test_export_writes_a_private_file_that_reads_back_intact(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a", "b"]), SecretsSection())

    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.partial"))
    with TransferReader.open(out, PASSPHRASE) as reader:
        manifest = reader.manifest
        assert (manifest.team_id, manifest.instance_name, manifest.exported_by.owner_person_id) == (
            "team_a",
            "alpha",
            "usr_owner",
        )
        assert [(s.name, s.schema_version, s.record_count, s.encrypted) for s in manifest.sections] == [
            ("notes", 2, 2, False),
            ("secrets", 1, 1, True),
        ]
        document = reader.section_document("notes")
        assert document is not None and [r["key"] for r in document.records] == ["a", "b"]
        assert reader.read_member("files/notes/docs/readme.txt") == b"carried file"
        ciphertext = reader.read_member("credentials/secrets.enc")
        assert b"s3cret-value" not in ciphertext
        assert json.loads(decrypt(ciphertext, reader.keys))["records"][0]["value"] == "s3cret-value"
    assert b"s3cret-value" not in out.read_bytes()


def test_export_never_overwrites_and_refuses_a_short_passphrase(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a"]))
    before = out.read_bytes()

    with pytest.raises(TransferFileError, match="already exists"):
        _export(tmp_path, NotesSection(["b"]))
    with pytest.raises(TransferKeyError, match="at least 12"):
        export_team(_source(), tmp_path / "other.uatransfer", "short", owner_person_id="usr_owner", sections=())

    assert out.read_bytes() == before
    assert not (tmp_path / "other.uatransfer").exists()


def test_a_failing_section_leaves_no_file(tmp_path: Path) -> None:
    class Broken(NotesSection):
        def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
            files.add("half.txt", b"partial")
            raise RuntimeError("store unavailable")

    with pytest.raises(RuntimeError):
        _export(tmp_path, Broken([]))

    assert list(tmp_path.iterdir()) == []


def test_a_wrong_passphrase_is_refused(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a"]))

    with pytest.raises(TransferKeyError, match="passphrase is wrong"):
        TransferReader.open(out, "not the right passphrase")


@pytest.mark.parametrize(
    "change",
    [
        # An edited section keeps the manifest's MAC but not its digest.
        lambda name, data: data.replace(b'"a"', b'"z"') if name == "sections/notes.json" else data,
        # An edited manifest no longer matches its MAC.
        lambda name, data: data.replace(b"team_a", b"team_x") if name == MANIFEST_MEMBER else data,
        # A removed member.
        lambda name, data: None if name == "files/notes/docs/readme.txt" else data,
    ],
    ids=["edited-section", "edited-manifest", "removed-member"],
)
def test_any_change_after_export_is_refused(tmp_path: Path, change) -> None:
    out = _export(tmp_path, NotesSection(["a"]))
    _rewrite(out, change)

    with pytest.raises((TransferFileError, TransferKeyError)):
        TransferReader.open(out, PASSPHRASE)


def test_an_added_member_is_refused(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a"]))
    with zipfile.ZipFile(out, "a") as archive:
        archive.writestr("files/notes/extra.txt", b"smuggled")

    with pytest.raises(TransferFileError, match="do not match its manifest"):
        TransferReader.open(out, PASSPHRASE)


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a//b", "a/./b", "a\\b", "c:/x", "dir/", ""])
def test_unsafe_member_names_are_refused(name: str) -> None:
    with pytest.raises(TransferFileError):
        check_member_name(name)


def test_unsafe_archive_entries_are_refused_before_the_manifest_is_read(tmp_path: Path) -> None:
    out = tmp_path / "evil.uatransfer"
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("../escape.txt", b"x")

    with pytest.raises(TransferFileError, match="unsafe entry name"):
        TransferReader.open(out, PASSPHRASE)


def test_a_file_that_is_not_a_transfer_file_is_named_as_such(tmp_path: Path) -> None:
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, json.dumps({"format": "something-else"}))

    with pytest.raises(TransferFileError, match="not a Universal Agents team transfer file"):
        TransferReader.open(plain, PASSPHRASE)
    (tmp_path / "text.uatransfer").write_text("hello")
    with pytest.raises(TransferFileError, match="not a ZIP archive"):
        TransferReader.open(tmp_path / "text.uatransfer", PASSPHRASE)


def test_dry_run_plans_every_record_and_writes_nothing(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a", "b"]), SecretsSection())
    target = tmp_path / "new"
    notes = NotesSection([], refuse=("b",))
    secrets = SecretsSection()

    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=True, sections=(notes, secrets))

    assert [(r.key, r.outcome) for r in plan.records] == [("a", RecordOutcome.IMPORT), ("b", RecordOutcome.REFUSED)]
    assert plan.later_sections == ["secrets"] and not secrets.planned
    assert notes.applied == []
    assert not target.exists()


def test_import_applies_writes_a_report_and_clears_its_marker(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a", "b"]))
    target = tmp_path / "new"
    notes = NotesSection([], refuse=("b",))

    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=(notes,))

    assert json.loads((target / "notes.json").read_text()) == ["a"]
    assert (target / "readme.txt").read_bytes() == b"carried file"
    reports = list((target / "runtime" / "team-import").glob("report-*.json"))
    assert len(reports) == 1 and stat.S_IMODE(reports[0].stat().st_mode) == 0o600
    report = json.loads(reports[0].read_text())
    assert report["finished"] is True and report["source"]["team_id"] == "team_a"
    assert [(r["key"], r["outcome"]) for r in report["records"]] == [("a", "import"), ("b", "refused")]
    assert not (target / "runtime" / "team-import" / "in-progress.json").exists()
    assert plan.by_outcome(RecordOutcome.REFUSED)[0].reason == "not allowed here"


def test_a_failed_import_blocks_the_runtime_and_a_retry_into_the_same_root(tmp_path: Path) -> None:
    from runtime_settings.instance_layout import InstanceLayout

    class Failing(NotesSection):
        def apply(self, records, plan, context) -> None:
            raise RuntimeError("disk full")

    out = _export(tmp_path, NotesSection(["a"]))
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader, pytest.raises(RuntimeError, match="disk full"):
        import_team(reader, target, dry_run=False, sections=(Failing([]),))

    layout = InstanceLayout.at(target)
    with pytest.raises(TransferError, match="did not finish, so this instance must not start"):
        require_finished_import(layout)
    with TransferReader.open(out, PASSPHRASE) as reader, pytest.raises(TransferError, match="did not finish"):
        import_team(reader, target, dry_run=True, sections=(NotesSection([]),))


def test_only_a_fresh_instance_root_is_imported_into(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a"]))
    target = tmp_path / "existing"
    target.mkdir()
    (target / "instance_config.yaml").write_text("instance_name: existing\n")

    with TransferReader.open(out, PASSPHRASE) as reader, pytest.raises(TransferError, match="fresh instance root"):
        import_team(reader, target, dry_run=True, sections=(NotesSection([]),))


def test_sections_this_version_does_not_know_or_the_file_lacks_are_listed(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a"]))

    class Other(NotesSection):
        name = "other"

    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, tmp_path / "new", dry_run=True, sections=(Other([]),))

    assert plan.unknown_sections == ["notes"]
    assert plan.missing_sections == ["other"]
    assert plan.records == []


def test_a_section_one_version_old_is_converted_and_older_or_newer_ones_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = SectionDocumentV1(section="notes", schema_version=1, records=[{"note": "a"}])
    monkeypatch.setitem(
        transfer_sections.SECTION_UPGRADES,
        ("notes", 1),
        lambda records: [{"key": record["note"]} for record in records],
    )

    assert transfer_sections.upgrade_records(old, 2) == [{"key": "a"}]
    with pytest.raises(TransferFileError, match="reads version 3 only. The file was exported by an older release"):
        transfer_sections.upgrade_records(old, 3)
    newer = SectionDocumentV1(section="notes", schema_version=4, records=[])
    with pytest.raises(TransferFileError, match="reads versions 1 and 2. The file was exported by a newer release"):
        transfer_sections.upgrade_records(newer, 2)


# -- the command line ------------------------------------------------------------


def _run_cli(*argv: str) -> None:
    import assistant_cli
    from team_transfer.cli import run_team_command

    run_team_command(assistant_cli.build_parser().parse_args(["team", *argv]))


def _owner_instance(tmp_path: Path) -> tuple[Path, Any]:
    from datetime import datetime, timezone

    from storage.access_repository import AccessRepository

    path, config = new_instance(tmp_path, "old", "team_old")
    now = datetime.now(timezone.utc)
    AccessRepository(config.manager_storage_settings).replace_people(
        "team_old",
        [
            TeamPersonV1(team_id="team_old", person_id="usr_owner", standing=PersonStanding.ACTIVE, roles=("owner", "admin"), changed_at_utc=now),
            TeamPersonV1(team_id="team_old", person_id="usr_member", standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now),
        ],
        at=now,
    )
    return path, config


def test_cli_exports_inspects_and_dry_runs_an_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import team_transfer.registry as registry
    from team_transfer import crypto

    monkeypatch.setattr(registry, "TRANSFER_SECTIONS", (NotesSection(["a"]),))
    monkeypatch.setattr(crypto, "DEFAULT_OPSLIMIT", 1)
    monkeypatch.setattr(crypto, "DEFAULT_MEMLIMIT", 8192)
    config_path, _config = _owner_instance(tmp_path)
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE + "\n")
    out = tmp_path / "team.uatransfer"

    _run_cli("export", "--config", str(config_path), "--owner", "usr_owner", "--out", str(out), "--passphrase-file", str(secret))
    printed = capsys.readouterr().out
    assert "Team team_old from instance old" in printed and "- notes v2: 1 record(s)" in printed
    assert "Stop this instance now" in printed

    _run_cli("inspect", "--in", str(out), "--passphrase-file", str(secret))
    assert "The file is intact and the passphrase matches." in capsys.readouterr().out

    _run_cli("import", "--in", str(out), "--instance-root", str(tmp_path / "fresh"), "--dry-run", "--passphrase-file", str(secret))
    printed = capsys.readouterr().out
    assert "Would import 1 of 1 record(s)" in printed and "Nothing was written (dry run)." in printed
    assert not (tmp_path / "fresh").exists()


@pytest.mark.parametrize("owner", ["usr_member", "usr_unknown"])
def test_cli_export_requires_an_active_owner(tmp_path: Path, owner: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import team_transfer.registry as registry

    monkeypatch.setattr(registry, "TRANSFER_SECTIONS", ())
    config_path, _config = _owner_instance(tmp_path)
    secret = tmp_path / "passphrase.txt"
    secret.write_text(PASSPHRASE)

    with pytest.raises(SystemExit, match="is not an active owner of team team_old"):
        _run_cli("export", "--config", str(config_path), "--owner", owner, "--out", str(tmp_path / "x"), "--passphrase-file", str(secret))
    assert not (tmp_path / "x").exists()


def test_cli_reports_a_wrong_passphrase_without_a_traceback(tmp_path: Path) -> None:
    out = _export(tmp_path, NotesSection(["a"]))
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("definitely not it")

    with pytest.raises(SystemExit, match="passphrase is wrong"):
        _run_cli("inspect", "--in", str(out), "--passphrase-file", str(wrong))


def test_the_launcher_refuses_an_instance_whose_import_did_not_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    import docker.launch_headless as launch_headless

    config_path, config = new_instance(tmp_path, "half", "team_half")
    marker = config.layout.team_import_marker_path
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")

    def no_run_lock(_path: Path) -> None:
        raise AssertionError("The run lock must not be taken for a half-imported instance.")

    monkeypatch.setattr(launch_headless, "acquire_process_run", no_run_lock)
    monkeypatch.setattr("sys.argv", ["launch_headless", "--config", str(config_path)])

    assert launch_headless.main() == 2
    assert "event=team_import_unfinished" in caplog.text
