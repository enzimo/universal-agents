"""Environment configuration and contracts."""

from __future__ import annotations
import json
import os
from typing import Any, Dict
from runtime_settings.code_sandbox_contract import (
    ENV_CODE_SANDBOX_EXTRA_READ_PATHS,
    ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS,
    ENV_CODE_SANDBOX_MODE,
)
from default_values import (
    EXECUTION_CONTROL_DEFAULTS,
    TOOL_POLICY_DEFAULTS,
    TRANSPORT_DEFAULTS,
    WEB_SERVICES_DEFAULTS,
)
from runtime_settings.governance import reject_removed_file_access_env_vars
from runtime_settings.network import reject_removed_public_url_env_vars
from runtime_settings.security import (
    reject_removed_credential_catalog_env_vars,
    reject_removed_sealbox_env_vars,
    reject_removed_secure_cli_env_var,
    reject_removed_tool_credential_env_vars,
)
from runtime_settings.services import CRAWL4AI_BASE_URL_ENV, reject_removed_web_service_env_vars
from runtime_settings.interactive_apps import (
    default_photon_app_config,
    default_signal_app_config,
)
from env_secrets import get_env_with_file_fallback
from runtime_settings.values import (
    DEFAULT_AUTHORIZATION_CONFIG,
    _mapping_or_empty,
    deep_merge_dicts,
    parse_bool_env_value,
)


def _apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Override config values with environment variables.

    Environment variable mapping:
    - OPENAI_API_KEY -> runtime OpenAI API key setting
    - GEMINI_API_KEY -> runtime Gemini API key setting
    - LITELLM_API_KEY -> runtime LiteLLM API key setting
    - OPENAI_CODEX_ACCESS_TOKEN -> runtime OpenAI Codex OAuth access token
    - EMBEDDING_API_KEY -> runtime embedding API key setting
    - LLM_PROVIDER -> llm.provider
    - LLM_MODEL_ID -> llm.model_id
    - LLM_STREAM -> llm.stream
    - OPENAI_CODEX_* -> llm.openai_codex non-secret settings
    - XAI_OAUTH_* -> llm.xai_oauth non-secret settings and ephemeral bearer override
    - AGENT_ID -> agent.agent_id
    - DEFAULT_TEAM_ID -> agent.default_team_id
    - DEFAULT_REQUEST_TIMEZONE -> agent.default_request_timezone
    - TASK_HISTORY_DB_PATH -> storage.sqlite.path
    - TEAM_INSTANCE_ROOT -> agent.instance_root (control/, dbs/, state/ and
      workspaces/ are derived from it)
    - AGENT_RESULT_TRANSFORMER -> agent.result_transformer
    - AGENT_DEBUG_TERMINAL_REASONING_ENABLED -> agent.debug.terminal_reasoning.enabled
    - CONVERSATION_COMPACTOR_TYPE -> agent.conversation_compactor.type
    """
    reject_removed_public_url_env_vars()
    reject_removed_file_access_env_vars()
    reject_removed_sealbox_env_vars()
    reject_removed_credential_catalog_env_vars()
    reject_removed_tool_credential_env_vars()
    reject_removed_secure_cli_env_var()
    reject_removed_web_service_env_vars()
    # Ensure nested dicts exist
    if "transport" not in config:
        config["transport"] = {}
    if "network" not in config:
        config["network"] = {}
    if "llm" not in config:
        config["llm"] = {}
    if "agent" not in config:
        config["agent"] = {}
    if "workspace" not in config:
        config["workspace"] = {}
    if "storage" not in config:
        config["storage"] = {}
    if "embedding" not in config:
        config["embedding"] = {}
    if "activity_collector" not in config:
        config["activity_collector"] = {}
    if "otel" not in config:
        config["otel"] = {}
    if "security_layer" not in config:
        config["security_layer"] = {}
    if "interactive_apps" not in config:
        config["interactive_apps"] = {}
    config["execution_control"] = deep_merge_dicts(
        {
            "enabled": EXECUTION_CONTROL_DEFAULTS.enabled.value,
            "runner": dict(EXECUTION_CONTROL_DEFAULTS.runner.value),
            "artifact_store": dict(EXECUTION_CONTROL_DEFAULTS.artifact_store.value),
            "profiles": [],
            "kubernetes": dict(EXECUTION_CONTROL_DEFAULTS.kubernetes.value),
            "ssh": dict(EXECUTION_CONTROL_DEFAULTS.ssh.value),
        },
        _mapping_or_empty(config.get("execution_control")),
    )
    config["web_services"] = deep_merge_dicts(
        {
            "crawl4ai": dict(WEB_SERVICES_DEFAULTS.crawl4ai.value),
            "searxng": dict(WEB_SERVICES_DEFAULTS.searxng.value),
        },
        _mapping_or_empty(config.get("web_services")),
    )
    interactive_app_plugins = config["interactive_apps"].setdefault("plugins", {})
    interactive_app_plugins.setdefault(
        "builtin.internal_website",
        {"settings": {}},
    )
    interactive_app_plugins.setdefault(
        "builtin.signal",
        {"settings": default_signal_app_config()},
    )
    interactive_app_plugins.setdefault(
        "builtin.photon",
        {"settings": default_photon_app_config()},
    )

    config["tool_policy"] = deep_merge_dicts(
        {"scratch_retention_days": TOOL_POLICY_DEFAULTS.scratch_retention_days.value},
        _mapping_or_empty(config.get("tool_policy")),
    )
    config["authorization"] = deep_merge_dicts(
        DEFAULT_AUTHORIZATION_CONFIG,
        _mapping_or_empty(config.get("authorization")),
    )

    crawl4ai = dict(config["web_services"].get("crawl4ai") or {})
    if env_value := os.getenv(CRAWL4AI_BASE_URL_ENV):
        crawl4ai["base_url"] = env_value
    if env_value := os.getenv("CRAWL4AI_TIMEOUT_SECONDS"):
        crawl4ai["timeout_seconds"] = float(env_value or "0")
    config["web_services"]["crawl4ai"] = crawl4ai

    searxng = dict(config["web_services"].get("searxng") or {})
    if env_value := os.getenv("SEARXNG_BASE_URL"):
        searxng["base_url"] = env_value
    if env_value := os.getenv("SEARXNG_VERIFY_TLS"):
        searxng["verify_tls"] = parse_bool_env_value(
            "SEARXNG_VERIFY_TLS", env_value or ""
        )
    if env_value := os.getenv("SEARXNG_TIMEOUT_SECONDS"):
        searxng["timeout_seconds"] = float(env_value or "0")
    if env_value := os.getenv("SEARXNG_DEFAULT_NUM_RESULTS"):
        searxng["default_num_results"] = int(env_value or "0")
    config["web_services"]["searxng"] = searxng

    config["transport"]["type"] = TRANSPORT_DEFAULTS.type.value
    if env_value := os.getenv("NATS_URL"):
        config["transport"]["nats_url"] = env_value
        config["transport"]["nats_servers"] = [env_value]
    if env_value := os.getenv("NATS_SERVERS"):
        config["transport"]["nats_servers"] = [
            item.strip()
            for item in os.getenv("NATS_SERVERS", "").split(",")
            if item.strip()
        ]
    if env_value := os.getenv("NATS_NAME"):
        config["transport"]["nats_name"] = env_value
    if env_value := os.getenv("NATS_SUBJECT_PREFIX"):
        config["transport"]["subject_prefix"] = env_value
    if env_value := os.getenv("NATS_JETSTREAM_ENABLED"):
        config["transport"]["jetstream_enabled"] = parse_bool_env_value(
            "NATS_JETSTREAM_ENABLED",
            env_value or "",
        )
    if env_value := os.getenv("NATS_ACK_WAIT_SECONDS"):
        config["transport"]["ack_wait_seconds"] = float(env_value)
    if env_value := os.getenv("NATS_MAX_DELIVER"):
        config["transport"]["max_deliver"] = int(env_value)
    if env_value := os.getenv("NATS_PROCESS_CONSUMER_INACTIVE_THRESHOLD_SECONDS"):
        config["transport"]["process_consumer_inactive_threshold_seconds"] = float(
            env_value or "0"
        )
    nats_user = get_env_with_file_fallback("NATS_USER")
    if nats_user:
        config["transport"]["nats_user"] = nats_user
    nats_password = get_env_with_file_fallback("NATS_PASSWORD")
    if nats_password:
        config["transport"]["nats_password"] = nats_password
    if env_value := os.getenv("NATS_CREDENTIALS_FILE"):
        config["transport"]["credentials_file"] = env_value

    # LLM settings
    if env_value := os.getenv("LLM_PROVIDER"):
        config["llm"]["provider"] = env_value
    if env_value := os.getenv("LLM_MODEL_ID"):
        config["llm"]["model_id"] = env_value
    if env_value := os.getenv("LLM_TEMPERATURE"):
        config["llm"]["temperature"] = float(env_value)
    if env_value := os.getenv("LLM_CONTEXT_WINDOW_TOKENS"):
        config["llm"]["context_window_tokens"] = int(env_value)
    if env_value := os.getenv("LLM_STREAM"):
        config["llm"]["stream"] = parse_bool_env_value("LLM_STREAM", env_value or "")
    if env_value := os.getenv("LLM_REQUEST_TIMEOUT_SECONDS"):
        config["llm"]["request_timeout_seconds"] = float(env_value or "0")
    if env_value := os.getenv("LLM_REASONING_TRACE_RETENTION_MESSAGES"):
        config["llm"]["reasoning_trace_retention_messages"] = int(env_value)
    if env_value := os.getenv("LLM_COMPACTION_MODEL_PROVIDER"):
        config["llm"]["compaction_model_provider"] = env_value
    if env_value := os.getenv("LLM_COMPACTION_MODEL_ID"):
        config["llm"]["compaction_model_id"] = env_value
    openai_codex = dict(config["llm"].get("openai_codex") or {})
    if env_value := os.getenv("OPENAI_CODEX_BASE_URL"):
        openai_codex["base_url"] = env_value
    if env_value := os.getenv("OPENAI_CODEX_AUTH_STORE_PATH"):
        openai_codex["auth_store_path"] = env_value
    if env_value := os.getenv("OPENAI_CODEX_SHARED_HOME"):
        openai_codex["shared_codex_home"] = env_value
    if env_value := os.getenv("OPENAI_CODEX_ALLOW_SHARED_AUTH_READ"):
        openai_codex["allow_shared_codex_auth_read"] = parse_bool_env_value(
            "OPENAI_CODEX_ALLOW_SHARED_AUTH_READ",
            env_value or "",
        )
    if env_value := os.getenv("OPENAI_CODEX_REFRESH_SKEW_SECONDS"):
        openai_codex["refresh_skew_seconds"] = int(env_value or "0")
    if env_value := os.getenv("OPENAI_CODEX_REFRESH_TIMEOUT_SECONDS"):
        openai_codex["refresh_timeout_seconds"] = float(env_value or "0")
    if openai_codex:
        config["llm"]["openai_codex"] = openai_codex
    xai_oauth = dict(config["llm"].get("xai_oauth") or {})
    if env_value := os.getenv("XAI_OAUTH_BASE_URL"):
        xai_oauth["base_url"] = env_value
    if env_value := os.getenv("XAI_OAUTH_AUTH_STORE_PATH"):
        xai_oauth["auth_store_path"] = env_value
    if env_value := os.getenv("XAI_OAUTH_REFRESH_SKEW_SECONDS"):
        xai_oauth["refresh_skew_seconds"] = int(env_value or "0")
    if env_value := os.getenv("XAI_OAUTH_REFRESH_TIMEOUT_SECONDS"):
        xai_oauth["refresh_timeout_seconds"] = float(env_value or "0")
    if xai_oauth:
        config["llm"]["xai_oauth"] = xai_oauth

    # API keys are env-owned and are resolved into runtime settings later.

    # LiteLLM Proxy settings
    if env_value := os.getenv("LITELLM_API_BASE"):
        config["llm"]["litellm_api_base"] = env_value

    # Embedding service settings
    if env_value := os.getenv("EMBEDDING_PROVIDER"):
        config["embedding"]["provider"] = env_value
    if env_value := os.getenv("EMBEDDING_MODEL_ID"):
        config["embedding"]["model_id"] = env_value
    if env_value := os.getenv("EMBEDDING_API_BASE"):
        config["embedding"]["api_base"] = env_value
    if env_value := os.getenv("EMBEDDING_DIMENSIONS"):
        config["embedding"]["dimensions"] = int(env_value)
    if env_value := os.getenv("EMBEDDING_TIMEOUT_SECONDS"):
        config["embedding"]["timeout_seconds"] = float(env_value)
    if env_value := os.getenv("EMBEDDING_ORGANIZATION"):
        config["embedding"]["organization"] = env_value

    # Agent settings
    if env_value := os.getenv("AGENT_ID"):
        config["agent"]["agent_id"] = env_value
    if env_value := os.getenv("DEFAULT_TEAM_ID"):
        config["agent"]["default_team_id"] = env_value
    if env_value := os.getenv("DEFAULT_REQUEST_TIMEZONE"):
        config["agent"]["default_request_timezone"] = env_value
    if env_value := os.getenv("TASK_HISTORY_DB_PATH"):
        if "sqlite" not in config["storage"]:
            config["storage"]["sqlite"] = {}
        config["storage"]["sqlite"]["path"] = env_value
    if env_value := os.getenv("STORAGE_BACKEND"):
        config["storage"]["backend"] = env_value
    if env_value := os.getenv("SQLITE_DB_PATH"):
        if "sqlite" not in config["storage"]:
            config["storage"]["sqlite"] = {}
        config["storage"]["sqlite"]["path"] = env_value
    postgres_dsn = get_env_with_file_fallback("POSTGRES_DSN")
    if postgres_dsn:
        if "postgres" not in config["storage"]:
            config["storage"]["postgres"] = {}
        config["storage"]["postgres"]["dsn"] = postgres_dsn
    if env_value := os.getenv("TEAM_INSTANCE_ROOT"):
        config["agent"]["instance_root"] = env_value
    if env_value := os.getenv("AGENT_RESULT_TRANSFORMER"):
        config["agent"]["result_transformer"] = env_value
    if env_value := os.getenv("AGENT_DEBUG_TERMINAL_REASONING_ENABLED"):
        debug_config = dict(config["agent"].get("debug") or {})
        terminal_reasoning = dict(debug_config.get("terminal_reasoning") or {})
        terminal_reasoning["enabled"] = parse_bool_env_value(
            "AGENT_DEBUG_TERMINAL_REASONING_ENABLED",
            env_value or "",
        )
        debug_config["terminal_reasoning"] = terminal_reasoning
        config["agent"]["debug"] = debug_config
    if env_value := os.getenv("CONVERSATION_COMPACTOR_TYPE"):
        if "conversation_compactor" not in config["agent"]:
            config["agent"]["conversation_compactor"] = {}
        config["agent"]["conversation_compactor"]["type"] = env_value
    if env_value := os.getenv("SECURITY_LAYER_MODE"):
        config["security_layer"]["mode"] = env_value
    code_execution = dict(config["security_layer"].get("code_execution") or {})
    if env_value := os.getenv(ENV_CODE_SANDBOX_MODE):
        code_execution["sandbox"] = env_value
    for env_key, field_name in (
        (ENV_CODE_SANDBOX_EXTRA_READ_PATHS, "extra_read_paths"),
        (ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS, "extra_write_paths"),
    ):
        if env_value := os.getenv(env_key):
            code_execution[field_name] = json.loads(env_value)
    if code_execution:
        config["security_layer"]["code_execution"] = code_execution
    if env_value := os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL"):
        config["security_layer"]["broker_internal_base_url"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_HOST"):
        config["security_layer"]["broker_bind_host"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_PORT"):
        config["security_layer"]["broker_bind_port"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR"):
        config["security_layer"]["broker_credentials_dir"] = env_value
    credential_state_backend = dict(
        config["security_layer"].get("credential_state_backend") or {}
    )
    if env_value := os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE"):
        credential_state_backend["mode"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE"):
        credential_state_backend["source"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE"):
        credential_state_backend["namespace"] = env_value
    if env_value := os.getenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES"
    ):
        credential_state_backend["fallback_to_files"] = os.getenv(
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
    if credential_state_backend:
        config["security_layer"]["credential_state_backend"] = credential_state_backend
    credential_catalog = dict(config["security_layer"].get("credential_catalog") or {})
    if env_value := os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH"):
        credential_catalog["template_overrides_path"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE"):
        credential_catalog["default_write_source"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_OPENSHELL_BINDING_TTL_SECONDS"):
        credential_catalog["openshell_binding_ttl_seconds"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_COLLECTION_SESSION_TTL_SECONDS"):
        credential_catalog["collection_session_ttl_seconds"] = env_value
    if env_value := os.getenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS"):
        credential_catalog["secret_cache_ttl_seconds"] = env_value
    if credential_catalog:
        config["security_layer"]["credential_catalog"] = credential_catalog
    sealbox = dict(config["security_layer"].get("sealbox") or {})
    sealbox_env_map = {
        "SECURE_TOOL_BROKER_SEALBOX_ENABLED": "enabled",
        "SECURE_TOOL_BROKER_SEALBOX_MANAGE_SERVICE": "manage_service",
        "SECURE_TOOL_BROKER_SEALBOX_AUTO_INITIALIZE": "auto_initialize",
        "SEALBOX_BIND_HOST": "bind_host",
        "SEALBOX_BIND_PORT": "bind_port",
        "SEALBOX_STORE_PATH": "store_path",
        "SEALBOX_URL": "service_url",
        "SEALBOX_ROOT_TOKEN_FILE": "root_token_file",
        "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR": "tenant_material_dir",
        "SEALBOX_API_VERSION": "api_version",
        "SEALBOX_READY_TIMEOUT_SECONDS": "ready_timeout_seconds",
    }
    for env_key, config_key in sealbox_env_map.items():
        raw_value = os.getenv(env_key)
        if raw_value is None or raw_value == "":
            continue
        if config_key in {"enabled", "manage_service", "auto_initialize"}:
            sealbox[config_key] = raw_value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            sealbox[config_key] = raw_value
    if sealbox:
        config["security_layer"]["sealbox"] = sealbox
    if env_value := os.getenv("NETWORK_PUBLIC_ORIGIN"):
        config["network"]["public_origin"] = env_value
    internal_website_cfg = config["interactive_apps"]["plugins"][
        "builtin.internal_website"
    ]["settings"]
    if env_value := os.getenv("INTERNAL_WEBSITE_ENABLED"):
        internal_website_cfg["enabled"] = parse_bool_env_value(
            "INTERNAL_WEBSITE_ENABLED",
            env_value or "",
        )
    if env_value := os.getenv("INTERNAL_WEBSITE_BIND_HOST"):
        internal_website_cfg["bind_host"] = env_value
    if env_value := os.getenv("INTERNAL_WEBSITE_BIND_PORT"):
        internal_website_cfg["bind_port"] = int(env_value or "0")
    if os.getenv("INTERNAL_WEBSITE_PUBLIC_PORT") is not None:
        raw_port = str(os.getenv("INTERNAL_WEBSITE_PUBLIC_PORT") or "").strip()
        internal_website_cfg["public_port"] = int(raw_port) if raw_port else None
    if env_value := os.getenv("INTERNAL_WEBSITE_DOCS_ROOT"):
        internal_website_cfg["docs_root"] = env_value
    if env_value := os.getenv("INTERNAL_WEBSITE_SKILLS_DIR"):
        internal_website_cfg["skills_dir"] = env_value
    if env_value := os.getenv("CONTEXT_COMPACTION_TRIGGER_RATIO"):
        config["agent"]["context_compaction_trigger_ratio"] = float(env_value)
    if env_value := os.getenv("CONTEXT_COMPACTION_TARGET_RATIO"):
        config["agent"]["context_compaction_target_ratio"] = float(env_value)
    if env_value := os.getenv("CONTEXT_COMPACTION_MIN_RECENT_TURNS"):
        config["agent"]["context_compaction_min_recent_turns"] = int(env_value)
    if env_value := os.getenv("CONTEXT_COMPACTION_MAX_RECENT_TURNS"):
        config["agent"]["context_compaction_max_recent_turns"] = int(env_value)
    if env_value := os.getenv("CONTEXT_COMPACTION_TRIGGER_TURN_COUNT"):
        config["agent"]["context_compaction_trigger_turn_count"] = int(env_value)
    if env_value := os.getenv("CONTEXT_COMPACTION_TRIGGER_MESSAGE_COUNT"):
        config["agent"]["context_compaction_trigger_message_count"] = int(env_value)

    if env_value := os.getenv("ACTIVITY_COLLECTOR_BASE_URL"):
        config["activity_collector"]["base_url"] = env_value
    if env_value := os.getenv("ACTIVITY_COLLECTOR_CONNECT_TIMEOUT_SECONDS"):
        config["activity_collector"]["connect_timeout_seconds"] = float(env_value)
    if env_value := os.getenv("ACTIVITY_COLLECTOR_REQUEST_TIMEOUT_SECONDS"):
        config["activity_collector"]["request_timeout_seconds"] = float(env_value)
    if env_value := os.getenv("ACTIVITY_COLLECTOR_BATCH_MAX_EVENTS"):
        config["activity_collector"]["batch_max_events"] = int(env_value)
    if env_value := os.getenv("ACTIVITY_COLLECTOR_FLUSH_INTERVAL_SECONDS"):
        config["activity_collector"]["flush_interval_seconds"] = float(env_value)
    if env_value := os.getenv("ACTIVITY_COLLECTOR_MAX_BUFFERED_EVENTS"):
        config["activity_collector"]["max_buffered_events"] = int(env_value)
    if env_value := os.getenv("ACTIVITY_COLLECTOR_DROP_WHEN_UNAVAILABLE"):
        config["activity_collector"]["drop_when_unavailable"] = (
            env_value.strip().lower() in {"1", "true", "yes", "on"}
        )
    if env_value := os.getenv("ACTIVITY_COLLECTOR_INGEST_TOKEN_ENV_VAR"):
        config["activity_collector"]["ingest_token_env_var"] = env_value

    if env_value := os.getenv("OTEL_ENABLED"):
        config["otel"]["enabled"] = env_value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if env_value := os.getenv("OTEL_ENABLE_TRACES"):
        config["otel"]["enable_traces"] = env_value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if env_value := os.getenv("OTEL_ENABLE_METRICS"):
        config["otel"]["enable_metrics"] = env_value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if env_value := os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        config["otel"]["otlp_endpoint"] = env_value
    if env_value := os.getenv("OTEL_SERVICE_NAME"):
        config["otel"]["service_name"] = env_value

    return config
