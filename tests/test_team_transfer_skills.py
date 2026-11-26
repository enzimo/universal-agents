"""The ``skills`` transfer section carries operator skills and reviewed installs with their attestations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from runtime_settings.instance_layout import InstanceLayout
from team_transfer.container import TransferFileError, TransferReader, TransferWriter
from team_transfer.control import ControlSection
from team_transfer.crypto import derive_keys, new_key_derivation
from team_transfer.format import (
    ExporterV1,
    SectionDocumentV1,
    SectionEntryV1,
    TransferManifestV1,
)
from team_transfer.sections import ImportContext, PlannedRecord, RecordOutcome
from team_transfer.service import (
    application_version,
    export_source,
    export_team,
    import_team,
    plan_import,
    utc_now_iso,
)
from team_transfer.settings import SettingsSection
from team_transfer.skills import SkillFileV1, SkillKind, SkillUnitV1, SkillsSection
from team_transfer.team_ids import TeamIds
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), ControlSection(), SkillsSection())


def _install(layout: Any, name: str) -> Path:
    """A reviewed install as SkillInstallationService.apply leaves it (tree plus attestation)."""

    from custom_agents.skill_repository import build_skill_tree_manifest

    skill = layout.installed_skills_dir / name
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Demo.\n---\nUse it.\n")
    (skill / "scripts" / "run.py").write_text("print('ok')\n")
    attestations = layout.skill_installation_dir / "attestations"
    attestations.mkdir(parents=True, exist_ok=True)
    attestation = {
        "schema_version": 2,
        "change_id": f"skill_install_{name}",
        "skill_name": name,
        "installed_path": str(skill.resolve()),
        "tree_sha256": build_skill_tree_manifest(skill).tree_sha256,
        "source_kind": "github",
    }
    path = attestations / f"{name}.json"
    path.write_text(json.dumps(attestation))
    path.chmod(0o444)
    return skill


def _old_instance(tmp_path: Path) -> Path:
    from assistant_gateway.bootstrap import bootstrap_instance

    path, config = new_instance(tmp_path, "alpha", "team_a")
    bootstrap_instance(config)
    operator = config.layout.control_dir / "skills" / "travel-tips"
    operator.mkdir(parents=True)
    (operator / "SKILL.md").write_text("---\nname: travel-tips\ndescription: Tips.\n---\nBe brief.\n")
    script = operator / "check.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    script.chmod(0o755)
    (config.layout.control_dir / "skills" / ".ua-external-skills.json").write_text("{}\n")
    _install(config.layout, "approved-skill")
    tampered = _install(config.layout, "tampered-skill")
    (tampered / "scripts" / "run.py").write_text("print('changed after approval')\n")
    return path


def _export_import(tmp_path: Path, *, dry_run: bool = False):
    config_path = _old_instance(tmp_path)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=dry_run, sections=SECTIONS)
    return plan, target


def test_skills_arrive_with_scripts_runnable_and_attestations_rewritten(tmp_path: Path) -> None:
    from custom_agents.skill_installation import verify_managed_skill_attestation_paths
    from runtime_settings.instance_layout import InstanceLayout

    plan, target = _export_import(tmp_path)

    outcomes = {record.key: record for record in plan.records if record.section == "skills"}
    assert outcomes["operator/travel-tips"].outcome is RecordOutcome.IMPORT
    assert outcomes["operator/.ua-external-skills.json"].outcome is RecordOutcome.IMPORT
    assert outcomes["installed/approved-skill"].outcome is RecordOutcome.IMPORT
    tampered = outcomes["installed/tampered-skill"]
    assert tampered.outcome is RecordOutcome.REFUSED and "changed after their reviewed installation" in tampered.reason

    layout = InstanceLayout.at(target)
    script = layout.control_dir / "skills" / "travel-tips" / "check.sh"
    assert os.access(script, os.X_OK)
    assert (layout.control_dir / "skills" / ".ua-external-skills.json").read_text() == "{}\n"
    installed = layout.installed_skills_dir / "approved-skill"
    attestations = layout.skill_installation_dir / "attestations"
    attestation = json.loads((attestations / "approved-skill.json").read_text())
    assert attestation["installed_path"] == str(installed.resolve())
    assert stat.S_IMODE((attestations / "approved-skill.json").stat().st_mode) == 0o444
    verify_managed_skill_attestation_paths(
        skill_path=installed, installed_root=layout.installed_skills_dir, attestations_root=attestations
    )
    assert not (layout.installed_skills_dir / "tampered-skill").exists()


def test_dry_run_checks_installs_without_writing(tmp_path: Path) -> None:
    plan, target = _export_import(tmp_path, dry_run=True)

    assert {r.key for r in plan.records if r.outcome is RecordOutcome.REFUSED} == {"installed/tampered-skill"}
    assert not target.exists()


class _MemoryReader:
    """Minimal TransferReader stand-in for direct SkillsSection plan/apply tests."""

    def __init__(self, members: dict[str, bytes]) -> None:
        self._members = dict(members)

    def read_member(self, name: str) -> bytes:
        if name not in self._members:
            raise TransferFileError(f"The file has no member {name!r}.")
        return self._members[name]


def _file_record(path: str, data: bytes, *, executable: bool = False) -> dict[str, Any]:
    return {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "executable": executable,
    }


def _context(root: Path, members: dict[str, bytes]) -> ImportContext:
    layout = InstanceLayout.at(root)
    return ImportContext(
        reader=_MemoryReader(members),  # type: ignore[arg-type]
        target_root=layout.root,
        layout=layout,
        versions={"skills": 1},
        team_ids=TeamIds.unchanged("team_a"),
    )


def _write_skills_archive(
    out: Path,
    skills_records: list[dict[str, Any]],
    *,
    files: dict[str, bytes] | None = None,
) -> Path:
    derivation = new_key_derivation(**FAST_KEYS)
    keys = derive_keys(PASSPHRASE, derivation)
    writer = TransferWriter.create(out)
    try:
        for member_name, data in (files or {}).items():
            writer.add_bytes(member_name, data)
        writer.add_section(
            SectionDocumentV1(
                section="skills",
                schema_version=SkillsSection.schema_version,
                records=skills_records,
            )
        )
        manifest = TransferManifestV1(
            application_version=application_version(),
            exported_at_utc=utc_now_iso(),
            team_id="team_a",
            instance_name="alpha",
            exported_by=ExporterV1(owner_person_id="usr_owner"),
            key_derivation=derivation,
            sections=(
                SectionEntryV1(
                    name="skills",
                    schema_version=SkillsSection.schema_version,
                    record_count=len(skills_records),
                    encrypted=False,
                ),
            ),
        )
        writer.publish(manifest, keys)
    except BaseException:
        writer.discard()
        raise
    return out


def _snapshot_tree(root: Path) -> list[tuple[str, str, int, int]]:
    if not root.exists():
        return []
    entries: list[tuple[str, str, int, int]] = []
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
        size = 0 if kind == "dir" else st.st_size
        entries.append((path.relative_to(root).as_posix(), kind, size, stat.S_IMODE(st.st_mode)))
    return entries


def _installed_record_and_members(
    name: str,
    files: dict[str, bytes],
    *,
    executable: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    from custom_agents.skill_repository import build_skill_tree_manifest_from_files

    exec_paths = executable or set()
    manifest = build_skill_tree_manifest_from_files(files.items())
    record = {
        "name": name,
        "kind": SkillKind.INSTALLED.value,
        "files": [
            _file_record(path, data, executable=(path in exec_paths))
            for path, data in sorted(files.items())
        ],
        "attestation": {
            "schema_version": 2,
            "change_id": f"skill_install_{name}",
            "skill_name": name,
            "installed_path": f"/old-instance/installed/{name}",
            "tree_sha256": manifest.tree_sha256,
            "source_kind": "github",
        },
    }
    members = {f"files/skills/installed/{name}/{path}": data for path, data in files.items()}
    return record, members


def test_plan_import_refuses_unsafe_skill_names_and_leaves_scratch_folder_intact(
    tmp_path: Path,
) -> None:
    """H1: absolute path and '../' skill names are refused and dry run never deletes the scratch folder."""

    scratch_victim = tmp_path / "scratch-victim"
    scratch_victim.mkdir()
    sentinel = scratch_victim / "sentinel.txt"
    sentinel.write_text("keep-me\n", encoding="utf-8")

    dummy_file = _file_record("SKILL.md", b"---\nname: x\ndescription: x\n---\n")
    abs_archive = _write_skills_archive(
        tmp_path / "abs.uatransfer",
        [
            {
                "name": str(scratch_victim),
                "kind": SkillKind.INSTALLED.value,
                "files": [dummy_file],
                "attestation": {
                    "schema_version": 2,
                    "skill_name": str(scratch_victim),
                    "installed_path": str(scratch_victim),
                    "tree_sha256": "0" * 64,
                },
            }
        ],
    )
    target = tmp_path / "target-abs"
    with TransferReader.open(abs_archive, PASSPHRASE) as reader:
        with pytest.raises(ValidationError):
            plan_import(reader, target, sections=(SkillsSection(),))

    assert scratch_victim.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep-me\n"
    assert not target.exists()

    dotdot_archive = _write_skills_archive(
        tmp_path / "dotdot.uatransfer",
        [
            {
                "name": "../scratch-victim",
                "kind": SkillKind.INSTALLED.value,
                "files": [dummy_file],
                "attestation": {
                    "schema_version": 2,
                    "skill_name": "../scratch-victim",
                    "installed_path": str(scratch_victim),
                    "tree_sha256": "0" * 64,
                },
            }
        ],
    )
    target_dotdot = tmp_path / "target-dotdot"
    with TransferReader.open(dotdot_archive, PASSPHRASE) as reader:
        with pytest.raises(ValidationError):
            plan_import(reader, target_dotdot, sections=(SkillsSection(),))

    assert scratch_victim.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep-me\n"
    assert not target_dotdot.exists()


def test_apply_with_missing_or_corrupt_member_leaves_existing_skills_unchanged(
    tmp_path: Path,
) -> None:
    """H2: direct apply() with a missing or mismatched member never deletes the existing skill."""

    from custom_agents.skill_installation import verify_managed_skill_attestation_paths

    target = tmp_path / "instance"
    layout = InstanceLayout.at(target)
    operator_root = layout.control_dir / "skills"
    existing_op = operator_root / "travel-tips"
    existing_op.mkdir(parents=True)
    (existing_op / "SKILL.md").write_text("original-op-skill\n", encoding="utf-8")
    (existing_op / "notes.txt").write_text("original-notes\n", encoding="utf-8")
    existing_loose = operator_root / ".ua-external-skills.json"
    existing_loose.write_text('{"keep": true}\n', encoding="utf-8")

    existing_inst = _install(layout, "approved-skill")
    attestations_root = layout.skill_installation_dir / "attestations"
    original_attestation_bytes = (attestations_root / "approved-skill.json").read_bytes()
    original_op_snapshot = _snapshot_tree(operator_root)
    original_inst_snapshot = _snapshot_tree(layout.installed_skills_dir)

    section = SkillsSection()

    # 1. Operator folder with a missing second member.
    op_skill_md = b"---\nname: travel-tips\ndescription: New.\n---\n"
    op_extra = b"new-extra\n"
    op_record = {
        "name": "travel-tips",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record("SKILL.md", op_skill_md), _file_record("extra.txt", op_extra)],
    }
    op_plan = [PlannedRecord("skills", "operator/travel-tips", RecordOutcome.IMPORT)]
    with pytest.raises(TransferFileError):
        section.apply(
            [op_record],
            op_plan,
            _context(target, {"files/skills/skills/travel-tips/SKILL.md": op_skill_md}),
        )
    assert _snapshot_tree(operator_root) == original_op_snapshot
    assert (existing_op / "SKILL.md").read_text(encoding="utf-8") == "original-op-skill\n"
    assert sorted(p.name for p in operator_root.iterdir()) == [".ua-external-skills.json", "travel-tips"]

    # 2. Operator folder with corrupt member bytes (SHA-256 mismatch).
    with pytest.raises(TransferFileError):
        section.apply(
            [op_record],
            op_plan,
            _context(
                target,
                {
                    "files/skills/skills/travel-tips/SKILL.md": op_skill_md,
                    "files/skills/skills/travel-tips/extra.txt": b"wrong-bytes",
                },
            ),
        )
    assert _snapshot_tree(operator_root) == original_op_snapshot
    assert sorted(p.name for p in operator_root.iterdir()) == [".ua-external-skills.json", "travel-tips"]

    # 3. Operator loose file with missing member.
    loose_record = {
        "name": ".ua-external-skills.json",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record(".", b'{"new": true}\n')],
    }
    loose_plan = [
        PlannedRecord("skills", "operator/.ua-external-skills.json", RecordOutcome.IMPORT)
    ]
    with pytest.raises(TransferFileError):
        section.apply([loose_record], loose_plan, _context(target, {}))
    assert existing_loose.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert sorted(p.name for p in operator_root.iterdir()) == [".ua-external-skills.json", "travel-tips"]

    # 4. Installed skill with a missing second member.
    v2_files = {
        "SKILL.md": b"---\nname: approved-skill\ndescription: v2\n---\nUpdated.\n",
        "scripts/run.py": b"print('v2')\n",
    }
    inst_record, inst_members = _installed_record_and_members("approved-skill", v2_files)
    inst_plan = [PlannedRecord("skills", "installed/approved-skill", RecordOutcome.IMPORT)]
    missing_member_map = {
        k: v for k, v in inst_members.items() if not k.endswith("scripts/run.py")
    }
    with pytest.raises(TransferFileError):
        section.apply([inst_record], inst_plan, _context(target, missing_member_map))
    assert _snapshot_tree(layout.installed_skills_dir) == original_inst_snapshot
    assert (attestations_root / "approved-skill.json").read_bytes() == original_attestation_bytes
    assert sorted(p.name for p in layout.installed_skills_dir.iterdir()) == ["approved-skill"]
    verify_managed_skill_attestation_paths(
        skill_path=existing_inst,
        installed_root=layout.installed_skills_dir,
        attestations_root=attestations_root,
    )

    # 5. Installed skill with wrong member bytes.
    corrupt_members = dict(inst_members)
    corrupt_members["files/skills/installed/approved-skill/scripts/run.py"] = b"print('bad')\n"
    with pytest.raises(TransferFileError):
        section.apply([inst_record], inst_plan, _context(target, corrupt_members))
    assert _snapshot_tree(layout.installed_skills_dir) == original_inst_snapshot
    assert (attestations_root / "approved-skill.json").read_bytes() == original_attestation_bytes
    assert sorted(p.name for p in layout.installed_skills_dir.iterdir()) == ["approved-skill"]
    verify_managed_skill_attestation_paths(
        skill_path=existing_inst,
        installed_root=layout.installed_skills_dir,
        attestations_root=attestations_root,
    )


def test_skill_unit_and_file_validators() -> None:
    valid_file = SkillFileV1(path="SKILL.md", size=1, sha256="a" * 64)

    for bad_name in ("", ".", "..", "/abs/path", "../escape", "a/b", "a\\b", "C:", "bad\x00name"):
        with pytest.raises(ValidationError):
            SkillUnitV1(name=bad_name, kind=SkillKind.OPERATOR, files=(valid_file,))

    for bad_path in (
        "",
        "/etc/passwd",
        "../SKILL.md",
        "a/../b",
        "./SKILL.md",
        "a//b",
        "dir/",
        "a\\b",
        "C:/x.md",
        "bad\x00file",
    ):
        with pytest.raises(ValidationError):
            SkillFileV1(path=bad_path, size=1, sha256="a" * 64)

    # '.' is valid only as the sole file of a unit.
    with pytest.raises(ValidationError):
        SkillUnitV1(
            name="loose",
            kind=SkillKind.OPERATOR,
            files=(
                SkillFileV1(path=".", size=1, sha256="a" * 64),
                valid_file,
            ),
        )

    # Duplicate, case-colliding, and parent/child colliding file paths are rejected.
    for bad_paths in (
        ("SKILL.md", "skill.md"),
        ("SKILL.md", "docs", "docs/x.md"),
        ("SKILL.md", "Docs/x.md", "docs"),
        ("SKILL.md", "docs", "Docs/x.md"),
    ):
        with pytest.raises(ValidationError):
            SkillUnitV1(
                name="colliding",
                kind=SkillKind.OPERATOR,
                files=tuple(SkillFileV1(path=p, size=1, sha256="a" * 64) for p in bad_paths),
            )

    # Operator loose dotfile and operator file with a tab remain accepted.
    loose = SkillUnitV1(
        name=".ua-external-skills.json",
        kind=SkillKind.OPERATOR,
        files=(SkillFileV1(path=".", size=3, sha256="a" * 64),),
    )
    assert loose.key == "operator/.ua-external-skills.json"
    tabbed = SkillUnitV1(
        name="notes-skill",
        kind=SkillKind.OPERATOR,
        files=(SkillFileV1(path="notes\tdraft.md", size=1, sha256="a" * 64),),
    )
    assert tabbed.files[0].path == "notes\tdraft.md"


def test_export_skips_installed_dotfiles_and_stray_files_and_import_refuses_legacy_ones_per_record(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = _old_instance(tmp_path)
    source = export_source(config_path)

    # Plant a killed install's staging folder and a stray dotfile in installed/,
    # plus a leftover transfer staging folder in control/skills/.
    leftover_incoming = source.layout.installed_skills_dir / ".incoming-chg1-abc"
    leftover_incoming.mkdir()
    (leftover_incoming / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")
    (source.layout.installed_skills_dir / ".DS_Store").write_bytes(b"junk")
    (source.layout.installed_skills_dir / "stray-file.txt").write_text("stray")
    leftover_op = source.layout.control_dir / "skills" / ".incoming-transfer-deadbeef"
    leftover_op.mkdir()
    (leftover_op / "SKILL.md").write_text("leftover")

    out = tmp_path / "clean-export.uatransfer"
    with caplog.at_level(logging.WARNING, logger="team_transfer.skills"):
        export_team(
            source,
            out,
            PASSPHRASE,
            owner_person_id="usr_owner",
            sections=SECTIONS,
            key_derivation=new_key_derivation(**FAST_KEYS),
        )
    with TransferReader.open(out, PASSPHRASE) as reader:
        doc = reader.section_document("skills")
        assert doc is not None
        exported_keys = {f"{r['kind']}/{r['name']}" for r in doc.records}
    assert "installed/approved-skill" in exported_keys
    assert "operator/travel-tips" in exported_keys
    assert "operator/.ua-external-skills.json" in exported_keys
    assert "installed/.incoming-chg1-abc" not in exported_keys
    assert "installed/.DS_Store" not in exported_keys
    assert "installed/stray-file.txt" not in exported_keys
    assert "operator/.incoming-transfer-deadbeef" not in exported_keys
    assert ".incoming-chg1-abc" in caplog.text
    assert ".DS_Store" in caplog.text

    # Now import a legacy archive containing records shaped like an older release's export
    # of .incoming-chg1-abc and .DS_Store alongside a valid installed and operator skill.
    valid_inst_record, valid_inst_members = _installed_record_and_members(
        "approved-skill",
        {"SKILL.md": b"---\nname: approved-skill\ndescription: ok\n---\nUse it.\n"},
    )
    op_skill_bytes = b"---\nname: travel-tips\ndescription: ok\n---\nUse it.\n"
    valid_op_record = {
        "name": "travel-tips",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record("SKILL.md", op_skill_bytes)],
    }
    legacy_incoming_bytes = b"partial\n"
    legacy_ds_bytes = b"junk"
    legacy_archive = _write_skills_archive(
        tmp_path / "legacy.uatransfer",
        [
            {
                "name": ".incoming-chg1-abc",
                "kind": SkillKind.INSTALLED.value,
                "files": [_file_record("SKILL.md", legacy_incoming_bytes)],
                "attestation": None,
            },
            {
                "name": ".DS_Store",
                "kind": SkillKind.INSTALLED.value,
                "files": [_file_record(".", legacy_ds_bytes)],
                "attestation": None,
            },
            valid_inst_record,
            valid_op_record,
        ],
        files={
            **valid_inst_members,
            "files/skills/skills/travel-tips/SKILL.md": op_skill_bytes,
            "files/skills/installed/.incoming-chg1-abc/SKILL.md": legacy_incoming_bytes,
            "files/skills/installed/.DS_Store": legacy_ds_bytes,
        },
    )

    legacy_target = tmp_path / "legacy-target"
    with TransferReader.open(legacy_archive, PASSPHRASE) as reader:
        dry_plan = import_team(
            reader, legacy_target, dry_run=True, sections=(SkillsSection(),)
        )
        dry_outcomes = {r.key: r.outcome for r in dry_plan.records}
        assert dry_outcomes == {
            "installed/.incoming-chg1-abc": RecordOutcome.REFUSED,
            "installed/.DS_Store": RecordOutcome.REFUSED,
            "installed/approved-skill": RecordOutcome.IMPORT,
            "operator/travel-tips": RecordOutcome.IMPORT,
        }
        assert not legacy_target.exists()

        live_plan = import_team(
            reader, legacy_target, dry_run=False, sections=(SkillsSection(),)
        )
    live_outcomes = {r.key: r.outcome for r in live_plan.records}
    assert live_outcomes["installed/approved-skill"] is RecordOutcome.IMPORT
    assert live_outcomes["operator/travel-tips"] is RecordOutcome.IMPORT
    assert live_outcomes["installed/.incoming-chg1-abc"] is RecordOutcome.REFUSED
    assert live_outcomes["installed/.DS_Store"] is RecordOutcome.REFUSED
    layout = InstanceLayout.at(legacy_target)
    assert (layout.installed_skills_dir / "approved-skill" / "SKILL.md").is_file()
    assert (layout.control_dir / "skills" / "travel-tips" / "SKILL.md").is_file()
    assert not (layout.installed_skills_dir / ".incoming-chg1-abc").exists()
    assert not (layout.installed_skills_dir / ".DS_Store").exists()


def test_plan_performs_no_filesystem_writes_or_tempdirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "existing-target"
    layout = InstanceLayout.at(target)
    _install(layout, "approved-skill")
    before = _snapshot_tree(target)

    good_record, good_members = _installed_record_and_members(
        "approved-skill",
        {"SKILL.md": b"---\nname: approved-skill\ndescription: v2\n---\nNew.\n"},
    )
    tampered_record, tampered_members = _installed_record_and_members(
        "tampered-skill",
        {"SKILL.md": b"---\nname: tampered-skill\ndescription: ok\n---\n"},
    )
    tampered_members["files/skills/installed/tampered-skill/SKILL.md"] = (
        b"---\nname: tampered-skill\ndescription: tampered\n---\n"
    )
    tampered_record["files"] = [
        _file_record("SKILL.md", tampered_members["files/skills/installed/tampered-skill/SKILL.md"])
    ]
    op_missing_record = {
        "name": "missing-op",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record("SKILL.md", b"missing")],
    }
    ctx = _context(target, {**good_members, **tampered_members})

    def forbid(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("SkillsSection.plan must not write, delete, or create temp dirs")

    monkeypatch.setattr(tempfile, "mkdtemp", forbid)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", forbid)
    monkeypatch.setattr(shutil, "rmtree", forbid)
    monkeypatch.setattr(os, "rename", forbid)
    monkeypatch.setattr(os, "replace", forbid)
    monkeypatch.setattr(Path, "write_bytes", forbid)

    planned = SkillsSection().plan([good_record, tampered_record, op_missing_record], ctx)
    by_key = {record.key: record for record in planned}
    assert by_key["installed/approved-skill"].outcome is RecordOutcome.IMPORT
    assert by_key["installed/tampered-skill"].outcome is RecordOutcome.REFUSED
    assert "changed after their reviewed installation" in by_key["installed/tampered-skill"].reason
    assert by_key["operator/missing-op"].outcome is RecordOutcome.REFUSED
    assert _snapshot_tree(target) == before


def test_apply_installed_skill_rolls_back_folder_and_attestation_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_agents.governance.authorized_operations as authorized_operations
    import custom_agents.skill_installation as skill_installation

    target = tmp_path / "instance"
    layout = InstanceLayout.at(target)
    existing_skill = _install(layout, "approved-skill")
    attestations_root = layout.skill_installation_dir / "attestations"
    v1_attestation = (attestations_root / "approved-skill.json").read_bytes()
    v1_run_py = (existing_skill / "scripts" / "run.py").read_bytes()

    v2_record, v2_members = _installed_record_and_members(
        "approved-skill",
        {
            "SKILL.md": b"---\nname: approved-skill\ndescription: v2\n---\n",
            "scripts/run.py": b"print('v2')\n",
        },
    )
    plan = [PlannedRecord("skills", "installed/approved-skill", RecordOutcome.IMPORT)]
    section = SkillsSection()

    # Case A: final on-disk check fails after new attestation is written.
    with monkeypatch.context() as patch:
        patch.setattr(
            skill_installation,
            "verify_managed_skill_attestation_paths",
            lambda **_kwargs: (_ for _ in ()).throw(PermissionError("final check failed")),
        )
        with pytest.raises(PermissionError, match="final check failed"):
            section.apply([v2_record], plan, _context(target, v2_members))

    assert (existing_skill / "scripts" / "run.py").read_bytes() == v1_run_py
    assert (attestations_root / "approved-skill.json").read_bytes() == v1_attestation
    assert stat.S_IMODE((attestations_root / "approved-skill.json").stat().st_mode) == 0o444
    assert sorted(p.name for p in layout.installed_skills_dir.iterdir()) == ["approved-skill"]
    assert sorted(p.name for p in attestations_root.iterdir()) == ["approved-skill.json"]
    skill_installation.verify_managed_skill_attestation_paths(
        skill_path=existing_skill,
        installed_root=layout.installed_skills_dir,
        attestations_root=attestations_root,
    )

    # Case B: attestation write fails after directory swap.
    with monkeypatch.context() as patch:
        patch.setattr(
            authorized_operations,
            "atomic_write_bytes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            section.apply([v2_record], plan, _context(target, v2_members))

    assert (existing_skill / "scripts" / "run.py").read_bytes() == v1_run_py
    assert (attestations_root / "approved-skill.json").read_bytes() == v1_attestation
    assert sorted(p.name for p in layout.installed_skills_dir.iterdir()) == ["approved-skill"]
    assert sorted(p.name for p in attestations_root.iterdir()) == ["approved-skill.json"]
    skill_installation.verify_managed_skill_attestation_paths(
        skill_path=existing_skill,
        installed_root=layout.installed_skills_dir,
        attestations_root=attestations_root,
    )

    # Case C: new installed skill (no prior attestation) whose final check fails leaves nothing behind.
    new_record, new_members = _installed_record_and_members(
        "brand-new",
        {"SKILL.md": b"---\nname: brand-new\ndescription: new\n---\n"},
    )
    new_plan = [PlannedRecord("skills", "installed/brand-new", RecordOutcome.IMPORT)]
    with monkeypatch.context() as patch:
        patch.setattr(
            skill_installation,
            "verify_managed_skill_attestation_paths",
            lambda **_kwargs: (_ for _ in ()).throw(PermissionError("final check failed")),
        )
        with pytest.raises(PermissionError, match="final check failed"):
            section.apply([new_record], new_plan, _context(target, new_members))

    assert sorted(p.name for p in layout.installed_skills_dir.iterdir()) == ["approved-skill"]
    assert sorted(p.name for p in attestations_root.iterdir()) == ["approved-skill.json"]

    # Case D: second os.rename fails during swap; backup is restored and staging removed.
    real_rename = os.rename
    calls = 0

    def fail_second_rename(src: Any, dst: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("rename failed")
        real_rename(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", fail_second_rename)
        with pytest.raises(OSError, match="rename failed"):
            section.apply([v2_record], plan, _context(target, v2_members))

    assert (existing_skill / "scripts" / "run.py").read_bytes() == v1_run_py
    assert (attestations_root / "approved-skill.json").read_bytes() == v1_attestation
    assert sorted(p.name for p in layout.installed_skills_dir.iterdir()) == ["approved-skill"]
    assert sorted(p.name for p in attestations_root.iterdir()) == ["approved-skill.json"]


def test_apply_preserves_umask_modes_and_leaves_no_staging_entries(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    layout = InstanceLayout.at(target)
    section = SkillsSection()

    # Reference folder and file created under the current umask.
    ref_dir = tmp_path / "ref-dir"
    os.mkdir(ref_dir)
    ref_file = ref_dir / "plain.txt"
    with open(ref_file, "xb") as handle:
        handle.write(b"hi")
    expected_dir_mode = stat.S_IMODE(ref_dir.stat().st_mode)
    expected_file_mode = stat.S_IMODE(ref_file.stat().st_mode)

    skill_md = b"---\nname: travel-tips\ndescription: v1\n---\n"
    script = b"#!/bin/sh\necho ok\n"
    op_record = {
        "name": "travel-tips",
        "kind": SkillKind.OPERATOR.value,
        "files": [
            _file_record("SKILL.md", skill_md),
            _file_record("check.sh", script, executable=True),
        ],
    }
    loose_bytes = b'{"version": 1}\n'
    loose_record = {
        "name": ".ua-external-skills.json",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record(".", loose_bytes)],
    }
    plan = [
        PlannedRecord("skills", "operator/travel-tips", RecordOutcome.IMPORT),
        PlannedRecord("skills", "operator/.ua-external-skills.json", RecordOutcome.IMPORT),
    ]
    members = {
        "files/skills/skills/travel-tips/SKILL.md": skill_md,
        "files/skills/skills/travel-tips/check.sh": script,
        "files/skills/skills/.ua-external-skills.json": loose_bytes,
    }
    section.apply([op_record, loose_record], plan, _context(target, members))

    operator_root = layout.control_dir / "skills"
    imported_dir = operator_root / "travel-tips"
    assert stat.S_IMODE(imported_dir.stat().st_mode) == expected_dir_mode
    assert stat.S_IMODE((imported_dir / "SKILL.md").stat().st_mode) == expected_file_mode
    assert os.access(imported_dir / "check.sh", os.X_OK)
    assert stat.S_IMODE((operator_root / ".ua-external-skills.json").stat().st_mode) == expected_file_mode
    assert sorted(p.name for p in operator_root.iterdir()) == [".ua-external-skills.json", "travel-tips"]

    # Replacing an existing operator skill folder and loose file leaves no staging or backup entries.
    skill_md_v2 = b"---\nname: travel-tips\ndescription: v2\n---\n"
    loose_v2 = b'{"version": 2}\n'
    op_v2 = {
        "name": "travel-tips",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record("SKILL.md", skill_md_v2)],
    }
    loose_rec_v2 = {
        "name": ".ua-external-skills.json",
        "kind": SkillKind.OPERATOR.value,
        "files": [_file_record(".", loose_v2)],
    }
    section.apply(
        [op_v2, loose_rec_v2],
        plan,
        _context(
            target,
            {
                "files/skills/skills/travel-tips/SKILL.md": skill_md_v2,
                "files/skills/skills/.ua-external-skills.json": loose_v2,
            },
        ),
    )
    assert (imported_dir / "SKILL.md").read_bytes() == skill_md_v2
    assert not (imported_dir / "check.sh").exists()
    assert (operator_root / ".ua-external-skills.json").read_bytes() == loose_v2
    assert sorted(p.name for p in operator_root.iterdir()) == [".ua-external-skills.json", "travel-tips"]
