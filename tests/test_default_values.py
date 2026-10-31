from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from runtime_settings.telemetry import ActivityCollectorSettings, OTelSettings
from runtime_settings.agents import AgentRuntimeDefaultsSettings
from runtime_settings.governance import AuthorizationSettings
from assistant_gateway.config import DeliverySettings, GatewayUsersSettings, LaunchSettings, StorageWarningSettings
from runtime_settings.models import InstanceEmbeddingSettings, InstanceLLMSettings
from runtime_settings.interactive_apps import InteractiveAppsSettings
from runtime_settings.security import SecurityLayerSettings
from runtime_settings.transport import TransportSettings
from runtime_settings.appearance import UISettings
from runtime_settings.voice import VoiceSettings
from runtime_settings.services import WebServicesSettings
from runtime_settings.workspace import WorkspaceSettings
from config_loader import Config, resolve_agent_type_settings
from custom_agents.embedding_service import create_embedding_service_settings
from custom_messages.contracts.authorization_presentation_mode import (
    AuthorizationPresentationMode,
)
from storage.backends import STORAGE_BACKEND_SQLITE
from custom_messages.contracts.runtime_v1 import (
    TEAM_ARCHITECT_AGENT_TYPE,
    TRANSPORT_NATS,
)
from default_values import (
    AGENT_DEFAULTS,
    CONVERSATION_COMPACTION_DEFAULTS,
    DEFAULT_VALUES,
    DefaultValue,
    GATEWAY_DEFAULTS,
    INTERACTIVE_APP_DEFAULTS,
    LLM_DEFAULTS,
    STORAGE_DEFAULTS,
    TRANSPORT_DEFAULTS,
    WEB_SERVICES_DEFAULTS,
    WORKSPACE_DEFAULTS,
    BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI,
)
from secure_tool_broker.credential_state_store import CredentialStateBackendConfig
from user_docs.skill_docs import resolve_user_docs_skills_dir
from runtime_settings.instance_layout import InstanceLayout


def _iter_default_values(group: object):
    assert is_dataclass(group)
    for field in fields(group):
        value = getattr(group, field.name)
        if isinstance(value, DefaultValue):
            yield field.name, value
        else:
            yield from _iter_default_values(value)


def test_default_values_have_descriptions_and_onboarding_metadata() -> None:
    defaults = list(_iter_default_values(DEFAULT_VALUES))

    assert defaults
    for name, default in defaults:
        assert default.description.strip(), name
        assert isinstance(default.onboarding_overridable, bool), name


def test_authorization_presentation_defaults_to_friendly_and_accepts_technical() -> (
    None
):
    assert (
        AuthorizationSettings().presentation.mode
        == AuthorizationPresentationMode.FRIENDLY
    )
    technical = AuthorizationSettings.model_validate(
        {"presentation": {"mode": "technical"}}
    )
    assert technical.presentation.mode == AuthorizationPresentationMode.TECHNICAL


def test_authorization_presentation_rejects_unknown_mode() -> None:
    with pytest.raises(
        ValidationError,
        match="authorization.presentation.mode must be 'friendly' or 'technical'",
    ):
        AuthorizationSettings.model_validate({"presentation": {"mode": "verbose"}})


def test_empty_config_resolves_from_centralized_defaults() -> None:
    settings = resolve_agent_type_settings(Config({}), None)

    assert LLM_DEFAULTS.provider.value == "litellm"
    assert LLM_DEFAULTS.model_id.value == "deepseek/deepseek-v4.1-flash"
    assert settings["transport_type"] == TRANSPORT_DEFAULTS.type.value
    assert settings["nats_servers"] == list(TRANSPORT_DEFAULTS.nats_servers.value)
    assert settings["model_provider"] == LLM_DEFAULTS.provider.value
    assert settings["model_id"] == LLM_DEFAULTS.model_id.value
    assert settings["reasoning_summary"] is None
    assert settings["litellm_api_mode"] == "chat_completions"
    assert settings["supports_image_input"] is None
    assert settings["supports_audio_input"] is None
    assert settings["supports_file_input"] is None
    assert settings["model_capabilities"] == LLM_DEFAULTS.model_capabilities.value
    # Only the instance root is configurable; every folder is derived from it.
    default_layout = InstanceLayout.at(WORKSPACE_DEFAULTS.instance_root.value)
    assert settings["instance_root"] == str(default_layout.root)
    assert settings["workspace_dir"] == str(default_layout.workspaces_dir)
    assert settings["skills_dir"] == str(default_layout.skills_dir)
    assert settings["llm_override_path"] == str(default_layout.llm_overrides_path)
    assert settings["team_templates_path"] == str(default_layout.team_templates_path)
    assert settings["team_composition_path"] == str(
        default_layout.team_composition_path
    )
    assert settings["task_history_db_path"] == str(default_layout.task_database_path)
    assert settings["objective_execution_enabled"] is True
    assert all(Path(path).is_file() for path in settings["objective_execution_paths"])
    assert WORKSPACE_DEFAULTS.objective_execution_paths.value == [
        BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI
    ]
    assert settings["storage_backend"] == STORAGE_DEFAULTS.backend.value
    assert AGENT_DEFAULTS.max_tool_calls_per_task.value == 150
    assert (
        settings["max_tool_calls_per_task"]
        == AGENT_DEFAULTS.max_tool_calls_per_task.value
    )
    assert (
        settings["max_tool_result_tokens_per_call"]
        == AGENT_DEFAULTS.max_tool_result_tokens_per_call.value
    )
    assert (
        settings["conversation_compactor_type"]
        == CONVERSATION_COMPACTION_DEFAULTS.compactor_type.value
    )
    assert (
        settings["conversation_summary_generator_type"]
        == AGENT_DEFAULTS.conversation_summary_generator_type.value
    )
    assert (
        settings["context_engine"]["type"]
        == AGENT_DEFAULTS.context_engine.value["type"]
    )
    assert (
        settings["context_engine"]["reserved_output_tokens"]
        == AGENT_DEFAULTS.context_engine.value["reserved_output_tokens"]
    )


def test_instance_llm_settings_validate_reasoning_summary_transport() -> None:
    settings = InstanceLLMSettings(
        provider="litellm",
        litellm_api_mode="responses",
        reasoning_summary="AUTO",
    )

    assert settings.reasoning_summary == "auto"
    assert settings.litellm_api_mode == "responses"

    codex_settings = InstanceLLMSettings(
        provider="openai-codex",
        model_id="gpt-5.6-luna",
        reasoning_effort="xhigh",
        reasoning_summary="detailed",
    )

    assert codex_settings.reasoning_summary == "detailed"

    with pytest.raises(ValidationError, match="reasoning_summary requires"):
        InstanceLLMSettings(
            provider="litellm",
            litellm_api_mode="chat_completions",
            reasoning_summary="auto",
        )

    with pytest.raises(ValidationError, match="stream=false"):
        InstanceLLMSettings(
            provider="litellm",
            litellm_api_mode="responses",
            stream=False,
        )


def test_agent_runtime_defaults_match_example_config_runtime_posture() -> None:
    inner_loop_context = AGENT_DEFAULTS.inner_loop_context.value
    filters = inner_loop_context["filters"]

    assert AGENT_DEFAULTS.max_queued_top_level_tasks.value == 50
    assert AGENT_DEFAULTS.subtask_timeout_seconds.value == 1200.0
    assert AGENT_DEFAULTS.max_tool_result_tokens_per_call.value == 20000
    assert inner_loop_context["max_total_tokens"] == 300000
    assert inner_loop_context["preserve_recent_messages"] == 8
    assert inner_loop_context["preserve_head_tokens"] == 1000
    assert inner_loop_context["preserve_tail_tokens"] == 1000
    assert inner_loop_context["reactive_overflow"] == {
        "max_total_recovery_attempts": 3,
        "min_reduction_tokens": 1,
    }
    assert filters["html_document"]["trigger_tokens"] == 2000
    assert filters["javascript_or_bundle"]["trigger_tokens"] == 1000
    assert filters["css_or_style_blob"]["trigger_tokens"] == 2000
    assert filters["base64_or_binary_like"]["trigger_tokens"] == 500
    assert filters["json_large"]["trigger_tokens"] == 3000
    assert filters["json_large"]["comms_json_trigger_tokens"] == 20000
    assert filters["xml_or_svg"]["trigger_tokens"] == 2000
    assert filters["log_or_trace"]["trigger_tokens"] == 750
    assert filters["csv_or_table"]["trigger_tokens"] == 2500
    assert filters["html_boilerplate_text"]["trigger_tokens"] == 1000
    assert filters["agent_transcript"]["trigger_tokens"] == 1500
    assert filters["code_file"]["trigger_tokens"] == 30000
    assert LLM_DEFAULTS.context_window_tokens.value == 800000
    assert LLM_DEFAULTS.max_output_tokens.value == 55296
    assert AGENT_DEFAULTS.context_engine.value["reserved_output_tokens"] == 55296
    assert (
        AGENT_DEFAULTS.debug.value["context_dump"]["include_reasoning_artifacts"]
        is True
    )
    assert (
        AGENT_DEFAULTS.debug.value["context_dump"]["web_chat_controls_enabled"] is False
    )
    assert AGENT_DEFAULTS.debug.value["terminal_reasoning"]["enabled"] is False
    assert GATEWAY_DEFAULTS.manager_transition_notices_enabled.value is False
    assert INTERACTIVE_APP_DEFAULTS.drop_pending_updates.value is True
    assert set(WEB_SERVICES_DEFAULTS.crawl4ai.value) == {"base_url", "timeout_seconds"}
    assert WEB_SERVICES_DEFAULTS.searxng.value["default_num_results"] == 8


def _assert_example_subset(
    code_defaults: dict[str, object],
    example_defaults: dict[str, object],
    *,
    path: str,
    exceptions: set[str] | None = None,
) -> None:
    exception_paths = exceptions or set()
    for key, expected in example_defaults.items():
        field_path = f"{path}.{key}" if path else key
        if field_path in exception_paths:
            continue
        assert key in code_defaults, f"Code defaults omit example field {field_path}"
        actual = code_defaults[key]
        if isinstance(expected, dict):
            assert isinstance(actual, dict), field_path
            _assert_example_subset(
                actual,
                expected,
                path=field_path,
                exceptions=exception_paths,
            )
        else:
            assert actual == expected, field_path


def test_config_models_match_example_defaults_except_approved_exceptions() -> None:
    example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    sections = {
        "launch": LaunchSettings(),
        "transport": TransportSettings(),
        "gateway.users": GatewayUsersSettings(),
        "gateway.storage_warnings": StorageWarningSettings(),
        "llm": InstanceLLMSettings(),
        "embedding": InstanceEmbeddingSettings(),
        "agent": AgentRuntimeDefaultsSettings(),
        "workspace": WorkspaceSettings(),
        "authorization": AuthorizationSettings(),
        "security_layer": SecurityLayerSettings(),
        "delivery": DeliverySettings(),
        "interactive_apps": InteractiveAppsSettings(),
        "activity_collector": ActivityCollectorSettings(),
        "otel": OTelSettings(),
        "voice": VoiceSettings(),
        "web_services": WebServicesSettings(),
        "ui": UISettings(),
    }
    example_sections = {
        **{name: example[name] for name in sections if "." not in name},
        "gateway.users": example["gateway"]["users"],
        "gateway.storage_warnings": example["gateway"]["storage_warnings"],
    }
    exceptions = {
        "agent.default_team_id",
        "agent.external_mcp_servers",
        # config.yaml-only: instance configs carry a top-level instance_root, so
        # the shared agent model has no instance_root field. Its default lives in
        # WORKSPACE_DEFAULTS and is checked below.
        "agent.instance_root",
    }

    for path, model in sections.items():
        _assert_example_subset(
            model.model_dump(mode="json"),
            example_sections[path],
            path=path,
            exceptions=exceptions,
        )

    assert AgentRuntimeDefaultsSettings().external_mcp_servers == []
    assert example["agent"]["external_mcp_servers"]
    assert example["agent"]["instance_root"] == WORKSPACE_DEFAULTS.instance_root.value
    assert AgentRuntimeDefaultsSettings().memory["plugins"]["order"] == [
        "coremem",
        "usermem",
        "domain_topic_recall",
        "knowledge_recall",
    ]
    website = InteractiveAppsSettings().internal_website
    assert website.bind_port == 18090
    assert website.public_port == 18090
    website_example = example["interactive_apps"]["plugins"][
        "builtin.internal_website"
    ]["settings"]
    assert website_example["bind_port"] == 18090
    assert website_example["public_port"] == 18090


def test_storage_defaults_match_example_paths() -> None:
    example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))

    assert (
        STORAGE_DEFAULTS.task_history_db_path.value
        == example["storage"]["sqlite"]["path"]
    )
    assert (
        STORAGE_DEFAULTS.activity_collector_db_path.value
        == example["activity_collector"]["storage"]["sqlite"]["path"]
    )


def test_subsystem_fallbacks_use_central_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    embedding = create_embedding_service_settings({})
    credential_state = CredentialStateBackendConfig.from_env({})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEAM_INSTANCE_ROOT", raising=False)

    assert embedding.provider == "openai_compatible"
    assert embedding.model_id == "harrier-oss-v1-0.6b"
    assert embedding.timeout_seconds == 10.0
    assert credential_state.mode == "credential_source"
    assert credential_state.source == "sealbox"
    assert credential_state.namespace == "team-cred-store"
    # Without a configured instance root the docs read the current folder's
    # control/skills.
    assert resolve_user_docs_skills_dir() == InstanceLayout.at(tmp_path).skills_dir


def test_core_defaults_use_shared_identifiers() -> None:
    assert AGENT_DEFAULTS.named_agent_type.value == TEAM_ARCHITECT_AGENT_TYPE
    assert STORAGE_DEFAULTS.backend.value == STORAGE_BACKEND_SQLITE
    assert TRANSPORT_DEFAULTS.type.value == TRANSPORT_NATS
