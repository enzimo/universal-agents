"""Shared helpers for creating onboarded gateway instances."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import logging
import secrets
from pathlib import Path
from typing import Any, Mapping, Optional

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import onboarding_choices
from assistant_gateway.bootstrap import bootstrap_instance, hand_over_onboarding_keys, seed_default_gateway_user
from assistant_gateway.onboarding_account_keys import keys_from_answers, model_keys_from_answers, sharing_from_answers
from assistant_gateway.instance_secrets import (
    SecretRequirement,
    environment_secret_values,
    ensure_instance_sealbox_material,
    ensure_internal_credential_env_file,
    missing_secrets,
)
from assistant_gateway.onboarding_reconciliation import (
    OnboardingReport,
    diff_config_paths,
    diff_env_keys,
    onboarding_report_detail_path,
    read_yaml_mapping,
    write_onboarding_report,
)
from runtime_settings.telemetry import ActivityCollectorSettings
from runtime_settings.agent_types import AgentTypeRuntimeSettings
from runtime_settings.agents import AgentRuntimeDefaultsSettings
from assistant_gateway.config import DeliverySettings, GatewayInstanceConfig, GatewayUsersSettings, StartupAgentSettings, ensure_required_startup_agents
from runtime_settings.execution import ExecutionControlSettings
from runtime_settings.governance import ToolPolicySettings
from runtime_settings.interactive_apps import InteractiveAppsSettings
from runtime_settings.network import NetworkSettings
from runtime_settings.appearance import OnboardingAppearanceSettings, UISettings
from runtime_settings.models import OpenAICodexSettings, XAIOAuthSettings
from runtime_settings.security import SecurityLayerSettings, normalize_security_layer_mode
from runtime_settings.voice import VoiceSettings
from runtime_settings.workspace import WorkspaceSettings
from config_loader import load_config
from default_values import (
    AGENT_DEFAULTS,
    EMBEDDING_DEFAULTS,
    GATEWAY_DEFAULTS,
    HEARTBEAT_DEFAULTS,
    INTERACTIVE_APP_DEFAULTS,
    LAUNCH_DEFAULTS,
    LLM_DEFAULTS,
    NETWORK_DEFAULTS,
    STORAGE_DEFAULTS,
    VOICE_DEFAULTS,
)
from internal_credentials import (
    merge_env_file,
    parse_env_file,
)
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.secrets_source import (
    SECRETS_SOURCE_ENV_FILE,
    SECRETS_SOURCE_ENVIRONMENT,
    normalize_secrets_source,
)
from secure_tool_broker.defaults import DEFAULT_INTERNAL_BASE_URL as DEFAULT_SECURE_TOOL_BROKER_INTERNAL_BASE_URL
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from runtime_settings.model_request_options import (
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)
from custom_agents.xai_oauth_auth import (
    is_xai_oauth_provider,
    normalize_xai_oauth_provider,
    resolve_xai_oauth_model_id,
)


LOGGER = logging.getLogger(__name__)

AGENT_RUNTIME_TEMPLATE_KEYS = {
    "memory_graph_enabled",
    "max_queued_top_level_tasks",
    "task_queue_warning_threshold",
    "subtask_timeout_seconds",
    "subtask_watchdog_interval_seconds",
    "subtask_status_probe_timeout_seconds",
    "restart_executing_task_grace_seconds",
    "same_role_runtime_takeover_enabled",
    "max_tool_calls_per_task",
    "max_tool_result_tokens_per_call",
    "runtime_budget_policy",
    "turn_action_outbox",
    "runtime_coordinator",
    "mid_turn_steering",
    "inner_loop_context",
    "context_engine",
    "debug",
    "structured_output_failure_threshold",
    "deferred_message_warning_threshold",
    "conversation_compactor",
    "conversation_summary_generator_type",
    "tool_allowlist",
    "tool_blocklist",
    "external_mcp_servers",
    "agent_definition_paths",
    "prompt_skill_description_exclusions",
    "prompt_skill_description_limit",
    "result_transformer",
}


WEB_CHAT_AUTH_TOKEN_ENV = "WEB_CHAT_AUTH_TOKEN"
PHOTON_PROJECT_ID_ENV = "PHOTON_PROJECT_ID"
PHOTON_PROJECT_SECRET_ENV = "PHOTON_PROJECT_SECRET"
CRAWL4AI_MCP_SERVER_URL_ENV = "CRAWL4AI_MCP_SERVER_URL"
POSTGRES_DSN_ENV = "POSTGRES_DSN"
SEARXNG_BASE_URL_ENV = "SEARXNG_BASE_URL"
SEARXNG_VERIFY_TLS_ENV = "SEARXNG_VERIFY_TLS"
SEARXNG_TIMEOUT_SECONDS_ENV = "SEARXNG_TIMEOUT_SECONDS"
SEARXNG_DEFAULT_NUM_RESULTS_ENV = "SEARXNG_DEFAULT_NUM_RESULTS"

# Secrets onboarding writes to team.env. Model keys (the provider key, the
# embedding key, the xAI key for voice) and the Exa and Brave Search keys are
# not here: they go to the credential store (assistant_gateway.
# onboarding_account_keys), in both secrets modes, because a variable in
# team.env would override every key saved later with /auth model.
USER_ENV_ANSWER_MAPPINGS = (
    ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
    ("DISCORD_BOT_TOKEN", "discord_bot_token"),
    (PHOTON_PROJECT_ID_ENV, "photon_project_id"),
    (PHOTON_PROJECT_SECRET_ENV, "photon_project_secret"),
    (CRAWL4AI_MCP_SERVER_URL_ENV, "crawl4ai_mcp_server_url"),
    (POSTGRES_DSN_ENV, "postgres_dsn"),
    (SEARXNG_BASE_URL_ENV, "searxng_base_url"),
    (SEARXNG_VERIFY_TLS_ENV, "searxng_verify_tls"),
    (SEARXNG_TIMEOUT_SECONDS_ENV, "searxng_timeout_seconds"),
    (SEARXNG_DEFAULT_NUM_RESULTS_ENV, "searxng_default_num_results"),
)
# Answers written to team.env that instance_config.yaml also holds, so an
# install without team.env does not need them in the environment.
CONFIG_HELD_ENV_KEYS = frozenset({PHOTON_PROJECT_ID_ENV})


def _deep_merge_dict(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base or {}))
    for key, value in dict(override or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _model_dump_or_empty(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return {}


@dataclass(frozen=True)
class OnboardingWriteResult:
    config: GatewayInstanceConfig
    report: OnboardingReport
    # None when secrets come from the environment: no team.env is written.
    env_path: Optional[Path]
    created_paths: tuple[Path, ...]
    # Set only when secrets come from the environment: required secrets the
    # environment lacks (names, never values) and secrets the person typed
    # that were not saved.
    missing_secrets: tuple[SecretRequirement, ...] = ()
    unsaved_secret_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class OnboardingWriteOptions:
    source_env_file_path: Optional[str] = None
    response_queue_name: Optional[str] = None
    manager_agent_id: Optional[str] = None
    generate_internal_credentials: bool = True
    rotate_internal_credentials: bool = False
    overwrite_existing: bool = True
    base_config_applied: bool = False


@dataclass(frozen=True)
class OnboardingTemplate:
    """The settings a new instance takes from a base config.yaml.

    Both ways of onboarding use this one template: the web onboarding page
    (``assistant_gateway.onboarding_web``) and ``assistant_cli.py onboard``.
    ``load_base_onboarding_template`` builds it and
    ``apply_onboarding_template`` copies it into a new instance config before
    the person's answers are applied, so the same base config gives the same
    instance whichever way is used. Without a base config every field keeps
    its default. Activity-collector tokens are dropped when loading; they
    belong in team.env.
    """

    team_id: str
    secrets_source: str = LAUNCH_DEFAULTS.secrets_source.value
    manager_storage_backend: str = STORAGE_DEFAULTS.backend.value
    manager_storage_sqlite_path: Optional[str] = STORAGE_DEFAULTS.task_history_db_path.value
    manager_storage_postgres_dsn: Optional[str] = None
    timezone: str = GATEWAY_DEFAULTS.user_timezone.value
    model_provider: str = LLM_DEFAULTS.provider.value
    model_id: str = LLM_DEFAULTS.model_id.value
    reasoning_effort: str = LLM_DEFAULTS.reasoning_effort.value or ""
    reasoning_summary: Optional[str] = LLM_DEFAULTS.reasoning_summary.value
    llm_temperature: Optional[float] = LLM_DEFAULTS.temperature.value
    llm_context_window_tokens: Optional[int] = LLM_DEFAULTS.context_window_tokens.value
    llm_max_output_tokens: Optional[int] = LLM_DEFAULTS.max_output_tokens.value
    llm_stream: Optional[bool] = LLM_DEFAULTS.stream.value
    litellm_api_mode: str = LLM_DEFAULTS.litellm_api_mode.value
    llm_request_timeout_seconds: float = LLM_DEFAULTS.request_timeout_seconds.value
    supports_image_input: Optional[bool] = None
    supports_audio_input: Optional[bool] = None
    supports_file_input: Optional[bool] = None
    model_capabilities: dict[str, Any] = field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.model_capabilities.value)
    )
    reasoning_trace_retention_messages: Optional[int] = LLM_DEFAULTS.reasoning_trace_retention_messages.value
    litellm_api_base: Optional[str] = LLM_DEFAULTS.litellm_api_base.value
    openai_codex: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.openai_codex.value))
    xai_oauth: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.xai_oauth.value))
    compaction_model_provider: str = ""
    compaction_model_id: str = ""
    llm_failover: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.failover.value))
    embedding_provider: Optional[str] = EMBEDDING_DEFAULTS.provider.value
    embedding_model_id: Optional[str] = EMBEDDING_DEFAULTS.model_id.value
    embedding_api_base: Optional[str] = EMBEDDING_DEFAULTS.api_base.value
    embedding_dimensions: Optional[int] = None
    embedding_timeout_seconds: Optional[float] = EMBEDDING_DEFAULTS.timeout_seconds.value
    embedding_organization: Optional[str] = EMBEDDING_DEFAULTS.organization.value
    embedding_extra_headers: dict[str, str] = field(
        default_factory=lambda: copy.deepcopy(EMBEDDING_DEFAULTS.extra_headers.value)
    )
    agent: AgentRuntimeDefaultsSettings = field(default_factory=AgentRuntimeDefaultsSettings)
    agent_types: dict[str, AgentTypeRuntimeSettings] = field(default_factory=dict)
    delivery: DeliverySettings = field(default_factory=DeliverySettings)
    gateway_users: GatewayUsersSettings = field(default_factory=GatewayUsersSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    security_layer: SecurityLayerSettings = field(default_factory=SecurityLayerSettings)
    interactive_apps: InteractiveAppsSettings = field(default_factory=InteractiveAppsSettings)
    activity_collector: ActivityCollectorSettings = field(default_factory=ActivityCollectorSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    ui: UISettings = field(default_factory=UISettings)
    execution_control: ExecutionControlSettings = field(default_factory=ExecutionControlSettings)
    tool_policy: ToolPolicySettings = field(default_factory=ToolPolicySettings)
    mcp_profiles: dict[str, Any] = field(default_factory=dict)


def load_base_onboarding_template(base_config_path: str | None) -> OnboardingTemplate:
    """Read the onboarding template from a base config.yaml (defaults when None)."""

    cfg = load_config(base_config_path)
    raw_config = cfg.to_dict()
    default_timezone = (
        cfg.get("agent.default_request_timezone", AGENT_DEFAULTS.default_request_timezone.value)
        or AGENT_DEFAULTS.default_request_timezone.value
    )
    storage_backend = cfg.get("storage.backend", STORAGE_DEFAULTS.backend.value)
    storage_sqlite_path = cfg.get(
        "storage.sqlite.path",
        cfg.get("agent.task_history_db_path", STORAGE_DEFAULTS.task_history_db_path.value),
    )
    storage_postgres_dsn = cfg.get("storage.postgres.dsn", None)
    activity_collector_payload = dict(cfg.get("activity_collector", {}) or {})
    activity_collector_payload.pop("user_token", None)
    activity_collector_payload.pop("admin_token", None)
    activity_collector_payload.pop("ingest_token", None)
    agent_payload = dict(raw_config.get("agent") or {})
    agent_payload_for_template = {
        key: agent_payload[key]
        for key in AGENT_RUNTIME_TEMPLATE_KEYS
        if key in agent_payload
    }
    agent_types_payload: dict[str, AgentTypeRuntimeSettings] = {}
    for agent_type, settings in dict(raw_config.get("agent_types") or {}).items():
        settings_payload = dict(settings or {})
        if settings_payload.get("agent") is None:
            settings_payload.pop("agent", None)
        validated_settings = AgentTypeRuntimeSettings.model_validate(settings_payload)
        dumped_settings = validated_settings.model_dump(mode="json", exclude_none=True)
        if dumped_settings.get("agent") or dumped_settings.get("llm"):
            agent_types_payload[str(agent_type)] = validated_settings
    return OnboardingTemplate(
        team_id=cfg.get("agent.default_team_id", AGENT_DEFAULTS.default_team_id.value),
        secrets_source=normalize_secrets_source(cfg.get("launch.secrets_source", None)),
        manager_storage_backend=storage_backend,
        manager_storage_sqlite_path=storage_sqlite_path,
        manager_storage_postgres_dsn=storage_postgres_dsn,
        timezone=default_timezone,
        model_provider=cfg.get("llm.provider", LLM_DEFAULTS.provider.value) or LLM_DEFAULTS.provider.value,
        model_id=cfg.get("llm.model_id", LLM_DEFAULTS.model_id.value) or LLM_DEFAULTS.model_id.value,
        reasoning_effort=cfg.get("llm.reasoning_effort", LLM_DEFAULTS.reasoning_effort.value) or "",
        reasoning_summary=normalize_reasoning_summary(
            cfg.get("llm.reasoning_summary", LLM_DEFAULTS.reasoning_summary.value)
        ),
        llm_temperature=cfg.get("llm.temperature", LLM_DEFAULTS.temperature.value),
        llm_context_window_tokens=cfg.get(
            "llm.context_window_tokens",
            LLM_DEFAULTS.context_window_tokens.value,
        ),
        llm_max_output_tokens=cfg.get("llm.max_output_tokens", LLM_DEFAULTS.max_output_tokens.value),
        llm_stream=cfg.get("llm.stream", LLM_DEFAULTS.stream.value),
        litellm_api_mode=normalize_litellm_api_mode(
            cfg.get("llm.litellm_api_mode", LLM_DEFAULTS.litellm_api_mode.value)
        ),
        llm_request_timeout_seconds=cfg.get(
            "llm.request_timeout_seconds",
            LLM_DEFAULTS.request_timeout_seconds.value,
        ),
        supports_image_input=cfg.get("llm.supports_image_input", None),
        supports_audio_input=cfg.get("llm.supports_audio_input", None),
        supports_file_input=cfg.get("llm.supports_file_input", None),
        model_capabilities=copy.deepcopy(
            cfg.get("llm.model_capabilities", LLM_DEFAULTS.model_capabilities.value) or {}
        ),
        reasoning_trace_retention_messages=cfg.get(
            "llm.reasoning_trace_retention_messages",
            LLM_DEFAULTS.reasoning_trace_retention_messages.value,
        ),
        litellm_api_base=cfg.get("llm.litellm_api_base", LLM_DEFAULTS.litellm_api_base.value) or "",
        openai_codex=copy.deepcopy(cfg.get("llm.openai_codex", LLM_DEFAULTS.openai_codex.value) or {}),
        xai_oauth=copy.deepcopy(cfg.get("llm.xai_oauth", LLM_DEFAULTS.xai_oauth.value) or {}),
        compaction_model_provider=cfg.get("llm.compaction_model_provider", "") or "",
        compaction_model_id=cfg.get("llm.compaction_model_id", "") or "",
        llm_failover=copy.deepcopy(cfg.get("llm.failover", LLM_DEFAULTS.failover.value) or {}),
        embedding_provider=cfg.get("embedding.provider", EMBEDDING_DEFAULTS.provider.value) or "",
        embedding_model_id=cfg.get("embedding.model_id", EMBEDDING_DEFAULTS.model_id.value) or "",
        embedding_api_base=cfg.get("embedding.api_base", EMBEDDING_DEFAULTS.api_base.value) or "",
        embedding_dimensions=cfg.get("embedding.dimensions", None),
        embedding_timeout_seconds=cfg.get(
            "embedding.timeout_seconds",
            EMBEDDING_DEFAULTS.timeout_seconds.value,
        ),
        embedding_organization=cfg.get("embedding.organization", EMBEDDING_DEFAULTS.organization.value) or "",
        embedding_extra_headers=dict(
            cfg.get("embedding.extra_headers", EMBEDDING_DEFAULTS.extra_headers.value) or {}
        ),
        agent=AgentRuntimeDefaultsSettings.model_validate(agent_payload_for_template),
        agent_types=agent_types_payload,
        delivery=DeliverySettings.model_validate(cfg.get("delivery", {}) or {}),
        gateway_users=GatewayUsersSettings.model_validate(cfg.get("gateway.users", {}) or {}),
        network=NetworkSettings.model_validate(cfg.get("network", {}) or {}),
        security_layer=SecurityLayerSettings.model_validate(cfg.get("security_layer", {}) or {}),
        interactive_apps=InteractiveAppsSettings.model_validate(cfg.get("interactive_apps", {}) or {}),
        activity_collector=ActivityCollectorSettings.model_validate(activity_collector_payload),
        voice=VoiceSettings.model_validate(dict(cfg.get("voice", {}) or {})),
        workspace=WorkspaceSettings.model_validate(raw_config.get("workspace", {}) or {}),
        ui=UISettings.model_validate(raw_config.get("ui", {}) or {}),
        tool_policy=ToolPolicySettings.model_validate(raw_config.get("tool_policy", {}) or {}),
        execution_control=ExecutionControlSettings.model_validate(
            raw_config.get("execution_control", {}) or {}
        ),
        mcp_profiles=dict(cfg.get("mcp_profiles", {}) or {}),
    )


def resolve_onboarding_model_id(
    provider: Any,
    requested_model_id: Any,
    *,
    template_provider: Any = None,
    template_model_id: Any = None,
) -> str:
    resolved = resolve_xai_oauth_model_id(
        provider,
        requested_model_id,
        inherited_provider=template_provider,
        inherited_model_id=template_model_id,
    )
    return resolved or LLM_DEFAULTS.model_id.value


def default_instance_root(instance_name: str, *, cwd: Path | None = None) -> str:
    """``./instances/<name>``: where both onboarding front ends put a new instance."""

    root = (cwd or Path.cwd()).resolve()
    return str((root / "instances" / instance_name).resolve())


def default_launch_env_file(instance_root: str) -> str:
    """The instance's ``team.env``, used by onboarding and the CLI launcher."""

    return str(InstanceLayout.at(instance_root).team_env_path)


def launch_env_file_for(instance_root: str, secrets_source: str) -> Optional[str]:
    """``launch.env_file_path`` for a secrets source: none when secrets come from the environment."""

    if secrets_source == SECRETS_SOURCE_ENVIRONMENT:
        return None
    return default_launch_env_file(instance_root)


def _account_sharing_answer(raw: Any) -> dict[str, str]:
    """The Secrets page's sharing choices, checked against the shipped tool catalog (``access_control.account_sharing``).

    ``{"share_with_team": bool, "choices": {subject: "shared"|"private"}}``;
    nothing given (the CLI wizard, an older page) records nothing, so every
    service and the model keys keep the shipped start values.
    """

    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not isinstance(raw.get("choices", {}), Mapping):
        raise ValueError("account_sharing must give share_with_team and a choices object.")
    choices = onboarding_choices(
        CatalogSource(None).current().tools,
        share_with_team=_bool_value(raw.get("share_with_team"), True),
        choices={str(subject): str(value) for subject, value in dict(raw.get("choices") or {}).items()},
    )
    return {subject: sharing.value for subject, sharing in choices.items()}


def normalize_onboarding_answers(raw_answers: Mapping[str, Any], template: Any) -> dict[str, Any]:
    instance_name = str(raw_answers.get("instance_name") or raw_answers.get("runtime_instance_name") or "").strip()
    assistant_name = str(raw_answers.get("assistant_name") or GATEWAY_DEFAULTS.assistant_display_name.value).strip()
    if not instance_name:
        slug = _slugify(assistant_name or "assistant")
        instance_name = f"instance_{slug}"
    instance_root = str(raw_answers.get("instance_root") or default_instance_root(instance_name)).strip()
    team_id = str(raw_answers.get("team_id") or raw_answers.get("runtime_team_id") or f"team_{_slugify(assistant_name)}").strip()
    appearance_payload = raw_answers.get("appearance") or raw_answers.get("ui", {}).get("onboarding", {}).get("appearance", {})
    secrets_source = normalize_secrets_source(
        raw_answers.get("secrets_source"),
        default=getattr(template, "secrets_source", SECRETS_SOURCE_ENV_FILE),
    )
    web_chat_token = str(raw_answers.get("web_chat_auth_token") or raw_answers.get("website_auth_token") or "").strip()
    if not web_chat_token and secrets_source == SECRETS_SOURCE_ENV_FILE:
        # With secrets from the environment nothing is generated: the token
        # comes from WEB_CHAT_AUTH_TOKEN (or its Docker secret).
        web_chat_token = f"uaweb_{secrets.token_urlsafe(32)}"
    template_security_layer = getattr(template, "security_layer", None)
    raw_security_layer = raw_answers.get("security_layer")
    raw_security_layer_payload = dict(raw_security_layer) if isinstance(raw_security_layer, Mapping) else {}
    security_layer_mode = normalize_security_layer_mode(
        raw_answers.get("security_layer_mode")
        or raw_security_layer_payload.get("mode")
        or getattr(template_security_layer, "mode", None)
    )
    template_network = getattr(template, "network", None)
    template_interactive_apps = getattr(template, "interactive_apps", None)
    template_internal_website = getattr(template_interactive_apps, "internal_website", None)
    template_discord = getattr(template_interactive_apps, "discord", None)
    template_telegram = getattr(template_interactive_apps, "telegram", None)
    template_signal = getattr(template_interactive_apps, "signal", None)
    template_whatsapp = getattr(template_interactive_apps, "whatsapp", None)
    template_photon = getattr(template_interactive_apps, "photon", None)

    def answer_or_template(key: str, owner: Any, attr: str, default: Any = None) -> Any:
        return _value_or_default(raw_answers.get(key), getattr(owner, attr, default))

    def string_answer(key: str) -> str:
        value = raw_answers.get(key)
        return "" if value is None else str(value).strip()

    internal_website_public_port_default = int(
        getattr(template_internal_website, "public_port", None)
        or INTERACTIVE_APP_DEFAULTS.internal_website_public_port.value
        or INTERACTIVE_APP_DEFAULTS.internal_website_bind_port.value
    )
    public_origin = str(
        raw_answers.get("public_origin")
        or raw_answers.get("network_public_origin")
        or getattr(template_network, "public_origin", NETWORK_DEFAULTS.public_origin.value)
        or NETWORK_DEFAULTS.public_origin.value
    ).strip()
    model_provider = str(
        raw_answers.get("model_provider")
        or raw_answers.get("runtime_provider")
        or getattr(template, "model_provider", "")
        or "litellm"
    )
    raw_failover = raw_answers.get("llm_failover")
    llm_failover = (
        _deep_merge_dict(getattr(template, "llm_failover", None) or LLM_DEFAULTS.failover.value, raw_failover)
        if isinstance(raw_failover, Mapping)
        else copy.deepcopy(getattr(template, "llm_failover", None) or LLM_DEFAULTS.failover.value)
    )
    raw_openai_codex = raw_answers.get("openai_codex")
    openai_codex = (
        _deep_merge_dict(getattr(template, "openai_codex", None) or LLM_DEFAULTS.openai_codex.value, raw_openai_codex)
        if isinstance(raw_openai_codex, Mapping)
        else copy.deepcopy(getattr(template, "openai_codex", None) or LLM_DEFAULTS.openai_codex.value)
    )
    raw_xai_oauth = raw_answers.get("xai_oauth")
    if isinstance(raw_xai_oauth, Mapping) and not is_xai_oauth_provider(model_provider):
        # The browser submits settings for every provider, but only the selected
        # provider owns model_endpoint. Do not let another provider's endpoint
        # overwrite the dormant xAI OAuth base URL. XAI_API_KEY is handled
        # separately as a voice credential and is intentionally unaffected.
        raw_xai_oauth = {
            key: value
            for key, value in raw_xai_oauth.items()
            if key != "base_url"
        }
    xai_oauth = (
        _deep_merge_dict(getattr(template, "xai_oauth", None) or LLM_DEFAULTS.xai_oauth.value, raw_xai_oauth)
        if isinstance(raw_xai_oauth, Mapping)
        else copy.deepcopy(getattr(template, "xai_oauth", None) or LLM_DEFAULTS.xai_oauth.value)
    )
    raw_model_capabilities = raw_answers.get("model_capabilities")
    model_capabilities = (
        copy.deepcopy(dict(raw_model_capabilities))
        if isinstance(raw_model_capabilities, Mapping)
        else copy.deepcopy(getattr(template, "model_capabilities", None) or LLM_DEFAULTS.model_capabilities.value)
    )
    security_layer_payload = _deep_merge_dict(_model_dump_or_empty(template_security_layer), raw_security_layer_payload)
    security_layer_payload["mode"] = security_layer_mode
    for answer_key, config_key in (
        ("secure_tool_broker_internal_base_url", "broker_internal_base_url"),
        ("secure_tool_broker_public_port", "broker_public_port"),
        ("secure_tool_broker_bind_host", "broker_bind_host"),
        ("secure_tool_broker_bind_port", "broker_bind_port"),
        ("secure_tool_broker_credentials_dir", "broker_credentials_dir"),
    ):
        if raw_answers.get(answer_key) not in (None, ""):
            security_layer_payload[config_key] = raw_answers.get(answer_key)
    security_layer = SecurityLayerSettings.model_validate(security_layer_payload)
    def security_answer_or_config(answer_key: str, attr: str, default: Any = None) -> Any:
        return raw_answers[answer_key] if answer_key in raw_answers else getattr(security_layer, attr, default)

    raw_activity_collector = raw_answers.get("activity_collector")
    activity_collector_payload = (
        _deep_merge_dict(_model_dump_or_empty(getattr(template, "activity_collector", None)), raw_activity_collector)
        if isinstance(raw_activity_collector, Mapping)
        else _model_dump_or_empty(getattr(template, "activity_collector", None))
    )
    activity_collector = ActivityCollectorSettings.model_validate(activity_collector_payload)
    startup_agents = _normalize_startup_agents(raw_answers)
    template_agent = getattr(template, "agent", None)
    model_id = resolve_onboarding_model_id(
        model_provider,
        raw_answers.get("model_id") or raw_answers.get("runtime_model"),
        template_provider=getattr(template, "model_provider", ""),
        template_model_id=getattr(template, "model_id", ""),
    )
    model_token = str(raw_answers.get("model_token") or raw_answers.get("runtime_token") or "").strip()
    if is_xai_oauth_provider(model_provider):
        model_token = ""
    return {
        "instance_name": instance_name,
        "instance_root": instance_root,
        "secrets_source": secrets_source,
        "team_id": team_id or getattr(template, "team_id", AGENT_DEFAULTS.default_team_id.value),
        "timezone": str(raw_answers.get("timezone") or raw_answers.get("runtime_timezone") or getattr(template, "timezone", GATEWAY_DEFAULTS.user_timezone.value)),
        "user_display_name": str(raw_answers.get("user_display_name") or raw_answers.get("display_name") or GATEWAY_DEFAULTS.user_display_name.value),
        "assistant_name": assistant_name or GATEWAY_DEFAULTS.assistant_display_name.value,
        "assistant_purpose": str(raw_answers.get("assistant_purpose") or "").strip(),
        "assistant_response_style": str(raw_answers.get("assistant_response_style") or "").strip(),
        "assistant_autonomy": str(raw_answers.get("assistant_autonomy") or "").strip(),
        "user_role": str(raw_answers.get("user_role") or "").strip(),
        "user_context": str(
            raw_answers.get("user_context")
            if "user_context" in raw_answers
            else getattr(template, "user_context", "")
        ).strip(),
        "default_delivery_channel": str(raw_answers.get("default_delivery_channel") or "website"),
        "default_delivery_chat_id": str(raw_answers.get("default_delivery_chat_id") or ""),
        "default_delivery_thread_id": str(raw_answers.get("default_delivery_thread_id") or ""),
        "request_timeout_seconds": _float_value(raw_answers.get("request_timeout_seconds"), GATEWAY_DEFAULTS.manager_request_timeout_seconds.value),
        "quiet_enabled": _bool_value(raw_answers.get("quiet_enabled"), True),
        "quiet_start": _int_value(raw_answers.get("quiet_start"), 22),
        "quiet_end": _int_value(raw_answers.get("quiet_end"), 8),
        "voice_enabled": _bool_value(raw_answers.get("voice_enabled"), VOICE_DEFAULTS.enabled.value),
        "voice_response_mode": _voice_response_mode(raw_answers.get("voice_response_mode")),
        "digest_hour": _int_value(raw_answers.get("digest_hour"), 9),
        "heartbeat_enabled": _bool_value(raw_answers.get("heartbeat_enabled"), True),
        "heartbeat_interval_seconds": _int_value(raw_answers.get("heartbeat_interval_seconds"), HEARTBEAT_DEFAULTS.interval_seconds.value),
        "heartbeat_active_hours_enabled": _bool_value(raw_answers.get("heartbeat_active_hours_enabled"), HEARTBEAT_DEFAULTS.active_hours_enabled.value),
        "heartbeat_active_start": _int_value(raw_answers.get("heartbeat_active_start"), HEARTBEAT_DEFAULTS.active_start_hour_local.value),
        "heartbeat_active_end": _int_value(raw_answers.get("heartbeat_active_end"), HEARTBEAT_DEFAULTS.active_end_hour_local.value),
        "model_provider": model_provider,
        "model_id": model_id,
        "model_endpoint": str(raw_answers.get("model_endpoint") or raw_answers.get("runtime_endpoint") or "").strip(),
        "model_token": model_token,
        "openai_codex": openai_codex,
        "xai_oauth": xai_oauth,
        "reasoning_effort": str(raw_answers.get("reasoning_effort") or getattr(template, "reasoning_effort", "") or "").strip(),
        "reasoning_summary": normalize_reasoning_summary(
            raw_answers.get("reasoning_summary", getattr(template, "reasoning_summary", None))
        ),
        "litellm_api_mode": normalize_litellm_api_mode(
            raw_answers.get(
                "litellm_api_mode",
                getattr(template, "litellm_api_mode", LLM_DEFAULTS.litellm_api_mode.value),
            )
        ),
        "context_window_tokens": _optional_int_value(
            raw_answers.get("context_window_tokens"),
            getattr(template, "llm_context_window_tokens", None),
        ),
        "stream": _optional_bool_value(raw_answers.get("stream"), getattr(template, "llm_stream", None)),
        "supports_image_input": _optional_bool_value(
            raw_answers.get("supports_image_input"),
            getattr(template, "supports_image_input", None),
        ),
        "supports_audio_input": _optional_bool_value(
            raw_answers.get("supports_audio_input"),
            getattr(template, "supports_audio_input", None),
        ),
        "supports_file_input": _optional_bool_value(
            raw_answers.get("supports_file_input"),
            getattr(template, "supports_file_input", None),
        ),
        "model_capabilities": model_capabilities,
        "llm_failover": llm_failover,
        "embedding_enabled": _bool_value(
            raw_answers.get("embedding_enabled"),
            bool(getattr(template_agent, "memory_graph_enabled", False)),
        ),
        "embedding_provider": str(answer_or_template("embedding_provider", template, "embedding_provider", "") or "").strip(),
        "embedding_model_id": str(answer_or_template("embedding_model_id", template, "embedding_model_id", "") or "").strip(),
        "embedding_api_base": str(answer_or_template("embedding_api_base", template, "embedding_api_base", "") or "").strip(),
        "embedding_api_key": string_answer("embedding_api_key"),
        "embedding_dimensions": _optional_int_value(
            raw_answers.get("embedding_dimensions"),
            getattr(template, "embedding_dimensions", None),
        ),
        "embedding_timeout_seconds": _optional_float_value(
            raw_answers.get("embedding_timeout_seconds"),
            getattr(template, "embedding_timeout_seconds", None),
        ),
        "embedding_organization": str(answer_or_template("embedding_organization", template, "embedding_organization", "") or "").strip(),
        "generate_internal_credentials": _bool_value(raw_answers.get("generate_internal_credentials"), True),
        "security_layer_mode": security_layer_mode,
        "security_layer": security_layer,
        "secure_tool_broker_internal_base_url": str(
            security_answer_or_config(
                "secure_tool_broker_internal_base_url",
                "broker_internal_base_url",
                DEFAULT_SECURE_TOOL_BROKER_INTERNAL_BASE_URL,
            )
            or DEFAULT_SECURE_TOOL_BROKER_INTERNAL_BASE_URL
        ),
        "public_origin": public_origin,
        "secure_tool_broker_public_port": security_answer_or_config(
            "secure_tool_broker_public_port",
            "broker_public_port",
        ),
        "secure_tool_broker_bind_host": security_answer_or_config(
            "secure_tool_broker_bind_host",
            "broker_bind_host",
        ),
        "secure_tool_broker_bind_port": security_answer_or_config(
            "secure_tool_broker_bind_port",
            "broker_bind_port",
        ),
        "secure_tool_broker_credentials_dir": security_answer_or_config(
            "secure_tool_broker_credentials_dir",
            "broker_credentials_dir",
        ),
        "activity_collector": activity_collector,
        "startup_agents": startup_agents,
        "internal_website_bind_host": str(
            raw_answers.get("internal_website_bind_host")
            or getattr(template_internal_website, "bind_host", INTERACTIVE_APP_DEFAULTS.internal_website_bind_host.value)
        ),
        "internal_website_bind_port": _int_value(
            raw_answers.get("internal_website_bind_port"),
            int(getattr(template_internal_website, "bind_port", INTERACTIVE_APP_DEFAULTS.internal_website_bind_port.value)),
        ),
        "internal_website_public_port": _int_value(
            raw_answers.get("internal_website_public_port") or raw_answers.get("documentation_port"),
            internal_website_public_port_default,
        ),
        "internal_website_docs_root": answer_or_template("internal_website_docs_root", template_internal_website, "docs_root"),
        "internal_website_skills_dir": answer_or_template("internal_website_skills_dir", template_internal_website, "skills_dir"),
        "discord_enabled": _bool_value(raw_answers.get("discord_enabled"), bool(getattr(template_discord, "enabled", False))),
        "discord_allowed_user_ids": _list_value(
            answer_or_template("discord_allowed_user_ids", template_discord, "allowed_user_ids", [])
        ),
        "discord_allowed_channel_ids": _list_value(
            answer_or_template("discord_allowed_channel_ids", template_discord, "allowed_channel_ids", [])
        ),
        "discord_group_policy": str(
            raw_answers.get("discord_group_policy")
            or getattr(template_discord, "group_policy", INTERACTIVE_APP_DEFAULTS.group_policy.value)
        ),
        "discord_thread_scoped_sessions": _bool_value(
            raw_answers.get("discord_thread_scoped_sessions"),
            bool(getattr(template_discord, "thread_scoped_sessions", INTERACTIVE_APP_DEFAULTS.scoped_sessions.value)),
        ),
        "discord_command_guild_ids": _list_value(
            answer_or_template("discord_command_guild_ids", template_discord, "command_guild_ids", [])
        ),
        "telegram_enabled": _bool_value(raw_answers.get("telegram_enabled"), bool(getattr(template_telegram, "enabled", False))),
        "telegram_allowed_user_ids": _list_value(
            answer_or_template("telegram_allowed_user_ids", template_telegram, "allowed_user_ids", [])
        ),
        "telegram_allowed_chat_ids": _list_value(
            answer_or_template("telegram_allowed_chat_ids", template_telegram, "allowed_chat_ids", [])
        ),
        "telegram_group_policy": str(
            raw_answers.get("telegram_group_policy")
            or getattr(template_telegram, "group_policy", INTERACTIVE_APP_DEFAULTS.group_policy.value)
        ),
        "telegram_manager_offline_policy": str(
            raw_answers.get("telegram_manager_offline_policy")
            or getattr(
                template_telegram,
                "manager_offline_policy",
                INTERACTIVE_APP_DEFAULTS.telegram_manager_offline_policy.value,
            )
        ),
        "telegram_topic_scoped_sessions": _bool_value(
            raw_answers.get("telegram_topic_scoped_sessions"),
            bool(getattr(template_telegram, "topic_scoped_sessions", INTERACTIVE_APP_DEFAULTS.scoped_sessions.value)),
        ),
        "telegram_drop_pending_updates": _bool_value(
            raw_answers.get("telegram_drop_pending_updates"),
            bool(getattr(template_telegram, "drop_pending_updates", INTERACTIVE_APP_DEFAULTS.drop_pending_updates.value)),
        ),
        "telegram_rich_messages": _bool_value(
            raw_answers.get("telegram_rich_messages"),
            bool(getattr(template_telegram, "rich_messages", True)),
        ),
        "signal_enabled": _bool_value(raw_answers.get("signal_enabled"), bool(getattr(template_signal, "enabled", False))),
        "signal_base_url": str(
            raw_answers.get("signal_base_url")
            or getattr(template_signal, "base_url", INTERACTIVE_APP_DEFAULTS.signal_base_url.value)
        ),
        "signal_account": str(
            raw_answers.get("signal_account")
            or getattr(template_signal, "account", "")
            or ""
        ),
        "signal_allowed_sender_ids": _list_value(
            answer_or_template("signal_allowed_sender_ids", template_signal, "allowed_sender_ids", [])
        ),
        "signal_allowed_group_ids": _list_value(
            answer_or_template("signal_allowed_group_ids", template_signal, "allowed_group_ids", [])
        ),
        "signal_group_policy": str(
            raw_answers.get("signal_group_policy")
            or getattr(template_signal, "group_policy", INTERACTIVE_APP_DEFAULTS.signal_group_policy.value)
        ),
        "signal_mention_markers": _list_value(
            answer_or_template("signal_mention_markers", template_signal, "mention_markers", [])
        ),
        "signal_poll_timeout_seconds": _float_value(
            raw_answers.get("signal_poll_timeout_seconds"),
            float(getattr(template_signal, "poll_timeout_seconds", INTERACTIVE_APP_DEFAULTS.signal_poll_timeout_seconds.value)),
        ),
        "signal_max_messages_per_poll": _int_value(
            raw_answers.get("signal_max_messages_per_poll"),
            int(getattr(template_signal, "max_messages_per_poll", INTERACTIVE_APP_DEFAULTS.signal_max_messages_per_poll.value)),
        ),
        "signal_drop_pending_messages_on_startup": _bool_value(
            raw_answers.get("signal_drop_pending_messages_on_startup"),
            bool(
                getattr(
                    template_signal,
                    "drop_pending_messages_on_startup",
                    INTERACTIVE_APP_DEFAULTS.signal_drop_pending_messages_on_startup.value,
                )
            ),
        ),
        "photon_enabled": _bool_value(raw_answers.get("photon_enabled"), bool(getattr(template_photon, "enabled", False))),
        "photon_project_id": str(
            raw_answers.get("photon_project_id")
            or getattr(template_photon, "project_id", "")
            or ""
        ).strip(),
        "photon_allowed_sender_ids": _list_value(
            answer_or_template("photon_allowed_sender_ids", template_photon, "allowed_sender_ids", [])
        ),
        "photon_allowed_group_ids": _list_value(
            answer_or_template("photon_allowed_group_ids", template_photon, "allowed_group_ids", [])
        ),
        "photon_group_policy": str(
            raw_answers.get("photon_group_policy")
            or getattr(template_photon, "group_policy", INTERACTIVE_APP_DEFAULTS.photon_group_policy.value)
        ),
        "photon_mention_markers": _list_value(
            answer_or_template("photon_mention_markers", template_photon, "mention_markers", [])
        ),
        "whatsapp_enabled": _bool_value(raw_answers.get("whatsapp_enabled"), bool(getattr(template_whatsapp, "enabled", False))),
        "whatsapp_allowed_sender_ids": _list_value(
            answer_or_template("whatsapp_allowed_sender_ids", template_whatsapp, "allowed_sender_ids", [])
        ),
        "whatsapp_webhook_host": str(
            raw_answers.get("whatsapp_webhook_host")
            or getattr(template_whatsapp, "webhook_host", INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_host.value)
        ),
        "whatsapp_webhook_port": _int_value(
            raw_answers.get("whatsapp_webhook_port"),
            int(getattr(template_whatsapp, "webhook_port", INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_port.value)),
        ),
        "whatsapp_webhook_path": str(
            raw_answers.get("whatsapp_webhook_path")
            or getattr(template_whatsapp, "webhook_path", INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_path.value)
        ),
        "whatsapp_public_base_url": str(
            raw_answers.get("whatsapp_public_base_url")
            or getattr(template_whatsapp, "public_base_url", "")
            or ""
        ),
        "whatsapp_validate_twilio_signature": _bool_value(
            raw_answers.get("whatsapp_validate_twilio_signature"),
            bool(getattr(template_whatsapp, "validate_twilio_signature", INTERACTIVE_APP_DEFAULTS.validate_twilio_signature.value)),
        ),
        "appearance": OnboardingAppearanceSettings.model_validate(appearance_payload),
        "web_chat_auth_token": web_chat_token,
        "account_sharing": _account_sharing_answer(raw_answers.get("account_sharing")),
        "exa_api_key": string_answer("exa_api_key"),
        "brave_search_api_key": string_answer("brave_search_api_key"),
        "xai_api_key": string_answer("xai_api_key"),
        "telegram_bot_token": string_answer("telegram_bot_token"),
        "discord_bot_token": string_answer("discord_bot_token"),
        "photon_project_secret": string_answer("photon_project_secret"),
        "crawl4ai_mcp_server_url": string_answer("crawl4ai_mcp_server_url"),
        "postgres_dsn": string_answer("postgres_dsn"),
        "searxng_base_url": string_answer("searxng_base_url"),
        "searxng_verify_tls": string_answer("searxng_verify_tls"),
        "searxng_timeout_seconds": string_answer("searxng_timeout_seconds"),
        "searxng_default_num_results": string_answer("searxng_default_num_results"),
    }


def _normalize_startup_agents(raw_answers: Mapping[str, Any]) -> list[StartupAgentSettings]:
    direct = raw_answers.get("startup_agents")
    if isinstance(direct, list):
        agents: list[StartupAgentSettings] = []
        for item in direct:
            if not isinstance(item, Mapping):
                continue
            setting = StartupAgentSettings.model_validate(item)
            if setting.enabled and setting.named_agent_type != TEAM_ARCHITECT_AGENT_TYPE:
                agents.append(setting)
        return ensure_required_startup_agents(agents)

    selected = raw_answers.get("selected_agents")
    counts = raw_answers.get("selected_agent_counts")
    selected_by_type = selected if isinstance(selected, Mapping) else {}
    counts_by_type = counts if isinstance(counts, Mapping) else {}
    agents: list[StartupAgentSettings] = []
    for agent_type in [str(key).strip() for key in selected_by_type if str(key).strip()]:
        if agent_type == TEAM_ARCHITECT_AGENT_TYPE:
            continue
        if not _bool_value(selected_by_type.get(agent_type), False):
            continue
        count = _int_value(counts_by_type.get(agent_type), 1)
        if count < 1:
            continue
        agents.append(
            StartupAgentSettings(
                named_agent_type=agent_type,
                count=count,
                enabled=True,
            )
        )
    return ensure_required_startup_agents(agents)


def create_onboarded_config(
    *,
    answers: Mapping[str, Any],
    template: OnboardingTemplate,
    options: OnboardingWriteOptions | None = None,
) -> GatewayInstanceConfig:
    options = options or OnboardingWriteOptions()
    config = GatewayInstanceConfig.create(
        instance_name=str(answers["instance_name"]),
        instance_root=str(answers["instance_root"]),
        team_id=str(answers["team_id"]),
        timezone=str(answers["timezone"]),
        response_queue_name=options.response_queue_name,
        manager_agent_id=options.manager_agent_id,
        request_timeout_seconds=float(answers["request_timeout_seconds"]),
        manager_storage_backend=template.manager_storage_backend,
        manager_storage_sqlite_path=template.manager_storage_sqlite_path,
        manager_storage_postgres_dsn=template.manager_storage_postgres_dsn,
        heartbeat_enabled=bool(answers["heartbeat_enabled"]),
        heartbeat_interval_seconds=int(answers["heartbeat_interval_seconds"]),
        quiet_hours_enabled=bool(answers["quiet_enabled"]),
        quiet_start_hour_local=int(answers["quiet_start"]),
        quiet_end_hour_local=int(answers["quiet_end"]),
        digest_delivery_hour_local=int(answers["digest_hour"]),
        display_name=str(answers["user_display_name"]),
        assistant_name=str(answers["assistant_name"]),
        initial_user_context=str(answers.get("user_context") or ""),
    )
    apply_onboarding_template(config, template)
    _apply_answers_to_config(config, answers)
    return GatewayInstanceConfig.model_validate(config.model_dump(mode="json"))


def write_onboarded_instance(
    *,
    answers: Mapping[str, Any],
    template: OnboardingTemplate,
    options: OnboardingWriteOptions | None = None,
) -> OnboardingWriteResult:
    options = options or OnboardingWriteOptions()
    config = create_onboarded_config(answers=answers, template=template, options=options)
    config_path = config.config_path
    existing_config_found = config_path.exists()
    before_config = read_yaml_mapping(config_path)
    if config.launch.secrets_from_environment:
        return _write_instance_with_environment_secrets(
            config,
            answers=answers,
            options=options,
            existing_config_found=existing_config_found,
            before_config=before_config,
        )
    env_path = Path(config.launch.env_file_path or default_launch_env_file(config.instance_root)).expanduser().resolve()
    before_env = parse_env_file(env_path)
    created_paths = _bootstrap_onboarded_instance(
        config, answers, source_env_file_path=options.source_env_file_path
    )
    _merge_user_secret_env(env_path, answers)
    if options.generate_internal_credentials:
        preserve_env_keys = _user_supplied_env_keys(answers, options.source_env_file_path)
        generated_env = ensure_internal_credential_env_file(
            config,
            existing_postgres_dsn=template.manager_storage_postgres_dsn,
            preserve_env_keys=preserve_env_keys,
            rotate=options.rotate_internal_credentials,
        )
        if generated_env not in created_paths:
            created_paths.append(generated_env)
        _append_new_paths(
            created_paths,
            ensure_instance_sealbox_material(
                config,
                rotate_token=options.rotate_internal_credentials,
            ),
        )

    seed_default_gateway_user(config, env=parse_env_file(env_path))
    report = _write_report(
        config,
        existing_config_found=existing_config_found,
        before_config=before_config,
        before_env=before_env,
        after_env=parse_env_file(env_path),
        created_paths=created_paths,
    )
    return OnboardingWriteResult(
        config=config,
        report=report,
        env_path=env_path,
        created_paths=tuple(created_paths),
    )


def _write_instance_with_environment_secrets(
    config: GatewayInstanceConfig,
    *,
    answers: Mapping[str, Any],
    options: OnboardingWriteOptions,
    existing_config_found: bool,
    before_config: Mapping[str, Any],
) -> OnboardingWriteResult:
    """Write an instance whose secrets come only from the environment.

    No team.env is written, nothing is generated and a source .env is not
    merged anywhere (an uploaded one may only have prefilled form fields). What onboarding
    itself needs (the storage settings used to create the first user) comes
    from the process environment, and the result names every required secret
    the environment lacks, so the person can add it as a Docker secret.
    Managed Sealbox material is still created: it is the broker's own store
    under the credentials folder, not a secret the environment supplies.
    """

    if options.source_env_file_path:
        # Callers leave it out in this mode (the web page reports an upload as
        # not saved; the CLI refuses --base-env-file); never merge it.
        LOGGER.warning(
            "A source .env was given, but secrets come from the environment; it was not saved. "
            "event=onboarding_source_env_ignored instance_name=%s",
            config.instance_name,
        )
    created_paths = _bootstrap_onboarded_instance(config, answers, source_env_file_path=None)
    _append_new_paths(created_paths, ensure_instance_sealbox_material(config))
    env = environment_secret_values()
    seed_default_gateway_user(config, env=env)
    unsaved = tuple(_entered_env_names(answers))
    missing = missing_secrets(config, env=env, entered_names=unsaved)
    LOGGER.info(
        "Onboarding wrote no team.env; secrets come from the environment. "
        "event=onboarding_secrets_from_environment instance_name=%s missing_count=%s "
        "missing_names=%s unsaved_names=%s",
        config.instance_name,
        len(missing),
        ",".join(item.name for item in missing),
        ",".join(unsaved),
    )
    report = _write_report(
        config,
        existing_config_found=existing_config_found,
        before_config=before_config,
        before_env={},
        after_env={},
        created_paths=created_paths,
    )
    return OnboardingWriteResult(
        config=config,
        report=report,
        env_path=None,
        created_paths=tuple(created_paths),
        missing_secrets=missing,
        unsaved_secret_names=unsaved,
    )


def _bootstrap_onboarded_instance(
    config: GatewayInstanceConfig,
    answers: Mapping[str, Any],
    *,
    source_env_file_path: Optional[str],
) -> list[Path]:
    created_paths = bootstrap_instance(
        config,
        source_env_file_path=source_env_file_path,
        assistant_purpose=str(answers.get("assistant_purpose") or "").strip() or None,
        assistant_response_style=str(answers.get("assistant_response_style") or "").strip() or None,
        assistant_autonomy=str(answers.get("assistant_autonomy") or "").strip() or None,
    )
    # Typed keys replace keys an imported env file handed over (bootstrap_instance).
    _append_new_paths(
        created_paths,
        hand_over_onboarding_keys(
            config,
            keys_from_answers(answers),
            model_keys=model_keys_from_answers(config, answers),
            sharing=sharing_from_answers(answers),
        ),
    )
    return created_paths


def _append_new_paths(created_paths: list[Path], paths: tuple[Path, ...]) -> None:
    for path in paths:
        if path not in created_paths:
            created_paths.append(path)


def _write_report(
    config: GatewayInstanceConfig,
    *,
    existing_config_found: bool,
    before_config: Mapping[str, Any],
    before_env: Mapping[str, str],
    after_env: Mapping[str, str],
    created_paths: list[Path],
) -> OnboardingReport:
    after_config = read_yaml_mapping(config.config_path)
    config_added, config_updated, config_removed = diff_config_paths(dict(before_config), after_config)
    env_added, env_updated, env_removed = diff_env_keys(dict(before_env), dict(after_env))
    report_path = onboarding_report_detail_path(config.instance_root)
    report = OnboardingReport(
        mode="updated" if existing_config_found else "created",
        config_path=config.config_path,
        detail_path=report_path,
        config_added_paths=config_added,
        config_updated_paths=config_updated,
        config_removed_paths=config_removed,
        env_added_keys=env_added,
        env_updated_keys=env_updated,
        env_removed_keys=env_removed,
        created_paths=tuple(created_paths + [report_path]),
    )
    write_onboarding_report(report)
    return report


# Whole settings sections a new instance takes from the base config unchanged.
# The person's answers are applied on top afterwards.
_TEMPLATE_SECTIONS = (
    "delivery",
    "agent",
    "workspace",
    "security_layer",
    "network",
    "interactive_apps",
    "activity_collector",
    "voice",
    "ui",
    "execution_control",
    "tool_policy",
)


def apply_onboarding_template(config: GatewayInstanceConfig, template: OnboardingTemplate) -> None:
    """Copy the base config's settings from ``template`` into a new instance config.

    The web onboarding (``create_onboarded_config``) and the command line
    (``assistant_cli._onboard``) both call this before applying the person's
    answers, so both carry the same sections: the ones in
    ``_TEMPLATE_SECTIONS``, the gateway user settings, agent-type overrides,
    MCP profiles, and the model and embedding settings.
    """

    for section in _TEMPLATE_SECTIONS:
        setattr(config, section, getattr(template, section).model_copy(deep=True))
    config.agent_types = {
        agent_type: settings.model_copy(deep=True)
        for agent_type, settings in template.agent_types.items()
    }
    config.mcp_profiles = dict(template.mcp_profiles)
    config.gateway.users = template.gateway_users.model_copy(deep=True)
    _apply_template_llm(config, template)
    _apply_template_embedding(config, template)


def _apply_template_llm(config: GatewayInstanceConfig, template: OnboardingTemplate) -> None:
    config.llm.provider = str(template.model_provider or "")
    config.llm.model_id = str(template.model_id or "")
    config.llm.temperature = template.llm_temperature
    config.llm.reasoning_effort = str(template.reasoning_effort or "").strip() or None
    config.llm.reasoning_summary = normalize_reasoning_summary(template.reasoning_summary)
    config.llm.context_window_tokens = template.llm_context_window_tokens
    config.llm.max_output_tokens = template.llm_max_output_tokens
    config.llm.stream = template.llm_stream
    config.llm.litellm_api_mode = normalize_litellm_api_mode(template.litellm_api_mode)
    config.llm.request_timeout_seconds = float(template.llm_request_timeout_seconds)
    config.llm.supports_image_input = template.supports_image_input
    config.llm.supports_audio_input = template.supports_audio_input
    config.llm.supports_file_input = template.supports_file_input
    config.llm.model_capabilities = copy.deepcopy(
        template.model_capabilities or LLM_DEFAULTS.model_capabilities.value
    )
    config.llm.reasoning_trace_retention_messages = template.reasoning_trace_retention_messages
    config.llm.litellm_api_base = str(template.litellm_api_base or "") or None
    config.llm.openai_codex = OpenAICodexSettings.model_validate(
        copy.deepcopy(template.openai_codex or LLM_DEFAULTS.openai_codex.value)
    )
    config.llm.xai_oauth = XAIOAuthSettings.model_validate(
        copy.deepcopy(template.xai_oauth or LLM_DEFAULTS.xai_oauth.value)
    )
    config.llm.compaction_model_provider = str(template.compaction_model_provider or "") or None
    config.llm.compaction_model_id = str(template.compaction_model_id or "") or None
    config.llm.failover = copy.deepcopy(template.llm_failover or LLM_DEFAULTS.failover.value)


def _apply_template_embedding(config: GatewayInstanceConfig, template: OnboardingTemplate) -> None:
    config.embedding.provider = str(template.embedding_provider or "") or None
    config.embedding.model_id = str(template.embedding_model_id or "") or None
    config.embedding.api_base = str(template.embedding_api_base or "") or None
    config.embedding.dimensions = template.embedding_dimensions
    config.embedding.timeout_seconds = template.embedding_timeout_seconds
    config.embedding.organization = str(template.embedding_organization or "") or None
    config.embedding.extra_headers = dict(template.embedding_extra_headers or {})


def _apply_answers_to_config(config: GatewayInstanceConfig, answers: Mapping[str, Any]) -> None:
    security_layer = answers.get("security_layer")
    if security_layer is not None:
        config.security_layer = security_layer.model_copy(deep=True)
    activity_collector = answers.get("activity_collector")
    if activity_collector is not None:
        config.activity_collector = activity_collector.model_copy(deep=True)
    config.user.role = str(answers.get("user_role") or "")
    config.user.initial_context = str(answers.get("user_context") or "").strip()
    config.delivery.quiet_hours.enabled = bool(answers["quiet_enabled"])
    config.delivery.quiet_hours.start_hour_local = int(answers["quiet_start"])
    config.delivery.quiet_hours.end_hour_local = int(answers["quiet_end"])
    config.delivery.digest_delivery_hour_local = int(answers["digest_hour"])
    config.delivery.default_target.channel_name = str(answers["default_delivery_channel"])
    config.delivery.default_target.chat_id = str(answers["default_delivery_chat_id"])
    config.delivery.default_target.thread_id = str(answers["default_delivery_thread_id"])
    config.heartbeat.active_hours_enabled = bool(answers["heartbeat_active_hours_enabled"])
    config.heartbeat.active_start_hour_local = int(answers["heartbeat_active_start"])
    config.heartbeat.active_end_hour_local = int(answers["heartbeat_active_end"])
    config.llm.provider = _normalize_model_provider(str(answers["model_provider"]))
    config.llm.model_id = str(answers["model_id"])
    config.llm.openai_codex = OpenAICodexSettings.model_validate(
        copy.deepcopy(answers.get("openai_codex") or LLM_DEFAULTS.openai_codex.value)
    )
    config.llm.xai_oauth = XAIOAuthSettings.model_validate(
        copy.deepcopy(answers.get("xai_oauth") or LLM_DEFAULTS.xai_oauth.value)
    )
    if str(answers.get("model_endpoint") or "").strip():
        if config.llm.provider == "litellm":
            config.llm.litellm_api_base = str(answers["model_endpoint"]).strip()
        elif config.llm.provider == "openai-codex":
            config.llm.openai_codex.base_url = str(answers["model_endpoint"]).strip()
        elif config.llm.provider == "xai-oauth":
            config.llm.xai_oauth.base_url = str(answers["model_endpoint"]).strip()
        else:
            config.llm.litellm_api_base = str(answers["model_endpoint"]).strip()
    config.llm.reasoning_effort = str(answers.get("reasoning_effort") or "").strip() or None
    config.llm.reasoning_summary = normalize_reasoning_summary(
        answers.get("reasoning_summary")
    )
    config.llm.context_window_tokens = answers.get("context_window_tokens")
    config.llm.stream = answers.get("stream")
    config.llm.litellm_api_mode = normalize_litellm_api_mode(
        answers.get("litellm_api_mode")
    )
    config.llm.supports_image_input = answers.get("supports_image_input")
    config.llm.supports_audio_input = answers.get("supports_audio_input")
    config.llm.supports_file_input = answers.get("supports_file_input")
    config.llm.model_capabilities = copy.deepcopy(
        answers.get("model_capabilities") or LLM_DEFAULTS.model_capabilities.value
    )
    config.llm.failover = copy.deepcopy(answers.get("llm_failover") or LLM_DEFAULTS.failover.value)
    config.agent.memory_graph_enabled = bool(answers.get("embedding_enabled", False))
    if config.agent.memory_graph_enabled:
        config.embedding.provider = str(answers.get("embedding_provider") or "").strip() or None
        config.embedding.model_id = str(answers.get("embedding_model_id") or "").strip() or None
        config.embedding.api_base = str(answers.get("embedding_api_base") or "").strip() or None
        config.embedding.dimensions = answers.get("embedding_dimensions")
        config.embedding.timeout_seconds = answers.get("embedding_timeout_seconds")
        config.embedding.organization = str(answers.get("embedding_organization") or "").strip() or None
    else:
        config.embedding.provider = None
        config.embedding.model_id = None
        config.embedding.api_base = None
        config.embedding.dimensions = None
        config.embedding.timeout_seconds = None
        config.embedding.organization = None
        config.embedding.extra_headers = {}
    config.security_layer.mode = normalize_security_layer_mode(answers.get("security_layer_mode"))
    config.security_layer.broker_internal_base_url = str(answers.get("secure_tool_broker_internal_base_url") or "").strip() or None
    config.network.public_origin = str(answers.get("public_origin") or NETWORK_DEFAULTS.public_origin.value).strip()
    if answers.get("secure_tool_broker_public_port") not in (None, ""):
        config.security_layer.broker_public_port = int(answers["secure_tool_broker_public_port"])
    config.security_layer.broker_bind_host = str(answers.get("secure_tool_broker_bind_host") or "").strip() or None
    if answers.get("secure_tool_broker_bind_port") not in (None, ""):
        config.security_layer.broker_bind_port = int(answers["secure_tool_broker_bind_port"])
    config.security_layer.broker_credentials_dir = str(answers.get("secure_tool_broker_credentials_dir") or "").strip() or None
    config.voice.enabled = bool(answers.get("voice_enabled", False))
    config.voice.response_mode = str(answers.get("voice_response_mode") or VOICE_DEFAULTS.response_mode.value)
    config.voice.input_enabled = bool(answers.get("voice_enabled", False))
    config.voice.output_enabled = bool(answers.get("voice_enabled", False)) and config.voice.response_mode != "text_only"
    config.interactive_apps.internal_website.enabled = True
    config.interactive_apps.internal_website.bind_host = str(answers["internal_website_bind_host"]).strip()
    config.interactive_apps.internal_website.bind_port = int(answers["internal_website_bind_port"])
    config.interactive_apps.internal_website.public_port = int(answers["internal_website_public_port"])
    config.interactive_apps.internal_website.docs_root = str(answers.get("internal_website_docs_root") or "").strip() or None
    config.interactive_apps.internal_website.skills_dir = str(answers.get("internal_website_skills_dir") or "").strip() or None
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = WEB_CHAT_AUTH_TOKEN_ENV
    config.interactive_apps.discord.enabled = bool(answers["discord_enabled"])
    config.interactive_apps.discord.allowed_user_ids = list(answers["discord_allowed_user_ids"])
    config.interactive_apps.discord.allowed_channel_ids = list(answers["discord_allowed_channel_ids"])
    config.interactive_apps.discord.group_policy = str(answers["discord_group_policy"])
    config.interactive_apps.discord.thread_scoped_sessions = bool(answers["discord_thread_scoped_sessions"])
    config.interactive_apps.discord.command_guild_ids = list(answers["discord_command_guild_ids"])
    config.interactive_apps.telegram.enabled = bool(answers["telegram_enabled"])
    config.interactive_apps.telegram.allowed_user_ids = list(answers["telegram_allowed_user_ids"])
    config.interactive_apps.telegram.allowed_chat_ids = list(answers["telegram_allowed_chat_ids"])
    config.interactive_apps.telegram.group_policy = str(answers["telegram_group_policy"])
    config.interactive_apps.telegram.manager_offline_policy = str(answers["telegram_manager_offline_policy"])
    config.interactive_apps.telegram.topic_scoped_sessions = bool(answers["telegram_topic_scoped_sessions"])
    config.interactive_apps.telegram.drop_pending_updates = bool(answers["telegram_drop_pending_updates"])
    config.interactive_apps.telegram.rich_messages = bool(answers["telegram_rich_messages"])
    config.interactive_apps.signal.enabled = bool(answers["signal_enabled"])
    config.interactive_apps.signal.base_url = str(answers["signal_base_url"])
    config.interactive_apps.signal.account = str(answers["signal_account"])
    config.interactive_apps.signal.allowed_sender_ids = list(answers["signal_allowed_sender_ids"])
    config.interactive_apps.signal.allowed_group_ids = list(answers["signal_allowed_group_ids"])
    config.interactive_apps.signal.group_policy = str(answers["signal_group_policy"])
    config.interactive_apps.signal.mention_markers = list(answers["signal_mention_markers"])
    config.interactive_apps.signal.poll_timeout_seconds = float(answers["signal_poll_timeout_seconds"])
    config.interactive_apps.signal.max_messages_per_poll = int(answers["signal_max_messages_per_poll"])
    config.interactive_apps.signal.drop_pending_messages_on_startup = bool(
        answers["signal_drop_pending_messages_on_startup"]
    )
    config.interactive_apps.photon.enabled = bool(answers["photon_enabled"])
    config.interactive_apps.photon.project_id = str(answers["photon_project_id"]).strip()
    config.interactive_apps.photon.allowed_sender_ids = list(answers["photon_allowed_sender_ids"])
    config.interactive_apps.photon.allowed_group_ids = list(answers["photon_allowed_group_ids"])
    config.interactive_apps.photon.group_policy = str(answers["photon_group_policy"])
    config.interactive_apps.photon.mention_markers = list(answers["photon_mention_markers"])
    config.interactive_apps.whatsapp.enabled = bool(answers["whatsapp_enabled"])
    config.interactive_apps.whatsapp.allowed_sender_ids = list(answers["whatsapp_allowed_sender_ids"])
    config.interactive_apps.whatsapp.webhook_host = str(answers["whatsapp_webhook_host"])
    config.interactive_apps.whatsapp.webhook_port = int(answers["whatsapp_webhook_port"])
    config.interactive_apps.whatsapp.webhook_path = str(answers["whatsapp_webhook_path"])
    config.interactive_apps.whatsapp.public_base_url = str(answers["whatsapp_public_base_url"]).strip() or None
    config.interactive_apps.whatsapp.validate_twilio_signature = bool(answers["whatsapp_validate_twilio_signature"])
    config.ui.onboarding.appearance = answers["appearance"]
    secrets_source = str(answers.get("secrets_source") or SECRETS_SOURCE_ENV_FILE)
    config.launch.secrets_source = secrets_source  # type: ignore[assignment]  # normalized in normalize_onboarding_answers
    config.launch.env_file_path = launch_env_file_for(config.instance_root, secrets_source)
    config.gateway.startup_agents = [agent.model_copy(deep=True) for agent in answers.get("startup_agents", [])]

    for agent_type in {agent.named_agent_type for agent in config.gateway.startup_agents}:
        _ensure_agent_type(config, agent_type)


def _ensure_agent_type(config: GatewayInstanceConfig, agent_type: str) -> None:
    config.agent_types.setdefault(agent_type, AgentTypeRuntimeSettings())


def _merge_user_secret_env(env_path: Path, answers: Mapping[str, Any]) -> None:
    values: dict[str, str] = {
        WEB_CHAT_AUTH_TOKEN_ENV: str(answers["web_chat_auth_token"]),
    }
    for key, answer_key in USER_ENV_ANSWER_MAPPINGS:
        raw_value = answers.get(answer_key)
        value = "" if raw_value is None else str(raw_value).strip()
        if value:
            values[key] = value
    merge_env_file(env_path, values, rotate=False)


def _answered_env_keys(answers: Mapping[str, Any]) -> list[str]:
    return [
        env_key
        for env_key, answer_key in USER_ENV_ANSWER_MAPPINGS
        if ("" if answers.get(answer_key) is None else str(answers.get(answer_key)).strip())
    ]


def _entered_env_names(answers: Mapping[str, Any]) -> list[str]:
    """Environment names the person gave a value for, which team.env would have held."""

    return [key for key in _answered_env_keys(answers) if key not in CONFIG_HELD_ENV_KEYS]


def _user_supplied_env_keys(answers: Mapping[str, Any], source_env_file_path: str | None) -> set[str]:
    keys = set(_answered_env_keys(answers))
    if source_env_file_path:
        source_env_path = Path(source_env_file_path).expanduser()
        if source_env_path.exists():
            keys.update(parse_env_file(source_env_path))
    return keys


def _slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return slug or "assistant"


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return int(default)


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return float(default)


def _optional_int_value(value: Any, default: int | None = None) -> int | None:
    raw = default if value is None else value
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _optional_float_value(value: Any, default: float | None = None) -> float | None:
    raw = default if value is None else value
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _value_or_default(raw: Any, default: Any) -> Any:
    return default if raw is None else raw


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_bool_value(value: Any, default: bool | None = None) -> bool | None:
    raw = default if value is None else value
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _voice_response_mode(value: Any) -> str:
    normalized = str(value or VOICE_DEFAULTS.response_mode.value).strip().lower()
    if normalized in {"voice_only", "voice_and_text", "text_only"}:
        return normalized
    return VOICE_DEFAULTS.response_mode.value


def _normalize_model_provider(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"openrouter", "openai compatible"}:
        return "litellm"
    if normalized in {"codex", "openai-codex", "openai codex oauth", "openai subscription"}:
        return "openai-codex"
    if is_xai_oauth_provider(normalized):
        return normalize_xai_oauth_provider(normalized)
    if normalized == "google ai":
        return "gemini"
    return normalized or "litellm"
