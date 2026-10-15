from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml
import pytest

from assistant_gateway.config import GatewayInstanceConfig

# Runtime composition installs the cycle-free catalog lookup used by startup tools.
from custom_agents import runtime_settings_llm
from model_providers.endpoints import openai_compatible_endpoint
from tests.model_key_support import env_model_keys
from custom_agents import agent_catalog as _agent_catalog  # noqa: F401
from custom_agents import strands_team as _strands_team  # noqa: F401
from custom_messages.contracts.runtime_v1 import (
    RESTART_SCOPE_ALL,
    RESTART_SCOPE_MANAGER,
    RESTART_SCOPE_NONE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from access_control.changes import ChangeApproval
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from strands.interrupt import Interrupt, InterruptException
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import AccessReviewChoice, ChangeApprovalMode
from custom_agents.runtime_settings_service import RuntimeSettingsService
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys
from custom_tools.runtime_context import _agent_ctx, set_agent_ref
from custom_tools.providers.runtime_control import (
    plan_runtime_restart,
    get_runtime_restart_status,
)
from runtime_settings.instance_layout import InstanceLayout
from custom_tools.providers.changes import apply_change
from custom_tools.providers.runtime_settings import (
    get_team_composition,
    list_runtime_settings_options,
    plan_runtime_setting_change,
    plan_runtime_settings_change,
    set_ephemeral_agent_policy,
    set_team_startup_agent,
    validate_llm_overrides_yaml,
)


CEDAR_BINARY = Path(__file__).resolve().parents[1] / "target" / "debug" / "ua-cedar-authorizer"


class _FakeTaskManager:
    """The manager of one running task that a person asked for through the gateway."""

    def __init__(self, storage_settings) -> None:
        self.storage_settings = storage_settings
        self.current_task_id = "task_settings"
        signed = context_signer().issue(team_id="test_team", person_id="user_1", person_roles=("owner",))
        self._meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=signed), conversation_id="conv_1")

    def get_task_info(self, task_id):
        return SimpleNamespace(meta=self._meta) if task_id == self.current_task_id else None

    def add_task_terminal_callback(self, _callback) -> None:
        return None


class _ToolCall:
    """A Strands tool context for one call; interrupts are not answered here."""

    def __init__(self) -> None:
        self.tool_use = {"toolUseId": "tooluse_settings", "name": "apply_change"}

    def interrupt(self, name, reason=None):
        raise AssertionError(f"unexpected pause for approval: {reason}")


def _agreement() -> ChangeApproval:
    """What apply_change passes to the service when authorization is off."""

    return ChangeApproval(ChangeApprovalMode.AGREEMENT, "agreement", datetime.now(timezone.utc))


def _apply_settings_change(change_id: str, confirmed: bool = False) -> str:
    """Apply through apply_change and return the settings service's own result."""

    payload = json.loads(asyncio.run(apply_change._tool_func(change_id, "test change", _ToolCall(), confirmed=confirmed)))
    if payload.get("status") in {"applied", "failed"}:
        return json.dumps(payload["result"])
    return json.dumps({"error": payload.get("message") or payload.get("reason"), "reason_code": payload["status"]})


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield
    access_runtime._runtime_access.cache_clear()


class _FakeAgent(SimpleNamespace):
    def probe_runtime_llm_profile(self, profile, *, request_settings=None) -> dict:
        self.probed_profiles.append(profile.model_dump(mode="python"))
        self.probed_request_settings.append(request_settings)
        callback = getattr(self, "probe_callback", None)
        if callable(callback):
            callback()
        probe_error = getattr(self, "probe_error", None)
        if probe_error is not None:
            raise probe_error
        if getattr(self, "fail_probe", False):
            raise RuntimeError("candidate probe failed")
        return {
            "status": "passed",
            "ready": True,
            "provider": profile.provider,
            "model_id": profile.model_id,
            "duration_seconds": 0.01,
            "timeout_seconds": 120.0,
            "tools_exposed": 0,
        }

    def apply_runtime_llm_profile(self, profile, *, source: str = "test") -> None:
        activation_error = getattr(self, "activation_error", None)
        if activation_error is not None:
            raise activation_error
        if getattr(self, "fail_activation", False):
            raise RuntimeError("candidate activation failed")
        self.applied_overrides.append((profile.provider, profile.model_id, source))
        self.model_provider = profile.provider
        self.model_id = profile.model_id
        self.reasoning_effort = profile.reasoning_effort
        self.reasoning_summary = profile.reasoning_summary
        self.litellm_api_mode = profile.litellm_api_mode
        self.context_window_tokens = profile.context_window_tokens
        self.supports_image_input = profile.supports_image_input
        self.supports_audio_input = profile.supports_audio_input
        self.supports_file_input = profile.supports_file_input

    def apply_runtime_llm_override(
        self,
        provider: str,
        model_id: str,
        *,
        reasoning_effort=None,
        reasoning_summary=None,
        litellm_api_mode="chat_completions",
        context_window_tokens=None,
        supports_image_input=None,
        supports_audio_input=None,
        supports_file_input=None,
        source: str = "test",
    ) -> None:
        self.applied_overrides.append((provider, model_id, source))
        self.model_provider = provider
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.reasoning_summary = reasoning_summary
        self.litellm_api_mode = litellm_api_mode
        self.context_window_tokens = context_window_tokens
        self.supports_image_input = supports_image_input
        self.supports_audio_input = supports_audio_input
        self.supports_file_input = supports_file_input

    def apply_runtime_context_dump_settings(
        self, *, enabled: bool, mode: str, source: str = "test"
    ) -> None:
        self.applied_context_dump_settings.append((enabled, mode, source))


def _write_config(tmp_path: Path, *, heartbeat_enabled: bool = False) -> Path:
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="nova",
        instance_root=str(instance_root),
        team_id="test_team",
        timezone="America/Chicago",
        heartbeat_enabled=heartbeat_enabled,
    )
    return config.save(instance_root / "instance_config.yaml")


def _layout(config_path: Path) -> InstanceLayout:
    """The instance layout of a config saved at ``<root>/instance_config.yaml``."""

    return InstanceLayout.at(config_path.parent)


def _overrides_path(config_path: Path) -> Path:
    """The team's LLM override file, in the instance's control folder."""

    return _layout(config_path).llm_overrides_path


def _fake_agent(config_path: Path) -> _FakeAgent:
    return _FakeAgent(
        id="agent_1",
        team_id="test_team",
        is_team_lead=True,
        name=TEAM_ARCHITECT_AGENT_TYPE,
        named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        authorization_config={"enabled": False},
        instance_config_path=str(config_path),
        instance_root=str(config_path.parent),
        workspace_dir=str(config_path.parent / "workspaces"),
        model_provider="litellm",
        model_id="old/model",
        reasoning_effort=None,
        reasoning_summary=None,
        litellm_api_mode="chat_completions",
        context_window_tokens=None,
        supports_image_input=None,
        supports_audio_input=None,
        supports_file_input=None,
        task_manager=_FakeTaskManager(
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(config_path.parent / "manager.db")}})
        ),
        applied_overrides=[],
        probed_profiles=[],
        probed_request_settings=[],
        applied_context_dump_settings=[],
    )


def test_lead_cannot_allow_automatic_manager_or_exceed_worker_ceiling(tmp_path):
    path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(path)
    from runtime_settings.schedule_workers import ScheduleWorkersConfig
    config.agent.schedule_workers = ScheduleWorkersConfig(max_claim_wait_seconds=30)
    config.save(path)
    original = path.read_bytes()
    set_agent_ref(_fake_agent(path))
    try:
        denied = json.loads(plan_runtime_setting_change._tool_func(
            path="agent.schedule_workers.eligible_agent_types", value=["TeamArchitect"], target_scope="team"
        ))
        assert "change_id" not in denied
        assert "automatic_manager_launch_forbidden" in denied["error"]
        plan = json.loads(plan_runtime_setting_change._tool_func(
            path="agent.schedule_worker_rules.claim_wait_seconds", value=60,
            target_scope="agent_type", target_agent_type="WebResearchAgent"
        ))
        if "change_id" in plan:
            applied = json.loads(_apply_settings_change(plan["change_id"]))
            assert applied.get("status") != "applied"
        assert path.read_bytes() == original
    finally:
        _agent_ctx.set(None)


@pytest.mark.parametrize("invalid_type,reason", [
    ("UnknownWorker", "agent_definition_unavailable"),
    ("BlockedWorker", "agent_type_disallowed"),
    ("TeamArchitect", "automatic_manager_launch_forbidden"),
])
def test_worker_eligibility_rejects_invalid_additions(tmp_path, invalid_type, reason):
    path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(path)
    config.agent.schedule_workers.denied_agent_types = ["BlockedWorker"]
    config.save(path)
    agent = _fake_agent(path)
    agent._local_team_runtime = SimpleNamespace(resolve_definition=lambda name: None)
    original = path.read_bytes()
    with pytest.raises((ValueError, PermissionError), match=reason):
        RuntimeSettingsService(agent).stage([{
            "path": "agent.schedule_workers.eligible_agent_types", "value": [invalid_type],
        }])
    assert path.read_bytes() == original


@pytest.mark.parametrize("path,value", [
    ("agent.schedule_workers.denied_agent_types", []),
    ("agent.schedule_workers.enabled", True),
    ("agent.worker_pools.max_workers_per_type_ceiling", 100),
    ("agent.worker_pools.default_max_workers_per_type", 8),
])
def test_worker_eligibility_does_not_unlock_operator_controls(tmp_path, path, value):
    config_path = _write_config(tmp_path)
    with pytest.raises(ValueError, match="operator-only"):
        RuntimeSettingsService(_fake_agent(config_path)).stage([{"path": path, "value": value}])


@pytest.mark.parametrize("change_after_plan,reason", [
    ("definition_removed", "agent_definition_unavailable"),
    ("operator_deny", "changed after planning"),
])
def test_worker_eligibility_rechecks_before_apply(tmp_path, change_after_plan, reason):
    path = _write_config(tmp_path)
    definitions = {"CustomResearcher": {"named_agent_type": "CustomResearcher"}}
    agent = _fake_agent(path)
    agent._local_team_runtime = SimpleNamespace(resolve_definition=definitions.get)
    service = RuntimeSettingsService(agent)
    plan = service.stage([{
        "path": "agent.schedule_workers.eligible_agent_types", "value": ["CustomResearcher"],
    }])
    if change_after_plan == "definition_removed":
        definitions.clear()
    else:
        config = GatewayInstanceConfig.load(path)
        config.agent.schedule_workers.denied_agent_types = ["CustomResearcher"]
        config.save(path)
    original = path.read_bytes()
    with pytest.raises((ValueError, PermissionError), match=reason):
        service.apply(plan["change_id"], _agreement())
    assert path.read_bytes() == original


def test_worker_eligibility_can_remove_stale_or_denied_entries(tmp_path):
    path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(path)
    config.agent.schedule_workers.eligible_agent_types = ["RemovedWorker", "BlockedWorker"]
    config.agent.schedule_workers.denied_agent_types = ["BlockedWorker"]
    config.save(path)
    service = RuntimeSettingsService(_fake_agent(path))
    plan = service.stage([{
        "path": "agent.schedule_workers.eligible_agent_types", "value": [],
    }])
    assert service.apply(plan["change_id"], _agreement())["status"] == "applied"
    assert GatewayInstanceConfig.load(path).agent.schedule_workers.eligible_agent_types == []


def _write_generated_specialist(
    config_path: Path,
    *,
    description_override: str | None = None,
) -> Path:
    from custom_agents.team_composition import (
        load_team_composition,
        save_team_composition,
        write_generated_agent_bundle,
    )

    layout = GatewayInstanceConfig.load(config_path).layout
    layout.ensure_directories()
    composition_path = layout.team_composition_path
    composition = load_team_composition(composition_path)
    bundle_path = write_generated_agent_bundle(
        control_dir=layout.control_dir,
        config=composition,
        agent_type="PlanningCriticAgent",
        template_name="TaskSpecialistTemplate",
        params={
            "display_name": "Planning Critic Agent",
            "role": "Planning review specialist",
            "description": "Old description.",
            "prompt": "# Planning Critic\n\nReview plans.",
        },
        overrides={
            "default_tools": ["fn:publishing"],
            **({"description": description_override} if description_override is not None else {}),
        },
        overwrite=True,
    )
    save_team_composition(composition_path, composition)
    return bundle_path


def test_runtime_settings_options_include_controllable_and_operator_only() -> None:
    payload = json.loads(list_runtime_settings_options._tool_func())

    paths = {item["path"] for item in payload["agent_controllable"]}
    operator_paths = {item["path"] for item in payload["operator_only"]}

    assert "current_agent.llm.model_id" in paths
    assert "current_agent.llm.supports_image_input" in paths
    assert "current_agent.llm.supports_audio_input" in paths
    assert "current_agent.llm.supports_file_input" in paths
    assert "current_agent.llm.reasoning_effort" in paths
    assert "current_agent.llm.reasoning_summary" in paths
    assert "current_agent.llm.litellm_api_mode" in paths
    assert "current_agent.llm.context_window_tokens" in paths
    assert "workspace_default.llm.provider" in paths
    assert "workspace_default.llm.model_id" in paths
    assert "workspace_default.llm.reasoning_effort" in paths
    assert "workspace_default.llm.reasoning_summary" in paths
    assert "workspace_default.llm.litellm_api_mode" in paths
    assert "workspace_default.llm.supports_file_input" in paths
    assert "llm.supports_image_input" in paths
    assert "llm.supports_file_input" in paths
    assert "llm.reasoning_trace_retention_messages" in paths
    assert "llm.reasoning_summary" in paths
    assert "llm.litellm_api_mode" in paths
    assert "llm.context_window_tokens" in paths
    assert "heartbeat.enabled" in paths
    assert "agent.debug.context_dump.enabled" in paths
    assert "agent.debug.context_dump.mode" in paths
    assert "tool_policy.*" in operator_paths
    assert [item["path_prefix"] for item in payload["llm_precedence"]] == [
        "llm.*",
        "workspace_default.llm.*",
        "current_agent.llm.*",
    ]
    model_setting = next(
        item
        for item in payload["agent_controllable"]
        if item["path"] == "current_agent.llm.model_id"
    )
    assert model_setting["restart_scope"] == RESTART_SCOPE_NONE
    assert "restart_scope_by_target" not in model_setting
    assert "other running agents of the target type apply it at their next turn" in model_setting["summary"]
    assert "tested at staging and again before any write" in " ".join(payload["workflow"])


def test_raw_agent_definition_tool_is_removed_and_reviewed_surface_is_present(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    from custom_agents.agent_catalog import load_agent_catalog

    manager_tools = {
        getattr(tool, "__name__", getattr(tool, "name", str(tool)))
        for tool in load_agent_catalog(include_configured=False).agent_types["TeamArchitect"].tools
    }

    assert "create_agent_definition_from_template" not in manager_tools
    assert "specialist_definitions" in manager_tools
    # Staged specialists are applied through the shared apply_change tool.
    assert "request_generated_agent_definition_authorization" not in manager_tools
    assert "apply_generated_agent_definition" not in manager_tools
    assert "apply_change" in manager_tools
    assert not list(config_path.parent.glob("**/PlanningCriticAgent/agent.yaml"))


def test_legacy_team_composition_tools_use_staged_receipted_service(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    _write_generated_specialist(config_path)
    composition_path = _layout(config_path).team_composition_path
    for index in range(6):
        composition_path.with_name(
            f"TEAM_AGENTS.yaml.bak.preexisting.{index}"
        ).write_text(f"backup {index}\n", encoding="utf-8")
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        # The roster tools stage plans; apply_change applies them.
        staged_startup = json.loads(
            set_team_startup_agent._tool_func(
                agent_type="PlanningCriticAgent",
                count=1,
                enabled=True,
                mode="merge",
            )
        )
        startup = json.loads(_apply_settings_change(staged_startup["change_id"], confirmed=True))
        staged_policy = json.loads(
            set_ephemeral_agent_policy._tool_func(
                agent_type="PlanningCriticAgent",
                idle_ttl_seconds=45,
                enabled=True,
            )
        )
        updated = json.loads(_apply_settings_change(staged_policy["change_id"], confirmed=True))
        composition = json.loads(get_team_composition._tool_func())
    finally:
        _agent_ctx.set(None)

    assert startup["status"] == "applied"
    assert Path(startup["receipt_path"]).is_file()
    assert Path(startup["backup_path"]).is_file()
    assert updated["status"] == "applied"
    assert Path(updated["receipt_path"]).is_file()
    assert len(list(composition_path.parent.glob("TEAM_AGENTS.yaml.bak.*"))) == 5
    startup_agents = {
        item["named_agent_type"]: item
        for item in composition["composition"]["startup_agents"]["agents"]
    }
    assert startup_agents["PlanningCriticAgent"]["count"] == 1
    assert (
        composition["composition"]["ephemeral_agents"]["agent_types"][
            "PlanningCriticAgent"
        ]["idle_ttl_seconds"]
        == 45
    )


def test_runtime_settings_plan_schema_requires_change_path_and_value() -> None:
    bulk_schema = plan_runtime_settings_change.tool_spec["inputSchema"]["json"]
    change_ref = bulk_schema["properties"]["changes"]["items"]["$ref"]
    change_schema = bulk_schema["$defs"][change_ref.rsplit("/", 1)[-1]]

    assert set(change_schema["required"]) == {"path", "value"}
    assert change_schema["properties"]["path"]["type"] == "string"
    assert change_schema["properties"]["target_scope"]["type"] == "string"
    assert change_schema["properties"]["target_agent_type"]["type"] == "string"

    single_schema = plan_runtime_setting_change.tool_spec["inputSchema"]["json"]
    assert set(single_schema["required"]) == {"path", "value"}
    assert single_schema["properties"]["path"]["type"] == "string"


def test_runtime_settings_change_switches_current_agent_model_live_and_persists(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    original_config_text = config_path.read_text(encoding="utf-8")
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        override_path.with_name(
            f"LLM_OVERRIDES.yaml.bak.preexisting.{index}"
        ).write_text(f"backup {index}\n", encoding="utf-8")
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "current_agent.llm.provider", "value": "litellm"},
                    {
                        "path": "current_agent.llm.model_id",
                        "value": "openrouter/test-model",
                    },
                ],
                reason="Switch current manager model.",
            )
        )
        result = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "applied"
    assert result["requires_restart"] is False
    assert result["persistence_target"] == "workspace_llm_overrides"
    assert result["live_applied"] == [
        "current_agent.llm.model_id",
        "current_agent.llm.provider",
    ]
    assert (
        result["capability_follow_up"]["recommended_agent_type"]
        == "InternalImprovementAgent"
    )
    assert (
        result["capability_follow_up"]["recommended_tool"]
        == "lookup_model_capability_candidates"
    )
    assert result["capability_follow_up"]["missing_fields"] == [
        "supports_image_input",
        "supports_audio_input",
        "supports_file_input",
    ]
    assert result["preflight"]["ready"] is True
    assert result["preflight"]["status"] == "passed"
    assert result["preflight"]["tools_exposed"] == 0
    assert result["preflight"]["tested_before_write"] is True
    assert result["persistence_verification"]["status"] == "passed"
    assert agent.applied_overrides == [
        ("litellm", "openrouter/test-model", "user_runtime_settings_change")
    ]

    assert config_path.read_text(encoding="utf-8") == original_config_text
    saved = GatewayInstanceConfig.load(config_path)
    assert TEAM_ARCHITECT_AGENT_TYPE not in saved.agent_types
    overrides_path = Path(result["saved_path"])
    assert overrides_path == _overrides_path(config_path)
    assert Path(result["backup_path"]).is_file()
    assert len(list(overrides_path.parent.glob("LLM_OVERRIDES.yaml.bak.*"))) == 5
    overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    assert overrides["agent_types"][TEAM_ARCHITECT_AGENT_TYPE] == {
        "provider": "litellm",
        "model_id": "openrouter/test-model",
        "reasoning_effort": None,
        "reasoning_summary": None,
        "litellm_api_mode": "chat_completions",
        "context_window_tokens": None,
        "supports_image_input": None,
        "supports_audio_input": None,
        "supports_file_input": None,
    }


def test_runtime_settings_workspace_default_is_tested_then_persisted_without_restart(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        """default:
  provider: litellm
  model_id: deepseek-v4-flash-0731
  reasoning_effort: high
  context_window_tokens: 500000
  supports_image_input: false
  supports_audio_input: false
  supports_file_input: false
agent_types:
  TeamArchitect:
    provider: ""
    model_id: ""
""",
        encoding="utf-8",
    )
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "workspace_default.llm.provider", "value": "openai"},
                    {"path": "workspace_default.llm.model_id", "value": "gpt-5.6-luna"},
                    {"path": "workspace_default.llm.reasoning_effort", "value": "high"},
                    {
                        "path": "workspace_default.llm.context_window_tokens",
                        "value": 500000,
                    },
                    {
                        "path": "workspace_default.llm.supports_image_input",
                        "value": True,
                    },
                    {
                        "path": "workspace_default.llm.supports_audio_input",
                        "value": False,
                    },
                    {
                        "path": "workspace_default.llm.supports_file_input",
                        "value": True,
                    },
                ],
                reason="Change the tested workspace-wide model default.",
                target_scope="team",
            )
        )
        result = json.loads(
            _apply_settings_change(
                plan["change_id"],
                confirmed=True,
            )
        )
    finally:
        _agent_ctx.set(None)

    assert [change["actual_path"] for change in plan["changes"]] == [
        "default.provider",
        "default.model_id",
        "default.reasoning_effort",
        "default.context_window_tokens",
        "default.supports_image_input",
        "default.supports_audio_input",
        "default.supports_file_input",
    ]
    assert all(change["target_scope"] == "team" for change in plan["changes"])
    assert all(change["target_agent_type"] is None for change in plan["changes"])
    assert result["status"] == "applied"
    # Every agent reads LLM_OVERRIDES.yaml again at its next turn.
    assert result["requires_restart"] is False
    assert result["restart_scope"] == RESTART_SCOPE_NONE
    assert result["restart_next_step"] is None
    assert result["running_agents"].startswith(
        "Running agents that use the team default switch at their next turn; no restart is needed."
    )
    assert result["live_applied"] == []
    assert agent.model_provider == "litellm"
    assert agent.model_id == "old/model", "a default-only change reaches the applying agent at its next turn"
    assert result["preflight"]["status"] == "passed"
    assert result["preflight"]["tested_before_write"] is True
    assert result["preflight"]["target"] == "default"
    assert result["persistence_verification"] == {
        "status": "passed",
        "validated_after_write": True,
        "verified_entries": ["default"],
    }
    tested_profile = {
        "provider": "openai",
        "model_id": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "reasoning_summary": None,
        "litellm_api_mode": "chat_completions",
        "context_window_tokens": 500000,
        "supports_image_input": True,
        "supports_audio_input": False,
        "supports_file_input": True,
    }
    # Tried once while staging (so the person confirms a model that answered)
    # and again right before the write.
    assert plan["model_test"]["status"] == "passed"
    assert plan["model_test"]["target"] == "default"
    assert "answered a test call" in plan["instructions"]
    assert agent.probed_profiles == [tested_profile, tested_profile]
    assert agent.applied_overrides == []
    persisted = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    assert persisted["default"] == agent.probed_profiles[0]
    assert persisted["agent_types"][TEAM_ARCHITECT_AGENT_TYPE] == {
        "provider": "",
        "model_id": "",
    }
    from assistant_gateway.runtime_config import (
        build_runtime_config_from_instance_config,
    )
    from config_loader import resolve_agent_type_settings

    fresh_runtime_config = build_runtime_config_from_instance_config(
        GatewayInstanceConfig.load(config_path)
    )
    for agent_type in (TEAM_ARCHITECT_AGENT_TYPE, "LearningCoachingAgent"):
        effective = resolve_agent_type_settings(fresh_runtime_config, agent_type)
        assert effective["model_provider"] == "openai"
        assert effective["model_id"] == "gpt-5.6-luna"
        assert effective["reasoning_effort"] == "high"
        assert effective["context_window_tokens"] == 500000
        assert effective["supports_image_input"] is True
        assert effective["supports_audio_input"] is False
        assert effective["supports_file_input"] is True


def test_runtime_settings_atomic_default_and_current_change_uses_staged_default(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        """default:
  provider: litellm
  model_id: old/model
agent_types:
  TeamArchitect:
    provider: ""
    model_id: ""
""",
        encoding="utf-8",
    )
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "workspace_default.llm.provider", "value": "openai"},
                    {
                        "path": "workspace_default.llm.model_id",
                        "value": "gpt-5.6-luna",
                    },
                    {"path": "current_agent.llm.reasoning_effort", "value": "high"},
                ],
                reason="Apply the new default now to the current lead, too.",
            )
        )
        result = json.loads(
            _apply_settings_change(
                plan["change_id"],
                confirmed=True,
            )
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "applied"
    assert result["requires_restart"] is False
    assert result["live_applied"] == ["current_agent.llm.reasoning_effort"]
    persisted = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    assert persisted["default"]["provider"] == "openai"
    assert persisted["default"]["model_id"] == "gpt-5.6-luna"
    current_entry = persisted["agent_types"][TEAM_ARCHITECT_AGENT_TYPE]
    assert current_entry["provider"] == "openai"
    assert current_entry["model_id"] == "gpt-5.6-luna"
    assert current_entry["reasoning_effort"] == "high"
    assert agent.model_provider == "openai"
    assert agent.model_id == "gpt-5.6-luna"


def test_runtime_settings_change_applies_complete_model_profile_live(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "current_agent.llm.reasoning_effort", "value": "high"},
                    {"path": "current_agent.llm.reasoning_summary", "value": "auto"},
                    {
                        "path": "current_agent.llm.litellm_api_mode",
                        "value": "responses",
                    },
                    {
                        "path": "current_agent.llm.context_window_tokens",
                        "value": 131072,
                    },
                    {"path": "current_agent.llm.supports_image_input", "value": True},
                    {"path": "current_agent.llm.supports_audio_input", "value": False},
                    {"path": "current_agent.llm.supports_file_input", "value": True},
                ],
                reason="Configure the selected model profile.",
            )
        )
        result = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "applied"
    assert result["requires_restart"] is False
    assert result["preflight"]["ready"] is True
    assert agent.reasoning_effort == "high"
    assert agent.reasoning_summary == "auto"
    assert agent.litellm_api_mode == "responses"
    assert agent.context_window_tokens == 131072
    assert agent.supports_image_input is True
    assert agent.supports_audio_input is False
    assert agent.supports_file_input is True
    persisted = yaml.safe_load(Path(result["saved_path"]).read_text(encoding="utf-8"))
    assert persisted["agent_types"][TEAM_ARCHITECT_AGENT_TYPE] == {
        "provider": "litellm",
        "model_id": "old/model",
        "reasoning_effort": "high",
        "reasoning_summary": "auto",
        "litellm_api_mode": "responses",
        "context_window_tokens": 131072,
        "supports_image_input": True,
        "supports_audio_input": False,
        "supports_file_input": True,
    }


def test_runtime_model_probe_failure_preserves_exact_override_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"# operator formatting must survive\ndefault: {provider: litellm, model_id: old/model}\n"
    override_path.write_bytes(original)
    agent = _fake_agent(config_path)
    # The probe used the endpoint's key; any key this process holds is cleaned from error text.
    keys = env_model_keys("secret-provider-token")
    keys.resolve(openai_compatible_endpoint("https://openrouter.ai/api/v1"))
    monkeypatch.setattr(runtime_settings_llm, "process_model_keys", lambda: keys)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="current_agent.llm.model_id",
                value="openrouter/unavailable",
            )
        )
        # The model answered while staging and stopped answering before apply.
        agent.probe_error = RuntimeError(
            "provider rejected secret-provider-token for the requested model"
        )
        result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert result["phase"] == "preflight"
    assert result["active_selection_unchanged"] is True
    assert result["persisted_selection_unchanged"] is True
    assert "secret-provider-token" not in result["error"]
    assert override_path.read_bytes() == original
    assert agent.model_id == "old/model"


def test_runtime_model_switch_rejects_concurrent_override_edit(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        "default: {provider: litellm, model_id: old/model}\n",
        encoding="utf-8",
    )
    concurrent_content = (
        "# concurrent operator edit\ndefault: {provider: openai, model_id: gpt-test}\n"
    )
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="current_agent.llm.model_id",
                value="openrouter/new-model",
            )
        )
        # The operator edits the file while apply's own model test runs.
        agent.probe_callback = lambda: override_path.write_text(
            concurrent_content,
            encoding="utf-8",
        )
        result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert result["phase"] == "persistence_commit"
    assert result["active_selection_unchanged"] is True
    assert result["persistence_write_applied"] is False
    assert "changed after the runtime settings plan was prepared" in result["error"]
    assert override_path.read_text(encoding="utf-8") == concurrent_content
    assert agent.applied_overrides == []


def test_runtime_settings_other_agent_plan_uses_target_effective_llm_default(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(
        "default:\n  provider: litellm\n  model_id: shared/default-model\n",
        encoding="utf-8",
    )
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="current_agent.llm.model_id",
                value="specialist/new-model",
                target_scope="agent_type",
                target_agent_type="LearningCoachingAgent",
            )
        )
        result = json.loads(
            _apply_settings_change(
                plan["change_id"],
                confirmed=True,
            )
        )
    finally:
        _agent_ctx.set(None)

    assert plan["changes"][0]["previous_value"] == "shared/default-model"
    assert plan["changes"][0]["previous_value"] != "old/model"
    assert plan["changes"][0]["actual_path"] == (
        "agent_types.LearningCoachingAgent.model_id"
    )
    # LearningCoachingAgent reads LLM_OVERRIDES.yaml again at its next turn.
    assert plan["changes"][0]["restart_scope"] == RESTART_SCOPE_NONE
    assert plan["requires_restart"] is False
    assert result["requires_restart"] is False
    assert result["restart_scope"] == RESTART_SCOPE_NONE
    assert result["running_agents"].startswith(
        "Running LearningCoachingAgent agents switch at their next turn; no restart is needed."
    )
    assert result["live_applied"] == []
    assert result["preflight"]["target"] == "LearningCoachingAgent"
    assert agent.applied_overrides == []
    persisted = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    # The API mode is not written, so the target keeps its configured route.
    assert persisted["agent_types"]["LearningCoachingAgent"] == {
        "model_id": "specialist/new-model",
        "reasoning_effort": None,
        "reasoning_summary": None,
        "context_window_tokens": None,
        "supports_image_input": None,
        "supports_audio_input": None,
        "supports_file_input": None,
    }


def test_validate_llm_overrides_yaml_rejects_invalid_capability_value(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    set_agent_ref(_fake_agent(config_path))
    try:
        result = json.loads(
            validate_llm_overrides_yaml._tool_func(
                content="""
agent_types:
  TeamArchitect:
    supports_image_input: probably
"""
            )
        )
    finally:
        _agent_ctx.set(None)

    assert result["valid"] is False
    assert (
        "agent_types.TeamArchitect.supports_image_input must be true, false, or null."
        in result["errors"]
    )


def test_validate_llm_overrides_yaml_reads_drafts_only_from_task_folders(
    tmp_path: Path,
) -> None:
    """Without a path the current control file is validated; a draft path must
    be in the task's folders, so it can never name the control file itself."""

    config_path = _write_config(tmp_path)
    layout = _layout(config_path)
    layout.ensure_directories()
    layout.llm_overrides_path.write_text(
        "agent_types:\n  TeamArchitect:\n    supports_image_input: probably\n",
        encoding="utf-8",
    )
    # The task works for user_1 (its signed context), so a relative path is in
    # that person's folder.
    layout.person_dir("user_1").mkdir(parents=True, exist_ok=True)
    draft = layout.person_dir("user_1") / "draft_overrides.yaml"
    draft.write_text(
        "default:\n  provider: litellm\n  model_id: openrouter/draft-model\n",
        encoding="utf-8",
    )
    set_agent_ref(_fake_agent(config_path))
    try:
        current = json.loads(validate_llm_overrides_yaml._tool_func())
        from_draft = json.loads(
            validate_llm_overrides_yaml._tool_func(path="draft_overrides.yaml")
        )
        control = json.loads(
            validate_llm_overrides_yaml._tool_func(path=str(layout.llm_overrides_path))
        )
        outside = json.loads(
            validate_llm_overrides_yaml._tool_func(path=str(tmp_path / "elsewhere.yaml"))
        )
    finally:
        _agent_ctx.set(None)

    assert current["valid"] is False
    assert current["source"] == "current_file"
    assert current["target_path"] == str(layout.llm_overrides_path)
    assert from_draft["valid"] is True
    assert from_draft["target_path"] == str(draft)
    for refused in (control, outside):
        assert refused["valid"] is False
        assert "LLM override draft must be in this task's folders" in refused["error"]
    assert "Team control files cannot be opened through generic tools" in control["error"]


def test_runtime_setting_change_rejects_invalid_existing_llm_override_file(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    original = """
model_capabilities:
  litellm:
    old/model:
      supports_image_input: maybe
"""
    override_path.write_text(original, encoding="utf-8")
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="current_agent.llm.supports_image_input",
                value=True,
                reason="User confirmed active model supports image input.",
            )
        )
    finally:
        _agent_ctx.set(None)

    # Staging builds the file it would write to try the model, so a broken
    # file is refused before anyone is asked to confirm.
    assert "change_id" not in plan
    assert "LLM override file validation failed before write" in plan["error"]
    assert (
        "model_capabilities.litellm.old/model.supports_image_input must be true, false, or null."
        in plan["error"]
    )
    assert override_path.read_text(encoding="utf-8") == original


def test_staging_a_model_that_does_not_answer_stages_nothing(tmp_path: Path) -> None:
    """Owner's decision 2026-10-10: try the model once, and ask only when it answered."""

    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    original = b"default: {provider: litellm, model_id: old/model}\n"
    override_path.write_bytes(original)
    agent = _fake_agent(config_path)
    agent.probe_error = RuntimeError("model anthropic/claude-haiku-5-5 not found on this endpoint")
    plan_dir = config_path.parent / "runtime" / "settings_change_plans"
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "current_agent.llm.model_id", "value": "claude-haiku-5-5"},
                    {"path": "current_agent.llm.reasoning_effort", "value": "high"},
                ],
                reason="The person asked for Haiku with high effort.",
            )
        )
    finally:
        _agent_ctx.set(None)

    assert "change_id" not in plan
    assert "did not answer its test call, so nothing was staged" in plan["error"]
    assert "not found on this endpoint" in plan["error"]
    assert agent.probed_profiles[0]["model_id"] == "claude-haiku-5-5"
    assert agent.probed_profiles[0]["reasoning_effort"] == "high"
    assert not plan_dir.exists() or not any(plan_dir.iterdir())
    assert override_path.read_bytes() == original
    assert agent.model_id == "old/model"


def test_staging_a_model_that_answers_asks_the_person_to_confirm(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "current_agent.llm.model_id", "value": "anthropic/claude-haiku-5-5"},
                    {"path": "current_agent.llm.reasoning_effort", "value": "high"},
                ],
                reason="The person asked for Haiku with high effort.",
            )
        )
    finally:
        _agent_ctx.set(None)

    assert plan["model_test"]["status"] == "passed"
    assert plan["model_test"]["model_id"] == "anthropic/claude-haiku-5-5"
    assert "answered a test call" in plan["instructions"]
    assert "ask them to confirm" in plan["instructions"]
    assert agent.model_id == "old/model"  # nothing applied yet
    plan_dir = config_path.parent / "runtime" / "settings_change_plans"
    assert [path.stem for path in plan_dir.iterdir()] == [plan["change_id"]]


def test_a_tested_switch_requires_confirmation_with_authorization_off(tmp_path: Path) -> None:
    agent = _fake_agent(_write_config(tmp_path))
    set_agent_ref(agent)
    try:
        plan = json.loads(plan_runtime_setting_change._tool_func(
            path="current_agent.llm.model_id", value="candidate/model"
        ))
        refused = json.loads(_apply_settings_change(plan["change_id"]))
        assert refused["reason_code"] == "agreement_required"
        assert agent.model_id == "old/model"
        assert not _overrides_path(Path(agent.instance_config_path)).exists()
        applied = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert plan["agreement_required"] is True
    assert applied["preflight"]["status"] == "passed"
    assert agent.model_id == "candidate/model"
    assert len(agent.probed_profiles) == 2


@pytest.mark.parametrize("path,value", [
    ("llm.model_id", "does-not-exist/model"),
    ("llm.provider", "openai"),
    ("llm.reasoning_effort", "high"),
])
def test_failed_baseline_model_test_stages_nothing(tmp_path: Path, path: str, value: str) -> None:
    config_path = _write_config(tmp_path)
    original = config_path.read_bytes()
    agent = _fake_agent(config_path)
    agent.probe_error = RuntimeError("the baseline model did not answer")
    set_agent_ref(agent)
    try:
        refused = json.loads(plan_runtime_setting_change._tool_func(path=path, value=value, target_scope="team"))
    finally:
        _agent_ctx.set(None)

    assert "change_id" not in refused
    assert "the baseline model did not answer" in refused["error"]
    assert len(agent.probed_profiles) == 1
    assert config_path.read_bytes() == original
    assert agent.model_id == "old/model"
    assert not list((config_path.parent / "runtime" / "settings_change_plans").glob("*.json"))


def test_baseline_switch_tests_candidate_settings_at_staging_and_apply(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    config.llm.litellm_api_base = "https://baseline.example.test/v1"
    config.save(config_path)
    agent = _fake_agent(config_path)
    agent.litellm_api_base = "https://lead.example.test/v1"
    set_agent_ref(agent)
    try:
        plan = json.loads(plan_runtime_settings_change._tool_func([
            {"path": "llm.model_id", "value": "candidate/baseline", "target_scope": "team"},
            {"path": "llm.temperature", "value": 0.7, "target_scope": "team"},
            {"path": "llm.max_output_tokens", "value": 256, "target_scope": "team"},
        ]))
        applied = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert plan["model_test"]["status"] == applied["preflight"]["status"] == "passed"
    assert len(agent.probed_profiles) == 2
    assert all(profile["model_id"] == "candidate/baseline" for profile in agent.probed_profiles)
    assert agent.probed_request_settings[0] == agent.probed_request_settings[1]
    tested_settings = agent.probed_request_settings[0]
    assert tested_settings["litellm_api_base"] == "https://baseline.example.test/v1"
    assert tested_settings["temperature"] == 0.7
    assert tested_settings["max_output_tokens"] == 256
    saved = GatewayInstanceConfig.load(config_path)
    assert saved.llm.model_id == "candidate/baseline"
    assert saved.llm.temperature == 0.7
    assert saved.llm.max_output_tokens == 256
    assert agent.model_id == "old/model"
    assert plan["requires_restart"] is True


def test_baseline_probe_respects_the_same_model_constraints_as_startup(tmp_path: Path) -> None:
    from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
    from config_loader import resolve_agent_type_settings

    config_path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    config.llm.provider = "openai"
    config.llm.reasoning_summary = "auto"
    config.llm.supports_image_input = None
    config.llm.model_capabilities = {
        "openai": {"candidate/baseline": {"reasoning_summary": None, "supports_image_input": True}}
    }
    config.save(config_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(plan_runtime_setting_change._tool_func(
            path="llm.model_id", value="candidate/baseline", target_scope="team"
        ))
        applied = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert applied["status"] == "applied"
    fresh_config = build_runtime_config_from_instance_config(GatewayInstanceConfig.load(config_path))
    settings = resolve_agent_type_settings(fresh_config)
    assert settings["reasoning_summary"] is None
    assert settings["supports_image_input"] is True
    assert len(agent.probed_profiles) == 2
    for profile in agent.probed_profiles:
        assert profile["reasoning_summary"] == settings["reasoning_summary"]
        assert profile["supports_image_input"] == settings["supports_image_input"]


def test_baseline_apply_failure_preserves_the_source(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    original = config_path.read_bytes()
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(plan_runtime_setting_change._tool_func(
            path="llm.model_id", value="candidate/baseline", target_scope="team"
        ))
        agent.probe_error = RuntimeError("model became unavailable")
        failed = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert failed["phase"] == "preflight"
    assert "model became unavailable" in failed["error"]
    assert len(agent.probed_profiles) == 2
    assert config_path.read_bytes() == original
    assert not list(config_path.parent.glob("instance_config.yaml.bak.*"))


def test_baseline_apply_refuses_an_edit_during_its_model_test(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    concurrent = config_path.read_bytes() + b"\n# concurrent operator edit\n"
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(plan_runtime_setting_change._tool_func(
            path="llm.model_id", value="candidate/baseline", target_scope="team"
        ))
        agent.probe_callback = lambda: config_path.write_bytes(concurrent)
        refused = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert "source changed after planning" in refused["error"]
    assert config_path.read_bytes() == concurrent
    assert not list(config_path.parent.glob("instance_config.yaml.bak.*"))


def test_staging_a_setting_without_a_model_call_skips_the_test(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="current_agent.llm.supports_image_input",
                value=True,
            )
        )
    finally:
        _agent_ctx.set(None)

    assert plan["model_test"]["status"] == "skipped"
    assert agent.probed_profiles == []
    assert "answered a test call" not in plan["instructions"]


class _PausingToolCall:
    """A Strands tool context whose interrupt pauses until a response is set."""

    def __init__(self) -> None:
        self.tool_use = {"toolUseId": "tooluse_person_approval", "name": "apply_change"}
        self.response = None
        self.reasons: list[dict] = []

    def interrupt(self, name, reason=None):
        if self.response is not None:
            return self.response
        self.reasons.append(reason)
        raise InterruptException(Interrupt(id=f"interrupt-{name}", name=name, reason=reason))


def _authorization_on(agent) -> None:
    agent.authorization_config = {"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)}
    access_runtime._runtime_access.cache_clear()


@pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_runtime_settings_with_authorization_on_wait_for_a_person_then_apply_once(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    _authorization_on(agent)
    set_agent_ref(agent)
    call = _PausingToolCall()
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "agent.debug.context_dump.enabled", "value": True},
                    {"path": "agent.debug.context_dump.mode", "value": "always"},
                ],
                reason="Temporarily capture debug context dumps.",
            )
        )
        with pytest.raises(InterruptException):
            asyncio.run(apply_change._tool_func(plan["change_id"], "Capture dumps", call, confirmed=True))
        requests = access_runtime.runtime_requests(agent)
        request = requests.service.store.get_request(call.reasons[0]["access_request_id"])
        requests.decide(request.request_id, PersonPrincipal("user_1", "test_team", ("owner",)), AccessReviewChoice.APPROVE)
        call.response = {"kind": "access_request_decision", "status": "granted"}
        applied = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Capture dumps", call)))
        again = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Capture dumps", call)))
    finally:
        _agent_ctx.set(None)

    assert plan["decision_class"] == "user_authorized"
    assert plan["authority_mode"] == "person_approval"
    assert request.kind.value == "change" and request.change.owner_person_id == "user_1"
    assert any("agent.debug.context_dump.mode" in line and "'always'" in line for line in request.change.effect)
    # The chat headline is written from the plan's values; the effect lines keep the full detail.
    assert request.change.summary == "Turn context dumps on; set the context dump mode to always. Takes effect now."
    assert applied["status"] == "applied", applied
    assert applied["approval"] == {
        "mode": "person_approval",
        "approved_by": "user_1",
        "approved_at_utc": applied["approval"]["approved_at_utc"],
        "access_request_id": request.request_id,
    }
    assert again["status"] == "refused" and "already applied" in again["reason"]
    saved = GatewayInstanceConfig.load(config_path)
    assert saved.agent.debug["context_dump"]["mode"] == "always"


@pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_runtime_settings_denied_change_is_not_applied(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, heartbeat_enabled=False)
    agent = _fake_agent(config_path)
    _authorization_on(agent)
    set_agent_ref(agent)
    call = _PausingToolCall()
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(path="heartbeat.enabled", value=True, reason="Enable heartbeat.")
        )
        with pytest.raises(InterruptException):
            asyncio.run(apply_change._tool_func(plan["change_id"], "Enable heartbeat", call))
        requests = access_runtime.runtime_requests(agent)
        request_id = call.reasons[0]["access_request_id"]
        with pytest.raises(HandDownRefused, match="approved as it is or denied"):
            requests.decide(request_id, PersonPrincipal("user_1", "test_team", ("owner",)), AccessReviewChoice.APPROVE_READ)
        requests.decide(request_id, PersonPrincipal("user_1", "test_team", ("owner",)), AccessReviewChoice.DENY)
        call.response = {"kind": "access_request_decision", "status": "denied"}
        result = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "Enable heartbeat", call)))
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "denied"
    assert GatewayInstanceConfig.load(config_path).heartbeat.enabled is False


def test_runtime_settings_user_authorized_change_uses_agreement_when_authorization_disabled(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "agent.debug.context_dump.enabled", "value": True},
                    {"path": "agent.debug.context_dump.mode", "value": "on_error"},
                ],
                reason="Capture diagnostic context only after errors.",
            )
        )
        rejected = json.loads(_apply_settings_change(plan["change_id"]))
        applied = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert plan["decision_class"] == "user_authorized"
    assert plan["authority_mode"] == "authorization_disabled_agreement"
    assert "confirmed=true" in plan["instructions"]
    assert rejected["reason_code"] == "agreement_required"
    assert applied["status"] == "applied"
    assert applied["approval"]["mode"] == "agreement"
    saved = GatewayInstanceConfig.load(config_path)
    assert saved.agent.debug["context_dump"]["enabled"] is True
    assert saved.agent.debug["context_dump"]["mode"] == "on_error"


@pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_runtime_settings_rejects_authorization_mode_change_after_staging(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="delivery.default_target.chat_id",
                value="new-recipient",
                reason="Mode-binding regression test.",
            )
        )
        _authorization_on(agent)
        result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        _agent_ctx.set(None)

    assert "turned on or off after planning" in result["error"]
    assert (
        GatewayInstanceConfig.load(config_path).delivery.default_target.chat_id
        != "new-recipient"
    )


def test_runtime_settings_heartbeat_enable_requires_separate_restart_authorization(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path, heartbeat_enabled=False)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [{"path": "heartbeat.enabled", "value": True}],
                reason="Enable heartbeat before creating heartbeat items.",
            )
        )
        rejected = json.loads(
            _apply_settings_change(plan["change_id"])
        )
        applied = json.loads(
            _apply_settings_change(
                plan["change_id"],
                confirmed=True,
            )
        )
    finally:
        _agent_ctx.set(None)

    assert plan["agreement_required"] is True
    assert plan["restart_scope"] == RESTART_SCOPE_ALL
    assert rejected["reason_code"] == "agreement_required"
    assert "agreement in the conversation" in rejected["error"]
    assert applied["status"] == "applied"
    assert "plan_runtime_restart" in applied["restart_next_step"]
    assert not list(
        (config_path.parent / "runtime" / "restart_requests").glob("*.json")
    )

    saved = GatewayInstanceConfig.load(config_path)
    assert saved.heartbeat.enabled is True


def test_runtime_settings_reject_operator_owned_setting(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    set_agent_ref(_fake_agent(config_path))
    try:
        result = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {
                        "path": "tool_policy.scratch_retention_days",
                        "value": 0,
                    }
                ],
                reason="Try to keep every scratch file forever.",
            )
        )
    finally:
        _agent_ctx.set(None)

    # What maintenance deletes from people's folders is the operator's choice.
    assert "operator-only" in result["error"]
    assert "how long scratch files are kept" in result["error"]


def test_runtime_settings_reject_missing_change_path_with_clear_error(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    set_agent_ref(_fake_agent(config_path))
    try:
        result = json.loads(
            plan_runtime_settings_change._tool_func(
                [{"value": True}],
                reason="Malformed model-authored runtime setting change.",
            )
        )
    finally:
        _agent_ctx.set(None)

    assert (
        result["error"]
        == "Each runtime setting change must include a non-empty 'path' field."
    )


def test_runtime_settings_plan_is_scoped_hashed_expiring_and_source_bound(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path, heartbeat_enabled=False)
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="heartbeat.enabled",
                value=True,
                reason="Enable proactive heartbeat review.",
                target_scope="team",
            )
        )
    finally:
        _agent_ctx.set(None)

    assert plan["schema_version"] == 5
    assert plan["decision_class"] == "lead_managed"
    assert plan["authority_mode"] == "authorization_disabled_agreement"
    assert plan["agreement_required"] is True
    assert len(plan["plan_digest"]) == 64
    assert plan["actor_principal_id"] == "agent_1"
    assert plan["actor_named_agent_type"] == TEAM_ARCHITECT_AGENT_TYPE
    assert plan["team_id"] == "test_team"
    assert plan["changes"][0]["previous_value"] is False
    assert plan["changes"][0]["value"] is True
    assert plan["changes"][0]["target_scope"] == "team"
    assert len(plan["source_snapshots"]) == 1
    assert Path(plan["plan_path"]).is_file()


def test_runtime_settings_stale_source_is_rejected_before_write(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, heartbeat_enabled=False)
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="heartbeat.enabled",
                value=True,
                reason="Stale-plan test.",
            )
        )
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + "\n# independent operator edit\n",
            encoding="utf-8",
        )
        result = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert "source changed after planning" in result["error"]
    assert GatewayInstanceConfig.load(config_path).heartbeat.enabled is False


def test_runtime_settings_tampered_plan_is_rejected(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, heartbeat_enabled=False)
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="heartbeat.enabled",
                value=True,
                reason="Tamper test.",
            )
        )
        plan_path = Path(plan["plan_path"])
        plan_path.chmod(0o644)
        tampered = json.loads(plan_path.read_text(encoding="utf-8"))
        tampered["changes"][0]["value"] = False
        plan_path.write_text(json.dumps(tampered), encoding="utf-8")
        result = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert "digest mismatch" in result["error"]


def test_runtime_settings_receipt_prevents_replay(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, heartbeat_enabled=False)
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="heartbeat.enabled",
                value=True,
                reason="Replay test.",
            )
        )
        applied = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
        replay = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert applied["status"] == "applied"
    assert Path(applied["receipt_path"]).is_file()
    assert applied["undo"]["kind"] == "restore_backup"
    assert replay["reason_code"] == "refused" and "already applied" in replay["error"]


def test_runtime_settings_can_update_team_lead_soul_through_staged_boundary(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    soul_path = _layout(config_path).soul_path
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("old soul\n", encoding="utf-8")
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="team_lead.soul",
                value="new bounded soul\n",
                reason="Adjust TeamArchitect response behavior.",
                target_scope="self",
            )
        )
        applied = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert applied["status"] == "applied"
    assert applied["persistence_target"] == "team_lead_profile"
    assert applied["approval"]["mode"] == "agreement"
    assert soul_path.read_text(encoding="utf-8") == "new bounded soul\n"


@pytest.mark.parametrize(
    ("instance_root", "expected_error"),
    [
        ("other-instance", "TeamArchitect instance_root does not match the canonical instance"),
        ("", "TeamArchitect instance_root is not configured"),
    ],
)
def test_runtime_settings_reject_team_lead_soul_workspace_mismatch(
    tmp_path: Path,
    instance_root: str,
    expected_error: str,
) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    agent.instance_root = str(tmp_path / instance_root) if instance_root else ""
    set_agent_ref(agent)
    try:
        result = json.loads(
            plan_runtime_setting_change._tool_func(
                path="team_lead.soul",
                value="new soul\n",
                target_scope="self",
            )
        )
    finally:
        _agent_ctx.set(None)

    assert expected_error in result["error"]
    assert "change_id" not in result
    assert not _layout(config_path).soul_path.exists()


def test_runtime_settings_can_update_generated_specialist_behavior_only(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    bundle_path = _write_generated_specialist(
        config_path,
        description_override="Effective override description.",
    )
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(
                path="specialist.description",
                value="Reviews implementation plans for gaps and operational risk.",
                reason="Clarify the generated specialist's remit.",
                target_scope="agent_type",
                target_agent_type="PlanningCriticAgent",
            )
        )
        applied = json.loads(
            _apply_settings_change(plan["change_id"], confirmed=True)
        )
    finally:
        _agent_ctx.set(None)

    assert plan["changes"][0]["previous_value"] == "Effective override description."
    assert applied["status"] == "applied"
    assert applied["persistence_target"] == "generated_agent_definition"
    payload = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    params = payload["agent_types"]["PlanningCriticAgent"]["params"]
    overrides = payload["agent_types"]["PlanningCriticAgent"]["overrides"]
    assert params["description"] == "Old description."
    assert (
        overrides["description"]
        == "Reviews implementation plans for gaps and operational risk."
    )
    assert params["display_name"] == "Planning Critic Agent"
    assert overrides["default_tools"] == ["fn:publishing"]


def test_runtime_settings_reject_cross_source_atomic_plan(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    set_agent_ref(_fake_agent(config_path))
    try:
        result = json.loads(
            plan_runtime_settings_change._tool_func(
                [
                    {"path": "current_agent.llm.model_id", "value": "new/model"},
                    {"path": "heartbeat.enabled", "value": True},
                ],
                reason="Invalid cross-source plan.",
            )
        )
    finally:
        _agent_ctx.set(None)

    assert "one persisted source" in result["error"]


def test_runtime_restart_with_authorization_off_needs_the_persons_agreement(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    set_agent_ref(_fake_agent(config_path))
    try:
        plan = json.loads(
            plan_runtime_restart._tool_func(scope=RESTART_SCOPE_MANAGER, reason="apply the heartbeat settings")
        )
        unconfirmed = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "reload", _ToolCall())))
        applied = json.loads(asyncio.run(apply_change._tool_func(plan["change_id"], "reload", _ToolCall(), confirmed=True)))
    finally:
        _agent_ctx.set(None)

    assert plan["authority_mode"] == "authorization_disabled_agreement"
    assert unconfirmed["status"] == "agreement_required"
    assert applied["status"] == "applied" and applied["approval"]["mode"] == "agreement"
    [request_path] = (config_path.parent / "runtime" / "restart_requests").glob("*.json")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["authority"]["mode"] == "authorization_disabled_agreement"
    assert request["authority"]["approval"]["approved_by"] == "agreement"


def test_runtime_settings_tools_are_manager_only() -> None:
    from custom_agents.strands_team import AGENT_TYPE_MAP

    manager_tools = AGENT_TYPE_MAP[TEAM_ARCHITECT_AGENT_TYPE]["tools"]
    manager_tool_names = {
        getattr(tool, "__name__", getattr(tool, "name", str(tool)))
        for tool in manager_tools
    }
    assert list_runtime_settings_options in manager_tools
    assert validate_llm_overrides_yaml in manager_tools
    assert plan_runtime_setting_change in manager_tools
    assert plan_runtime_settings_change in manager_tools
    assert apply_change in manager_tools
    assert plan_runtime_restart in manager_tools
    assert get_runtime_restart_status in manager_tools
    assert "request_runtime_restart" not in manager_tool_names

    # Staging and applying settings stay with the team lead. Reading the
    # options and validating override YAML are read-only tools of every
    # agent's own runtime set (catalog fn:runtime_status).
    for agent_type, config in AGENT_TYPE_MAP.items():
        if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            continue
        tools = config.get("tools", [])
        assert plan_runtime_setting_change not in tools
        assert plan_runtime_settings_change not in tools
        assert apply_change not in tools


def test_a_switch_without_a_context_window_says_which_window_will_apply(tmp_path: Path) -> None:
    """The reported switch reset Haiku's window to the 128K fallback without a word."""

    agent = _fake_agent(_write_config(tmp_path))
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_setting_change._tool_func(path="current_agent.llm.model_id", value="claude-haiku-5-5")
        )
    finally:
        _agent_ctx.set(None)

    notice = plan["context_window_notice"]
    assert "assume 128,000 tokens for claude-haiku-5-5" in notice
    assert "current_agent.llm.context_window_tokens" in notice
    assert plan["instructions"].startswith(notice)


@pytest.mark.parametrize(
    "extra, model_id",
    [
        ([{"path": "current_agent.llm.context_window_tokens", "value": 1_000_000}], "claude-haiku-5-5"),
        ([], "gpt-4o"),  # a window the runtime knows by name
    ],
    ids=["window_staged", "known_model"],
)
def test_a_switch_with_a_known_window_has_no_notice(tmp_path: Path, extra: list, model_id: str) -> None:
    agent = _fake_agent(_write_config(tmp_path))
    set_agent_ref(agent)
    try:
        plan = json.loads(
            plan_runtime_settings_change._tool_func(
                [{"path": "current_agent.llm.model_id", "value": model_id}, *extra]
            )
        )
    finally:
        _agent_ctx.set(None)

    assert "change_id" in plan
    assert "context_window_notice" not in plan


@pytest.mark.parametrize("target", ["agent_type", "team"])
def test_window_notice_uses_the_target_model_instead_of_the_leads(tmp_path: Path, target: str) -> None:
    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    override_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"provider": "litellm", "model_id": "unknown/target-model", "context_window_tokens": 1_000_000}
    payload = {"agent_types": {"WebResearchAgent": entry}} if target == "agent_type" else {"default": entry}
    override_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    agent = _fake_agent(config_path)
    agent.model_id = "gpt-4o"
    change = (
        {"path": "current_agent.llm.provider", "value": "openai", "target_scope": "agent_type", "target_agent_type": "WebResearchAgent"}
        if target == "agent_type"
        else {"path": "workspace_default.llm.provider", "value": "openai", "target_scope": "team"}
    )
    set_agent_ref(agent)
    try:
        plan = json.loads(plan_runtime_settings_change._tool_func([change]))
    finally:
        _agent_ctx.set(None)

    assert "assume 128,000 tokens for unknown/target-model" in plan["context_window_notice"]
    assert "gpt-4o" not in plan["context_window_notice"]
    assert agent.probed_profiles[0]["model_id"] == "unknown/target-model"
    assert agent.probed_profiles[0]["context_window_tokens"] is None


@pytest.mark.live
@pytest.mark.external
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_LITELLM_PROVIDER_SMOKE") != "1",
    reason="Set RUN_LIVE_LITELLM_PROVIDER_SMOKE=1 to call the configured LiteLLM endpoint.",
)
def test_live_staging_a_model_switch_tries_the_model_once(tmp_path: Path) -> None:
    """Owner's decision 2026-10-10: staging tries the model, and a model that fails stages nothing.

    Uses the live LiteLLM smoke settings (tests/test_live_litellm_provider_smoke.py):
    the endpoint and key from TEAM_NATS_LIVE_ENV, the model from the instance
    config or TEAM_NATS_PROVIDER_SMOKE_MODEL_ID.
    """

    from custom_agents.runtime_model_switch import probe_strands_model
    from tests.test_live_litellm_provider_smoke import _litellm_model, _runtime_settings

    settings = _runtime_settings()
    agent = _fake_agent(_write_config(tmp_path))

    def probe(profile) -> dict:
        candidate = {**settings, "model_id": profile.model_id, "reasoning_effort": profile.reasoning_effort}
        model = _litellm_model(stream=None, max_tokens=64, settings=candidate)
        return probe_strands_model(model, timeout_seconds=float(settings.get("request_timeout_seconds") or 120))

    agent.probe_runtime_llm_profile = probe
    set_agent_ref(agent)
    try:
        working = json.loads(
            plan_runtime_settings_change._tool_func([{"path": "current_agent.llm.model_id", "value": settings["model_id"]}])
        )
        missing = json.loads(
            plan_runtime_settings_change._tool_func(
                [{"path": "current_agent.llm.model_id", "value": "ua-live-no-such-model-0000"}]
            )
        )
    finally:
        _agent_ctx.set(None)

    assert working.get("model_test", {}).get("status") == "passed", working
    assert "answered a test call" in working["instructions"]
    assert "change_id" not in missing, missing
    assert "did not answer its test call, so nothing was staged" in missing["error"]


def test_another_agent_types_context_window_reaches_its_running_agent_on_the_next_turn(
    tmp_path: Path,
) -> None:
    """No restart: a running agent of the target type reads the change at its next turn.

    In production an approved 1,050,000-token window for four other types said
    "requires a manager restart", and TeamArchitect asked for one. Every agent
    reads LLM_OVERRIDES.yaml again at the start of each turn, so the restart
    was never needed.
    """

    from unittest.mock import MagicMock, patch

    from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
    from config_loader import get_config, set_config
    from custom_agents.runtime_configuration_context import build_runtime_configuration_context
    from custom_agents.strands_agent import UniversalAgent
    from tests.test_turn_action_outbox import _make_task_manager

    config_path = _write_config(tmp_path)
    instance_root = config_path.parent
    previous_config = get_config()
    with (
        patch("custom_agents.strands_agent.Agent"),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        researcher = UniversalAgent(
            role="Researcher",
            tools=[],
            team_id="test_team",
            named_agent_type="WebResearchAgent",
            task_manager=_make_task_manager(tmp_path / "researcher"),
            instance_root=str(instance_root),
        )
        window_before = researcher.context_window_tokens
        lead = _fake_agent(config_path)
        set_agent_ref(lead)
        try:
            plan = json.loads(
                plan_runtime_setting_change._tool_func(
                    path="current_agent.llm.context_window_tokens",
                    value=1_050_000,
                    target_scope="agent_type",
                    target_agent_type="WebResearchAgent",
                )
            )
            result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
        finally:
            _agent_ctx.set(None)
        researcher.refresh_runtime_llm_selection()  # the first step of its next turn

        set_config(build_runtime_config_from_instance_config(GatewayInstanceConfig.load(config_path)))
        try:
            context = build_runtime_configuration_context(
                lead,
                include_config_file_snapshot=False,
                agent_types=["WebResearchAgent", "NoSuchAgent"],
                known_agent_types={"WebResearchAgent", TEAM_ARCHITECT_AGENT_TYPE},
            )
        finally:
            set_config(previous_config)

    assert result["status"] == "applied", result
    assert result["requires_restart"] is False and result["restart_next_step"] is None
    assert "WebResearchAgent" in result["running_agents"]
    assert window_before != 1_050_000
    assert researcher.context_window_tokens == 1_050_000
    assert lead.context_window_tokens != 1_050_000, "the lead's own type did not change"
    assert context["agent_type_models"]["WebResearchAgent"]["context_window_tokens"] == 1_050_000
    assert "error" in context["agent_type_models"]["NoSuchAgent"]
    assert "next turn" in context["agent_type_models_note"]


@pytest.mark.parametrize("failover_enabled", [False, True])
@pytest.mark.parametrize(
    "target_scope,inherits_default",
    [("agent_type", False), ("team", True), ("agent_type", True)],
    ids=["agent_type", "workspace_default", "inherited_default"],
)
def test_clearing_a_saved_reasoning_summary_reaches_a_running_agent_without_restart(
    tmp_path: Path, failover_enabled: bool, target_scope: str, inherits_default: bool,
) -> None:
    from unittest.mock import MagicMock, patch

    from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
    from config_loader import resolve_agent_type_settings
    from custom_agents.strands_agent import UniversalAgent
    from tests.test_turn_action_outbox import _make_task_manager

    config_path = _write_config(tmp_path)
    override_path = _overrides_path(config_path)
    profile = {
        "provider": "litellm", "model_id": "primary/model",
        "reasoning_summary": "auto", "litellm_api_mode": "responses",
    }
    payload = (
        {"default": profile} if inherits_default
        else {"agent_types": {"WebResearchAgent": profile}}
    )
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with (
        patch("custom_agents.strands_agent.Agent"),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        researcher = UniversalAgent(
            role="Researcher", tools=[], team_id="test_team", named_agent_type="WebResearchAgent",
            task_manager=_make_task_manager(tmp_path / "researcher"),
            instance_root=str(config_path.parent), model_provider="litellm", model_id="primary/model",
            reasoning_summary="auto", litellm_api_mode="responses",
            llm_failover={"enabled": failover_enabled},
        )
        assert researcher.reasoning_summary == "auto"
        set_agent_ref(_fake_agent(config_path))
        try:
            plan = json.loads(plan_runtime_setting_change._tool_func(
                path=("workspace_default.llm.reasoning_summary" if target_scope == "team"
                      else "current_agent.llm.reasoning_summary"),
                value=None, target_scope=target_scope,
                target_agent_type="" if target_scope == "team" else "WebResearchAgent",
            ))
            result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
        finally:
            _agent_ctx.set(None)

        assert result["status"] == "applied", result
        assert result["requires_restart"] is False
        saved = yaml.safe_load(override_path.read_text(encoding="utf-8"))
        saved_profile = saved["default"] if target_scope == "team" else saved["agent_types"]["WebResearchAgent"]
        assert saved_profile["reasoning_summary"] is None
        if inherits_default and target_scope == "agent_type":
            # A clear for an inheriting type must stay sparse and override the
            # default, rather than pinning the type to the default's model.
            assert saved_profile == {"reasoning_summary": None}
            assert saved["default"]["reasoning_summary"] == "auto"
        # Startup and next-turn selection must read the same cleared profile.
        startup = resolve_agent_type_settings(
            build_runtime_config_from_instance_config(GatewayInstanceConfig.load(config_path)), "WebResearchAgent"
        )
        assert startup["reasoning_summary"] is None
        for _ in range(2):
            researcher.refresh_runtime_llm_selection()
            assert researcher.reasoning_summary is None
            assert researcher.model_id == "primary/model"
