"""Real SDK formatting and protected installation with inert model responses."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.models import Model

from custom_agents import access_runtime
from custom_agents import skill_semantic_review as semantic
from custom_agents.skill_installation import SkillInstallationService
from custom_agents.skill_repository import build_skill_tree_manifest
from custom_tools.runtime_context import task_work
from runtime_settings.skill_assessment import SkillSemanticReviewSettings
from tests.change_approval_support import agreement, use_signed_contexts
from tests.test_skill_installation import _agent, _patch_acquisition, _repository, _write_config, REPOSITORY_URL


class ReviewModel(Model):
    def __init__(self, *, output=None, fail=False):
        self.calls = []
        self.tool_names = []
        self.output = output
        self.fail = fail

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "offline-review", "api_key": "test-key-must-not-be-recorded"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("Use the SDK formatting tool, not a second provider path")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.fail:
            raise RuntimeError("synthetic provider failure")
        self.calls.append(deepcopy(messages))
        self.tool_names.append([tool["name"] for tool in tool_specs])
        source = json.loads(messages[0]["content"][0]["text"])
        output = self.output or {
            "complete": True,
            "inspected_files": [f["path"] for f in source["untrusted_files"]],
            "findings": [], "summary": "Reviewed the supplied inert files.",
        }
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {"name": "SemanticDecision", "toolUseId": "review"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(output)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.fixture
def artifact(tmp_path):
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: example-skill\ndescription: Formats a greeting.\n---\nReturn a greeting.\n")
    (root / ".hidden.txt").write_text("Hidden supporting data.")
    return root


def review(root, model, **settings):
    return semantic.assess_skill(
        SimpleNamespace(_strands_model=model), root, build_skill_tree_manifest(root),
        SkillSemanticReviewSettings(mode="task_model", **settings),
    )


def test_real_sdk_review_contains_every_file_and_no_executable_tools(artifact):
    model = ReviewModel()
    report = review(artifact, model)
    assert report["status"] == "completed", report
    assert set(report["decision"]["inspected_files"]) == {"SKILL.md", ".hidden.txt"}
    assert model.tool_names == [["SemanticDecision"]]
    assert len(model.calls[0]) == 1
    assert "test-key-must-not-be-recorded" not in json.dumps(report)


def test_off_and_missing_model_finish_with_configuration_choice(artifact, monkeypatch):
    monkeypatch.setattr(semantic, "_task_model", lambda _: (_ for _ in ()).throw(AssertionError("unused model")))
    manifest = build_skill_tree_manifest(artifact)
    off = semantic.assess_skill(SimpleNamespace(), artifact, manifest, SkillSemanticReviewSettings())
    assert off["status"] == "not_configured"
    assert off["configure"]["choices"] == ["off", "task_model"]
    monkeypatch.setattr(semantic, "_task_model", lambda _: (None, "", ""))
    missing = review(artifact, None)
    assert missing["status"] == "not_configured"
    assert missing["reason"] == "task_model_unavailable"


def test_personal_task_model_is_used_instead_of_team_model(artifact):
    team, personal = ReviewModel(fail=True), ReviewModel()
    with task_work("review-person-task") as work:
        work.person_model = SimpleNamespace(model=personal, profile=SimpleNamespace(model_id="person-review"), provider="test")
        report = review(artifact, team)
    assert report["status"] == "completed", report
    assert report["model_id"] == "person-review"
    assert personal.calls and not team.calls


def test_each_review_has_an_independent_conversation(artifact):
    model = ReviewModel()
    assert review(artifact, model)["status"] == "completed"
    (artifact / ".hidden.txt").write_text("Second independent input.")
    assert review(artifact, model)["status"] == "completed"
    assert len(model.calls) == 2
    assert all(len(messages) == 1 for messages in model.calls)
    assert "Hidden supporting data." not in json.dumps(model.calls[1])


@pytest.mark.parametrize("output", [
    {"complete": False, "inspected_files": ["SKILL.md", ".hidden.txt"], "findings": [], "summary": "Partial."},
    {"complete": True, "inspected_files": ["SKILL.md"], "findings": [], "summary": "Missing hidden file."},
    {"complete": True, "inspected_files": ["SKILL.md", "SKILL.md"], "findings": [], "summary": "Duplicated file."},
])
def test_incomplete_or_duplicate_coverage_never_passes(artifact, output):
    assert review(artifact, ReviewModel(output=output))["status"] == "incomplete"


def test_selected_model_failure_is_not_downgraded(artifact):
    assert review(artifact, ReviewModel(fail=True))["status"] == "incomplete"


def test_unreadable_or_oversized_content_does_not_reach_the_model(artifact):
    model = ReviewModel()
    (artifact / "binary").write_bytes(b"\xff\x00")
    assert review(artifact, model)["status"] == "incomplete"
    assert not model.calls
    (artifact / "binary").unlink()
    (artifact / "large.txt").write_text("data" * 1024)
    assert review(artifact, model, max_input_bytes=1024)["status"] == "incomplete"
    assert not model.calls


def test_medium_findings_block_without_changing_the_static_verdict(artifact):
    output = {"complete": True, "inspected_files": ["SKILL.md", ".hidden.txt"],
              "findings": [{"file_path": "SKILL.md", "severity": "MEDIUM", "title": "Unexpected capability",
                            "detail": "The requested behavior exceeds its stated purpose."}], "summary": "Needs review."}
    assert review(artifact, ReviewModel(output=output))["status"] == "blocked"


@pytest.fixture
def installation(tmp_path, monkeypatch):
    use_signed_contexts(monkeypatch)
    config, path = _write_config(tmp_path)
    config.workspace.skill_installation.semantic_review.mode = "task_model"
    agent = _agent(config, path)
    agent._strands_model = ReviewModel()
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    yield agent, SkillInstallationService(agent)
    access_runtime._runtime_access.cache_clear()


def test_protected_plan_binds_static_and_semantic_reports_and_receipt(installation):
    agent, service = installation
    plan = service.stage(repository_url=REPOSITORY_URL)
    assert plan["scan"]["mode"] == "static_no_llm"
    assert plan["semantic_review"]["status"] == "completed"
    installed = service.apply(plan["change_id"], agreement())
    assert installed["status"] == "installed"
    assert installed["semantic_review"]["report_sha256"] == plan["semantic_review"]["report_sha256"]


def test_changed_semantic_report_refuses_application(installation):
    _, service = installation
    plan = service.stage(repository_url=REPOSITORY_URL)
    report = Path(plan["semantic_review"]["report_path"])
    with pytest.raises(PermissionError):
        report.write_text("{}")
    report.chmod(0o600)  # Simulate operator-level damage to protected evidence.
    report.write_text("{}")
    with pytest.raises(PermissionError, match="Semantic review evidence"):
        service.apply(plan["change_id"], agreement())
    assert not Path(plan["destination_path"]).exists()


def test_changed_review_configuration_requires_a_new_plan(installation):
    agent, service = installation
    plan = service.stage(repository_url=REPOSITORY_URL)
    agent.skill_installation_config["semantic_review"]["mode"] = "off"
    with pytest.raises(PermissionError, match="settings changed"):
        service.apply(plan["change_id"], agreement())


def test_failed_selected_review_never_creates_an_installable_plan(installation):
    agent, service = installation
    agent._strands_model = ReviewModel(fail=True)
    with pytest.raises(PermissionError, match="blocked or incomplete"):
        service.stage(repository_url=REPOSITORY_URL)
    paths = __import__("custom_agents.skill_installation", fromlist=["resolve_skill_installation_paths"]).resolve_skill_installation_paths(agent)
    assert not list(paths.plans.glob("*.json"))
    assert not list(paths.installed.iterdir())
    assert list(paths.reports.glob("*.semantic.json"))


def test_static_rejection_never_sends_skill_contents_to_the_model(installation, monkeypatch):
    from custom_agents import skill_installation
    from custom_agents.skill_repository import SkillScanRejected

    agent, service = installation
    def reject(*args, **kwargs):
        raise SkillScanRejected("Synthetic mandatory local rejection")

    monkeypatch.setattr(skill_installation, "scan_skill_with_skillspector", reject)
    with pytest.raises(SkillScanRejected, match="mandatory local rejection"):
        service.stage(repository_url=REPOSITORY_URL)
    assert not agent._strands_model.calls


def test_settings_transfer_preserves_old_choices_and_defaults_to_non_llm():
    from team_transfer.sections import SECTION_UPGRADES
    from team_transfer.settings import SettingsSection
    from assistant_gateway.config import GatewayInstanceConfig

    records = [{"path": ["workspace", "skill_installation", "safe_max_score"], "value": 10}]
    assert SECTION_UPGRADES[("settings", 8, SettingsSection.schema_version)](records) == records
    config = GatewayInstanceConfig.create(instance_name="example", instance_root="/example", team_id="example", timezone="UTC")
    assert config.workspace.skill_installation.semantic_review.mode == "off"
