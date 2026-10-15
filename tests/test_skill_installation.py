"""Team skill installs and removals staged by TeamArchitect and applied with apply_change.

Covers: GitHub skills are cloned, quarantined and scanned before a plan
exists; with authorization off the person's agreement installs or removes
them; with it on, a lead whose agent type and person hold full access on
fn:team_skills applies at once (held access) and administrators get a notice,
while a lead without that access waits for a person (real Cedar and SQLite);
plans refuse another task, the wrong approval mode, replay, artifact tampering
and changed installed skills; removal is recoverable; after every change the
running agent reloads its skills and the team is told, and a failure there is
reported without undoing the change; the operator can turn the whole feature
off, which also hides its tools.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.interrupt import InterruptException

# Runtime composition installs the cycle-free catalog lookup the services use.
from custom_agents import agent_catalog as _agent_catalog  # noqa: F401
import custom_agents.skill_installation as skill_installation
import custom_agents.team_skill_changes as team_skill_changes
from access_control.changes import ChangeApproval
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.skill_installation import SkillInstallationService, verify_managed_skill_attestation
from custom_agents.skill_repository import SkillScanRejected, SkillScanResult
from custom_agents.skill_uninstallation import SkillUninstallationService
from custom_agents.team_config_helpers import filter_agent_tools
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    ChangeApprovalMode,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from custom_tools.providers.capabilities import (
    plan_skill_installation,
    plan_skill_uninstallation,
    plan_team_skill_change,
)
from tests.change_approval_support import (
    TASK_ID,
    ToolCall,
    agreement,
    change_agent,
    decide,
    needs_cedar,
    owner,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)

COMMIT_SHA = "a" * 40
REPOSITORY_URL = "https://github.com/example/reviewed-skills.git"
TEAM = "team_skill_install"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _safe_scan_result(*, score: int = 7) -> SkillScanResult:
    report = {
        "skill": {"name": "reviewed-skill", "source": "/quarantine/artifact"},
        "risk_assessment": {
            "score": score,
            "severity": "LOW",
            "recommendation": "SAFE",
        },
        "components": [{"path": "SKILL.md", "type": "markdown"}],
        "issues": [
            {
                "rule_id": "RP1",
                "severity": "medium",
                "message": "Review changing external instructions before future updates.",
            }
        ],
        "metadata": {
            "skillspector_version": "2.5.1",
            "llm_requested": False,
        },
        "execution_successful": True,
        "analysis_completeness": {
            "coverage_percent": 100.0,
            "execution_successful": True,
            "partially_inspected_files": 0,
            "entirely_uninspected_files": 0,
            "ledger_exceptions": [],
            "scope_exclusions": [],
        },
    }
    return SkillScanResult(
        scanner_name="SkillSpector",
        scanner_version="2.5.1",
        mode="static_no_llm",
        score=score,
        severity="LOW",
        recommendation="SAFE",
        issue_count=1,
        report_sha256=hashlib.sha256(_canonical_json(report).encode()).hexdigest(),
        report=report,
    )


def _repository(tmp_path: Path, *, duplicate: bool = False) -> Path:
    root = tmp_path / "source-repository"
    first = root / "packages" / "primary" / "skills" / "reviewed-skill"
    first.mkdir(parents=True)
    (first / "SKILL.md").write_text(
        "---\nname: reviewed-skill\ndescription: Reviewed test skill.\n---\n"
        "Use the supplied evidence and report a short result.\n",
        encoding="utf-8",
    )
    (first / "references").mkdir()
    (first / "references" / "guide.md").write_text("Bound resource.", encoding="utf-8")
    if duplicate:
        second = root / "other" / "skills" / "reviewed-skill-copy"
        second.mkdir(parents=True)
        (second / "SKILL.md").write_text(
            "---\nname: reviewed-skill\ndescription: Duplicate.\n---\nDuplicate.\n",
            encoding="utf-8",
        )
    return root


def _write_config(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="skill-install-test",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
    )
    config.workspace.skill_installation.scanner_executable = "/opt/skillspector/bin/skillspector"
    path = config.save(root / "instance_config.yaml")
    return config, path


def _agent(
    config: GatewayInstanceConfig,
    config_path: Path,
    *,
    authorization_enabled: bool = False,
    is_team_lead: bool = True,
) -> SimpleNamespace:
    refreshed = {"value": False}
    return change_agent(
        config,
        config_path,
        authorization_enabled=authorization_enabled,
        is_team_lead=is_team_lead,
        skill_installation_config=config.workspace.skill_installation.model_dump(mode="json"),
        skills_dir=str(config.workspace_path / "skills"),
        additional_skill_directories=[],
        plugins=[],
        _refresh_skills_plugins=lambda force=False: refreshed.update(value=force),
        _test_refreshed=refreshed,
    )


def _patch_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    *,
    scan_result: SkillScanResult | None = None,
) -> None:
    def fake_clone(url, destination, *, max_repository_bytes):
        assert url == REPOSITORY_URL
        shutil.copytree(repository, destination)
        return COMMIT_SHA

    monkeypatch.setattr(skill_installation, "clone_public_github_repository", fake_clone)
    monkeypatch.setattr(
        skill_installation,
        "scan_skill_with_skillspector",
        lambda *args, **kwargs: scan_result or _safe_scan_result(),
    )


def record_skill_refresh(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record the running agent's skill reload and the team notice after a change."""

    calls: dict[str, list] = {"reloaded": [], "published": []}

    async def reload_agent_skills(agent) -> None:
        calls["reloaded"].append(agent)

    async def publish(_agent, _result, *, operation):
        calls["published"].append(operation)
        return SimpleNamespace(notification_id="skill_cat_msg_0123456789ab")

    monkeypatch.setattr(team_skill_changes, "reload_agent_skills", reload_agent_skills)
    monkeypatch.setattr(team_skill_changes, "publish_managed_skill_catalog_notification", publish)
    return calls


@pytest.fixture
def skill_refresh(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    return record_skill_refresh(monkeypatch)


def _install(agent: SimpleNamespace) -> dict:
    """Install reviewed-skill with authorization off (the person agreed)."""

    service = SkillInstallationService(agent)
    plan = service.stage(repository_url=REPOSITORY_URL, skill_name="reviewed-skill")
    installed = service.apply(plan["change_id"], agreement())
    assert installed["status"] == "installed", installed
    return installed


def _let_team_architect_manage_team_skills(agent: SimpleNamespace) -> None:
    """The owner grants TeamArchitect's agent type full access to the team's skills."""

    access_runtime.runtime_access(agent).service.grant_by_person(
        owner(agent),
        GrantV1(
            grant_id="new",
            team_id=agent.team_id,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{agent.team_id}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="fn:team_skills"),
            level=AccessLevel.FULL,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id="placeholder"),
            created_at_utc=datetime.now(timezone.utc),
        ),
    )


def _administrator_notices(agent: SimpleNamespace) -> list:
    store = access_runtime.runtime_access(agent).service.store
    return [
        notice
        for notice, _read in store.notices_for(agent.team_id, person_id=owner(agent).person_id, roles=("admin", "owner"))
    ]


class _NoPause(ToolCall):
    """A tool context for a change that must apply without waiting for anyone."""

    def interrupt(self, name, reason=None):
        raise AssertionError(f"unexpected pause for approval: {reason}")


# -- installing ------------------------------------------------------------------------


def test_authorization_off_installs_a_scanned_skill_with_the_persons_agreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_refresh: dict
) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    service = SkillInstallationService(agent)

    plan = service.stage(repository_url="https://github.com/example/reviewed-skills", skill_name="reviewed-skill")
    unconfirmed = run_apply_change(agent, plan["change_id"])
    nothing_installed = not Path(plan["destination_path"]).exists()
    applied = run_apply_change(agent, plan["change_id"], confirmed=True)

    assert plan["status"] == "safe_pending_approval"
    assert plan["authority_mode"] == "authorization_disabled_agreement"
    assert plan["commit_sha"] == COMMIT_SHA
    assert plan["scan"]["recommendation"] == "SAFE"
    assert plan["scan_issues"]
    assert Path(plan["scan_report_artifact"]).is_file()
    assert "confirmed=true" in plan["next_step"]
    assert unconfirmed["status"] == "agreement_required"
    assert nothing_installed
    assert applied["status"] == "applied", applied
    assert applied["approval"]["mode"] == "agreement"
    result = applied["result"]
    assert result["status"] == "installed"
    installed = Path(result["installed_path"])
    assert (installed / "SKILL.md").is_file()
    assert verify_managed_skill_attestation(agent, installed) == result["installed_tree_sha256"]
    assert agent._test_refreshed["value"] is True
    assert result["runtime_skill_refresh"]["status"] == "completed"
    assert skill_refresh["published"] == ["install"]
    assert service.status(plan["change_id"])["status"] == "installed"
    with pytest.raises(access_runtime.AuthorizationOff):
        access_runtime.runtime_requests(agent)


@needs_cedar
def test_a_lead_with_team_skill_access_changes_skills_at_once_and_administrators_are_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_refresh: dict
) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path, authorization_enabled=True)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    _let_team_architect_manage_team_skills(agent)
    plan = SkillInstallationService(agent).stage(repository_url=REPOSITORY_URL, skill_name="reviewed-skill")

    installed = run_apply_change(agent, plan["change_id"], _NoPause())
    removal = SkillUninstallationService(agent).stage(skill_name="reviewed-skill")
    removed = run_apply_change(agent, removal["change_id"], _NoPause("tooluse_removal"))

    assert plan["authority_mode"] == "person_approval"
    for applied, status in ((installed, "installed"), (removed, "uninstalled")):
        assert applied["status"] == "applied", applied
        assert applied["result"]["status"] == status
        assert applied["approval"]["mode"] == "held_access"
        assert applied["approval"]["approved_by"] == "TeamArchitect"
        assert applied["approval"]["access_request_id"] is None
    assert not Path(installed["result"]["installed_path"]).exists()
    notices = _administrator_notices(agent)
    assert [notice.kind for notice in notices] == [AccessNoticeKind.TEAM_SKILL_CHANGED] * 2
    assert {notice.title for notice in notices} == {
        "TeamArchitect installed the team skill reviewed-skill",
        "TeamArchitect removed the team skill reviewed-skill",
    }
    assert all(notice.recipient_roles == ("admin",) for notice in notices)
    assert skill_refresh["published"] == ["install", "uninstall"]


@needs_cedar
def test_without_team_skill_access_a_person_approves_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_refresh: dict
) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path, authorization_enabled=True)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    plan = SkillInstallationService(agent).stage(repository_url=REPOSITORY_URL, skill_name="reviewed-skill")
    call = ToolCall()

    with pytest.raises(InterruptException):
        run_apply_change(agent, plan["change_id"], call, confirmed=True)
    nothing_installed = not Path(plan["destination_path"]).exists()
    request = stored_request(agent, call)
    decide(agent, call)
    applied = run_apply_change(agent, plan["change_id"], call)

    assert nothing_installed
    assert request.change.kind == "team_skill_install"
    assert request.change.title == "Install the team skill reviewed-skill"
    assert request.change.summary.startswith("Install the team skill reviewed-skill (security scan: ")
    assert request.change.summary.endswith("). Takes effect now.")
    assert applied["status"] == "applied", applied
    assert applied["result"]["status"] == "installed"
    assert applied["approval"]["mode"] == "person_approval"
    assert applied["approval"]["access_request_id"] == request.request_id
    assert applied["result"]["authority"]["approval"] == applied["approval"]
    assert [notice.kind for notice in _administrator_notices(agent)] == [AccessNoticeKind.TEAM_SKILL_CHANGED]


def test_skill_plans_refuse_other_tasks_wrong_approval_replay_and_name_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    service = SkillInstallationService(agent)
    plan = service.stage(repository_url=REPOSITORY_URL, skill_name="reviewed-skill")

    agent.task_manager.current_task_id = "task_other"
    with pytest.raises(PermissionError, match="another task"):
        service.apply(plan["change_id"], agreement())
    agent.task_manager.current_task_id = TASK_ID
    person = ChangeApproval(ChangeApprovalMode.PERSON_APPROVAL, "User", datetime.now(timezone.utc), "access_req_x")
    with pytest.raises(PermissionError, match="not agreement"):
        service.apply(plan["change_id"], person)
    applied = service.apply(plan["change_id"], agreement())
    replay = service.apply(plan["change_id"], agreement())

    assert applied["status"] == "installed"
    assert replay["reason_code"] == "skill_installation_replay"
    assert Path(applied["installed_path"]).is_dir()
    with pytest.raises(ValueError, match="semantic name"):
        service.stage(repository_url=REPOSITORY_URL, skill_name="reviewed-skill")


def test_artifact_tampered_after_scanning_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    plan = SkillInstallationService(agent).stage(repository_url=REPOSITORY_URL, skill_name="reviewed-skill")
    (Path(plan["artifact_path"]) / "references" / "guide.md").write_text("tampered", encoding="utf-8")

    refused = run_apply_change(agent, plan["change_id"], confirmed=True)

    assert refused["status"] == "refused" and "changed after scanning" in refused["reason"]
    assert not Path(plan["destination_path"]).exists()


def test_installed_skill_tamper_blocks_activation_attestation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    installed = Path(_install(agent)["installed_path"])
    (installed / "SKILL.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(PermissionError, match="contents changed"):
        verify_managed_skill_attestation(agent, installed)


def test_stage_returns_nested_candidates_and_rejects_specialist_and_unsafe_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path, duplicate=True))
    selection = SkillInstallationService(agent).stage(repository_url=REPOSITORY_URL)
    assert selection["status"] == "selection_required"
    assert {item["relative_path"] for item in selection["candidates"]} == {
        "other/skills/reviewed-skill-copy",
        "packages/primary/skills/reviewed-skill",
    }

    specialist = _agent(config, config_path, is_team_lead=False)
    with pytest.raises(PermissionError, match="Only TeamArchitect"):
        SkillInstallationService(specialist).stage(repository_url=REPOSITORY_URL)

    def rejected(*args, **kwargs):
        raise SkillScanRejected("unsafe test skill")

    monkeypatch.setattr(skill_installation, "scan_skill_with_skillspector", rejected)
    with pytest.raises(SkillScanRejected, match="unsafe"):
        SkillInstallationService(agent).stage(
            repository_url=REPOSITORY_URL,
            skill_path="packages/primary/skills/reviewed-skill",
        )


# -- removing --------------------------------------------------------------------------


def test_authorization_off_removal_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_refresh: dict
) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    installed = _install(agent)
    installed_path = Path(installed["installed_path"])
    agent._test_refreshed["value"] = False
    removal = SkillUninstallationService(agent)

    plan = removal.stage(skill_name="reviewed-skill")
    unconfirmed = run_apply_change(agent, plan["change_id"])
    still_installed = installed_path.is_dir()
    applied = run_apply_change(agent, plan["change_id"], confirmed=True)

    assert plan["status"] == "pending_approval"
    assert plan["installed_tree_sha256"] == installed["installed_tree_sha256"]
    assert plan["recoverable"] is True
    assert unconfirmed["status"] == "agreement_required"
    assert still_installed
    assert applied["status"] == "applied", applied
    assert applied["approval"]["mode"] == "agreement"
    result = applied["result"]
    assert result["status"] == "uninstalled"
    assert not installed_path.exists()
    assert not Path(plan["attestation_path"]).exists()
    assert (Path(result["removed_skill_path"]) / "SKILL.md").is_file()
    assert Path(result["removed_attestation_path"]).is_file()
    assert agent._test_refreshed["value"] is True
    assert skill_refresh["published"] == ["uninstall"]
    assert removal.status(plan["change_id"])["status"] == "uninstalled"
    assert removal.apply(plan["change_id"], agreement())["reason_code"] == "skill_uninstallation_replay"


def test_skill_removal_refuses_a_skill_changed_after_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    installed = _install(agent)
    removal = SkillUninstallationService(agent)
    plan = removal.stage(skill_name="reviewed-skill")
    Path(installed["installed_path"], "SKILL.md").write_text("tampered after planning\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="changed after"):
        removal.apply(plan["change_id"], agreement())

    assert Path(installed["installed_path"]).is_dir()
    assert removal.status(plan["change_id"])["status"] == "pending"


def test_skill_removal_rejects_workspace_owned_skill(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    local = Path(agent.skills_dir) / "local-only"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("---\nname: local-only\ndescription: Local.\n---\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="workspace and external"):
        SkillUninstallationService(agent).stage(skill_name="local-only")


# -- turned off by the operator --------------------------------------------------------


def test_disabled_skill_installation_hides_its_tools_and_refuses_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = [plan_skill_installation, plan_team_skill_change, plan_skill_uninstallation]
    assert filter_agent_tools(tools, skill_installation_enabled=False) == []
    assert filter_agent_tools(tools, skill_installation_enabled=True) == tools

    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    agent.skill_installation_config = {**agent.skill_installation_config, "enabled": False}
    monkeypatch.setattr(
        skill_installation,
        "clone_public_github_repository",
        lambda *_args, **_kwargs: pytest.fail("nothing is downloaded while skill installation is off"),
    )
    with pytest.raises(PermissionError, match="disabled by operator configuration"):
        SkillInstallationService(agent).stage(repository_url=REPOSITORY_URL)
    with pytest.raises(PermissionError, match="disabled by operator configuration"):
        SkillUninstallationService(agent).stage(skill_name="reviewed-skill")
