"""The ``skills`` transfer section carries operator skills and reviewed installs with their attestations."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from team_transfer.container import TransferReader
from team_transfer.control import ControlSection
from team_transfer.crypto import new_key_derivation
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from team_transfer.skills import SkillsSection
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
