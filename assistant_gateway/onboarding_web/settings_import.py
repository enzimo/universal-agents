"""Read onboarding form values from an uploaded config.yaml or instance_config.yaml.

The "I'm migrating from another UA instance" path only fills form fields, so
it reads just those fields from the parsed YAML. It does not run the full
settings loaders: they refuse removed settings and old schema versions by
design (a running instance must not start with them), but an old file is
exactly what a person migrating has. Everything the import leaves out is
listed in plain words so the person knows what to set up again:

- settings this version removed,
- the old instance's folder and name (the new instance keeps its own),
- whole sections onboarding never imports (access, MCP servers, agent types),
- settings no version of the files knows,
- values that could not be read.

Secrets are never read into the form: fields such as tokens and database
connection strings are simply not in the field list.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from assistant_gateway.config import REMOVED_TEAM_MODEL_KEYS_SETTING, GatewayInstanceConfig
from runtime_settings.appearance import OnboardingAppearanceSettings
from runtime_settings.common import removed_mcp_server_keys
from runtime_settings.governance import removed_access_settings
from runtime_settings.legacy_layout import present_removed_location_settings
from runtime_settings.secrets_source import normalize_secrets_source
from runtime_settings.security import (
    REMOVED_CREDENTIAL_CATALOG_KEYS,
    REMOVED_SEALBOX_KEYS,
    REMOVED_SECURE_CLI_SETTING,
    REMOVED_SECURITY_LAYER_MODE,
    CredentialCatalogSettings,
    SealboxSecuritySettings,
)

LOGGER = logging.getLogger(__name__)

IMPORT_KIND_INSTANCE_CONFIG = "instance_config"
IMPORT_KIND_CONFIG = "config"

REASON_REMOVED = "Removed in this version, so it was not imported."
REASON_SECTION = "Onboarding does not import this section. Set it up again in the new instance if you need it."
REASON_INSTANCE_ROOT = "The old instance's folder. The new instance keeps its own folder."
REASON_INSTANCE_NAME = "The old instance's name. The new instance keeps its own name."
REASON_UNKNOWN = "Not a setting this version knows, so it was not imported."
REASON_INVALID = "The value could not be read, so the default was kept."
REASON_PARTLY = "Only the settings onboarding asks for were imported. Not imported: {names}."
REASON_BROKER_ALWAYS_RUNS = (
    "The 'disabled' mode was removed in this version: the secure tool broker always runs. "
    "The new instance uses 'broker'."
)

# Sections with a more specific note than REASON_SECTION.
SECTION_REASONS: dict[str, str] = {
    "authorization": "Access settings are not imported. Set up access in the new instance.",
    "access_workflows": "Access settings are not imported. Set up access in the new instance.",
    "mcp": "MCP servers are not imported. Install them again in the new instance.",
    "mcp_profiles": "MCP servers are not imported. Install them again in the new instance.",
    "agent_types": "Agent type settings are not imported.",
}

# Removed settings the refusing validators name one by one
# (GatewayInstanceConfig and the config.yaml migrations). The shared helpers
# below cover access, location and MCP server settings.
_REMOVED_PATHS: tuple[tuple[str, ...], ...] = (
    ("manager",),
    ("agent_default",),
    ("user_docs",),
    ("automation_jobs",),
    ("workspace_dir",),
    ("agent", "workspace_dir"),
    ("workspace", "task_history_db_path"),
    ("workspace", "objective_execution_path"),
    ("security_layer", "broker_public_base_url"),
    ("security_layer", REMOVED_SECURE_CLI_SETTING),
    *(("security_layer", "sealbox", key) for key in REMOVED_SEALBOX_KEYS),
    *(("security_layer", "credential_catalog", key) for key in REMOVED_CREDENTIAL_CATALOG_KEYS),
    ("interactive_apps", "website"),
    REMOVED_TEAM_MODEL_KEYS_SETTING,
)
_SILENT_TOP_LEVEL = frozenset({"schema_version"})
_MAX_NAMES_PER_LINE = 8

SettingPath = tuple[str, ...]


def _site(*keys: str) -> tuple[SettingPath, ...]:
    """The website setting under the current and the pre-plugin (schema < 6) shape."""

    return (
        ("interactive_apps", "plugins", "builtin.internal_website", "settings", *keys),
        ("interactive_apps", "internal_website", *keys),
    )


def _channel(name: str, *keys: str) -> tuple[SettingPath, ...]:
    return (
        ("interactive_apps", "plugins", f"builtin.{name}", "settings", *keys),
        ("interactive_apps", name, *keys),
    )


@dataclass(frozen=True)
class _Field:
    """One form value: where it may sit in either file, and its shape."""

    key: str
    paths: tuple[SettingPath, ...]
    shape: str = "scalar"  # scalar | list | mapping
    # For mappings: the sub-keys the form uses (nested dicts allowed); None keeps
    # the value as it is. Everything else, secrets included, is dropped.
    keep: Mapping[str, Any] | None = None


_SECURITY_LAYER_KEEP: dict[str, Any] = {
    key: None
    for key in (
        "mode",
        "broker_internal_base_url",
        "broker_public_port",
        "broker_bind_host",
        "broker_bind_port",
        "broker_credentials_dir",
        "credential_state_backend",
        "bitwarden_server_url",
    )
}
# Only the Sealbox and credential catalog settings this version has: a removed
# one would make the new instance's settings refuse to load (it is listed as
# removed instead).
_SECURITY_LAYER_KEEP["sealbox"] = {key: None for key in SealboxSecuritySettings.model_fields}
_SECURITY_LAYER_KEEP["credential_catalog"] = {
    key: None for key in CredentialCatalogSettings.model_fields
}
_ACTIVITY_COLLECTOR_KEEP: dict[str, Any] = {
    **{
        key: None
        for key in (
            "base_url",
            "connect_timeout_seconds",
            "request_timeout_seconds",
            "batch_max_events",
            "flush_interval_seconds",
            "max_buffered_events",
            "drop_when_unavailable",
            "bind_host",
            "bind_port",
            "user_url_path",
            "admin_url_path",
            "otel_trace_base_url",
            "ingest_token_env_var",
        )
    },
    # Only the backend and SQLite path: a PostgreSQL connection string is a secret.
    "storage": {"backend": None, "sqlite": {"path": None}},
}
_FAILOVER_KEEP: dict[str, Any] = {
    key: None for key in ("enabled", "switch_for", "demotion", "notify_user", "fallbacks")
}
_EMBEDDING_KEEP: dict[str, Any] = {
    key: None for key in ("provider", "model_id", "api_base", "dimensions", "timeout_seconds", "organization")
}

FIELDS: tuple[_Field, ...] = (
    _Field("secrets_source", (("launch", "secrets_source"),)),
    _Field("network_public_origin", (("network", "public_origin"),)),
    _Field("internal_website_bind_port", _site("bind_port")),
    _Field("internal_website_public_port", _site("public_port")),
    _Field("internal_website_docs_root", _site("docs_root")),
    _Field("internal_website_skills_dir", _site("skills_dir")),
    _Field("web_chat_enabled", _site("web_chat_enabled")),
    _Field("team_skills_repositories", (("workspace", "external_skill_repositories"),), "list"),
    _Field("team_id", (("gateway", "team_id"), ("agent", "default_team_id"))),
    _Field("timezone", (("user", "timezone"), ("agent", "default_request_timezone"))),
    _Field("user_display_name", (("user", "display_name"),)),
    _Field("user_role", (("user", "role"),)),
    _Field("user_context", (("user", "initial_context"),)),
    _Field("assistant_name", (("assistant", "display_name"),)),
    _Field("llm_provider", (("llm", "provider"),)),
    _Field("llm_litellm_api_base", (("llm", "litellm_api_base"),)),
    _Field("model_id", (("llm", "model_id"),)),
    _Field("openai_codex", (("llm", "openai_codex"),), "mapping"),
    _Field("xai_oauth", (("llm", "xai_oauth"),), "mapping"),
    _Field("reasoning_effort", (("llm", "reasoning_effort"),)),
    _Field("reasoning_summary", (("llm", "reasoning_summary"),)),
    _Field("litellm_api_mode", (("llm", "litellm_api_mode"),)),
    _Field("context_window_tokens", (("llm", "context_window_tokens"),)),
    _Field("stream", (("llm", "stream"),)),
    _Field("supports_image_input", (("llm", "supports_image_input"),)),
    _Field("supports_audio_input", (("llm", "supports_audio_input"),)),
    _Field("supports_file_input", (("llm", "supports_file_input"),)),
    _Field("model_capabilities", (("llm", "model_capabilities"),), "mapping"),
    _Field("llm_failover", (("llm", "failover"),), "mapping", _FAILOVER_KEEP),
    _Field("embedding_enabled", (("agent", "memory_graph_enabled"),)),
    _Field("embedding", (("embedding",),), "mapping", _EMBEDDING_KEEP),
    _Field("quiet_enabled", (("delivery", "quiet_hours", "enabled"),)),
    _Field("quiet_start", (("delivery", "quiet_hours", "start_hour_local"),)),
    _Field("quiet_end", (("delivery", "quiet_hours", "end_hour_local"),)),
    _Field("default_delivery_channel", (("delivery", "default_target", "channel_name"),)),
    _Field("default_delivery_chat_id", (("delivery", "default_target", "chat_id"),)),
    _Field("voice_enabled", (("voice", "enabled"),)),
    _Field("voice_response_mode", (("voice", "response_mode"),)),
    _Field("security_layer", (("security_layer",),), "mapping", _SECURITY_LAYER_KEEP),
    _Field("activity_collector", (("activity_collector",),), "mapping", _ACTIVITY_COLLECTOR_KEEP),
    _Field("startup_agents", (("gateway", "startup_agents"),), "list"),
    _Field("appearance", (("ui", "onboarding", "appearance"),), "mapping"),
    _Field("telegram_enabled", _channel("telegram", "enabled")),
    _Field("telegram_allowed_user_ids", _channel("telegram", "allowed_user_ids"), "list"),
    _Field("telegram_allowed_chat_ids", _channel("telegram", "allowed_chat_ids"), "list"),
    _Field("telegram_manager_offline_policy", _channel("telegram", "manager_offline_policy")),
    _Field("telegram_rich_messages", _channel("telegram", "rich_messages")),
    _Field("discord_enabled", _channel("discord", "enabled")),
    _Field("discord_allowed_user_ids", _channel("discord", "allowed_user_ids"), "list"),
    _Field("discord_allowed_channel_ids", _channel("discord", "allowed_channel_ids"), "list"),
    _Field("discord_group_policy", _channel("discord", "group_policy")),
    _Field("signal_enabled", _channel("signal", "enabled")),
    _Field("signal_base_url", _channel("signal", "base_url")),
    _Field("signal_account", _channel("signal", "account")),
    _Field("signal_allowed_sender_ids", _channel("signal", "allowed_sender_ids"), "list"),
    _Field("signal_allowed_group_ids", _channel("signal", "allowed_group_ids"), "list"),
    _Field("signal_group_policy", _channel("signal", "group_policy")),
    _Field("signal_mention_markers", _channel("signal", "mention_markers"), "list"),
    _Field("signal_poll_timeout_seconds", _channel("signal", "poll_timeout_seconds")),
    _Field("signal_max_messages_per_poll", _channel("signal", "max_messages_per_poll")),
    _Field("signal_drop_pending_messages_on_startup", _channel("signal", "drop_pending_messages_on_startup")),
    _Field("photon_enabled", _channel("photon", "enabled")),
    _Field("photon_project_id", _channel("photon", "project_id")),
    _Field("photon_allowed_sender_ids", _channel("photon", "allowed_sender_ids"), "list"),
    _Field("photon_allowed_group_ids", _channel("photon", "allowed_group_ids"), "list"),
    _Field("photon_group_policy", _channel("photon", "group_policy")),
    _Field("photon_mention_markers", _channel("photon", "mention_markers"), "list"),
    _Field("whatsapp_enabled", _channel("whatsapp", "enabled")),
    _Field("whatsapp_allowed_sender_ids", _channel("whatsapp", "allowed_sender_ids"), "list"),
    _Field("whatsapp_webhook_path", _channel("whatsapp", "webhook_path")),
)
IMPORTED_SECTIONS = frozenset(path[0] for field in FIELDS for path in field.paths)


class OnboardingImportError(ValueError):
    """The uploaded file cannot be read at all; the message is shown as it is."""


@dataclass(frozen=True)
class SkippedSetting:
    path: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class ImportedSettings:
    kind: str
    values: dict[str, Any]
    skipped: tuple[SkippedSetting, ...]


def read_onboarding_import(filename: str, content: str) -> ImportedSettings:
    """Parse ``content`` and pick the form values; raise only when it is not readable YAML."""

    raw = _parse_mapping(filename, content)
    kind = IMPORT_KIND_INSTANCE_CONFIG if looks_like_instance_config(raw, filename) else IMPORT_KIND_CONFIG
    values: dict[str, Any] = {}
    skipped: list[SkippedSetting] = []
    consumed: set[SettingPath] = set()
    for field in FIELDS:
        _import_field(raw, field, values=values, skipped=skipped, consumed=consumed)
    removed = _removed_paths(raw)
    skipped = [
        *(SkippedSetting(path, REASON_REMOVED) for path in removed),
        *_drop_removed_security_mode(values),
        *_top_level_skips(raw, removed),
        *_partly_imported_sections(raw, consumed, removed),
        *skipped,
    ]
    return ImportedSettings(kind=kind, values=values, skipped=tuple(skipped))


def looks_like_instance_config(raw: Mapping[str, Any], filename: str) -> bool:
    """Every instance_config.yaml has top-level instance_name and instance_root.

    config.yaml keeps its instance root under agent.instance_root and shares
    sections such as gateway and delivery, so those do not tell the two apart.
    """

    return "instance_config" in filename.lower() or "instance_name" in raw or "instance_root" in raw


def _parse_mapping(filename: str, content: str) -> dict[str, Any]:
    name = Path(filename or "uploaded.yaml").name
    if not str(content or "").strip():
        raise OnboardingImportError(f"{name} is empty.")
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "it could not be read"
        raise OnboardingImportError(f"{name} is not valid YAML{where}: {problem}.") from exc
    if not isinstance(raw, dict):
        raise OnboardingImportError(
            f"{name} does not hold settings. Choose a config.yaml or instance_config.yaml file."
        )
    return raw


def _lookup(raw: Mapping[str, Any], path: SettingPath) -> tuple[bool, Any]:
    node: Any = raw
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _import_field(
    raw: Mapping[str, Any],
    field: _Field,
    *,
    values: dict[str, Any],
    skipped: list[SkippedSetting],
    consumed: set[SettingPath],
) -> None:
    for path in field.paths:
        found, value = _lookup(raw, path)
        if not found:
            continue
        consumed.add(path)
        converted, ok = _convert(field, value)
        if ok:
            values[field.key] = converted
        else:
            skipped.append(SkippedSetting(".".join(path), REASON_INVALID))
        return


def _convert(field: _Field, value: Any) -> tuple[Any, bool]:
    if field.key == "secrets_source":
        try:
            return normalize_secrets_source(value), True
        except ValueError:
            return None, False
    if field.key == "appearance":
        return _appearance(value)
    if field.key == "startup_agents":
        return _startup_agents(value)
    if field.shape == "list":
        return (list(value), True) if isinstance(value, list) and all(_is_scalar(item) for item in value) else (None, False)
    if field.shape == "mapping":
        return (_kept(value, field.keep), True) if isinstance(value, Mapping) else (None, False)
    return (value, True) if _is_scalar(value) else (None, False)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _kept(value: Mapping[str, Any], keep: Mapping[str, Any] | None) -> dict[str, Any]:
    if keep is None:
        return dict(value)
    result: dict[str, Any] = {}
    for key, nested in keep.items():
        if key not in value:
            continue
        item = value[key]
        result[key] = _kept(item, nested) if nested is not None and isinstance(item, Mapping) else item
    return result


def _appearance(value: Any) -> tuple[Any, bool]:
    try:
        return OnboardingAppearanceSettings.model_validate(value).model_dump(mode="json"), True
    except ValidationError:
        return None, False


def _startup_agents(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, list):
        return None, False
    agents = []
    for item in value:
        if not isinstance(item, Mapping) or not str(item.get("named_agent_type") or "").strip():
            return None, False
        agents.append(
            {
                "named_agent_type": str(item["named_agent_type"]).strip(),
                "count": item.get("count", 1),
                "enabled": item.get("enabled", True),
            }
        )
    return agents, True


def _drop_removed_security_mode(values: dict[str, Any]) -> list[SkippedSetting]:
    """Leave out the removed ``security_layer.mode: disabled`` so the form keeps ``broker``."""

    security_layer = values.get("security_layer")
    if not isinstance(security_layer, dict):
        return []
    if str(security_layer.get("mode") or "").strip().lower() != REMOVED_SECURITY_LAYER_MODE:
        return []
    security_layer.pop("mode")
    return [SkippedSetting("security_layer.mode", REASON_BROKER_ALWAYS_RUNS)]


def _removed_paths(raw: Mapping[str, Any]) -> list[str]:
    removed = [*removed_access_settings(raw), *present_removed_location_settings(raw)]
    removed += [".".join(path) for path in _REMOVED_PATHS if _lookup(raw, path)[0]]
    agent = raw.get("agent")
    if isinstance(agent, Mapping):
        removed += [
            f"agent.external_mcp_servers.{name}"
            for name in removed_mcp_server_keys(agent.get("external_mcp_servers"))
        ]
    return list(dict.fromkeys(removed))


def _top_level_skips(raw: Mapping[str, Any], removed: list[str]) -> list[SkippedSetting]:
    removed_top_level = {path for path in removed if "." not in path}
    skips: list[SkippedSetting] = []
    for key in raw:
        name = str(key)
        if name in IMPORTED_SECTIONS or name in _SILENT_TOP_LEVEL or name in removed_top_level:
            continue
        skips.append(SkippedSetting(name, _top_level_reason(name)))
    return skips


def _top_level_reason(name: str) -> str:
    if name == "instance_root":
        return REASON_INSTANCE_ROOT
    if name == "instance_name":
        return REASON_INSTANCE_NAME
    if name in SECTION_REASONS:
        return SECTION_REASONS[name]
    return REASON_SECTION if name in _known_top_level_keys() else REASON_UNKNOWN


def _partly_imported_sections(
    raw: Mapping[str, Any],
    consumed: set[SettingPath],
    removed: list[str],
) -> list[SkippedSetting]:
    """One line per imported section naming the settings in it the form does not use."""

    removed_set = set(removed)
    skips: list[SkippedSetting] = []
    for section in sorted(name for name in IMPORTED_SECTIONS if isinstance(raw.get(name), Mapping)):
        if (section,) in consumed:
            continue
        names = _unconsumed_paths(raw[section], (section,), consumed, removed_set)
        if names:
            skips.append(SkippedSetting(section, REASON_PARTLY.format(names=_listed(names))))
    return skips


def _unconsumed_paths(node: Any, prefix: SettingPath, consumed: set[SettingPath], removed: set[str]) -> list[str]:
    if prefix in consumed or ".".join(prefix) in removed:
        return []
    if not isinstance(node, Mapping):
        return [".".join(prefix[1:])]
    found = []
    for key, value in node.items():
        path = (*prefix, str(key))
        if not any(c[:len(path)] == path for c in consumed):
            if ".".join(path) not in removed:
                found.append(".".join(path[1:]))
        else:
            found.extend(_unconsumed_paths(value, path, consumed, removed))
    return found


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:_MAX_NAMES_PER_LINE])
    rest = len(names) - _MAX_NAMES_PER_LINE
    return f"{shown} and {rest} more" if rest > 0 else shown


@lru_cache(maxsize=1)
def _known_top_level_keys() -> frozenset[str]:
    """Top-level names either file may hold in this version.

    instance_config.yaml's come from its model; config.yaml's from the
    checked-in config.example.yaml, which documents that file's schema.
    """

    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    try:
        example_keys = set(yaml.safe_load(example.read_text(encoding="utf-8")) or {})
    except (OSError, yaml.YAMLError) as exc:
        # Only the wording of skipped config.yaml sections depends on it.
        LOGGER.warning(
            "config.example.yaml could not be read; config.yaml sections are described as unknown. "
            "event=onboarding_import_example_unreadable path=%s error_type=%s",
            example,
            type(exc).__name__,
        )
        example_keys = set()
    return frozenset(set(GatewayInstanceConfig.model_fields) | example_keys)


__all__ = [
    "IMPORT_KIND_CONFIG",
    "IMPORT_KIND_INSTANCE_CONFIG",
    "ImportedSettings",
    "OnboardingImportError",
    "SkippedSetting",
    "looks_like_instance_config",
    "read_onboarding_import",
]
