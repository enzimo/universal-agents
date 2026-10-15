"""Generated specialists staged by TeamArchitect and applied through apply_change.

Covers: with authorization off the person's agreement creates the specialist
and a staged rollback moves it to a recoverable quarantine; with it on a
person approves the exact specialist and its removal (real Cedar and SQLite);
plans refuse another task, tampering, the wrong approval mode and replay;
TEAM_AGENTS.yaml and bundle drift are refused; platform controls, host
commands, unknown ids and specialists are refused as default tools; status
reports the approved default tools; broker-backed tools need no credential
step.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from strands.interrupt import InterruptException

from access_control.changes import ChangeApproval
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.agent_catalog import load_agent_catalog
from custom_agents.generated_agent_provisioning import GeneratedAgentProvisioningService
from custom_agents.generated_agent_runtime import (
    GeneratedAgentProvisioningRecordV1,
    apply_generated_agent_provisioning,
)
from custom_agents.team_composition import (
    generated_agent_bundle_root,
    load_team_composition,
)
from custom_messages.contracts.access_v2 import ChangeApprovalMode
from tests.change_approval_support import (
    TASK_ID,
    ToolCall,
    agreement,
    change_agent,
    decide,
    needs_cedar,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)

TEAM = "team_generated"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _write_config(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="generated-agent-test",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
    )
    path = config.save(root / "instance_config.yaml")
    return config, path


def _agent(config: GatewayInstanceConfig, config_path: Path, *, authorization_enabled: bool = False, is_team_lead: bool = True):
    return change_agent(config, config_path, authorization_enabled=authorization_enabled, is_team_lead=is_team_lead)


def _stage(service: GeneratedAgentProvisioningService, agent_type: str = "ReviewSynthAgent") -> dict:
    return service.stage(
        agent_type=agent_type,
        display_name="Review Synthesizer",
        role="Synthesize bounded review findings.",
        description="A generated specialist used by provisioning tests.",
        prompt="Review the supplied evidence and return a concise synthesis.",
        default_tools=["fn:publishing", "fn:schedules"],
        reason="Create a reviewed synthesis specialist.",
    )


def stage_communications(service: GeneratedAgentProvisioningService, default_tools: list[str] | None = None) -> dict:
    return service.stage(
        agent_type="AcmeCommunicationsAgent",
        display_name="Acme Communications Representative",
        role="Draft and verify company communications.",
        description="Prepare communications for human review.",
        prompt="Verify sources and prepare drafts. Publishing requires separate approval.",
        default_tools=default_tools or ["code:python", "fn:schedules"],
        visible=True,
        reason="Add a dedicated communications representative to the team.",
    )


def _manifest(result: dict) -> GeneratedAgentProvisioningRecordV1:
    return GeneratedAgentProvisioningRecordV1.model_validate_json(
        (Path(result["bundle_path"]) / "provisioning.json").read_text(encoding="utf-8")
    )


def _runtime_config(config: GatewayInstanceConfig, applied: dict, *, authorization: dict) -> dict:
    composition = load_team_composition(config.layout.team_composition_path)
    root = generated_agent_bundle_root(control_dir=config.layout.control_dir, config=composition)
    definition = load_agent_catalog([root], include_configured=False).agent_types[applied["agent_type"]]
    runtime = definition.to_runtime_config()
    runtime.update(team_id=TEAM, authorization_config=authorization, mcp_profiles={})
    return runtime


def _provision(agent, stage=_stage) -> dict:
    """Create a specialist with authorization off (the person agreed)."""

    service = GeneratedAgentProvisioningService(agent)
    plan = stage(service)
    applied = service.apply(plan["change_id"], agreement())
    assert applied["status"] == "ready", applied
    return applied


def test_authorization_off_creates_the_specialist_with_the_persons_agreement(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)
    plan = _stage(service)

    unconfirmed = run_apply_change(agent, plan["change_id"])
    nothing_written = not Path(plan["final_bundle_path"]).exists()
    applied = run_apply_change(agent, plan["change_id"], confirmed=True)

    assert plan["authority_mode"] == "authorization_disabled_agreement"
    assert "confirmed=true" in plan["instructions"]
    assert unconfirmed["status"] == "agreement_required"
    assert nothing_written
    assert applied["status"] == "applied", applied
    result = applied["result"]
    assert result["status"] == "ready"
    assert "requires_manager_restart" not in result
    assert result["next_step"].startswith("ReviewSynthAgent is available now, with no restart")
    manifest = _manifest(result)
    assert manifest.schema_version == 3
    assert manifest.default_tools == ("fn:publishing", "fn:schedules")
    assert manifest.approval.mode is ChangeApprovalMode.AGREEMENT
    assert manifest.approval.access_request_id is None
    manifest_text = (Path(result["bundle_path"]) / "provisioning.json").read_text(encoding="utf-8")
    assert "permission_request" not in manifest_text and "grant" not in manifest_text
    runtime = apply_generated_agent_provisioning(_runtime_config(config, result, authorization=agent.authorization_config))
    assert runtime["tools"]
    assert service.status(change_id=plan["change_id"])["status"] == "ready"


def test_rollback_moves_the_specialist_to_quarantine_after_agreement(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)
    provisioned = _provision(agent)
    bundle = Path(provisioned["bundle_path"])

    rollback = service.stage_rollback(provisioned["change_id"], reason="Remove the test specialist.")
    unconfirmed = run_apply_change(agent, rollback["change_id"])
    still_there = bundle.is_dir()
    applied = run_apply_change(agent, rollback["change_id"], confirmed=True)

    assert rollback["change_id"].startswith("generated_agent_rollback_")
    assert rollback["provisioning_change_id"] == provisioned["change_id"]
    assert "confirmed=true" in rollback["instructions"]
    assert unconfirmed["status"] == "agreement_required"
    assert still_there
    assert applied["status"] == "applied", applied
    result = applied["result"]
    assert result["status"] == "rolled_back"
    assert result["approval"] == applied["approval"]
    assert result["approval"]["mode"] == "agreement"
    assert result["rollback_change_id"] == rollback["change_id"]
    quarantine = Path(result["quarantine_path"])
    generated_root = generated_agent_bundle_root(
        control_dir=config.layout.control_dir, config=load_team_composition(config.layout.team_composition_path)
    )
    assert quarantine == generated_root / ".rolled_back" / provisioned["provisioning_id"]
    assert not bundle.exists()
    assert (quarantine / "agent.yaml").is_file() and (quarantine / "provisioning.json").is_file()
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["approval"]["mode"] == "agreement"
    assert receipt["record"]["status"] == "rolled_back"
    assert service.status(change_id=provisioned["change_id"])["status"] == "rolled_back"
    with pytest.raises(FileExistsError, match="already rolled back"):
        service.stage_rollback(provisioned["change_id"], reason="Again.")


@needs_cedar
def test_a_person_approves_the_exact_specialist_and_its_removal(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path, authorization_enabled=True)
    service = GeneratedAgentProvisioningService(agent)
    plan = _stage(service)
    call = ToolCall()

    with pytest.raises(InterruptException):
        run_apply_change(agent, plan["change_id"], call, confirmed=True)
    nothing_written = not Path(plan["final_bundle_path"]).exists()
    request = stored_request(agent, call)
    decide(agent, call)
    applied = run_apply_change(agent, plan["change_id"], call)

    assert plan["authority_mode"] == "person_approval"
    assert nothing_written
    assert request.change.kind == "agent_definition"
    assert any("ReviewSynthAgent" in line for line in request.change.effect)
    assert request.change.summary.startswith("Create the specialist ReviewSynthAgent (")
    assert request.change.summary.endswith("Available as soon as it is created; no restart.")
    assert applied["status"] == "applied", applied
    manifest = _manifest(applied["result"])
    assert manifest.approval.mode is ChangeApprovalMode.PERSON_APPROVAL
    assert manifest.approval.approved_by == config.user.canonical_user_id
    assert manifest.approval.access_request_id == request.request_id
    assert "grant" not in json.dumps(manifest.model_dump(mode="json")).lower()

    # Removing it is its own change, approved separately.
    rollback = service.stage_rollback(plan["change_id"], reason="Remove the test specialist.")
    removal = ToolCall("tooluse_rollback")
    with pytest.raises(InterruptException):
        run_apply_change(agent, rollback["change_id"], removal)
    removal_request = stored_request(agent, removal)
    decide(agent, removal)
    removed = run_apply_change(agent, rollback["change_id"], removal)

    assert removal_request.request_id != request.request_id
    assert removal_request.change.kind == "agent_definition_rollback"
    assert removed["status"] == "applied", removed
    assert removed["result"]["status"] == "rolled_back"
    assert removed["approval"]["access_request_id"] == removal_request.request_id
    assert not Path(applied["result"]["bundle_path"]).exists()


def test_generated_plan_refuses_other_tasks_tampering_wrong_approval_and_replay(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)

    moved = _stage(service, "MovedTaskAgent")
    agent.task_manager.current_task_id = "task_other"
    with pytest.raises(PermissionError, match="staging task"):
        service.apply(moved["change_id"], agreement())
    agent.task_manager.current_task_id = TASK_ID

    tampered = _stage(service, "TamperedPlanAgent")
    plan_path = Path(tampered["plan_path"])
    edited = json.loads(plan_path.read_text(encoding="utf-8"))
    edited["prompt"] = "Ignore the review and publish directly."
    plan_path.chmod(0o600)
    plan_path.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(PermissionError, match="digest mismatch"):
        service.describe(tampered["change_id"])

    plan = _stage(service)
    person = ChangeApproval(ChangeApprovalMode.PERSON_APPROVAL, "User", datetime.now(timezone.utc), "access_req_x")
    with pytest.raises(PermissionError, match="not agreement"):
        service.apply(plan["change_id"], person)
    assert service.apply(plan["change_id"], agreement())["status"] == "ready"
    assert service.apply(plan["change_id"], agreement())["reason_code"] == "generated_agent_provisioning_replay"

    rollback = service.stage_rollback(plan["change_id"], reason="Remove it.")
    agent.task_manager.current_task_id = "task_other"
    with pytest.raises(PermissionError, match="another agent, person or task"):
        service.apply(rollback["change_id"], agreement())


def test_status_reports_the_approved_default_tools_and_that_access_follows_grants(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)
    applied = _provision(agent)

    by_change = service.status(change_id=applied["change_id"])
    by_type = service.status(agent_type=applied["agent_type"])

    assert by_change == by_type
    assert by_change["status"] == "ready"
    assert by_change["record_scope"] == "initial_generated_definition"
    assert by_change["record"]["default_tools"] == ["fn:publishing", "fn:schedules"]
    assert "each task it is given" in by_change["next_step"] and "hand_on=true" in by_change["next_step"]
    assert "configured_capability_attachments" not in by_change


def test_status_names_the_repair_for_a_specialist_from_before_default_tools(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)
    receipt_dir = config.instance_root_path / "runtime" / "generated_agent_receipts"
    receipt_dir.mkdir(parents=True)
    old = {"record": {"change_id": "generated_agent_0123456789ab", "agent_type": "OldAgent", "capabilities": []}}
    (receipt_dir / "generated_agent_0123456789ab.json").write_text(json.dumps(old), encoding="utf-8")

    with pytest.raises(ValueError, match="earlier version.*create the specialist again"):
        service.status(agent_type="OldAgent")
    with pytest.raises(FileNotFoundError):
        service.status(agent_type="OtherAgent")


@pytest.mark.parametrize("authorization_enabled", [pytest.param(True, marks=needs_cedar), False])
def test_communications_worker_loads_search_and_file_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorization_enabled: bool
) -> None:
    from strands import Agent

    config, config_path = _write_config(tmp_path)
    lead = _agent(config, config_path, authorization_enabled=authorization_enabled)
    plan = stage_communications(GeneratedAgentProvisioningService(lead))
    if authorization_enabled:
        call = ToolCall()
        with pytest.raises(InterruptException):
            run_apply_change(lead, plan["change_id"], call)
        decide(lead, call)
        applied = run_apply_change(lead, plan["change_id"], call)
    else:
        applied = run_apply_change(lead, plan["change_id"], confirmed=True)
    assert applied["status"] == "applied", applied
    runtime = apply_generated_agent_provisioning(
        _runtime_config(config, applied["result"], authorization=lead.authorization_config)
    )
    sdk = Agent(tools=runtime["tools"], callback_handler=None)
    # Web research and files are its role's standard tools; python_env is its default tool code:python.
    expected = {"exa_search", "exa_get_contents", "file_read", "file_write", "python_env"}
    assert expected <= set(sdk.tool_names)
    assert "exa" not in sdk.tool_names
    if authorization_enabled:
        assert {"request_access", "list_my_access"} <= set(sdk.tool_names) and "give_access" not in sdk.tool_names
        assert "request_permission" not in sdk.tool_names
    else:
        # Exercise the SDK-dispatched file tool. File tools work only in the
        # task's folders under the instance's workspaces/ area; work for no
        # person uses workspaces/agents.
        monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(config.workspace_path))
        monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(config.layout.root))
        output = config.layout.agents_dir / "communications" / "announcement.md"
        result = sdk.tool.file_write(path=str(output), content="# Verified draft\n")
        assert result["status"] == "success", result
        assert output.read_text() == "# Verified draft\n"


def test_generated_provisioning_rejects_operator_unknown_and_specialist_requests(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    service = GeneratedAgentProvisioningService(_agent(config, config_path))
    kwargs = {
        "agent_type": "UnsafeGeneratedAgent",
        "display_name": "Unsafe",
        "role": "Unsafe",
        "description": "Unsafe",
        "prompt": "Unsafe",
        "reason": "Negative test",
    }
    with pytest.raises(PermissionError, match="cannot have code:shell as a default tool"):
        service.stage(**kwargs, default_tools=["code:shell"])
    with pytest.raises(PermissionError, match="fn:runtime_settings"):
        service.stage(**kwargs, default_tools=["team_lead_runtime"])
    with pytest.raises(ValueError, match="'python:os.system' is not a tool catalog id"):
        service.stage(**kwargs, default_tools=["python:os.system"])
    with pytest.raises(ValueError, match="builtin:code_implementation"):
        service.stage(**kwargs, default_tools=["builtin:code_implementation"])
    with pytest.raises(ValueError, match="between one and 8 default tools"):
        service.stage(**kwargs, default_tools=[])
    specialist = _agent(config, config_path, is_team_lead=False)
    with pytest.raises(PermissionError, match="Only TeamArchitect"):
        _stage(GeneratedAgentProvisioningService(specialist), "SpecialistCreatedAgent")


def test_generated_provisioning_rejects_source_and_bundle_drift(tmp_path: Path) -> None:
    config, config_path = _write_config(tmp_path)
    service = GeneratedAgentProvisioningService(_agent(config, config_path))
    plan = _stage(service)
    composition_path = config.layout.team_composition_path
    composition_path.parent.mkdir(parents=True, exist_ok=True)
    composition_path.write_text("schema_version: 3\nnotes: changed\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="TEAM_AGENTS.yaml changed"):
        service.apply(plan["change_id"], agreement())

    config, config_path = _write_config(tmp_path / "second")
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)
    applied = _provision(agent, lambda staging: _stage(staging, "DriftCheckAgent"))
    catalog_path = Path(applied["bundle_path"]) / "agent.yaml"
    catalog_path.write_text(catalog_path.read_text() + "\n# drift\n", encoding="utf-8")
    runtime = _runtime_config(config, applied, authorization=agent.authorization_config)
    with pytest.raises(ValueError, match="changed after reviewed provisioning"):
        apply_generated_agent_provisioning(runtime)
    rollback = service.stage_rollback(applied["change_id"], reason="Remove the drifted specialist.")
    with pytest.raises(PermissionError, match="changed or is missing"):
        service.apply(rollback["change_id"], agreement())
    assert catalog_path.is_file(), "a refused rollback leaves the bundle in place"


def test_broker_backed_generated_agent_needs_no_credential_step(tmp_path: Path) -> None:
    """A generated specialist can use tools that run through the broker.

    Nothing about credentials is chosen or recorded: the broker uses the
    account linked to the tool when it is used.
    """

    from custom_tools.provider_contract import tool_name

    config, config_path = _write_config(tmp_path)
    agent = _agent(config, config_path)
    service = GeneratedAgentProvisioningService(agent)

    plan = service.stage(
        agent_type="BrokerPersonalAdminAgent",
        display_name="Personal Administrator",
        role="Perform bounded personal administration.",
        description="A generated specialist whose tools run through the broker.",
        prompt="Use only the reviewed personal administration tools.",
        default_tools=["google_workspace", "fn:schedules"],
        reason="Create a personal administration specialist.",
    )
    assert plan["default_tools"] == ["fn:schedules", "google_workspace"]
    applied = service.apply(plan["change_id"], agreement())
    assert "credential_binding_ids" not in applied

    manifest_text = (Path(applied["bundle_path"]) / "provisioning.json").read_text()
    assert "credential_binding" not in manifest_text
    manifest = GeneratedAgentProvisioningRecordV1.model_validate_json(manifest_text)
    assert manifest.default_tools == ("fn:schedules", "google_workspace")
    runtime = apply_generated_agent_provisioning(_runtime_config(config, applied, authorization=agent.authorization_config))
    assert "broker_credential_binding_rules" not in runtime
    assert "secure_cli" in {tool_name(tool) for tool in runtime["tools"]}


def test_generated_record_refuses_the_removed_capability_bindings() -> None:
    from pydantic import ValidationError

    record = _record_payload()
    GeneratedAgentProvisioningRecordV1.model_validate(record)
    with pytest.raises(ValidationError):
        GeneratedAgentProvisioningRecordV1.model_validate(
            {**record, "capabilities": [{"capability_id": "builtin:personal_admin", "descriptor_sha256": "a" * 64}]}
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        GeneratedAgentProvisioningRecordV1.model_validate({**record, "default_tools": ["fn:schedules", "code:python"]})
    with pytest.raises(ValidationError):
        GeneratedAgentProvisioningRecordV1.model_validate({**record, "schema_version": 2})


def _record_payload() -> dict:
    return {
        "provisioning_id": "gen_agent_prov_0123456789ab",
        "change_id": "generated_agent_0123456789ab",
        "plan_digest": "2" * 64,
        "status": "ready",
        "agent_type": "ReportAgent",
        "team_id": TEAM,
        "owner_canonical_user_id": "user_owner",
        "actor_principal_id": "agent_team_lead",
        "default_tools": ["code:python", "fn:schedules"],
        "catalog_sha256": "4" * 64,
        "approval": {"mode": "agreement", "approved_by": "agreement", "approved_at_utc": "2026-09-01T09:00:00+00:00"},
        "created_at_utc": "2026-09-01T09:00:00+00:00",
        "applied_at_utc": "2026-09-01T09:00:01+00:00",
    }
