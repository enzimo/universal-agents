"""Runtime config translation helpers for launched agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from assistant_gateway.config import (
    GatewayInstanceConfig,
    resolve_activity_collector_runtime_base_url,
)
from runtime_settings.network import reject_removed_public_url_env_vars
from config_loader import Config, load_config
from runtime_settings.environment import _apply_env_overrides
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_SQLITE,
)


def non_empty_env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_bool(name: str) -> Optional[bool]:
    value = non_empty_env(name)
    if value is None:
        return None
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value for {name}: {value!r}")


def env_int(name: str) -> Optional[int]:
    value = non_empty_env(name)
    if value is None:
        return None
    return int(value)


def env_float(name: str) -> Optional[float]:
    value = non_empty_env(name)
    if value is None:
        return None
    return float(value)


def is_instance_config_document(data: Mapping[str, Any]) -> bool:
    """Return whether a YAML mapping has the required instance identity fields."""

    return {"instance_name", "instance_root"}.issubset(data)


def load_runtime_config_file(path: str | Path) -> Config:
    """Load either supported config document into the agent runtime shape.

    Generic ``config.yaml`` and onboarded ``instance_config.yaml`` have
    independent schemas. Callers that accept either file must classify the
    document before choosing its loader instead of comparing schema numbers.
    """

    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a mapping config document in {config_path}.")
    if is_instance_config_document(raw):
        return build_runtime_config_from_instance_config(
            GatewayInstanceConfig.load_for_process(config_path)
        )
    return load_config(str(config_path), search_default_paths=False)


def build_runtime_config_from_instance_config(
    instance_config: GatewayInstanceConfig,
) -> Config:
    reject_removed_public_url_env_vars()
    manager_storage = instance_config.resolve_manager_storage_settings(env=os.environ)

    llm_cfg = instance_config.llm.model_dump(mode="json", exclude_none=True)
    # An unspecified capability means model discovery owns the value; false is
    # an explicit operator override and must survive this projection.
    for name in (
        "supports_image_input",
        "supports_audio_input",
        "supports_file_input",
        "reasoning_trace_retention_messages",
    ):
        if name not in instance_config.llm.model_fields_set:
            llm_cfg.pop(name, None)
    embedding_cfg = instance_config.embedding.model_dump(mode="json", exclude_none=True)

    # This is a heterogeneous, recursively nested configuration payload. ``Any``
    # is intentional at this translation boundary; individual values are
    # validated when ``Config`` materializes the runtime settings.
    generic_config: dict[str, Any] = {
        "transport": instance_config.transport.model_dump(
            mode="json", exclude_none=True
        ),
        "llm": llm_cfg,
        "embedding": embedding_cfg,
        "web_services": instance_config.web_services.model_dump(
            mode="json", exclude_none=True
        ),
        "agent": {
            **instance_config.agent.model_dump(mode="json", exclude_none=True),
            "default_team_id": instance_config.manager.team_id,
            "default_request_timezone": instance_config.user.timezone,
            "instance_root": str(instance_config.instance_root_path),
            "instance_config_path": str(instance_config.config_path),
            "bootstrap": {
                "instance_name": instance_config.instance_name,
                "instance_root": str(instance_config.instance_root_path),
                "workspace_dir": str(instance_config.workspace_path),
                "team_id": instance_config.manager.team_id,
                "canonical_user_id": instance_config.user.canonical_user_id,
                "timezone": instance_config.user.timezone,
                "initial_user_context": instance_config.user.initial_context,
                "heartbeat_enabled": instance_config.heartbeat.enabled,
                "heartbeat_source_id": instance_config.heartbeat.source_id,
            },
        },
        "agent_types": {
            agent_type: settings.model_dump(mode="json", exclude_none=True)
            for agent_type, settings in instance_config.agent_types.items()
        },
        "network": instance_config.network.model_dump(mode="json", exclude_none=True),
        "storage": {
            "backend": manager_storage.backend,
            STORAGE_BACKEND_SQLITE: {
                "path": manager_storage.sqlite_path,
                "vector": (
                    {
                        "enabled": manager_storage.sqlite_vector.enabled,
                        "provider": manager_storage.sqlite_vector.provider,
                        "require_extension": manager_storage.sqlite_vector.require_extension,
                    }
                    if manager_storage.sqlite_vector is not None
                    else None
                ),
            },
            STORAGE_BACKEND_POSTGRES: {"dsn": manager_storage.postgres_dsn},
        },
        "workspace": instance_config.workspace.model_dump(mode="json"),
        "activity_collector": {
            "base_url": resolve_activity_collector_runtime_base_url(instance_config),
            "connect_timeout_seconds": instance_config.activity_collector.connect_timeout_seconds,
            "request_timeout_seconds": instance_config.activity_collector.request_timeout_seconds,
            "batch_max_events": instance_config.activity_collector.batch_max_events,
            "flush_interval_seconds": instance_config.activity_collector.flush_interval_seconds,
            "max_buffered_events": instance_config.activity_collector.max_buffered_events,
            "drop_when_unavailable": instance_config.activity_collector.drop_when_unavailable,
            "ingest_token_env_var": instance_config.activity_collector.ingest_token_env_var,
        },
        "tool_policy": instance_config.tool_policy.model_dump(mode="json"),
        "authorization": instance_config.authorization.model_dump(mode="json"),
        "access_workflows": instance_config.access_workflows.model_dump(mode="json"),
        "execution_control": instance_config.execution_control.model_dump(mode="json"),
        "otel": instance_config.otel.model_dump(mode="json"),
        "security_layer": {
            **instance_config.security_layer.model_dump(mode="json", exclude_none=True),
            "broker_credentials_dir": str(instance_config.broker_credentials_path),
        },
        "mcp": instance_config.mcp.model_dump(mode="json", exclude_none=True),
        "interactive_apps": instance_config.interactive_apps.model_dump(
            mode="json",
            exclude_none=True,
        ),
        "mcp_profiles": dict(instance_config.mcp_profiles or {}),
    }

    # Preserve explicit null public ports through serialization.
    generic_config["security_layer"]["broker_public_port"] = (
        instance_config.security_layer.broker_public_port
    )
    generic_config["interactive_apps"]["plugins"]["builtin.internal_website"][
        "settings"
    ]["public_port"] = instance_config.interactive_apps.internal_website.public_port
    return Config(_apply_env_overrides(generic_config))


def build_collector_runtime_settings(config: GatewayInstanceConfig):
    from activity_collector.settings import CollectorRuntimeSettings

    return CollectorRuntimeSettings(
        activity_collector=config.activity_collector,
        storage=config.activity_collector_storage_settings,
        team_id=config.manager.team_id,
    )
