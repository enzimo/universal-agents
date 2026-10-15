"""Config loader configuration and contracts."""

from __future__ import annotations
import logging
import os
from runtime_settings.security import (
    CodeExecutionSecuritySettings,
    CredentialCatalogSettings,
    CredentialStateBackendSettings,
    SealboxSecuritySettings,
    normalize_security_layer_mode,
    reject_removed_security_layer_settings,
)
from runtime_settings.resolution import normalize_runtime_sections, validate_agent_tool_lists
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.legacy_layout import (
    drop_removed_location_settings,
    present_removed_location_settings,
)
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
import yaml
from config_backup_retention import prune_config_backups
from default_values import (
    AGENT_DEFAULTS,
    INTERACTIVE_APP_DEFAULTS,
    LLM_DEFAULTS,
    NETWORK_DEFAULTS,
    SECURITY_LAYER_DEFAULTS,
    WORKSPACE_DEFAULTS,
    BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI,
)
from runtime_settings.network import compose_public_base_url
from secure_tool_broker.defaults import resolve_secure_tool_broker_internal_base_url
from storage import DEFAULT_SQLITE_STORAGE_PATH, resolve_storage_settings
from custom_agents.xai_oauth_auth import (
    is_xai_oauth_provider,
    resolve_xai_oauth_model_id,
)
from custom_agents.model_runtime_profile import (
    MODEL_CAPABILITY_FIELDS,
    MODEL_TOOL_LIMIT_FIELD,
    MODEL_TUNING_FIELDS,
)
from runtime_settings.model_request_options import (
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)
from runtime_settings.environment import _apply_env_overrides
from runtime_settings.governance import refuse_removed_access_settings
from runtime_settings.worker_pools import move_worker_capacity_settings
from runtime_settings.values import (
    DEFAULT_AUTHORIZATION_CONFIG,
    _mapping_or_empty,
    deep_merge_dicts,
    parse_bool_env_value,
)
from runtime_settings.projection import (
    _project_agent,
    _project_context,
    _project_embedding,
    _project_model,
    _project_security,
    _project_telemetry,
    _project_transport,
)


logger = logging.getLogger(__name__)


CONFIG_SCHEMA_VERSION = 10


DIRECTLY_COMPATIBLE_CONFIG_VERSIONS = {CONFIG_SCHEMA_VERSION}


DEFAULT_INSTANCE_ROOT = WORKSPACE_DEFAULTS.instance_root.value




BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH = (
    Path(__file__).resolve().parent
    / "ua_definitions"
    / "objective_execution"
    / "default.yaml"
)


def _parse_bool_config_value(name: str, value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return parse_bool_env_value(name, str(value))


CONFIG_PATHS = [
    Path("config.yaml"),
    Path("config.yml"),
    Path.home() / ".config" / "team_nats_agents" / "config.yaml",
]


def _drop_env_owned_api_key_fields(config: Dict[str, Any]) -> tuple[str, ...]:
    """Remove API-key fields from user-authored config maps.

    API keys are intentionally env-owned. Runtime settings still resolve them
    from environment variables and Docker secret-style *_FILE variables.
    """

    removed: list[str] = []

    def pop_path(root: Dict[str, Any], path: tuple[str, ...]) -> None:
        current: Any = root
        for part in path[:-1]:
            if not isinstance(current, dict):
                return
            current = current.get(part)
        if isinstance(current, dict) and path[-1] in current:
            current.pop(path[-1], None)
            removed.append(".".join(path))

    for path in (
        ("llm", "openai_api_key"),
        ("llm", "gemini_api_key"),
        ("llm", "litellm_api_key"),
        ("embedding", "api_key"),
    ):
        pop_path(config, path)

    agent_types = config.get("agent_types")
    if isinstance(agent_types, dict):
        for agent_type, overrides in agent_types.items():
            if not isinstance(overrides, dict):
                continue
            for path in (
                ("llm", "openai_api_key"),
                ("llm", "gemini_api_key"),
                ("llm", "litellm_api_key"),
                ("embedding", "api_key"),
            ):
                before = len(removed)
                pop_path(overrides, path)
                if len(removed) > before:
                    removed[-1] = f"agent_types.{agent_type}.{removed[-1]}"

    return tuple(removed)


@dataclass(frozen=True)
class ConfigVersionStatus:
    path: Path
    detected_version: int
    latest_version: int
    directly_compatible_versions: tuple[int, ...]
    needs_migration: bool
    is_directly_compatible: bool


@dataclass(frozen=True)
class ConfigMigrationResult:
    path: Path
    original_version: int
    migrated_version: int
    backup_path: Optional[Path]
    applied_steps: tuple[str, ...]


class Config:
    """Configuration container with dot-notation access."""

    def __init__(self, data: Dict[str, Any] = None):
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dot-notation key (e.g., 'transport.nats_url')."""
        keys = key.split(".")
        value: Any = self._data
        index = 0
        while index < len(keys):
            if not isinstance(value, dict):
                return default
            matched = False
            for end in range(len(keys), index, -1):
                candidate = ".".join(keys[index:end])
                if candidate not in value:
                    continue
                value = value[candidate]
                index = end
                matched = True
                break
            if not matched:
                return default
            if value is None:
                return default
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        value = self._data.get(name)
        if isinstance(value, dict):
            return Config(value)
        return value

    def to_dict(self) -> Dict[str, Any]:
        """Return the raw dictionary."""
        return self._data


def _apply_terminal_reasoning_env_override(debug: Dict[str, Any]) -> Dict[str, Any]:
    if os.getenv("AGENT_DEBUG_TERMINAL_REASONING_ENABLED") is None:
        return debug
    resolved = dict(debug or {})
    terminal_reasoning = dict(resolved.get("terminal_reasoning") or {})
    terminal_reasoning["enabled"] = parse_bool_env_value(
        "AGENT_DEBUG_TERMINAL_REASONING_ENABLED",
        os.getenv("AGENT_DEBUG_TERMINAL_REASONING_ENABLED") or "",
    )
    resolved["terminal_reasoning"] = terminal_reasoning
    return resolved


def _normalize_optional_string(value: Any) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_optional_port(value: Any, *, field_name: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    if parsed < 1 or parsed > 65_535:
        raise ValueError(f"{field_name} must be between 1 and 65535.")
    return parsed


def resolve_config_file_path(
    config_path: Optional[str] = None,
    *,
    search_default_paths: bool = True,
) -> Optional[Path]:
    """Resolve the first config file path the loader would use."""
    if config_path:
        paths_to_try = [Path(config_path)]
    elif search_default_paths:
        paths_to_try = CONFIG_PATHS
    else:
        paths_to_try = []
    for path in paths_to_try:
        candidate = path.expanduser()
        if candidate.exists():
            return candidate.resolve()
    return None


def _detect_config_version(data: Dict[str, Any]) -> int:
    if "schema_version" not in data:
        raise ValueError("config.yaml must declare schema_version")
    raw = data["schema_version"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"Invalid config.yaml schema_version: {raw!r}")
    if raw < 1:
        raise ValueError(f"Invalid config.yaml schema_version: {raw}")
    return raw


def inspect_config_version(path: str | Path) -> ConfigVersionStatus:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    detected_version = _detect_config_version(data)
    if detected_version > CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"config.yaml schema_version {detected_version} is newer than supported latest "
            f"version {CONFIG_SCHEMA_VERSION}"
        )
    return ConfigVersionStatus(
        path=config_path,
        detected_version=detected_version,
        latest_version=CONFIG_SCHEMA_VERSION,
        directly_compatible_versions=tuple(sorted(DIRECTLY_COMPATIBLE_CONFIG_VERSIONS)),
        needs_migration=detected_version not in DIRECTLY_COMPATIBLE_CONFIG_VERSIONS,
        is_directly_compatible=detected_version in DIRECTLY_COMPATIBLE_CONFIG_VERSIONS,
    )


def _migrate_config_dict(
    data: Dict[str, Any],
    *,
    source_path: Path,
) -> tuple[Dict[str, Any], tuple[str, ...]]:
    """Validate the public configuration baseline and apply future upgrades."""
    del source_path
    migrated = dict(data)
    version = _detect_config_version(migrated)
    if version > CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"config.yaml schema_version {version} is newer than supported latest "
            f"version {CONFIG_SCHEMA_VERSION}"
        )

    applied_steps: list[str] = []
    if version in (1, 2, 3):
        # These steps used to merge in settings of the old permission system
        # (authorization.permission_delegation selectors, emergency_deny,
        # enforced_targets). Those settings are refused by name now (below), so
        # the steps only advance the version.
        applied_steps.extend(
            f"{step} -> {step + 1}: no change (the settings it added were removed)"
            for step in range(version, 4)
        )
        migrated["schema_version"] = 4
        version = 4

    if version == 4:
        # Phase 3 deliberately removes the old per-adapter configuration
        # shape. Validation below rejects it instead of translating it into
        # the registry-owned map; configurations already using the normalized
        # shape only need their schema marker advanced.
        migrated["schema_version"] = 5
        applied_steps.append(
            "4 -> 5: require registry-keyed interactive app configuration"
        )
        version = 5

    if version == 5:
        workspace = _mapping_or_empty(migrated.get("workspace"))
        if "objective_execution_path" in workspace:
            raise ValueError(
                "workspace.objective_execution_path has been removed. Use the ordered "
                "workspace.objective_execution_paths array instead."
            )
        migrated["schema_version"] = 6
        applied_steps.append(
            "5 -> 6: require ordered bounded Objective execution policy paths"
        )
        version = 6

    if version == 6:
        if "automation_jobs" in migrated:
            raise ValueError(
                "automation_jobs cannot be migrated. Recreate immutable execution artifacts, "
                "revisions, schedules, and activations under execution_control."
            )
        migrated["schema_version"] = 7
        applied_steps.append(
            "6 -> 7: require immutable execution-control configuration"
        )
        version = 7

    if version == 7:
        _migrate_to_instance_layout(migrated)
        migrated["schema_version"] = 8
        applied_steps.append(
            "7 -> 8: derive control/, dbs/, state/ and workspaces/ from agent.instance_root"
        )
        version = 8

    if version == 8:
        notes = move_worker_capacity_settings(migrated)
        migrated["schema_version"] = 9
        applied_steps.append(
            "8 -> 9: worker capacity moved to agent.worker_pools"
            + (f" ({'; '.join(notes)})" if notes else "")
        )
        version = 9

    if version == 9:
        # The MCP folder settings (mcp.registry_dir, mcp.hosting.definitions_dir,
        # mcp.hosting.state_dir) became fixed places under runtime/. A setting
        # still holding its default is dropped; a custom one is refused.
        drop_removed_location_settings(migrated)
        migrated["schema_version"] = 10
        applied_steps.append(
            "9 -> 10: MCP connection and hosting folders derived from agent.instance_root"
        )
        version = 10

    refuse_removed_access_settings(migrated)
    agent_cfg = _mapping_or_empty(migrated.get("agent"))
    removed_agent_workspace_keys = {
        "skills_dir",
        "python_envs_dir",
        "task_history_db_path",
        "llm_override_path",
        "team_composition_path",
    }
    stale_keys = sorted(key for key in removed_agent_workspace_keys if key in agent_cfg)
    if stale_keys:
        rendered = ", ".join(f"agent.{key}" for key in stale_keys)
        raise ValueError(
            f"{rendered} has been removed. Use workspace.* settings instead."
        )
    if "workspace_dir" in agent_cfg:
        raise ValueError(
            "agent.workspace_dir has been removed. Set agent.instance_root; control/, "
            "dbs/, state/ and workspaces/ are derived from it."
        )
    workspace_cfg = _mapping_or_empty(migrated.get("workspace"))
    if "task_history_db_path" in workspace_cfg:
        raise ValueError(
            "workspace.task_history_db_path has been removed. Use storage.sqlite.path instead."
        )
    removed_paths = present_removed_location_settings(migrated)
    if removed_paths:
        raise ValueError(
            f"{', '.join(removed_paths)} has been removed. Every location is derived "
            "from agent.instance_root (control/, dbs/, state/, runtime/, workspaces/)."
        )
    if "user_docs" in migrated:
        raise ValueError(
            "user_docs has been removed. Use "
            "interactive_apps.plugins.builtin.internal_website.settings.* instead."
        )
    interactive_apps_cfg = _mapping_or_empty(migrated.get("interactive_apps"))
    removed_apps = sorted(
        set(interactive_apps_cfg).intersection(
            {
                "website",
                "internal_website",
                "discord",
                "telegram",
                "whatsapp",
                "signal",
                "photon",
            }
        )
    )
    if removed_apps:
        rendered = ", ".join(f"interactive_apps.{name}" for name in removed_apps)
        raise ValueError(
            f"{rendered} has been removed. Use "
            "interactive_apps.plugins.<plugin-id>.settings instead."
        )
    security_layer_cfg = _mapping_or_empty(migrated.get("security_layer"))
    if "broker_public_base_url" in security_layer_cfg:
        raise ValueError(
            "security_layer.broker_public_base_url has been removed. Use "
            "security_layer.broker_public_port with network.public_origin instead."
        )

    return migrated, tuple(applied_steps)


def _migrate_to_instance_layout(migrated: dict[str, Any]) -> None:
    """Schema 8: the old workspace folder becomes the instance root.

    Files are not moved here; startup refuses while old-layout files remain and
    prints the commands (runtime_settings.legacy_layout).
    """

    agent_cfg = _mapping_or_empty(migrated.get("agent"))
    if "workspace_dir" in agent_cfg:
        agent_cfg = dict(agent_cfg)
        agent_cfg["instance_root"] = agent_cfg.pop("workspace_dir")
        migrated["agent"] = agent_cfg
    drop_removed_location_settings(migrated)


def migrate_config_file(
    path: str | Path,
    *,
    create_backup: bool = True,
) -> ConfigMigrationResult:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    original_version = _detect_config_version(raw)
    migrated, applied_steps = _migrate_config_dict(raw, source_path=config_path)
    migrated_version = _detect_config_version(migrated)

    backup_path: Optional[Path] = None
    if create_backup:
        backup_path = config_path.with_name(
            f"{config_path.name}.bak.v{original_version}"
        )
        backup_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    config_path.write_text(yaml.safe_dump(migrated, sort_keys=False), encoding="utf-8")
    if backup_path is not None:
        prune_config_backups(
            config_path,
            event_logger=logger,
            preserve_paths=(backup_path,),
        )
    return ConfigMigrationResult(
        path=config_path,
        original_version=original_version,
        migrated_version=migrated_version,
        backup_path=backup_path,
        applied_steps=applied_steps,
    )


def load_config(
    config_path: Optional[str] = None,
    *,
    search_default_paths: bool = True,
) -> Config:
    """
    Load configuration from YAML file and environment variables.

    Args:
        config_path: Explicit path to config file. If None, optionally searches default locations.
        search_default_paths: When False and config_path is None, do not look for repo-root/default config files.

    Returns:
        Config object with loaded settings.
    """
    config_data = {}

    # Determine config file path
    config_file = resolve_config_file_path(
        config_path, search_default_paths=search_default_paths
    )

    if config_file:
        try:
            with open(config_file, "r") as f:
                raw_data = yaml.safe_load(f) or {}
            config_data, applied_steps = _migrate_config_dict(
                raw_data, source_path=config_file
            )
            removed_secret_fields = _drop_env_owned_api_key_fields(config_data)
            validate_agent_tool_lists(config_data)
            logger.info(f"Loaded configuration from {config_file}")
            for step in applied_steps:
                logger.warning(
                    "Loaded legacy config.yaml in compatibility mode from %s; migrate on disk: %s",
                    config_file,
                    step,
                )
            if removed_secret_fields:
                logger.warning(
                    "Ignoring env-owned API key field(s) from %s: %s. "
                    "Use environment variables or *_FILE secret variables instead.",
                    config_file,
                    ", ".join(removed_secret_fields),
                )
        except Exception as e:
            logger.error(f"Failed to load config from {config_file}: {e}")
            raise
    else:
        logger.info("No config file found, using defaults and environment variables")

    # Apply environment variable overrides
    config_data = _apply_env_overrides(config_data)

    return Config(config_data)


def resolve_instance_root(instance_root: Optional[str]) -> str:
    raw = str(instance_root).strip() if instance_root is not None else ""
    return str(Path(raw or DEFAULT_INSTANCE_ROOT).expanduser().resolve())


def instance_layout(instance_root: Optional[str]) -> InstanceLayout:
    return InstanceLayout.at(resolve_instance_root(instance_root))


def resolve_task_history_db_path(
    task_history_db_path: Optional[str],
    *,
    instance_root: Optional[str] = None,
) -> str:
    raw = str(task_history_db_path).strip() if task_history_db_path is not None else ""
    target = Path(raw or DEFAULT_SQLITE_STORAGE_PATH).expanduser()
    if target.is_absolute():
        return str(target.resolve())
    return str((Path(resolve_instance_root(instance_root)) / target).resolve())


def resolve_control_relative_path(
    configured_path: str,
    *,
    instance_root: Optional[str] = None,
) -> str:
    """Resolve an operator-named file relative to ``control/`` (absolute stays)."""

    target = Path(str(configured_path).strip()).expanduser()
    if target.is_absolute():
        return str(target.resolve())
    return str((instance_layout(instance_root).control_dir / target).resolve())


LLM_OVERRIDE_TOP_LEVEL_KEYS = {"default", "agent_types", "model_capabilities"}


LLM_OVERRIDE_ENTRY_KEYS = {
    "provider",
    "model_provider",
    "model_id",
    *MODEL_TUNING_FIELDS,
    *MODEL_CAPABILITY_FIELDS,
}
# A model_capabilities entry also takes facts about the model alone.
MODEL_CAPABILITY_ENTRY_KEYS = LLM_OVERRIDE_ENTRY_KEYS | {MODEL_TOOL_LIMIT_FIELD}


def _positive_int(value: Any) -> Optional[int]:
    """A whole number of at least 1, or None."""

    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isinstance(value, (str, int)) and parsed != value:
        return None
    return parsed if parsed >= 1 else None


# Values already reported, so a bad entry read before every model call is logged once.
_REPORTED_TOOL_LIMITS: set[str] = set()


def _normalized_tool_limit(entry: Mapping[str, Any]) -> dict[str, int]:
    """The entry's max_tools_per_request when it is a whole number of at least 1 (logged once and left out otherwise)."""

    if MODEL_TOOL_LIMIT_FIELD not in entry:
        return {}
    raw = entry.get(MODEL_TOOL_LIMIT_FIELD)
    limit = _positive_int(raw)
    if limit is None:
        if repr(raw) not in _REPORTED_TOOL_LIMITS:
            _REPORTED_TOOL_LIMITS.add(repr(raw))
            logger.warning(
                "A model's max_tools_per_request is not a whole number of at least 1, so the default applies. "
                "event=llm_override_field_ignored field=%s value=%r",
                MODEL_TOOL_LIMIT_FIELD,
                raw,
            )
        return {}
    return {MODEL_TOOL_LIMIT_FIELD: limit}


def _tool_limit_errors(entry: Mapping[str, Any], path: str, allowed: set[str]) -> list[str]:
    """Validation errors for an entry's max_tools_per_request, where the entry may set it."""

    if MODEL_TOOL_LIMIT_FIELD not in entry or MODEL_TOOL_LIMIT_FIELD not in allowed:
        return []
    if _positive_int(entry.get(MODEL_TOOL_LIMIT_FIELD)) is not None:
        return []
    return [f"{path}.{MODEL_TOOL_LIMIT_FIELD} must be a whole number of at least 1."]


def _is_valid_optional_bool_override(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, bool):
        return True
    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "0",
        "false",
        "no",
        "off",
    }


def _normalize_optional_bool_override(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _normalize_llm_override_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    provider = str(entry.get("provider") or entry.get("model_provider") or "").strip()
    model_id = str(entry.get("model_id") or "").strip()
    resolved: Dict[str, Any] = {}
    if provider:
        resolved["model_provider"] = provider
    if model_id:
        resolved["model_id"] = model_id
    for field in MODEL_CAPABILITY_FIELDS:
        if field in entry:
            resolved[field] = _normalize_optional_bool_override(entry.get(field))
    if "reasoning_effort" in entry:
        resolved["reasoning_effort"] = (
            str(entry.get("reasoning_effort") or "").strip() or None
        )
    if "reasoning_summary" in entry:
        try:
            resolved["reasoning_summary"] = normalize_reasoning_summary(
                entry.get("reasoning_summary")
            )
        except ValueError as exc:
            logger.warning(
                "event=llm_override_field_ignored field=reasoning_summary reason=%s",
                exc,
            )
    if "litellm_api_mode" in entry:
        try:
            resolved["litellm_api_mode"] = normalize_litellm_api_mode(
                entry.get("litellm_api_mode")
            )
        except ValueError as exc:
            logger.warning(
                "event=llm_override_field_ignored field=litellm_api_mode reason=%s",
                exc,
            )
    if "context_window_tokens" in entry:
        raw_context_window = entry.get("context_window_tokens")
        if raw_context_window in (None, ""):
            resolved["context_window_tokens"] = None
        else:
            try:
                resolved["context_window_tokens"] = int(raw_context_window)
            except (TypeError, ValueError):
                pass
    resolved.update(_normalized_tool_limit(entry))
    return resolved or None


def validate_llm_override_payload(raw: Any) -> Dict[str, Any]:
    """
    Validate the persistent LLM override file shape.

    This validation is intentionally stricter than load_persistent_llm_override,
    which remains lenient so a bad optional field cannot prevent startup.
    """
    errors: list[str] = []
    warnings: list[str] = []
    agent_types: list[str] = []
    model_capability_entries: list[str] = []

    def add_unknown_key_warnings(entry: dict[str, Any], path: str, allowed: set[str]) -> None:
        for key in sorted(set(entry) - allowed):
            warnings.append(
                f"{path}.{key} is not recognized by the runtime and will be ignored."
            )

    def validate_entry(entry: Any, path: str, allowed: set[str] = LLM_OVERRIDE_ENTRY_KEYS) -> None:
        if not isinstance(entry, dict):
            errors.append(f"{path} must be a mapping.")
            return
        add_unknown_key_warnings(entry, path, allowed)
        errors.extend(_tool_limit_errors(entry, path, allowed))
        for key in ("provider", "model_provider", "model_id"):
            if (
                key in entry
                and entry.get(key) is not None
                and not isinstance(entry.get(key), str)
            ):
                errors.append(f"{path}.{key} must be a string when provided.")
        for field in MODEL_CAPABILITY_FIELDS:
            if field in entry and not _is_valid_optional_bool_override(
                entry.get(field)
            ):
                errors.append(f"{path}.{field} must be true, false, or null.")
        if "reasoning_effort" in entry:
            value = entry.get("reasoning_effort")
            if value is not None and not isinstance(value, str):
                errors.append(f"{path}.reasoning_effort must be a string or null.")
        if "reasoning_summary" in entry:
            try:
                normalize_reasoning_summary(entry.get("reasoning_summary"))
            except ValueError as exc:
                errors.append(f"{path}.reasoning_summary {exc}")
        if "litellm_api_mode" in entry:
            try:
                normalize_litellm_api_mode(entry.get("litellm_api_mode"))
            except ValueError as exc:
                errors.append(f"{path}.litellm_api_mode {exc}")
        if "context_window_tokens" in entry:
            value = entry.get("context_window_tokens")
            if value not in (None, ""):
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    errors.append(
                        f"{path}.context_window_tokens must be an integer or null."
                    )
                else:
                    if parsed < 1024:
                        errors.append(
                            f"{path}.context_window_tokens must be at least 1024."
                        )

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return {
            "valid": False,
            "errors": ["LLM override YAML must contain a top-level mapping."],
            "warnings": [],
            "summary": {
                "agent_types": [],
                "model_capability_entries": [],
            },
        }

    for key in sorted(set(raw) - LLM_OVERRIDE_TOP_LEVEL_KEYS):
        warnings.append(
            f"Top-level key {key!r} is not recognized by the runtime and will be ignored."
        )

    if "default" in raw:
        validate_entry(raw.get("default"), "default")

    raw_agent_types = raw.get("agent_types")
    if raw_agent_types is not None:
        if not isinstance(raw_agent_types, dict):
            errors.append(
                "agent_types must be a mapping from agent type to override entry."
            )
        else:
            for agent_type, entry in raw_agent_types.items():
                agent_type_text = str(agent_type or "").strip()
                if not agent_type_text:
                    errors.append("agent_types contains an empty agent type key.")
                    continue
                agent_types.append(agent_type_text)
                validate_entry(entry, f"agent_types.{agent_type_text}")

    raw_capabilities = raw.get("model_capabilities")
    if raw_capabilities is not None:
        if not isinstance(raw_capabilities, dict):
            errors.append("model_capabilities must be a mapping.")
        else:
            for provider_or_model, provider_value in raw_capabilities.items():
                provider_or_model_text = str(provider_or_model or "").strip()
                if not provider_or_model_text:
                    errors.append("model_capabilities contains an empty key.")
                    continue
                if not isinstance(provider_value, dict):
                    errors.append(
                        f"model_capabilities.{provider_or_model_text} must be a mapping."
                    )
                    continue
                if set(provider_value) & MODEL_CAPABILITY_ENTRY_KEYS:
                    model_capability_entries.append(provider_or_model_text)
                    validate_entry(
                        provider_value,
                        f"model_capabilities.{provider_or_model_text}",
                        MODEL_CAPABILITY_ENTRY_KEYS,
                    )
                    continue
                for model_id, entry in provider_value.items():
                    model_id_text = str(model_id or "").strip()
                    if not model_id_text:
                        errors.append(
                            f"model_capabilities.{provider_or_model_text} contains an empty model id key."
                        )
                        continue
                    model_capability_entries.append(
                        f"{provider_or_model_text}/{model_id_text}"
                    )
                    validate_entry(
                        entry,
                        f"model_capabilities.{provider_or_model_text}.{model_id_text}",
                        MODEL_CAPABILITY_ENTRY_KEYS,
                    )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "agent_types": sorted(agent_types),
            "model_capability_entries": sorted(model_capability_entries),
        },
    }


def _model_capability_map(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _lookup_model_override_entry(
    raw: Any, *, provider: Any, model_id: Any
) -> dict[str, Any]:
    capabilities = _model_capability_map(raw)
    provider_key = str(provider or "").strip()
    model_key = str(model_id or "").strip()
    if not provider_key or not model_key:
        return {}
    candidates: list[Any] = []
    provider_map = capabilities.get(provider_key)
    if isinstance(provider_map, dict):
        candidates.append(provider_map.get(model_key))
    candidates.append(capabilities.get(f"{provider_key}/{model_key}"))
    candidates.append(capabilities.get(model_key))
    resolved: dict[str, Any] = {}
    for candidate in reversed(candidates):
        normalized = _normalize_llm_override_entry(candidate)
        if normalized:
            resolved.update(normalized)
    return resolved


def lookup_configured_model_capabilities(
    raw: Any, *, provider: Any, model_id: Any
) -> dict[str, Any]:
    """Return configured provider/model capability facts from a model_capabilities map."""
    normalized = _lookup_model_override_entry(raw, provider=provider, model_id=model_id)
    return {
        field: normalized[field]
        for field in MODEL_CAPABILITY_FIELDS
        if field in normalized
    }


def lookup_configured_model_tool_limit(raw: Any, *, provider: Any, model_id: Any) -> Optional[int]:
    """How many tools one request to this model may carry, from a model_capabilities map, or None."""

    value = _lookup_model_override_entry(raw, provider=provider, model_id=model_id).get(MODEL_TOOL_LIMIT_FIELD)
    return value if isinstance(value, int) else None


def lookup_configured_model_request_constraints(
    raw: Any,
    *,
    provider: Any,
    model_id: Any,
) -> dict[str, Any]:
    """Return request options pinned for one provider/model combination.

    Unlike capability facts, an explicit null value is meaningful here: it
    disables an option which a broader default or agent-type profile enabled.
    """

    normalized = _lookup_model_override_entry(raw, provider=provider, model_id=model_id)
    return {
        field: normalized[field]
        for field in ("reasoning_summary",)
        if field in normalized
    }


def _apply_known_model_capabilities(
    llm_cfg: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    overwrite: bool = False,
    protected_fields: Optional[set[str]] = None,
) -> None:
    protected = protected_fields or set()
    for field in MODEL_CAPABILITY_FIELDS:
        if field in protected:
            continue
        value = capabilities.get(field)
        if value is not None and (overwrite or llm_cfg.get(field) is None):
            llm_cfg[field] = value


def _explicit_or_layout_path(explicit_path: Optional[str], layout_path: Path) -> Path:
    """An agent carries its resolved control paths; fall back to the layout."""

    raw = str(explicit_path or "").strip()
    return Path(raw).expanduser().resolve() if raw else layout_path


def resolve_llm_override_path(
    *,
    instance_root: Optional[str] = None,
    explicit_override_path: Optional[str] = None,
) -> Path:
    return _explicit_or_layout_path(
        explicit_override_path, instance_layout(instance_root).llm_overrides_path
    )


def resolve_team_templates_path(
    *,
    instance_root: Optional[str] = None,
    explicit_templates_path: Optional[str] = None,
) -> Path:
    return _explicit_or_layout_path(
        explicit_templates_path, instance_layout(instance_root).team_templates_path
    )


def resolve_team_composition_path(
    *,
    instance_root: Optional[str] = None,
    explicit_composition_path: Optional[str] = None,
) -> Path:
    return _explicit_or_layout_path(
        explicit_composition_path, instance_layout(instance_root).team_composition_path
    )


def load_persistent_llm_override(
    *,
    instance_root: Optional[str] = None,
    explicit_override_path: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    override_path = resolve_llm_override_path(
        instance_root=instance_root,
        explicit_override_path=explicit_override_path,
    )
    if override_path is None or not override_path.exists():
        return None

    try:
        with override_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning(
            f"Failed to load persistent LLM overrides from {override_path}: {exc}"
        )
        return None

    if not isinstance(raw, dict):
        logger.warning(
            f"Persistent LLM override file {override_path} did not contain a mapping."
        )
        return None

    resolved = _normalize_llm_override_entry(raw.get("default")) or {}
    if agent_type:
        agent_override = _normalize_llm_override_entry(
            (raw.get("agent_types") or {}).get(agent_type)
        )
        if agent_override:
            # Missing fields inherit the default; explicit nulls clear it.
            # A type may disable an optional setting without pinning its model.
            resolved = deep_merge_dicts(resolved, agent_override)
    model_capabilities = raw.get("model_capabilities")
    if isinstance(model_capabilities, dict):
        resolved["model_capabilities"] = model_capabilities
    return resolved or None


def resolve_agent_type_settings(
    config: Config, agent_type: Optional[str] = None
) -> Dict[str, Any]:
    """
    Resolve runtime settings for one agent type using generic defaults plus optional overrides.

    Generic defaults come from the top-level `transport`, `llm`, and `agent` maps.
    Optional per-agent overrides come from `agent_types.<AgentType>`.
    """
    raw_config = config.to_dict()
    # Configurations built in memory skip the file loader's checks.
    refuse_removed_access_settings(raw_config)
    authorization_cfg = deep_merge_dicts(
        DEFAULT_AUTHORIZATION_CONFIG,
        _mapping_or_empty(raw_config.get("authorization")),
    )
    base_llm_cfg = _mapping_or_empty(raw_config.get("llm"))
    merged = {
        "transport": _mapping_or_empty(raw_config.get("transport")),
        "network": _mapping_or_empty(raw_config.get("network")),
        "llm": base_llm_cfg,
        "agent": _mapping_or_empty(raw_config.get("agent")),
        "workspace": _mapping_or_empty(raw_config.get("workspace")),
        "storage": _mapping_or_empty(raw_config.get("storage")),
        "embedding": _mapping_or_empty(raw_config.get("embedding")),
        "activity_collector": _mapping_or_empty(raw_config.get("activity_collector")),
        "otel": _mapping_or_empty(raw_config.get("otel")),
        "security_layer": _mapping_or_empty(raw_config.get("security_layer")),
        "mcp": _mapping_or_empty(raw_config.get("mcp")),
        "interactive_apps": _mapping_or_empty(raw_config.get("interactive_apps")),
        "access_workflows": _mapping_or_empty(raw_config.get("access_workflows")),
        "execution_control": _mapping_or_empty(raw_config.get("execution_control")),
    }
    if agent_type:
        agent_overrides = _mapping_or_empty(
            _mapping_or_empty(raw_config.get("agent_types")).get(agent_type)
        )
        merged = deep_merge_dicts(merged, agent_overrides)

    merged = normalize_runtime_sections(merged)
    llm_cfg = _mapping_or_empty(merged.get("llm"))
    agent_llm_override = _mapping_or_empty(
        _mapping_or_empty(
            _mapping_or_empty(raw_config.get("agent_types")).get(agent_type)
        ).get("llm")
        if agent_type
        else None
    )
    if is_xai_oauth_provider(agent_llm_override.get("provider")):
        llm_cfg["model_id"] = resolve_xai_oauth_model_id(
            agent_llm_override.get("provider"),
            agent_llm_override.get("model_id"),
            inherited_provider=base_llm_cfg.get("provider"),
            inherited_model_id=base_llm_cfg.get("model_id"),
        )
    network_cfg = _mapping_or_empty(merged.get("network"))
    transport_cfg = _mapping_or_empty(merged.get("transport"))
    agent_cfg = _mapping_or_empty(merged.get("agent"))
    workspace_cfg = _mapping_or_empty(merged.get("workspace"))
    skill_installation_cfg = deep_merge_dicts(
        copy.deepcopy(WORKSPACE_DEFAULTS.skill_installation.value),
        _mapping_or_empty(workspace_cfg.get("skill_installation")),
    )
    storage_cfg = _mapping_or_empty(merged.get("storage"))
    embedding_cfg = _mapping_or_empty(merged.get("embedding"))
    activity_collector_cfg = _mapping_or_empty(merged.get("activity_collector"))
    otel_cfg = _mapping_or_empty(merged.get("otel"))
    security_layer_cfg = _mapping_or_empty(merged.get("security_layer"))
    mcp_cfg = _mapping_or_empty(merged.get("mcp"))
    interactive_apps_cfg = _mapping_or_empty(merged.get("interactive_apps"))
    removed_apps = sorted(
        set(interactive_apps_cfg).intersection(
            {
                "website",
                "internal_website",
                "discord",
                "telegram",
                "whatsapp",
                "signal",
                "photon",
            }
        )
    )
    if removed_apps:
        rendered = ", ".join(f"interactive_apps.{name}" for name in removed_apps)
        raise ValueError(
            f"{rendered} has been removed. Use "
            "interactive_apps.plugins.<plugin-id>.settings instead."
        )
    plugins_cfg = _mapping_or_empty(interactive_apps_cfg.get("plugins"))
    internal_website_entry = _mapping_or_empty(
        plugins_cfg.get("builtin.internal_website")
    )
    internal_website_cfg = _mapping_or_empty(internal_website_entry.get("settings"))
    network_public_origin = str(
        network_cfg.get("public_origin", NETWORK_DEFAULTS.public_origin.value)
        or NETWORK_DEFAULTS.public_origin.value
    ).strip()
    compose_public_base_url(network_public_origin, None)
    if "broker_base_url" in security_layer_cfg:
        raise ValueError(
            "security_layer.broker_base_url has been removed. Use "
            "security_layer.broker_internal_base_url for broker API calls and "
            "security_layer.broker_public_port with network.public_origin for user-facing broker links."
        )
    if "broker_public_base_url" in security_layer_cfg:
        raise ValueError(
            "security_layer.broker_public_base_url has been removed. Use "
            "security_layer.broker_public_port with network.public_origin instead."
        )
    reject_removed_security_layer_settings(security_layer_cfg)
    security_layer_mode = normalize_security_layer_mode(security_layer_cfg.get("mode"))
    code_execution = CodeExecutionSecuritySettings.model_validate(
        security_layer_cfg.get("code_execution") or {}
    )
    secure_tool_broker_internal_base_url = resolve_secure_tool_broker_internal_base_url(
        security_layer_cfg.get(
            "broker_internal_base_url",
            SECURITY_LAYER_DEFAULTS.broker_internal_base_url.value,
        )
    )
    secure_tool_broker_public_port = _normalize_optional_port(
        security_layer_cfg.get(
            "broker_public_port",
            SECURITY_LAYER_DEFAULTS.broker_public_port.value,
        ),
        field_name="security_layer.broker_public_port",
    )
    secure_tool_broker_public_base_url = compose_public_base_url(
        network_public_origin, secure_tool_broker_public_port
    )
    secure_tool_broker_bind_host = _normalize_optional_string(
        security_layer_cfg.get(
            "broker_bind_host", SECURITY_LAYER_DEFAULTS.broker_bind_host.value
        )
    )
    secure_tool_broker_bind_port = _normalize_optional_port(
        security_layer_cfg.get(
            "broker_bind_port", SECURITY_LAYER_DEFAULTS.broker_bind_port.value
        ),
        field_name="security_layer.broker_bind_port",
    )
    secure_tool_broker_credentials_dir = _normalize_optional_string(
        security_layer_cfg.get(
            "broker_credentials_dir",
            SECURITY_LAYER_DEFAULTS.broker_credentials_dir.value,
        )
    )
    internal_website_public_port = _normalize_optional_port(
        internal_website_cfg.get(
            "public_port",
            INTERACTIVE_APP_DEFAULTS.internal_website_public_port.value,
        ),
        field_name=(
            "interactive_apps.plugins.builtin.internal_website.settings.public_port"
        ),
    )
    internal_website_base_url = (
        os.getenv("INTERNAL_WEBSITE_BASE_URL")
        or compose_public_base_url(network_public_origin, internal_website_public_port)
    ).rstrip("/")
    credential_catalog = CredentialCatalogSettings.model_validate(
        security_layer_cfg.get("credential_catalog") or {}
    ).model_dump(mode="json")
    credential_state_backend = CredentialStateBackendSettings.model_validate(
        security_layer_cfg.get("credential_state_backend") or {}
    ).model_dump(mode="json")
    sealbox = SealboxSecuritySettings.model_validate(
        security_layer_cfg.get("sealbox") or {}
    ).model_dump(mode="json")
    instance_root = resolve_instance_root(agent_cfg.get("instance_root"))
    layout = InstanceLayout.at(instance_root)
    # Agents work under workspaces/; the per-task folder inside it is chosen
    # from the person the task runs for (runtime_settings.instance_layout).
    workspace_dir = str(layout.workspaces_dir)
    sqlite_cfg = _mapping_or_empty(storage_cfg.get("sqlite"))
    configured_sqlite_path = sqlite_cfg.get("path")
    resolved_storage = resolve_storage_settings(
        storage_cfg,
        base_dir=instance_root,
        sqlite_fallback_path=configured_sqlite_path,
    )
    task_history_db_path = resolved_storage.sqlite_path or resolve_task_history_db_path(
        configured_sqlite_path,
        instance_root=instance_root,
    )
    skills_dir = str(layout.skills_dir)
    objective_execution_enabled = _parse_bool_config_value(
        "workspace.objective_execution_enabled",
        workspace_cfg.get("objective_execution_enabled"),
        default=WORKSPACE_DEFAULTS.objective_execution_enabled.value,
    )
    configured_objective_execution_paths = workspace_cfg.get(
        "objective_execution_paths",
        WORKSPACE_DEFAULTS.objective_execution_paths.value,
    )
    if not isinstance(configured_objective_execution_paths, list):
        raise ValueError("workspace.objective_execution_paths must be a YAML array")
    if not objective_execution_enabled:
        objective_execution_paths: list[str] = []
    else:
        objective_execution_paths = []
        for index, configured_path in enumerate(configured_objective_execution_paths):
            rendered_path = str(configured_path or "").strip()
            if not rendered_path:
                raise ValueError(
                    f"workspace.objective_execution_paths[{index}] must not be blank"
                )
            if rendered_path == BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI:
                objective_execution_paths.append(
                    str(BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH)
                )
            else:
                objective_execution_paths.append(
                    resolve_control_relative_path(
                        rendered_path, instance_root=instance_root
                    )
                )
    resolved_llm_override_path = layout.llm_overrides_path
    resolved_team_templates_path = layout.team_templates_path
    resolved_team_composition_path = layout.team_composition_path
    persistent_override = load_persistent_llm_override(
        instance_root=instance_root,
        agent_type=agent_type,
    )
    direct_capability_fields = {
        field for field in MODEL_CAPABILITY_FIELDS if llm_cfg.get(field) is not None
    }
    if persistent_override:
        llm_patch: dict[str, Any] = {}
        persistent_direct_capability_fields: set[str] = set()
        if persistent_override.get("model_provider"):
            llm_patch["provider"] = persistent_override["model_provider"]
        if persistent_override.get("model_id"):
            llm_patch["model_id"] = persistent_override["model_id"]
        for field in MODEL_TUNING_FIELDS:
            if field in persistent_override:
                llm_patch[field] = persistent_override[field]
        for field in MODEL_CAPABILITY_FIELDS:
            if field in persistent_override:
                llm_patch[field] = persistent_override[field]
                if persistent_override[field] is not None:
                    persistent_direct_capability_fields.add(field)
        if is_xai_oauth_provider(persistent_override.get("model_provider")):
            llm_patch["model_id"] = resolve_xai_oauth_model_id(
                persistent_override.get("model_provider"),
                persistent_override.get("model_id"),
                inherited_provider=llm_cfg.get("provider"),
                inherited_model_id=llm_cfg.get("model_id"),
            )
        if llm_patch:
            llm_cfg = deep_merge_dicts(llm_cfg, llm_patch)
        direct_capability_fields.update(persistent_direct_capability_fields)
    if (
        is_xai_oauth_provider(llm_cfg.get("provider"))
        and not str(llm_cfg.get("model_id") or "").strip()
    ):
        llm_cfg["model_id"] = resolve_xai_oauth_model_id(llm_cfg.get("provider"), None)
    config_model_capabilities = llm_cfg.get("model_capabilities")
    _apply_known_model_capabilities(
        llm_cfg,
        lookup_configured_model_capabilities(
            config_model_capabilities,
            provider=llm_cfg.get("provider", LLM_DEFAULTS.provider.value),
            model_id=llm_cfg.get("model_id", LLM_DEFAULTS.model_id.value),
        ),
    )
    if persistent_override:
        _apply_known_model_capabilities(
            llm_cfg,
            lookup_configured_model_capabilities(
                persistent_override.get("model_capabilities"),
                provider=llm_cfg.get("provider", LLM_DEFAULTS.provider.value),
                model_id=llm_cfg.get("model_id", LLM_DEFAULTS.model_id.value),
            ),
            overwrite=True,
            protected_fields=direct_capability_fields,
        )
    configured_request_constraints = lookup_configured_model_request_constraints(
        config_model_capabilities,
        provider=llm_cfg.get("provider", LLM_DEFAULTS.provider.value),
        model_id=llm_cfg.get("model_id", LLM_DEFAULTS.model_id.value),
    )
    persistent_request_constraints = lookup_configured_model_request_constraints(
        persistent_override.get("model_capabilities") if persistent_override else None,
        provider=llm_cfg.get("provider", LLM_DEFAULTS.provider.value),
        model_id=llm_cfg.get("model_id", LLM_DEFAULTS.model_id.value),
    )
    llm_cfg.update(configured_request_constraints)
    llm_cfg.update(persistent_request_constraints)
    compactor_cfg = _mapping_or_empty(agent_cfg.get("conversation_compactor"))
    compactor_options = _mapping_or_empty(compactor_cfg.get("options"))
    inner_loop_context = deep_merge_dicts(
        dict(AGENT_DEFAULTS.inner_loop_context.value),
        dict(agent_cfg.get("inner_loop_context", {}) or {}),
    )
    context_engine = deep_merge_dicts(
        dict(AGENT_DEFAULTS.context_engine.value),
        dict(agent_cfg.get("context_engine", {}) or {}),
    )
    memory_config = deep_merge_dicts(
        dict(AGENT_DEFAULTS.memory.value),
        dict(agent_cfg.get("memory", {}) or {}),
    )
    schedule_maintenance = deep_merge_dicts(
        dict(AGENT_DEFAULTS.schedule_maintenance.value),
        dict(agent_cfg.get("schedule_maintenance", {}) or {}),
    )
    debug = deep_merge_dicts(
        dict(AGENT_DEFAULTS.debug.value),
        dict(agent_cfg.get("debug", {}) or {}),
    )
    debug = _apply_terminal_reasoning_env_override(debug)
    llm_failover = deep_merge_dicts(
        dict(LLM_DEFAULTS.failover.value),
        dict(llm_cfg.get("failover", {}) or {}),
    )
    return {
        **_project_transport(transport_cfg=transport_cfg),
        **_project_model(
            llm_cfg=llm_cfg,
            llm_failover=llm_failover,
            resolved_llm_override_path=resolved_llm_override_path,
        ),
        **_project_agent(
            agent_cfg=agent_cfg,
            authorization_cfg=authorization_cfg,
            debug=debug,
            internal_website_base_url=internal_website_base_url,
            internal_website_cfg=internal_website_cfg,
            llm_cfg=llm_cfg,
            mcp_cfg=mcp_cfg,
            memory_config=memory_config,
            network_public_origin=network_public_origin,
            objective_execution_enabled=objective_execution_enabled,
            objective_execution_paths=objective_execution_paths,
            instance_root=instance_root,
            raw_config=raw_config,
            resolved_storage=resolved_storage,
            resolved_team_composition_path=resolved_team_composition_path,
            resolved_team_templates_path=resolved_team_templates_path,
            schedule_maintenance=schedule_maintenance,
            skill_installation_cfg=skill_installation_cfg,
            skills_dir=skills_dir,
            task_history_db_path=task_history_db_path,
            workspace_dir=workspace_dir,
        ),
        **_project_security(
            credential_catalog=credential_catalog,
            credential_state_backend=credential_state_backend,
            sealbox=sealbox,
            secure_tool_broker_bind_host=secure_tool_broker_bind_host,
            secure_tool_broker_bind_port=secure_tool_broker_bind_port,
            secure_tool_broker_credentials_dir=secure_tool_broker_credentials_dir,
            secure_tool_broker_internal_base_url=secure_tool_broker_internal_base_url,
            secure_tool_broker_public_base_url=secure_tool_broker_public_base_url,
            secure_tool_broker_public_port=secure_tool_broker_public_port,
            security_layer_mode=security_layer_mode,
            code_execution=code_execution,
        ),
        **_project_embedding(embedding_cfg=embedding_cfg),
        **_project_context(
            agent_cfg=agent_cfg,
            compactor_cfg=compactor_cfg,
            compactor_options=compactor_options,
            context_engine=context_engine,
            inner_loop_context=inner_loop_context,
        ),
        **_project_telemetry(
            activity_collector_cfg=activity_collector_cfg, otel_cfg=otel_cfg
        ),
    }


def apply_runtime_default_overrides(overrides: Dict[str, Any]) -> Config:
    """
    Publish resolved process-wide runtime defaults for later child-agent resolution.

    This updates the shared in-process config object so agents created later via
    `instantiate_agent_type()` resolve the same generic NATS / LLM / agent
    defaults as the explicitly launched manager, while still allowing
    `agent_types.<AgentType>` overrides from config.yaml to win.
    """
    global _config
    current = _config.to_dict() if _config is not None else {}
    _config = Config(deep_merge_dicts(current, overrides or {}))
    return _config


_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: Config | Dict[str, Any]) -> Config:
    """Replace the global config cache with the provided runtime config."""
    global _config
    _config = config if isinstance(config, Config) else Config(config)
    return _config


def reload_config(
    config_path: Optional[str] = None, *, search_default_paths: bool = True
) -> Config:
    """Reload configuration from file."""
    global _config
    _config = load_config(config_path, search_default_paths=search_default_paths)
    return _config
