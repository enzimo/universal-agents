"""Tests for runtime settings plan decision class and requester ownership."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import InterruptException

from access_control.changes import ChangeApproval
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from access_control.starter_profiles import apply_starter_profile
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents import agent_catalog as _agent_catalog  # noqa: F401
from custom_agents.governance.authorized_operations import model_digest
from custom_agents.model_choice import MODEL_CHOICE_CHANGE_KIND
from custom_agents.runtime_setting_registry import SettingDecisionClass
from custom_agents.runtime_settings_service import (
    RUNTIME_SETTINGS_CHANGE_KIND,
    RuntimeSettingChange,
    RuntimeSettingsChangePlan,
    RuntimeSettingsService,
)
from custom_messages.contracts.access_v2 import (
    AccessRequestStatus,
    AccessReviewChoice,
    ChangeApprovalMode,
    ChangeProposalV1,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from tests.change_approval_support import (
    ToolCall,
    change_agent,
    needs_cedar,
    owner,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    use_signed_contexts(monkeypatch)
    access_runtime._runtime_access.cache_clear()
    yield
    access_runtime._runtime_access.cache_clear()


def _make_config(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="nova",
        instance_root=str(instance_root),
        team_id="test_team",
        timezone="America/Chicago",
        heartbeat_enabled=False,
    )
    config_path = config.save(instance_root / "instance_config.yaml")
    return config, config_path


def _member_agent(
    config: GatewayInstanceConfig,
    config_path: Path,
    *,
    person_id: str = "member_1",
    authorization_enabled: bool = True,
) -> SimpleNamespace:
    return change_agent(
        config,
        config_path,
        authorization_enabled=authorization_enabled,
        person_id=person_id,
        roles=("member",),
    )


def _rewrite_plan_on_disk(
    target_plan_path: Path,
    replacement: RuntimeSettingsChangePlan,
) -> None:
    """Replace a staged plan file with a valid plan carrying the same change_id."""

    target_plan_path.chmod(0o644)
    target_plan_path.write_text(replacement.model_dump_json(), encoding="utf-8")


@needs_cedar
@pytest.mark.parametrize(
    "changes",
    [
        [
            {"path": "agent.debug.context_dump.mode", "value": "always"},
            {"path": "heartbeat.enabled", "value": True},
        ],
        [
            {"path": "heartbeat.enabled", "value": True},
            {"path": "agent.debug.context_dump.mode", "value": "always"},
        ],
    ],
    ids=["user_authorized_first", "lead_managed_first"],
)
def test_mixed_runtime_settings_plan_requires_administrator_approval(
    tmp_path: Path,
    changes: list[RuntimeSettingChange],
) -> None:
    """F1: bundling a user_authorized setting with a lead_managed setting stays admin-only."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    team_id = config.gateway.team_id
    member = PersonPrincipal("member_1", team_id, ("member",))
    admin = PersonPrincipal("admin_1", team_id, ("admin",))

    staged = RuntimeSettingsService(agent).stage(
        changes,
        reason="Turn on always-on debug dumps and heartbeat together.",
    )
    assert staged["decision_class"] == SettingDecisionClass.LEAD_MANAGED.value

    before_bytes = config_path.read_bytes()
    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(agent, staged["change_id"], call, reason="Apply bundled settings")

    request = stored_request(agent, call)
    assert request.change is not None
    assert request.change.owner_person_id is None

    requests = access_runtime.runtime_requests(agent)
    with pytest.raises(HandDownRefused):
        requests.decide(request.request_id, member, AccessReviewChoice.APPROVE)

    assert stored_request(agent, call).status is AccessRequestStatus.WAITING
    assert config_path.read_bytes() == before_bytes

    decided = requests.decide(request.request_id, admin, AccessReviewChoice.APPROVE)
    assert decided.status is AccessRequestStatus.GRANTED
    call.response = {"kind": "access_request_decision", "status": decided.status.value}
    applied = run_apply_change(agent, staged["change_id"], call, reason="Apply bundled settings")

    assert applied["status"] == "applied", applied
    assert applied["approval"]["approved_by"] == "admin_1"
    saved = GatewayInstanceConfig.load(config_path)
    assert saved.heartbeat.enabled is True
    assert saved.agent.debug["context_dump"]["mode"] == "always"


@needs_cedar
def test_user_authorized_only_plan_allows_requester_member_approval(tmp_path: Path) -> None:
    """F2: a plan containing only user_authorized settings still lets its requester approve."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    member = PersonPrincipal("member_1", config.gateway.team_id, ("member",))

    staged = RuntimeSettingsService(agent).stage(
        [
            {"path": "agent.debug.context_dump.enabled", "value": True},
            {"path": "agent.debug.context_dump.mode", "value": "always"},
        ],
        reason="Capture context dumps for debugging.",
    )
    assert staged["decision_class"] == SettingDecisionClass.USER_AUTHORIZED.value

    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(agent, staged["change_id"], call, reason="Apply debug dump settings")

    request = stored_request(agent, call)
    assert request.change is not None
    assert request.change.owner_person_id == "member_1"

    requests = access_runtime.runtime_requests(agent)
    decided = requests.decide(request.request_id, member, AccessReviewChoice.APPROVE)
    assert decided.status is AccessRequestStatus.GRANTED
    call.response = {"kind": "access_request_decision", "status": decided.status.value}
    applied = run_apply_change(agent, staged["change_id"], call, reason="Apply debug dump settings")

    assert applied["status"] == "applied", applied
    assert applied["approval"]["approved_by"] == "member_1"
    saved = GatewayInstanceConfig.load(config_path)
    assert saved.agent.debug["context_dump"]["enabled"] is True
    assert saved.agent.debug["context_dump"]["mode"] == "always"


@needs_cedar
def test_lead_managed_only_plan_requires_administrator_approval(tmp_path: Path) -> None:
    """F2: a plan containing only lead_managed settings cannot be approved by a member."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    team_id = config.gateway.team_id
    member = PersonPrincipal("member_1", team_id, ("member",))
    admin = PersonPrincipal("admin_1", team_id, ("admin",))

    staged = RuntimeSettingsService(agent).stage(
        [{"path": "heartbeat.enabled", "value": True}],
        reason="Enable heartbeat.",
    )
    assert staged["decision_class"] == SettingDecisionClass.LEAD_MANAGED.value

    before_bytes = config_path.read_bytes()
    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(agent, staged["change_id"], call, reason="Enable heartbeat")

    request = stored_request(agent, call)
    assert request.change is not None
    assert request.change.owner_person_id is None

    requests = access_runtime.runtime_requests(agent)
    with pytest.raises(HandDownRefused):
        requests.decide(request.request_id, member, AccessReviewChoice.APPROVE)

    assert stored_request(agent, call).status is AccessRequestStatus.WAITING
    assert config_path.read_bytes() == before_bytes

    decided = requests.decide(request.request_id, admin, AccessReviewChoice.APPROVE)
    call.response = {"kind": "access_request_decision", "status": decided.status.value}
    applied = run_apply_change(agent, staged["change_id"], call, reason="Enable heartbeat")

    assert applied["status"] == "applied", applied
    assert applied["approval"]["approved_by"] == "admin_1"
    assert GatewayInstanceConfig.load(config_path).heartbeat.enabled is True


@needs_cedar
def test_pre_fix_mixed_plan_waiting_request_is_refused_on_resume(tmp_path: Path) -> None:
    """Upgrade window: a mixed plan staged before the fix cannot apply after member approval."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    team_id = config.gateway.team_id
    member = PersonPrincipal("member_1", team_id, ("member",))

    staged = RuntimeSettingsService(agent).stage(
        [
            {"path": "agent.debug.context_dump.mode", "value": "always"},
            {"path": "heartbeat.enabled", "value": True},
        ],
        reason="Staged before upgrade.",
    )
    # Rewrite the staged plan on disk to match the pre-fix state: mixed changes
    # tagged decision_class=user_authorized with a valid recomputed digest.
    plan_path = Path(staged["plan_path"])
    plan = RuntimeSettingsChangePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    plan.decision_class = SettingDecisionClass.USER_AUTHORIZED
    plan.plan_digest = model_digest(plan)
    plan_path.chmod(0o644)
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")

    # Submit the pre-fix waiting request with the member recorded as owner.
    call = ToolCall(tool_use_id="tooluse_pre_fix_mixed")
    requests = access_runtime.runtime_requests(agent)
    principal, person, task_id = access_runtime.task_access(agent, access_runtime.runtime_access(agent))
    pre_fix_proposal = ChangeProposalV1(
        kind=RUNTIME_SETTINGS_CHANGE_KIND,
        change_id=plan.change_id,
        plan_digest=plan.plan_digest,
        title="Change runtime settings: agent.debug.context_dump.mode, heartbeat.enabled",
        summary="Pre-fix mixed settings plan.",
        effect=("Pre-fix mixed settings plan.",),
        owner_person_id="member_1",
        agreement_required=True,
    )
    outcome = requests.submit_change(
        requester=principal,
        person=person,
        proposal=pre_fix_proposal,
        reason="Staged before upgrade.",
        task_id=task_id,
        conversation_id="conversation_change",
        tool_use_id=call.tool_use["toolUseId"],
    )
    assert outcome.request is not None
    waiting_request = outcome.request

    before_bytes = config_path.read_bytes()
    decided = requests.decide(waiting_request.request_id, member, AccessReviewChoice.APPROVE)
    assert decided.status is AccessRequestStatus.GRANTED
    call.response = {"kind": "access_request_decision", "status": decided.status.value}

    first = run_apply_change(agent, plan.change_id, call, reason="Resume pre-fix plan")
    assert first["status"] == "refused", first
    assert "decision class does not match" in first["reason"]
    assert config_path.read_bytes() == before_bytes

    after = requests.service.store.get_request(waiting_request.request_id)
    assert after is not None
    assert after.applied_at_utc is not None
    assert after.apply_error is not None
    assert "decision class does not match" in after.apply_error

    second = run_apply_change(agent, plan.change_id, call, reason="Resume pre-fix plan again")
    assert second["status"] == "refused", second
    assert config_path.read_bytes() == before_bytes


@needs_cedar
def test_plan_whose_setting_class_tightened_after_staging_is_refused(tmp_path: Path) -> None:
    """A stored plan tagging a lead_managed setting as user_authorized is refused on load."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    staged = RuntimeSettingsService(agent).stage(
        [{"path": "heartbeat.enabled", "value": True}],
        reason="Staged before tightening.",
    )
    plan_path = Path(staged["plan_path"])
    plan = RuntimeSettingsChangePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    plan.changes[0].decision_class = SettingDecisionClass.USER_AUTHORIZED
    plan.decision_class = SettingDecisionClass.USER_AUTHORIZED
    plan.plan_digest = model_digest(plan)
    _rewrite_plan_on_disk(plan_path, plan)

    with pytest.raises(PermissionError, match="approval class changed after planning"):
        RuntimeSettingsService(agent).load_plan(plan.change_id)


@needs_cedar
@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ("empty_changes", "contains no changes"),
        ("unregistered_path", "is no longer agent-controllable"),
    ],
)
def test_load_plan_refuses_empty_or_unregistered_changes(
    tmp_path: Path,
    mutation: str,
    expected_message: str,
) -> None:
    """load_plan refuses stored plans with empty changes or paths removed from the registry."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    staged = RuntimeSettingsService(agent).stage(
        [{"path": "agent.debug.context_dump.mode", "value": "always"}],
        reason="Stage before mutation.",
    )
    plan_path = Path(staged["plan_path"])
    plan = RuntimeSettingsChangePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    if mutation == "empty_changes":
        plan.changes = []
    else:
        plan.changes[0].path = "removed.setting.path"
    plan.plan_digest = model_digest(plan)
    _rewrite_plan_on_disk(plan_path, plan)

    with pytest.raises(PermissionError, match=expected_message):
        RuntimeSettingsService(agent).load_plan(plan.change_id)


@needs_cedar
@pytest.mark.parametrize(
    ("case", "changes", "stored_kind", "stored_owner"),
    [
        (
            "owner_mismatch",
            [
                {"path": "agent.debug.context_dump.mode", "value": "always"},
                {"path": "heartbeat.enabled", "value": True},
            ],
            RUNTIME_SETTINGS_CHANGE_KIND,
            "member_1",
        ),
        (
            "kind_mismatch",
            [{"path": "agent.debug.context_dump.mode", "value": "always"}],
            MODEL_CHOICE_CHANGE_KIND,
            "member_1",
        ),
    ],
    ids=["owner_mismatch", "kind_mismatch"],
)
def test_approved_request_with_mismatched_owner_or_kind_is_refused_on_resume(
    tmp_path: Path,
    case: str,
    changes: list[RuntimeSettingChange],
    stored_kind: str,
    stored_owner: str,
) -> None:
    """_apply_approved refuses when a stored request's owner or kind differs from describe()."""

    _ = case
    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    member = PersonPrincipal("member_1", config.gateway.team_id, ("member",))

    staged = RuntimeSettingsService(agent).stage(changes, reason="Post-fix plan.")
    call = ToolCall(tool_use_id=f"tooluse_{case}")
    requests = access_runtime.runtime_requests(agent)
    principal, person, task_id = access_runtime.task_access(agent, access_runtime.runtime_access(agent))
    mismatched_proposal = ChangeProposalV1(
        kind=stored_kind,
        change_id=staged["change_id"],
        plan_digest=staged["plan_digest"],
        title="Stored mismatched proposal",
        summary="Stored proposal with different owner or kind.",
        effect=("Stored proposal with different owner or kind.",),
        owner_person_id=stored_owner,
        agreement_required=True,
    )
    outcome = requests.submit_change(
        requester=principal,
        person=person,
        proposal=mismatched_proposal,
        reason="Stored mismatched proposal.",
        task_id=task_id,
        conversation_id="conversation_change",
        tool_use_id=call.tool_use["toolUseId"],
    )
    assert outcome.request is not None
    waiting_request = outcome.request

    before_bytes = config_path.read_bytes()
    decided = requests.decide(waiting_request.request_id, member, AccessReviewChoice.APPROVE)
    assert decided.status is AccessRequestStatus.GRANTED
    call.response = {"kind": "access_request_decision", "status": decided.status.value}

    first = run_apply_change(agent, staged["change_id"], call, reason="Resume mismatched proposal")
    assert first["status"] == "refused", first
    assert "Who may approve this change or its kind no longer matches" in first["reason"]
    assert config_path.read_bytes() == before_bytes

    after = requests.service.store.get_request(waiting_request.request_id)
    assert after is not None
    assert after.applied_at_utc is not None
    assert after.apply_error is not None
    assert "Who may approve this change or its kind no longer matches" in after.apply_error

    second = run_apply_change(agent, staged["change_id"], call, reason="Resume mismatched proposal again")
    assert second["status"] == "refused", second
    assert config_path.read_bytes() == before_bytes


@needs_cedar
def test_swapped_plan_before_resume_is_refused_and_uses_up_approval(tmp_path: Path) -> None:
    """Swapping a plan file after member approval is refused before apply and consumes the approval."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    member = PersonPrincipal("member_1", config.gateway.team_id, ("member",))

    original = RuntimeSettingsService(agent).stage(
        [{"path": "agent.debug.context_dump.mode", "value": "always"}],
        reason="Original user-authorized plan.",
    )
    other = RuntimeSettingsService(agent).stage(
        [{"path": "agent.debug.context_dump.enabled", "value": True}],
        reason="Replacement user-authorized plan.",
    )

    call = ToolCall(tool_use_id="tooluse_swap_before_resume")
    with pytest.raises(InterruptException):
        run_apply_change(agent, original["change_id"], call, reason="Apply original plan")

    request = stored_request(agent, call)
    requests = access_runtime.runtime_requests(agent)
    decided = requests.decide(request.request_id, member, AccessReviewChoice.APPROVE)
    assert decided.status is AccessRequestStatus.GRANTED
    call.response = {"kind": "access_request_decision", "status": decided.status.value}

    # Overwrite the approved plan file with a different valid plan reusing the same change_id.
    replacement = RuntimeSettingsChangePlan.model_validate_json(
        Path(other["plan_path"]).read_text(encoding="utf-8")
    )
    replacement.change_id = original["change_id"]
    replacement.plan_digest = model_digest(replacement)
    _rewrite_plan_on_disk(Path(original["plan_path"]), replacement)

    before_bytes = config_path.read_bytes()
    refused = run_apply_change(agent, original["change_id"], call, reason="Resume swapped plan")
    assert refused["status"] == "refused", refused
    assert "plan changed after it was approved" in refused["reason"].lower()
    assert config_path.read_bytes() == before_bytes

    after = requests.service.store.get_request(request.request_id)
    assert after is not None
    assert after.applied_at_utc is not None
    assert after.apply_error is not None
    assert "plan changed after it was approved" in after.apply_error.lower()


@needs_cedar
def test_toctou_plan_swap_between_describe_and_apply_is_refused_in_person_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping the plan on disk after describe() is refused by RuntimeSettingsService.apply()."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path)
    member = PersonPrincipal("member_1", config.gateway.team_id, ("member",))

    original = RuntimeSettingsService(agent).stage(
        [{"path": "agent.debug.context_dump.mode", "value": "always"}],
        reason="Original user-authorized plan.",
    )
    other = RuntimeSettingsService(agent).stage(
        [{"path": "heartbeat.enabled", "value": True}],
        reason="Swapped admin-only plan.",
    )

    call = ToolCall(tool_use_id="tooluse_toctou_person")
    with pytest.raises(InterruptException):
        run_apply_change(agent, original["change_id"], call, reason="Apply original plan")

    request = stored_request(agent, call)
    requests = access_runtime.runtime_requests(agent)
    decided = requests.decide(request.request_id, member, AccessReviewChoice.APPROVE)
    assert decided.status is AccessRequestStatus.GRANTED
    call.response = {"kind": "access_request_decision", "status": decided.status.value}

    replacement = RuntimeSettingsChangePlan.model_validate_json(
        Path(other["plan_path"]).read_text(encoding="utf-8")
    )
    replacement.change_id = original["change_id"]
    replacement.plan_digest = model_digest(replacement)

    real_describe = RuntimeSettingsService.describe

    def describe_then_swap(self: RuntimeSettingsService, change_id: str) -> ChangeProposalV1:
        proposal = real_describe(self, change_id)
        _rewrite_plan_on_disk(Path(original["plan_path"]), replacement)
        return proposal

    monkeypatch.setattr(RuntimeSettingsService, "describe", describe_then_swap)

    before_bytes = config_path.read_bytes()
    refused = run_apply_change(agent, original["change_id"], call, reason="Resume with TOCTOU swap")
    assert refused["status"] == "refused", refused
    assert "plan digest is missing or no longer matches" in refused["reason"]
    assert config_path.read_bytes() == before_bytes

    after = requests.service.store.get_request(request.request_id)
    assert after is not None
    assert after.applied_at_utc is not None
    assert after.apply_error is not None
    assert "plan digest is missing or no longer matches" in after.apply_error


@needs_cedar
def test_toctou_plan_swap_between_describe_and_apply_is_refused_in_held_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping a model-choice plan after describe() is refused in held_access mode."""

    config, config_path = _make_config(tmp_path)
    live = SimpleNamespace(activated=[])

    def probe(profile: Any) -> dict[str, Any]:
        return {"status": "passed", "ready": True, "provider": profile.provider, "model_id": profile.model_id}

    agent = change_agent(
        config,
        config_path,
        authorization_enabled=True,
        model_provider="litellm",
        model_id="old/model",
        reasoning_effort=None,
        reasoning_summary=None,
        litellm_api_mode="chat_completions",
        context_window_tokens=None,
        supports_image_input=None,
        supports_audio_input=None,
        supports_file_input=None,
        probe_runtime_llm_profile=probe,
        apply_runtime_llm_profile=lambda profile, *, source="test": live.activated.append(profile.model_id),
    )
    service = access_runtime.runtime_requests(agent).service
    apply_starter_profile(service, owner(agent), "trusted", lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE)

    original = RuntimeSettingsService(agent).stage(
        [{"path": "current_agent.llm.model_id", "value": "deepseek/deepseek-v4.1-flash"}],
        reason="Switch model.",
    )
    other = RuntimeSettingsService(agent).stage(
        [{"path": "current_agent.llm.model_id", "value": "openai/gpt-5.2"}],
        reason="Swapped model.",
    )
    replacement = RuntimeSettingsChangePlan.model_validate_json(
        Path(other["plan_path"]).read_text(encoding="utf-8")
    )
    replacement.change_id = original["change_id"]
    replacement.plan_digest = model_digest(replacement)

    real_describe = RuntimeSettingsService.describe

    def describe_then_swap(self: RuntimeSettingsService, change_id: str) -> ChangeProposalV1:
        proposal = real_describe(self, change_id)
        _rewrite_plan_on_disk(Path(original["plan_path"]), replacement)
        return proposal

    monkeypatch.setattr(RuntimeSettingsService, "describe", describe_then_swap)

    refused = run_apply_change(agent, original["change_id"], ToolCall(tool_use_id="tooluse_toctou_held"))
    assert refused["status"] == "refused", refused
    assert "plan digest is missing or no longer matches" in refused["reason"]
    assert live.activated == []


def test_toctou_plan_swap_between_describe_and_apply_is_refused_in_agreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping a plan after describe() is refused when authorization is off (agreement mode)."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path, authorization_enabled=False)

    original = RuntimeSettingsService(agent).stage(
        [{"path": "agent.debug.context_dump.mode", "value": "always"}],
        reason="Original agreement plan.",
    )
    other = RuntimeSettingsService(agent).stage(
        [{"path": "heartbeat.enabled", "value": True}],
        reason="Swapped agreement plan.",
    )
    replacement = RuntimeSettingsChangePlan.model_validate_json(
        Path(other["plan_path"]).read_text(encoding="utf-8")
    )
    replacement.change_id = original["change_id"]
    replacement.plan_digest = model_digest(replacement)

    real_describe = RuntimeSettingsService.describe

    def describe_then_swap(self: RuntimeSettingsService, change_id: str) -> ChangeProposalV1:
        proposal = real_describe(self, change_id)
        _rewrite_plan_on_disk(Path(original["plan_path"]), replacement)
        return proposal

    monkeypatch.setattr(RuntimeSettingsService, "describe", describe_then_swap)

    before_bytes = config_path.read_bytes()
    refused = run_apply_change(
        agent,
        original["change_id"],
        ToolCall(tool_use_id="tooluse_toctou_agreement"),
        confirmed=True,
    )
    assert refused["status"] == "refused", refused
    assert "plan digest is missing or no longer matches" in refused["reason"]
    assert config_path.read_bytes() == before_bytes


@pytest.mark.parametrize(
    ("authorization_enabled", "mode", "approved_by", "request_id"),
    [
        (True, ChangeApprovalMode.PERSON_APPROVAL, "admin_1", "access_req_test"),
        (True, ChangeApprovalMode.HELD_ACCESS, TEAM_ARCHITECT_AGENT_TYPE, None),
        (False, ChangeApprovalMode.AGREEMENT, "agreement", None),
    ],
    ids=["person_approval", "held_access", "agreement"],
)
@pytest.mark.parametrize("bad_digest", [None, "", "0" * 64], ids=["none", "empty", "wrong"])
def test_runtime_settings_apply_requires_matching_plan_digest_for_all_modes(
    tmp_path: Path,
    authorization_enabled: bool,
    mode: ChangeApprovalMode,
    approved_by: str,
    request_id: str | None,
    bad_digest: str | None,
) -> None:
    """RuntimeSettingsService.apply() refuses missing or mismatched plan_digest in every mode."""

    config, config_path = _make_config(tmp_path)
    agent = _member_agent(config, config_path, authorization_enabled=authorization_enabled)
    service = RuntimeSettingsService(agent)
    staged = service.stage(
        [{"path": "heartbeat.enabled", "value": True}],
        reason="Direct apply digest check.",
    )
    before_bytes = config_path.read_bytes()
    approval = ChangeApproval(
        mode=mode,
        approved_by=approved_by,
        approved_at_utc=datetime.now(timezone.utc),
        request_id=request_id,
        plan_digest=bad_digest,
    )

    with pytest.raises(PermissionError, match="plan digest is missing or no longer matches"):
        service.apply(staged["change_id"], approval)
    assert config_path.read_bytes() == before_bytes

