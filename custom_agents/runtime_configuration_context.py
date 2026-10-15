"""Non-secret runtime configuration summaries for agent self-inspection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from config_loader import Config, get_config, resolve_agent_type_settings
from custom_messages.contracts.runtime_v1 import (
    INTERACTIVE_APP_CHANNELS,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from default_values import AGENT_DEFAULTS

logger = logging.getLogger(__name__)

RUNTIME_NOTE = (
    "live_agent is this running process. runtime_config_cache is this process's configuration as it "
    "started, except its llm values, which include LLM_OVERRIDES.yaml read now. config_file_snapshot is "
    "the saved instance_config.yaml, read now; a setting that needs a restart shows there before the "
    "running process uses it. schedule_workers is what the scheduled-worker launcher enforces now: it "
    "reads the saved file on every check, so a saved change needs no restart."
)
# Model settings in LLM_OVERRIDES.yaml reach running agents at the start of
# their next turn (``UniversalAgent.refresh_persistent_llm_override``), so the
# saved values are what each agent type uses. Status reports
# (``query_agent_state``) come from the agents themselves and lag until then.
AGENT_TYPE_MODELS_NOTE = (
    "agent_type_models shows each named agent type's model settings as saved (instance_config.yaml with "
    "LLM_OVERRIDES.yaml on top), read now. Model overrides reach running agents from their next turn and "
    "new agents from their start, with no restart. Instance-config baseline changes need the reported "
    "restart. An idle agent's status report can still show the old "
    "values until its next turn."
)
# One call reports a bounded number of agent types.
MAX_AGENT_TYPES_PER_CALL = 20


def _truthy_configured(value: Any) -> bool:
    return bool(str(value or "").strip())


def _string_or_none(value: Any) -> Optional[str]:
    rendered = str(value or "").strip()
    return rendered or None


def _safe_agent_attr(agent: Any, name: str, default: Any = None) -> Any:
    return getattr(agent, name, default) if agent is not None else default


def _live_storage_summary(agent: Any) -> dict[str, Any]:
    task_manager = _safe_agent_attr(agent, "task_manager")
    storage_settings = getattr(task_manager, "storage_settings", None)
    return {
        "storage_backend": getattr(storage_settings, "backend", None)
        or _safe_agent_attr(agent, "storage_backend"),
        "storage_sqlite_path": getattr(storage_settings, "sqlite_path", None)
        or _safe_agent_attr(agent, "storage_sqlite_path"),
        "storage_postgres_dsn_configured": _truthy_configured(
            getattr(storage_settings, "postgres_dsn", None)
            or _safe_agent_attr(agent, "storage_postgres_dsn")
        ),
    }


def _safe_resolve_agent_settings(
    config: Config, agent_type: Optional[str]
) -> tuple[dict[str, Any], Optional[str]]:
    try:
        return resolve_agent_type_settings(
            config, agent_type or TEAM_ARCHITECT_AGENT_TYPE
        ), None
    except Exception as exc:
        # ValidationError renders the rejected input, which can contain tokens
        # and host paths. Agent-visible diagnostics expose the failure class only.
        return {}, f"{exc.__class__.__name__}: configuration resolution failed"


def _llm_settings(config: Config, settings: dict[str, Any]) -> dict[str, Any]:
    """One agent type's model settings from its resolved settings (non-secret)."""

    return {
        "provider": settings.get("model_provider") or config.get("llm.provider"),
        "model_id": settings.get("model_id") or config.get("llm.model_id"),
        "reasoning_effort": settings.get("reasoning_effort"),
        "reasoning_summary": settings.get("reasoning_summary"),
        "litellm_api_mode": settings.get("litellm_api_mode"),
        "context_window_tokens": settings.get("context_window_tokens"),
        "supports_image_input": settings.get("supports_image_input"),
        "supports_audio_input": settings.get("supports_audio_input"),
        "supports_file_input": settings.get("supports_file_input"),
        "capability_note": (
            "null means unknown; use configured overrides or researched "
            "provider/model facts before assuming optional input support."
        ),
    }


def _enabled_interactive_apps(instance_config: Any) -> dict[str, bool]:
    apps = {}
    interactive_apps = getattr(instance_config, "interactive_apps", None)
    for name in INTERACTIVE_APP_CHANNELS:
        app_config = getattr(interactive_apps, name, None)
        if app_config is not None and hasattr(app_config, "enabled"):
            apps[name] = bool(getattr(app_config, "enabled"))
    return apps


def _user_communication_payload(instance_config: Any) -> dict[str, Any]:
    if instance_config is None:
        return {}
    delivery = getattr(instance_config, "delivery", None)
    target = getattr(delivery, "default_target", None)
    notification_routing = getattr(delivery, "notification_routing", None)
    manager = getattr(instance_config, "manager", None)
    user = getattr(instance_config, "user", None)
    return {
        "preferred_channel": getattr(target, "channel_name", None),
        "preferred_chat_id": getattr(target, "chat_id", None),
        "preferred_thread_id": getattr(target, "thread_id", None),
        "notification_routing_mode": getattr(notification_routing, "mode", None),
        "gateway_response_queue": getattr(manager, "response_queue_name", None),
        "canonical_user_id": getattr(user, "canonical_user_id", None),
        "default_session_key": getattr(user, "default_session_key", None),
        "tool": "notify_user",
        "note": (
            "Use notify_user for proactive user-visible messages. Its default target='auto' follows "
            "notification_routing_mode: source_thread_or_default returns notifications to an active "
            "interactive chat/thread when one exists, while default_target uses the preferred channel "
            "below. For screenshots or other binary outputs, save the file locally and pass "
            "notify_user attachments with file paths; do not inline bytes or base64."
        ),
    }


def _config_snapshot_payload(
    *,
    config: Config,
    agent_type: Optional[str],
    source: str,
    instance_config: Any = None,
) -> dict[str, Any]:
    settings, settings_error = _safe_resolve_agent_settings(config, agent_type)
    raw = config.to_dict()
    security_layer = (
        raw.get("security_layer") if isinstance(raw.get("security_layer"), dict) else {}
    )
    credential_catalog = (
        security_layer.get("credential_catalog")
        if isinstance(security_layer.get("credential_catalog"), dict)
        else {}
    )
    credential_state_backend = (
        security_layer.get("credential_state_backend")
        if isinstance(security_layer.get("credential_state_backend"), dict)
        else {}
    )
    sealbox = (
        security_layer.get("sealbox")
        if isinstance(security_layer.get("sealbox"), dict)
        else {}
    )
    workspace = {
        "instance_root": settings.get("instance_root")
        or config.get("agent.instance_root"),
        # The agents' area; each task works in its person's folder there.
        "workspaces_dir": settings.get("workspace_dir"),
        "skills_dir": settings.get("skills_dir"),
        "external_skill_repositories": config.get(
            "workspace.external_skill_repositories", []
        ),
        "refresh_external_skills_on_start": config.get(
            "workspace.refresh_external_skills_on_start", False
        ),
        "llm_override_path": settings.get("llm_override_path"),
        "team_templates_path": settings.get("team_templates_path"),
        "team_composition_path": settings.get("team_composition_path"),
        "task_history_db_path": settings.get("task_history_db_path"),
    }
    storage = {
        "backend": settings.get("storage_backend") or config.get("storage.backend"),
        "sqlite_path": settings.get("storage_sqlite_path")
        or config.get("storage.sqlite.path"),
        "postgres_dsn_configured": _truthy_configured(
            settings.get("storage_postgres_dsn") or config.get("storage.postgres.dsn")
        ),
    }
    llm = _llm_settings(config, settings)
    return {
        "source": source,
        "agent_type": agent_type or TEAM_ARCHITECT_AGENT_TYPE,
        "settings_error": settings_error,
        "workspace": {
            key: value for key, value in workspace.items() if value not in (None, "")
        },
        "storage": {
            key: value for key, value in storage.items() if value not in (None, "")
        },
        "llm": llm,
        "security_layer": {
            "mode": security_layer.get("mode"),
            "broker_internal_base_url": security_layer.get("broker_internal_base_url"),
            "broker_public_port": security_layer.get("broker_public_port"),
            "broker_bind_host": security_layer.get("broker_bind_host"),
            "broker_bind_port": security_layer.get("broker_bind_port"),
            "broker_credentials_dir_configured": _truthy_configured(
                security_layer.get("broker_credentials_dir")
            ),
            "credential_catalog_enabled": credential_catalog.get("enabled"),
            "credential_catalog_default_write_source": credential_catalog.get(
                "default_write_source"
            ),
            "credential_state_backend_mode": credential_state_backend.get("mode"),
            "credential_state_backend_source": credential_state_backend.get("source"),
            "sealbox_enabled": sealbox.get("enabled"),
            "sealbox_manage_service": sealbox.get("manage_service"),
            "sealbox_auto_initialize": sealbox.get("auto_initialize"),
        },
        "activity_collector": {
            "base_url": config.get("activity_collector.base_url"),
            "ingest_token_env_var": config.get(
                "activity_collector.ingest_token_env_var"
            ),
        },
        "interactive_apps": _enabled_interactive_apps(instance_config)
        if instance_config is not None
        else {},
        "user_communication": _user_communication_payload(instance_config),
        "mcp_profile_names": sorted((raw.get("mcp_profiles") or {}).keys())
        if isinstance(raw.get("mcp_profiles"), dict)
        else [],
    }


def _load_instance_config_snapshot(
    *,
    instance_config_path: Optional[str],
    agent_type: Optional[str],
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[Config]]:
    path_text = _string_or_none(instance_config_path)
    if not path_text:
        return None, None, None
    path = Path(path_text).expanduser()
    if not path.exists():
        return None, f"instance_config_path does not exist: {path}", None
    try:
        from assistant_gateway.config import GatewayInstanceConfig
        from assistant_gateway.runtime_config import (
            build_runtime_config_from_instance_config,
        )

        # The saved file exactly as stored (GatewayInstanceConfig.load_saved),
        # without this process's deployment details.
        instance_config = GatewayInstanceConfig.load_saved(path)
        runtime_config = build_runtime_config_from_instance_config(instance_config)
        return (
            _config_snapshot_payload(
                config=runtime_config,
                agent_type=agent_type,
                source=str(instance_config.config_path),
                instance_config=instance_config,
            ),
            None,
            runtime_config,
        )
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: configuration loading failed", None


def _schedule_workers_in_effect(agent: Any) -> dict[str, Any]:
    """The scheduled-worker settings the launcher enforces now, from the launcher's own reader.

    Showing the process's startup copy beside the saved file once made a saved
    eligible type look excluded from the "live" scheduler.
    """

    from custom_agents.agent_instantiation_service import current_worker_limits

    if agent is None:
        return {"error": "No running agent; the launcher reads these settings for an agent."}
    try:
        limits = current_worker_limits(agent)
        configured = _string_or_none(_safe_agent_attr(agent, "instance_config_path"))
        if configured:
            from assistant_gateway.config import GatewayInstanceConfig

            source = str(GatewayInstanceConfig.load_saved(configured).config_path)
        else:
            source = "runtime_config_cache"
    except Exception as exc:
        logger.warning(
            "Scheduled-worker settings could not be read for the runtime configuration context. "
            "event=runtime_configuration_schedule_workers_unreadable agent_id=%s error_type=%s",
            _safe_agent_attr(agent, "id"),
            exc.__class__.__name__,
        )
        return {"error": f"{exc.__class__.__name__}: the saved scheduled-worker settings could not be read"}
    return {"source": source, "limits": limits.model_dump(mode="json")}


# How TeamLead switches the team's OpenAI-compatible endpoint (custom_agents.endpoint_switch).
SWITCH_ENDPOINT_GUIDE = (
    "To switch the team's OpenAI-compatible model endpoint, stage llm.litellm_api_base (team scope) with the "
    "base_url of one of saved_endpoints. Match what the person calls it (for example 'Bifrost') to a host here; "
    "its key is already saved, so never ask for a key or for the address. Keep the provider, model and request "
    "API unless the person asks otherwise. Staging tests the endpoint first; an administrator then approves it. "
    "An endpoint missing from saved_endpoints needs its key saved first with /auth model by an administrator. "
    "OpenAI, Anthropic, Google Gemini and xAI are not switched this way: they have fixed addresses and are chosen "
    "with llm.provider."
)


def model_endpoints_context(agent: Any) -> dict[str, Any]:
    """The team's current OpenAI-compatible endpoint and every endpoint with a saved key (never keys).

    Without this TeamLead could not see that an endpoint such as Bifrost was
    already set up, and asked the person for its address (owner's report
    2026-10-02). The list comes from the broker (``MODEL_KEY_ENDPOINTS_ROUTE``).
    """

    from custom_agents.model_keys import BrokerNotConfigured, BrokerUnreachable, saved_team_endpoints
    from model_providers.endpoints import display_host, endpoint_address

    current = str(_safe_agent_attr(agent, "litellm_api_base") or "")
    current_address = endpoint_address(current) if current else ""
    context: dict[str, Any] = {"current_openai_compatible_endpoint": current or None, "how_to_switch": SWITCH_ENDPOINT_GUIDE}
    try:
        saved = saved_team_endpoints().endpoints
    except (BrokerUnreachable, BrokerNotConfigured, RuntimeError, ValueError) as exc:
        logger.warning(
            "Could not list the team's saved model endpoints. event=saved_model_endpoints_unavailable error=%s", exc
        )
        context["saved_endpoints_error"] = f"The secure tool broker could not list saved endpoints: {exc}"
        return context
    context["saved_endpoints"] = [
        {
            "host": display_host(item.address),
            "base_url": item.base_url or item.address,
            "provider_kind": item.kind.value,
            "tested_model_id": item.tested_model_id or None,
            "needs_no_key": item.no_key_needed,
            "in_use": item.address == current_address,
        }
        for item in saved
    ]
    return context


def agent_type_models(
    config: Optional[Config], agent_types: list[str], known_agent_types: Optional[set[str]] = None,
    *, settings_error: Optional[str] = None,
) -> dict[str, Any]:
    """Saved model settings for each named agent type (``AGENT_TYPE_MODELS_NOTE``).

    A name the team does not know is reported as an error rather than shown
    with the team defaults, which would look like that type's settings.
    """

    models: dict[str, Any] = {}
    for agent_type in list(dict.fromkeys(str(name).strip() for name in agent_types if str(name).strip()))[
        :MAX_AGENT_TYPES_PER_CALL
    ]:
        if known_agent_types is not None and agent_type not in known_agent_types:
            models[agent_type] = {"error": "No such agent type; get_available_agent_types lists them."}
            continue
        if config is None:
            models[agent_type] = {"settings_error": settings_error or "No saved instance_config.yaml is configured."}
            continue
        settings, resolution_error = _safe_resolve_agent_settings(config, agent_type)
        models[agent_type] = (
            {"settings_error": resolution_error} if resolution_error else _llm_settings(config, settings)
        )
    return models


def build_runtime_configuration_context(
    agent: Any = None,
    *,
    include_config_file_snapshot: bool = True,
    agent_types: Optional[list[str]] = None,
    known_agent_types: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Return a non-secret runtime/config snapshot for explicit agent tool use."""
    agent_type = (
        _safe_agent_attr(agent, "named_agent_type") or TEAM_ARCHITECT_AGENT_TYPE
    )
    global_config = get_config()
    global_snapshot = _config_snapshot_payload(
        config=global_config,
        agent_type=agent_type,
        source="runtime_config_cache",
    )
    instance_config_path = _safe_agent_attr(
        agent, "instance_config_path"
    ) or global_config.get("agent.instance_config_path")
    config_file_snapshot = None
    config_file_error = None
    saved_runtime_config = None
    saved_config_error = None
    if include_config_file_snapshot or agent_types:
        saved_snapshot, saved_config_error, saved_runtime_config = _load_instance_config_snapshot(
            instance_config_path=instance_config_path,
            agent_type=agent_type,
        )
        if include_config_file_snapshot:
            config_file_snapshot = saved_snapshot
            config_file_error = saved_config_error
    live_storage = _live_storage_summary(agent)

    return {
        "description": (
            "Non-secret runtime configuration context. Secret values such as tokens, passwords, "
            "API keys, transport passwords, credential paths, and DSNs are omitted or reduced to configured booleans."
        ),
        "live_agent": {
            "agent_id": _safe_agent_attr(agent, "id"),
            "agent_name": _safe_agent_attr(agent, "name"),
            "named_agent_type": agent_type,
            "team_id": _safe_agent_attr(agent, "team_id"),
            "is_team_lead": _safe_agent_attr(agent, "is_team_lead"),
            "instance_root": _safe_agent_attr(agent, "instance_root"),
            "workspaces_dir": _safe_agent_attr(agent, "workspace_dir"),
            "skills_dir": _safe_agent_attr(agent, "skills_dir"),
            "additional_skill_directories": list(
                _safe_agent_attr(agent, "additional_skill_directories", []) or []
            ),
            "skill_exclusions": list(
                _safe_agent_attr(agent, "skill_exclusions", []) or []
            ),
            "prompt_skill_description_limit": _safe_agent_attr(
                agent,
                "prompt_skill_description_limit",
                AGENT_DEFAULTS.prompt_skill_description_limit.value,
            ),
            "llm_override_path": _safe_agent_attr(agent, "llm_override_path"),
            "team_templates_path": _safe_agent_attr(agent, "team_templates_path"),
            "team_composition_path": _safe_agent_attr(agent, "team_composition_path"),
            "instance_config_path": instance_config_path,
            "model_provider": _safe_agent_attr(agent, "model_provider"),
            "model_id": _safe_agent_attr(agent, "model_id"),
            "reasoning_effort": _safe_agent_attr(agent, "reasoning_effort"),
            "reasoning_summary": _safe_agent_attr(agent, "reasoning_summary"),
            "litellm_api_mode": _safe_agent_attr(agent, "litellm_api_mode"),
            "litellm_api_base": _safe_agent_attr(agent, "litellm_api_base"),
            "context_window_tokens": _safe_agent_attr(agent, "context_window_tokens"),
            "supports_image_input": _safe_agent_attr(agent, "supports_image_input"),
            "supports_audio_input": _safe_agent_attr(agent, "supports_audio_input"),
            "supports_file_input": _safe_agent_attr(agent, "supports_file_input"),
            **live_storage,
            "activity_collector_base_url": _safe_agent_attr(
                agent, "activity_collector_base_url"
            ),
            "activity_collector_ingest_token_env_var": _safe_agent_attr(
                agent,
                "activity_collector_ingest_token_env_var",
            ),
        },
        "runtime_config_cache": global_snapshot,
        "environment": {
            "INTERNAL_WEBSITE_BASE_URL": _string_or_none(
                os.getenv("INTERNAL_WEBSITE_BASE_URL")
            ),
            "NETWORK_PUBLIC_ORIGIN": _string_or_none(
                os.getenv("NETWORK_PUBLIC_ORIGIN")
            ),
            "AGENT_WORKSPACE_DIR": _string_or_none(os.getenv("AGENT_WORKSPACE_DIR")),
            "TEAM_INSTANCE_ROOT": _string_or_none(os.getenv("TEAM_INSTANCE_ROOT")),
            "TEAM_REFRESH_EXTERNAL_SKILLS": _string_or_none(
                os.getenv("TEAM_REFRESH_EXTERNAL_SKILLS")
            ),
        },
        "config_file_snapshot": config_file_snapshot,
        "config_file_snapshot_error": config_file_error,
        "schedule_workers": _schedule_workers_in_effect(agent),
        "model_endpoints": model_endpoints_context(agent),
        "runtime_note": RUNTIME_NOTE,
        **(
            {
                "agent_type_models": agent_type_models(
                    saved_runtime_config, agent_types, known_agent_types, settings_error=saved_config_error,
                ),
                "agent_type_models_note": AGENT_TYPE_MODELS_NOTE,
            }
            if agent_types
            else {}
        ),
    }
