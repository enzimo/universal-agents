"""Team skill drafts: TeamArchitect proposes a new or changed team skill.

Agents cannot write installed skills. A draft folder in the task's own folders
goes through the reviewed installation pipeline (quarantine, SkillSpector scan,
apply_change with the person's agreement or approval, attested install); a
replacement shows a file diff and keeps the old version recoverable.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

import custom_agents.skill_installation as skill_installation
from custom_agents.skill_installation import (
    SkillInstallationService,
    verify_managed_skill_attestation,
)
from custom_agents import access_runtime
from custom_tools.path_policy import PathPolicyError
from tests.task_work_support import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout
from tests.change_approval_support import agreement, run_apply_change, use_signed_contexts
from tests.test_skill_installation import _agent, _safe_scan_result, _write_config, record_skill_refresh


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


@pytest.fixture
def lead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    record_skill_refresh(monkeypatch)
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path, authorization_enabled=False)
    agent.instance_root = str(config.instance_root_path)
    layout = InstanceLayout.at(config.instance_root_path)
    layout.ensure_directories()
    agent.skills_dir = str(layout.skills_dir)
    monkeypatch.setattr(
        skill_installation,
        "scan_skill_with_skillspector",
        lambda *args, **kwargs: _safe_scan_result(),
    )
    set_agent_ref(agent)
    home = layout.person_dir(config.user.canonical_user_id)
    home.mkdir(parents=True)
    yield agent, layout, home
    set_agent_ref(None)


def _draft(folder: Path, name: str = "team-notes", body: str = "Write short notes.") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Team note taking.\n---\n{body}\n",
        encoding="utf-8",
    )
    return folder


def _install(service: SkillInstallationService, draft: Path, *, replace: bool = False) -> dict:
    plan = service.stage_draft(draft_path=str(draft), replace_existing=replace)
    applied = service.apply(plan["change_id"], agreement())
    assert applied["status"] == "installed"
    return {"plan": plan, "applied": applied}


def test_a_draft_becomes_an_attested_team_skill(lead):
    agent, layout, home = lead
    draft = _draft(home / "drafts" / "team-notes")
    service = SkillInstallationService(agent)

    plan = service.stage_draft(draft_path=str(draft))

    assert plan["status"] == "safe_pending_approval"
    assert plan["source_kind"] == "team_draft"
    assert plan["changes"] == {"kind": "new_skill", "files": ["SKILL.md"]}
    applied = run_apply_change(agent, plan["change_id"], confirmed=True)
    assert applied["status"] == "applied", applied
    assert applied["approval"]["mode"] == "agreement"
    assert applied["result"]["draft_source_path"] == str(draft)
    applied = applied["result"]
    installed = Path(applied["installed_path"])
    assert installed == layout.installed_skills_dir / "team-notes"
    assert verify_managed_skill_attestation(agent, installed) == applied["installed_tree_sha256"]
    # The draft stays where the agent wrote it.
    assert (draft / "SKILL.md").is_file()


def test_replacing_a_skill_shows_the_diff_and_keeps_the_old_version(lead):
    agent, layout, home = lead
    service = SkillInstallationService(agent)
    first = _install(service, _draft(home / "v1"))["applied"]
    edited = home / "v2"
    shutil.copytree(first["installed_path"], edited)
    _draft(edited, body="Write short notes with dates.")
    (edited / "references").mkdir()
    (edited / "references" / "format.md").write_text("dates first", encoding="utf-8")

    with pytest.raises(ValueError, match="replace_existing=true"):
        service.stage_draft(draft_path=str(edited))
    plan = service.stage_draft(draft_path=str(edited), replace_existing=True)

    assert plan["changes"] == {
        "kind": "replacement",
        "added": ["references/format.md"],
        "removed": [],
        "changed": ["SKILL.md"],
    }
    applied = service.apply(plan["change_id"], agreement())
    installed = Path(applied["installed_path"])
    assert "with dates" in (installed / "SKILL.md").read_text(encoding="utf-8")
    assert verify_managed_skill_attestation(agent, installed) == applied["installed_tree_sha256"]
    retired = list(layout.skill_installation_dir.glob("removed/team-notes.*"))
    assert len(retired) == 1
    assert "with dates" not in (retired[0] / "SKILL.md").read_text(encoding="utf-8")


def test_a_replacement_is_refused_if_the_skill_changed_after_review(lead):
    agent, _layout, home = lead
    service = SkillInstallationService(agent)
    _install(service, _draft(home / "v1"))
    first_edit = service.stage_draft(
        draft_path=str(_draft(home / "v2", body="Second version.")), replace_existing=True
    )
    _install(service, _draft(home / "v3", body="Third version."), replace=True)

    with pytest.raises(PermissionError, match="changed after this replacement was reviewed"):
        service.apply(first_edit["change_id"], agreement())


def test_operator_skills_cannot_be_replaced_by_a_draft(lead):
    agent, layout, home = lead
    _draft(layout.skills_dir / "operator-skill", name="operator-skill")
    service = SkillInstallationService(agent)

    with pytest.raises(ValueError, match="operator-provided"):
        service.stage_draft(draft_path=str(_draft(home / "copy", name="operator-skill")))


def test_drafts_must_come_from_the_task_folders(lead):
    agent, layout, _home = lead
    service = SkillInstallationService(agent)
    in_skills = _draft(layout.skills_dir / "operator-skill", name="operator-skill")
    in_control = _draft(layout.control_dir / "sneaky")

    with pytest.raises(ValueError, match="must be in this task's folders"):
        service.stage_draft(draft_path=str(in_skills))
    with pytest.raises(PathPolicyError):
        service.stage_draft(draft_path=str(in_control))


def test_only_team_architect_proposes_team_skill_changes(tmp_path, monkeypatch):
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path, authorization_enabled=False, is_team_lead=False)

    with pytest.raises(PermissionError, match="Only TeamArchitect"):
        SkillInstallationService(agent).stage_draft(draft_path=str(tmp_path))
