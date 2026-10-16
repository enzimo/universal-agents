from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant_gateway.config import (
    GatewayInstanceConfig,
    GatewayUsersSettings,
    INSTANCE_CONFIG_SCHEMA_VERSION,
    dump_instance_config_yaml,
)
from strands_main import (
    _build_runtime_config_from_instance_config,
    _discover_instance_config_path,
)
from config_loader import resolve_agent_type_settings
from default_values import AUTHORIZATION_DEFAULTS, BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI
from runtime_settings.governance import REMOVED_TOOL_POLICY_KEYS
from runtime_settings.legacy_layout import present_removed_location_settings


def _make_config(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="manualtest",
        instance_root=str(tmp_path / "instance"),
        team_id="team_manual",
        timezone="America/Los_Angeles",
        manager_storage_sqlite_path="runtime/task_history.db",
    )


def _clear_runtime_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (
        "TASK_HISTORY_DB_PATH",
        "STORAGE_BACKEND",
        "SQLITE_DB_PATH",
        "POSTGRES_DSN",
        "POSTGRES_DSN_FILE",
        "LLM_STREAM",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "ACTIVITY_COLLECTOR_INGEST_TOKEN_ENV_VAR",
        "AGENT_DEBUG_TERMINAL_REASONING_ENABLED",
        "SECURE_TOOL_BROKER_PUBLIC_PORT",
    ):
        monkeypatch.delenv(env_name, raising=False)


@pytest.mark.parametrize(
    "external_mcp_servers",
    [
        pytest.param(None, id="null"),
        pytest.param({}, id="empty-map"),
        pytest.param([], id="empty-list"),
    ],
)
def test_instance_config_empty_external_mcp_blocks_resolve_to_no_servers(
    tmp_path: Path,
    external_mcp_servers: object,
) -> None:
    config = _make_config(tmp_path)
    config_path = config.save()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["agent"]["external_mcp_servers"] = external_mcp_servers
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    loaded = GatewayInstanceConfig.load(config_path)
    runtime_config = _build_runtime_config_from_instance_config(loaded)

    assert loaded.agent.external_mcp_servers == []
    assert runtime_config.get("agent.external_mcp_servers") == []


def test_instance_config_omitted_external_mcp_block_resolves_to_no_servers(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config_path = config.save()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["agent"].pop("external_mcp_servers", None)
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    loaded = GatewayInstanceConfig.load(config_path)
    runtime_config = _build_runtime_config_from_instance_config(loaded)

    assert loaded.agent.external_mcp_servers == []
    assert runtime_config.get("agent.external_mcp_servers") == []


def test_instance_config_persists_bounded_web_user_invite_ttl(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.gateway.users.web_user_invite_ttl_seconds = 120
    config_path = config.save()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert raw["gateway"]["users"]["web_user_invite_ttl_seconds"] == 120
    assert (
        GatewayInstanceConfig.load(
            config_path
        ).gateway.users.web_user_invite_ttl_seconds
        == 120
    )
    with pytest.raises(ValueError, match="must be >= 60"):
        GatewayUsersSettings(web_user_invite_ttl_seconds=59)
    with pytest.raises(ValueError, match="must be <= 86400"):
        GatewayUsersSettings(web_user_invite_ttl_seconds=86401)


def test_generated_instance_config_yaml_is_annotated(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config_path = config.save()
    config_text = config_path.read_text(encoding="utf-8")

    assert "schema_version:" in config_text
    assert "# Instance config schema version" in config_text
    assert (
        "# Instance root. control/ (team control files), dbs/, state/ (runtime records) "
        "and workspaces/ (users/, team/, agents/) are derived from it; relative "
        "instance paths resolve here."
    ) in config_text
    assert (
        "# Manager/task-history/schedule SQLite DB. Relative paths resolve under "
        "instance_root; keep it outside workspaces/."
    ) in config_text
    assert (
        "# Gateway-local SQLite DB. Relative paths resolve under instance_root."
        in config_text
    )
    assert all(
        "#" in line
        for line in config_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    raw = yaml.safe_load(config_text)
    assert raw["schema_version"] == INSTANCE_CONFIG_SCHEMA_VERSION
    # Generated configs never write the removed location settings.
    assert present_removed_location_settings(raw) == []
    assert raw["gateway"]["storage"]["sqlite"]["path"] == str(
        config.instance_root_path / "dbs" / "gateway_state.db"
    )
    # Only the access model's settings are written (old ones are refused).
    assert raw["authorization"] == {
        "enabled": True,
        "starter_profile": AUTHORIZATION_DEFAULTS.starter_profile.value,
        "presentation": {"mode": "friendly"},
    }
    assert "human_interactions" not in raw


def test_instance_web_service_urls_reject_embedded_credentials(tmp_path: Path) -> None:
    config = _make_config(tmp_path)

    with pytest.raises(ValueError, match="must not contain embedded credentials"):
        config.web_services.crawl4ai.__class__(
            base_url="https://user:password@crawl4ai.example.test"
        )


def test_instance_authorization_settings_reach_agent_runtime(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config.authorization.enabled = False
    config.authorization.cedar_binary_path = "/opt/ua/ua-cedar-authorizer"
    config.authorization.starter_profile = "careful"
    runtime_config = _build_runtime_config_from_instance_config(config)

    resolved = resolve_agent_type_settings(runtime_config, "TeamArchitect")

    assert resolved["authorization_config"] == {
        "enabled": False,
        "cedar_binary_path": "/opt/ua/ua-cedar-authorizer",
        "starter_profile": "careful",
        "presentation": {"mode": "friendly"},
    }


def test_instance_yaml_drops_a_null_agent_types_section() -> None:
    assert "agent_types" not in yaml.safe_load(dump_instance_config_yaml({"agent_types": None}))


@pytest.mark.parametrize("agent_type", [None, "WebResearchAgent"])
def test_saved_model_catalog_keeps_null_options_that_disable_inherited_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_type: str | None,
) -> None:
    from runtime_settings.agent_types import AgentTypeRuntimeSettings
    from runtime_settings.models import AgentTypeLLMOverrideSettings

    _clear_runtime_config_env(monkeypatch)
    config = _make_config(tmp_path)
    catalog = {"openai": {"candidate/model": {"reasoning_summary": None}}}
    if agent_type is None:
        model_settings = config.llm
    else:
        config.agent_types[agent_type] = AgentTypeRuntimeSettings(llm=AgentTypeLLMOverrideSettings())
        model_settings = config.agent_types[agent_type].llm
    model_settings.provider = "openai"
    model_settings.model_id = "candidate/model"
    model_settings.reasoning_summary = "auto"
    model_settings.model_capabilities = catalog

    saved = GatewayInstanceConfig.load(config.save())
    saved_llm = saved.llm if agent_type is None else saved.agent_types[agent_type].llm
    assert saved_llm.model_capabilities == catalog
    settings = resolve_agent_type_settings(_build_runtime_config_from_instance_config(saved), agent_type)
    assert settings["reasoning_summary"] is None


def test_instance_config_runtime_forwards_structured_output_fallback_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_config_env(monkeypatch)
    config = _make_config(tmp_path)
    config_path = config.save()
    config.llm.reasoning_effort = "high"
    config.llm.context_window_tokens = 200000
    config.llm.max_output_tokens = 4096
    config.llm.stream = False
    config.llm.supports_image_input = True
    config.llm.supports_audio_input = False
    config.llm.supports_file_input = True
    config.llm.model_capabilities = {
        "litellm": {
            "minimax/minimax-m3": {
                "supports_image_input": True,
                "supports_audio_input": False,
                "supports_file_input": True,
            }
        }
    }
    config.llm.reasoning_trace_retention_messages = 6
    config.llm.failover["enabled"] = True
    config.llm.failover["fallbacks"] = [
        {"provider": "openai", "model_id": "fallback-model"}
    ]
    config.embedding.extra_headers = {"X-Test": "value"}
    config.agent.max_queued_top_level_tasks = 22
    config.agent.prompt_skill_description_limit = 41
    config.agent.task_queue_warning_threshold = 9
    config.agent.max_tool_calls_per_task = 42
    config.agent.max_tool_result_tokens_per_call = 3000
    config.agent.runtime_budget_policy["mode"] = "user_guided"
    config.agent.runtime_budget_policy["max_user_guided_pauses_per_task"] = 2
    config.agent.inner_loop_context["max_total_tokens"] = 111111
    config.agent.debug["context_dump"]["enabled"] = True
    config.agent.debug["context_dump"]["mode"] = "on_error"
    config.agent.debug["context_dump"]["capture_model_calls"] = True
    config.agent.debug["context_dump"]["include_tool_specs"] = True
    config.agent.debug["context_dump"]["web_chat_controls_enabled"] = True
    config.agent.debug["terminal_reasoning"]["enabled"] = True
    config.agent.context_engine["reserved_output_tokens"] = 2048
    config.agent.conversation_compactor["type"] = "noop"
    config.agent.conversation_summary_generator_type = "llm_summarizing"
    config.agent.deferred_message_warning_threshold = 13
    config.agent.structured_output_failure_threshold = 6
    config.state.storage.sqlite.path = "runtime/custom-history.db"
    config.workspace.external_skill_repositories = [
        "https://github.com/example/skills.git"
    ]
    config.workspace.refresh_external_skills_on_start = True
    config.authorization.enabled = True
    config.authorization.starter_profile = "manual"
    config.mcp_profiles = {"skills_mcp": {"transport": "streamable_http"}}

    runtime_config = _build_runtime_config_from_instance_config(config)

    assert runtime_config.get("llm.context_window_tokens") == 200000
    assert runtime_config.get("llm.reasoning_effort") == "high"
    assert runtime_config.get("llm.max_output_tokens") == 4096
    assert runtime_config.get("llm.stream") is False
    assert runtime_config.get("llm.supports_image_input") is True
    assert runtime_config.get("llm.supports_audio_input") is False
    assert runtime_config.get("llm.supports_file_input") is True
    assert runtime_config.get("llm.model_capabilities") == {
        "litellm": {
            "minimax/minimax-m3": {
                "supports_image_input": True,
                "supports_audio_input": False,
                "supports_file_input": True,
            }
        }
    }
    assert runtime_config.get("llm.reasoning_trace_retention_messages") == 6
    assert runtime_config.get("llm.failover.enabled") is True
    assert runtime_config.get("llm.failover.fallbacks") == [
        {"provider": "openai", "model_id": "fallback-model"}
    ]
    assert runtime_config.get("embedding.extra_headers.X-Test") == "value"
    assert runtime_config.get("agent.max_queued_top_level_tasks") == 22
    assert runtime_config.get("agent.prompt_skill_description_limit") == 41
    assert runtime_config.get("agent.task_queue_warning_threshold") == 9
    assert runtime_config.get("agent.max_tool_calls_per_task") == 42
    assert runtime_config.get("agent.max_tool_result_tokens_per_call") == 3000
    assert runtime_config.get("agent.runtime_budget_policy.mode") == "user_guided"
    assert (
        runtime_config.get(
            "agent.runtime_budget_policy.max_user_guided_pauses_per_task"
        )
        == 2
    )
    assert runtime_config.get("agent.inner_loop_context.max_total_tokens") == 111111
    assert runtime_config.get("agent.debug.context_dump.enabled") is True
    assert runtime_config.get("agent.debug.context_dump.mode") == "on_error"
    assert runtime_config.get("agent.debug.context_dump.capture_model_calls") is True
    assert runtime_config.get("agent.debug.context_dump.include_tool_specs") is True
    assert (
        runtime_config.get("agent.debug.context_dump.web_chat_controls_enabled") is True
    )
    assert runtime_config.get("agent.debug.terminal_reasoning.enabled") is True
    assert runtime_config.get("agent.context_engine.reserved_output_tokens") == 2048
    assert runtime_config.get("agent.conversation_compactor.type") == "noop"
    assert (
        runtime_config.get("agent.conversation_summary_generator_type")
        == "llm_summarizing"
    )
    assert runtime_config.get("agent.deferred_message_warning_threshold") == 13
    assert runtime_config.get("agent.structured_output_failure_threshold") == 6
    assert runtime_config.get("agent.instance_config_path") == str(config_path)
    # Relative database paths resolve under the instance root.
    assert runtime_config.get("storage.sqlite.path") == str(
        (config.instance_root_path / "runtime" / "custom-history.db").resolve()
    )
    assert runtime_config.get("workspace.objective_execution_enabled") is True
    assert runtime_config.get("workspace.objective_execution_paths") == [
        BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI
    ]
    assert runtime_config.get("workspace.external_skill_repositories") == [
        "https://github.com/example/skills.git"
    ]
    assert runtime_config.get("workspace.refresh_external_skills_on_start") is True
    # Control-file locations are derived from the instance root, not forwarded.
    for removed in (
        "skills_dir",
        "python_envs_dir",
        "llm_override_path",
        "team_templates_path",
        "team_composition_path",
    ):
        assert runtime_config.get(f"workspace.{removed}") is None
    # Data folders live in the access store (decision D16), never in the runtime config.
    assert not set(REMOVED_TOOL_POLICY_KEYS) & set(runtime_config.get("tool_policy") or {})
    assert runtime_config.get("authorization.enabled") is True
    assert runtime_config.get("authorization.starter_profile") == "manual"
    assert runtime_config.get("mcp_profiles.skills_mcp.transport") == "streamable_http"


def test_instance_config_rejects_legacy_top_level_workspace_dir(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    workspace_path = instance_root / "workspace"
    config_path = instance_root / "instance_config.yaml"
    instance_root.mkdir(parents=True)
    workspace_path.mkdir()
    config_path.write_text(
        f"""
schema_version: 1
instance_name: mybot
instance_root: {instance_root}
workspace_dir: {workspace_path}
gateway:
  team_id: team_test
agent:
  max_tool_calls_per_task: 12
  runtime_budget_policy:
    mode: user_guided
agent_types: {{}}
assistant: {{}}
user: {{}}
delivery: {{}}
heartbeat: {{}}
llm: {{}}
embedding: {{}}
tool_policy: {{}}
interactive_apps: {{}}
activity_collector: {{}}
otel: {{}}
voice: {{}}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="workspace_dir has been removed"):
        GatewayInstanceConfig.load(config_path)


def test_a_leftover_concurrency_setting_loads_and_the_next_save_drops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent.max_concurrent_top_level_tasks was removed without a refusing check.

    Onboarding wrote it into every instance_config.yaml. Values above 1 never
    had an effect, so startup ignores the leftover line and the next save of
    the file leaves it out.
    """

    _clear_runtime_config_env(monkeypatch)
    config_path = _make_config(tmp_path).save()
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    saved["agent"]["max_concurrent_top_level_tasks"] = 4
    config_path.write_text(dump_instance_config_yaml(saved), encoding="utf-8")

    loaded = GatewayInstanceConfig.load(config_path)

    assert "max_concurrent_top_level_tasks" not in loaded.to_yaml_dict()["agent"]
    runtime_config = _build_runtime_config_from_instance_config(loaded)
    assert runtime_config.get("agent.max_concurrent_top_level_tasks") is None
    loaded.save()
    resaved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "max_concurrent_top_level_tasks" not in resaved["agent"]


@pytest.mark.parametrize("filename", ["instance_config.yaml", "config.yaml"])
def test_onboarding_import_reports_the_concurrency_setting_as_removed(filename: str) -> None:
    from assistant_gateway.onboarding_web.settings_import import (
        REASON_REMOVED,
        read_onboarding_import,
    )

    imported = read_onboarding_import(
        filename,
        "agent:\n  default_team_id: team_old\n  max_concurrent_top_level_tasks: 1\n",
    )

    skipped = [(item.path, item.reason) for item in imported.skipped]
    assert ("agent.max_concurrent_top_level_tasks", REASON_REMOVED) in skipped
    # Without the removed-settings entry the agent section was reported as partly imported.
    assert not [path for path, _reason in skipped if path == "agent"]


def test_manager_discovers_team_instance_config_env(
    tmp_path: Path, monkeypatch
) -> None:
    config = _make_config(tmp_path)
    config_path = config.save()
    monkeypatch.setenv("TEAM_INSTANCE_CONFIG", str(config_path))
    monkeypatch.delenv("TEAM_INSTANCE_ROOT", raising=False)

    assert (
        _discover_instance_config_path(SimpleNamespace(instance_root=None))
        == config_path
    )


def test_litellm_model_receives_resolved_context_window_limit() -> None:
    from custom_agents.strands_agent import UniversalAgent

    agent = UniversalAgent.model_construct(
        model_provider="litellm",
        model_id="dinfra/example-unknown-window-model",
        context_window_tokens=98765,
        temperature=0.0,
        max_output_tokens=None,
        reasoning_effort=None,
        litellm_api_base=None,
    )

    model = agent._configure_model()

    assert agent._resolved_context_window_tokens() == 98765
    assert model.context_window_limit == 98765
    assert model.get_config()["context_window_limit"] == 98765


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek/deepseek-v4-flash",
        "novita/deepseek/deepseek-v4-pro",
    ],
)
def test_deepseek_v4_litellm_model_ignores_global_stream_true(model_id: str) -> None:
    from custom_agents.strands_agent import UniversalAgent

    agent = UniversalAgent.model_construct(
        model_provider="litellm",
        model_id=model_id,
        stream=True,
        context_window_tokens=98765,
        temperature=0.0,
        max_output_tokens=None,
        reasoning_effort=None,
        litellm_api_base=None,
    )

    model = agent._configure_model()

    assert model.get_config()["params"]["stream"] is False
