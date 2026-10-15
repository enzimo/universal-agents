"""Scheduling a team-written specialist right after it is created (live report).

The owner asked TeamLead for BackgroundOperationsAgent (BOA), approved the
manager restart, then approved adding BOA to
``agent.schedule_workers.eligible_agent_types`` ("takes effect now"). Two
hours later creating a scheduled job for BOA still reported BOA as not
eligible, and a Google requirement was refused as ``not_in_tool_catalog``.

These tests replay that order with the real services: the specialist is
created through ``GeneratedAgentProvisioningService``, the manager restarts
(a fresh process configuration), the eligibility change is applied through
``RuntimeSettingsService``, and the job is created through
``install_scheduled_worker_draft`` (what ``create_scheduled_job`` calls).
Each runs twice: started from the saved ``instance_config.yaml`` and started
from another file (``TEAM_INSTANCE_CONFIG`` elsewhere), which still reads the
saved one. Until 2026-09-29 the Docker bundle ran every process from a copy of
the saved file written at container start, so a saved eligible type stayed
ineligible; tests/test_saved_settings_every_process.py covers its removal.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
from custom_agents import access_runtime
from custom_agents import strands_team as _strands_team  # noqa: F401  (installs the agent type source)
from custom_agents.agent_catalog import load_agent_type_map
from custom_agents.agent_instantiation_service import (
    AgentInstantiationService,
    current_worker_limits,
    effective_worker_rules,
)
from custom_agents.generated_agent_provisioning import GeneratedAgentProvisioningService
from custom_agents.runtime_configuration_context import build_runtime_configuration_context
from custom_agents.runtime_settings_service import RuntimeSettingsService
from custom_agents.schedule_access import (
    NOT_IN_TOOL_CATALOG,
    REQUIREMENT_NOT_A_CATALOG_TOOL,
    SUGGESTION_LIMIT,
    TOOL_CATALOG_CHANGED,
    RequirementProblem,
    operation_suggestions,
    requirement_resolution,
)
from custom_agents.schedule_changes import WorkerConfigurationRequestV1
from custom_agents.schedule_store import ScheduleStore
from custom_agents.system_schedule_changes import configure_worker
from custom_messages.contracts.access_v2 import OperationResolutionV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleCapabilityRequirementsV1,
    ScheduleWorkerPolicySpecV1,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from custom_tools.providers.agent_management import get_available_agent_types
from custom_tools.providers.schedule_workers import install_scheduled_worker_draft, prepare_scheduled_job_access
from custom_tools.runtime_context import _agent_ctx, set_agent_ref
from runtime_settings.schedule_workers import SCHEDULE_WORKER_ELIGIBILITY_PATH
from tests.change_approval_support import (
    agreement,
    authorization_config,
    change_agent,
    needs_cedar,
    use_signed_contexts,
)

BOA = "BackgroundOperationsAgent"
STARTED_FROM = ("operator_file", "started_elsewhere")
GOOGLE_BY_FUNCTION_NAME = {"requirements": [{"capability_id": "cli:google", "operation": "secure_cli"}]}
GOOGLE_READS = {
    "requirements": [
        {"capability_id": "cli:google", "operation": "gmail_search", "necessity": "required"},
        {"capability_id": "cli:google", "operation": "gmail_read", "necessity": "required"},
    ]
}


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()
    _agent_ctx.set(None)


def _instance(tmp_path: Path, started_from: str) -> tuple[GatewayInstanceConfig, Path]:
    """The operator's instance file, and the file the runtime processes start from."""

    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="boa", instance_root=str(root), team_id="team_boa", timezone="America/Chicago"
    )
    saved = config.save(root / "instance_config.yaml")
    if started_from == "operator_file":
        return GatewayInstanceConfig.load(saved), saved
    running = GatewayInstanceConfig.load(saved).save(tmp_path / "started_elsewhere" / "instance_config.yaml")
    return GatewayInstanceConfig.load(saved), running


def _start_manager(config: GatewayInstanceConfig, running: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """TeamLead as a (re)started manager process builds it (strands_main loads ``running``)."""

    monkeypatch.setattr(
        "config_loader._config", build_runtime_config_from_instance_config(GatewayInstanceConfig.load(running))
    )
    agent = change_agent(
        config,
        running,
        authorization_enabled=False,
        _local_team_runtime=SimpleNamespace(
            agent_types=lambda: tuple(load_agent_type_map()),
            resolve_definition=lambda agent_type: load_agent_type_map().get(agent_type),
        ),
        _schedule_store=ScheduleStore(storage_settings=config.manager_storage_settings),
        _scheduler_wake=None,
        schedule_workers={},
        team_composition_path=None,
        mcp_profiles={},
    )
    set_agent_ref(agent)
    return agent


def _create_boa(agent: SimpleNamespace) -> None:
    service = GeneratedAgentProvisioningService(agent)
    plan = service.stage(
        agent_type=BOA,
        display_name="Background Operations Agent",
        role="Run recurring background work for the team.",
        description="A generic background-task specialist.",
        prompt="Run the scheduled task and report only what matters.",
        default_tools=["google_workspace", "fn:publishing"],
        reason="The owner asked for a background operations agent.",
    )
    assert service.apply(plan["change_id"], agreement())["status"] == "ready"


def _make_boa_eligible(agent: SimpleNamespace) -> dict:
    service = RuntimeSettingsService(agent)
    current = current_worker_limits(agent).eligible_agent_types
    plan = service.stage([{"path": SCHEDULE_WORKER_ELIGIBILITY_PATH, "value": [*current, BOA], "target_scope": "team"}])
    return service.apply(plan["change_id"], agreement())


def _schedule(agent: SimpleNamespace, config: GatewayInstanceConfig, schedule_id: str) -> ScheduledMessage:
    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name="Gmail check",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check email; report only important items."),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=config.user.canonical_user_id)),
        claim_team_id=agent.team_id,
        interval_ms=3 * 3600 * 1000,
    )


def _live_sequence(tmp_path, monkeypatch, started_from):
    """Definition installed, manager restarted, eligibility applied after the restart."""

    config, running = _instance(tmp_path, started_from)
    lead = _start_manager(config, running, monkeypatch)
    _create_boa(lead)
    lead = _start_manager(config, running, monkeypatch)  # the approved manager restart
    applied = _make_boa_eligible(lead)
    assert applied["status"] == "applied", applied
    assert BOA in GatewayInstanceConfig.load(config.config_path).agent.schedule_workers.eligible_agent_types
    return config, lead


@pytest.mark.parametrize("started_from", STARTED_FROM)
def test_saved_eligibility_is_what_the_launcher_enforces(tmp_path, monkeypatch, started_from):
    config, lead = _live_sequence(tmp_path, monkeypatch, started_from)

    assert BOA in current_worker_limits(lead).eligible_agent_types
    AgentInstantiationService(lead).validate_type(BOA, automatic=True)
    # What the runtime configuration tool shows is the list the launcher uses.
    shown = build_runtime_configuration_context(lead)
    assert BOA in shown["schedule_workers"]["limits"]["eligible_agent_types"]
    assert shown["schedule_workers"]["source"] == str(config.config_path)
    assert "schedule_workers" not in shown["runtime_config_cache"]


@pytest.mark.parametrize("started_from", STARTED_FROM)
def test_saved_worker_rules_apply_without_a_restart(tmp_path, monkeypatch, started_from):
    config, lead = _live_sequence(tmp_path, monkeypatch, started_from)
    service = RuntimeSettingsService(lead)
    plan = service.stage([{
        "path": "agent.schedule_worker_rules.claim_wait_seconds", "value": 42,
        "target_scope": "agent_type", "target_agent_type": BOA,
    }])
    assert service.apply(plan["change_id"], agreement())["status"] == "applied"

    assert effective_worker_rules(lead, BOA, {}).claim_wait_seconds == 42


def test_a_new_specialist_is_listed_without_a_restart(tmp_path, monkeypatch):
    config, running = _instance(tmp_path, "operator_file")
    lead = _start_manager(config, running, monkeypatch)
    _create_boa(lead)

    assert BOA in json.loads(get_available_agent_types._tool_func())
    AgentInstantiationService(lead).validate_type(BOA, automatic=False)


@pytest.mark.parametrize("started_from", STARTED_FROM)
def test_google_requirement_named_by_function_says_which_operations_exist(tmp_path, monkeypatch, started_from):
    config, lead = _live_sequence(tmp_path, monkeypatch, started_from)
    schedule = _schedule(lead, config, "sched_gmail_check")

    draft = install_scheduled_worker_draft(
        lead, lead._schedule_store, schedule,
        target_agent_type=BOA, worker_policy_json="", requirements_json=json.dumps(GOOGLE_BY_FUNCTION_NAME),
    )

    assert draft["configured"] is True and draft["enabled"] is False, draft
    (check,) = draft["readiness"]["checks"]
    assert check["reason"] == NOT_IN_TOOL_CATALOG
    assert check["suggestions"][:2] == ["gmail_search", "gmail_read"]
    assert all(not name.startswith("command:") for name in check["suggestions"])
    assert "secure_cli" in check["detail"] and "cli:google" in check["detail"] and "find_tools" in check["detail"]

    # With the operations named, the same job saves and turns on.
    repaired = configure_worker(lead, WorkerConfigurationRequestV1(
        schedule_id=schedule.schedule_id,
        target_agent_type=BOA,
        expected_revision=draft["policy_revision"],
        activate_schedule=True,
        policy=ScheduleWorkerPolicySpecV1(),
        capabilities=ScheduleCapabilityRequirementsV1.model_validate(GOOGLE_READS),
    ))
    assert repaired["enabled"] is True, repaired
    assert [item["status"] for item in repaired["readiness"]["checks"]] == ["deferred", "deferred"]


@pytest.mark.parametrize("started_from", STARTED_FROM)
def test_inspection_reports_the_new_specialist_eligible(tmp_path, monkeypatch, started_from):
    from custom_agents.schedule_worker_inspection import inspect_schedule_worker

    config, lead = _live_sequence(tmp_path, monkeypatch, started_from)
    schedule = _schedule(lead, config, "sched_summary")
    install_scheduled_worker_draft(
        lead, lead._schedule_store, schedule,
        target_agent_type=BOA, worker_policy_json="", requirements_json=json.dumps(GOOGLE_READS),
    )

    eligibility = inspect_schedule_worker(lead, "sched_summary")["eligibility"]
    assert eligibility["registered"] and eligibility["listed"] and eligibility["eligible"], eligibility
    assert eligibility["suggested_settings_change"] is None


@needs_cedar
def test_with_authorization_on_the_named_operations_resolve_to_access_to_give(tmp_path, monkeypatch):
    config, lead = _live_sequence(tmp_path, monkeypatch, "started_elsewhere")
    schedule = _schedule(lead, config, "sched_gmail_on")
    install_scheduled_worker_draft(
        lead, lead._schedule_store, schedule, target_agent_type=BOA, worker_policy_json="", requirements_json=""
    )
    lead.authorization_config = authorization_config(True)
    access_runtime._runtime_access.cache_clear()

    wrong = json.loads(prepare_scheduled_job_access._tool_func(
        "sched_gmail_on", BOA, json.dumps(GOOGLE_BY_FUNCTION_NAME)
    ))
    right = json.loads(prepare_scheduled_job_access._tool_func("sched_gmail_on", BOA, json.dumps(GOOGLE_READS)))

    (blocked,) = wrong["checks"]
    assert blocked["reason"] == NOT_IN_TOOL_CATALOG and blocked["suggestions"][0] == "gmail_search"
    assert [(item["tool_id"], item["operation"], item["level"]) for item in right["missing_access"]] == [
        ("cli:google", "gmail_search", "read"),
        ("cli:google", "gmail_read", "read"),
    ]
    assert all("worker_type" in item["missing_for"] for item in right["missing_access"])


def _google_tool():
    from custom_agents.default_tools import builtin_tool_catalog

    catalog = builtin_tool_catalog()
    return catalog, catalog.tools["cli:google"]


def test_suggestions_prefer_close_names_and_named_operations():
    catalog, google = _google_tool()

    assert operation_suggestions(google, "gmail_serch")[0] == "gmail_search"
    # A dotted command path finds the named operation before the command entry.
    dotted = operation_suggestions(google, "gmail.search")
    assert dotted.index("gmail_search") < dotted.index("command:gmail.search")
    assert len(operation_suggestions(google, "anything")) <= SUGGESTION_LIMIT

    with pytest.raises(RequirementProblem) as unknown_tool:
        requirement_resolution(catalog, "google", "gmail_search")
    assert unknown_tool.value.code == REQUIREMENT_NOT_A_CATALOG_TOOL
    assert "cli:google" in unknown_tool.value.suggestions
    assert str(unknown_tool.value) == REQUIREMENT_NOT_A_CATALOG_TOOL


def test_a_catalog_change_during_the_check_has_its_own_reason(caplog):
    from access_control.decisions import HandDownRefused
    from access_control.entities import AgentPrincipal, PersonPrincipal
    from custom_agents.schedule_access import ScheduleAccessCheck

    class _CatalogChanged:
        def holds(self, *args, **kwargs):
            raise HandDownRefused("mcp:notes#search is not an operation in the tool catalog.")

    check = ScheduleAccessCheck(
        service=_CatalogChanged(),
        worker=AgentPrincipal(BOA, BOA, "worker", "team_boa"),
        owner=PersonPrincipal("owner", "team_boa", ("owner",)),
    )
    _catalog, google = _google_tool()
    resolution = OperationResolutionV1(
        tool_id="cli:google", operation="gmail_search", level=google.operation("gmail_search").level,
        source=google.operation("gmail_search").source,
    )

    with caplog.at_level("WARNING"), pytest.raises(RequirementProblem) as refused:
        check.need(resolution)

    assert refused.value.code == TOOL_CATALOG_CHANGED != NOT_IN_TOOL_CATALOG
    assert "prepare_scheduled_job_access again" in refused.value.detail
    assert "event=schedule_access_check_catalog_changed" in caplog.text


def test_saved_settings_fall_back_to_the_started_file_without_an_instance_file(tmp_path):
    config, running = _instance(tmp_path, "started_elsewhere")
    assert GatewayInstanceConfig.load_saved(running).config_path == config.config_path
    config.config_path.unlink()
    assert GatewayInstanceConfig.load_saved(running).config_path == running.resolve()
