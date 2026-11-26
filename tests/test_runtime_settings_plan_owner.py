"""Tests for runtime settings plan decision class and requester ownership."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.interrupt import InterruptException

from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.governance.authorized_operations import model_digest
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
    ChangeProposalV1,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from tests.change_approval_support import (
    SignedTaskManager,
    ToolCall,
    authorization_config,
    needs_cedar,
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
) -> SimpleNamespace:
    team_id = config.gateway.team_id
    return SimpleNamespace(
        id="agent_team_lead",
        team_id=team_id,
        is_team_lead=True,
        name=TEAM_ARCHITECT_AGENT_TYPE,
        named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        access_role="worker",
        authorization_config=authorization_config(True),
        instance_config_path=str(config_path),
        instance_root=str(config.instance_root_path),
        bootstrap_config={"instance_root": str(config.instance_root_path)},
        workspace_dir=str(config.workspace_path),
        task_manager=SignedTaskManager(
            team_id=team_id,
            person_id=person_id,
            storage_settings=config.manager_storage_settings,
            roles=("member",),
        ),
    )


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
    assert config_path.read_bytes() == before_bytes

    after = requests.service.store.get_request(waiting_request.request_id)
    assert after is not None
    assert after.applied_at_utc is not None
    assert after.apply_error

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
    plan_path.chmod(0o644)
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")

    with pytest.raises(PermissionError, match="approval class changed after planning"):
        RuntimeSettingsService(agent).load_plan(plan.change_id)

