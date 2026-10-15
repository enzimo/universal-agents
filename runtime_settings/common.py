"""Common configuration and contracts."""

from __future__ import annotations
from typing import Any, Dict, Optional
from default_values import CONVERSATION_COMPACTION_DEFAULTS, INTERACTIVE_APP_DEFAULTS


TELEGRAM_MANAGER_OFFLINE_POLICY_NOTIFY = "notify"


TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING = "pause_telegram_polling"


DEFAULT_TELEGRAM_MANAGER_OFFLINE_POLICY = (
    INTERACTIVE_APP_DEFAULTS.telegram_manager_offline_policy.value
)


DEFAULT_DISCORD_GROUP_POLICY = INTERACTIVE_APP_DEFAULTS.group_policy.value


REMOVED_PUBLIC_URL_ENV_VARS = (
    "USER_DOCS_BIND_HOST",
    "USER_DOCS_BIND_PORT",
    "USER_DOCS_BASE_URL",
    "USER_DOCS_ROOT",
    "USER_DOCS_SKILLS_DIR",
    "WEBSITE_APP_ENABLED",
    "WEBSITE_APP_BIND_HOST",
    "WEBSITE_APP_BIND_PORT",
    "WEBSITE_APP_BASE_URL",
    "WEBSITE_APP_SITES_DIR",
    "SECURE_TOOL_BROKER_PUBLIC_BASE_URL",
)


def _normalized_optional_string(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


# Keys an external_mcp_servers entry no longer has. continue_on_error went when
# agents started loading servers before model calls with bounded retries: a
# server that does not answer never stops an agent from starting any more.
_REMOVED_MCP_SERVER_KEYS = {
    "continue_on_error": (
        "is no longer used: agents load MCP servers before model calls and retry a server that does not "
        "answer (mcp.load_retry_window_minutes), so it never stops an agent from starting. Delete the key; "
        "the server's definition then changes and it waits for approval in Settings again"
    ),
}


def removed_mcp_server_keys(servers: Any) -> list[str]:
    """``<name>.<key>`` for each removed key in an external_mcp_servers list (never raises)."""

    found: list[str] = []
    for index, entry in enumerate(servers if isinstance(servers, list) else []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or f"#{index + 1}"
        found += [f"{name}.{key}" for key in _REMOVED_MCP_SERVER_KEYS if key in entry]
    return found


def _normalize_external_mcp_server_list(value: Any, *, preserve_none: bool) -> Any:
    if value is None:
        return None if preserve_none else []
    if isinstance(value, dict) and not value:
        return []
    for index, entry in enumerate(value if isinstance(value, list) else []):
        if isinstance(entry, dict):
            _refuse_removed_mcp_server_parts(entry, entry.get("name") or f"#{index + 1}")
    return value


def _refuse_removed_mcp_server_parts(entry: Dict[str, Any], name: str) -> None:
    """Refuse removed keys, and headers that still read a key now kept in the credential store."""

    from runtime_settings.services import removed_key_references

    for key, why in _REMOVED_MCP_SERVER_KEYS.items():
        if key in entry:
            raise ValueError(f"external_mcp_servers entry {name}: {key} {why}.")
    references = removed_key_references(entry)
    if references:
        raise ValueError(
            f"external_mcp_servers entry {name} reads a key that is no longer an environment variable "
            f"({', '.join(references)}). Replace that header with a credential slot, for example "
            "credential_injections: [{target_kind: header, target_name: Authorization, field_name: api_token, "
            f"template: 'Bearer {{value}}'}}], then link the key with /link mcp:{name} team."
        )


def _render_url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _normalized_string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    normalized: list[str] = []
    for raw in values:
        item = str(raw or "").strip()
        if item:
            normalized.append(item)
    return normalized


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Return defaults filled with existing values, preserving existing nested config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing
    merged = dict(defaults)
    for key, value in existing.items():
        if key in merged:
            merged[key] = _merge_missing_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def _default_conversation_compactor_config() -> Dict[str, Any]:
    return {
        "type": CONVERSATION_COMPACTION_DEFAULTS.compactor_type.value,
        "options": {
            "trigger_ratio": CONVERSATION_COMPACTION_DEFAULTS.trigger_ratio.value,
            "target_ratio": CONVERSATION_COMPACTION_DEFAULTS.target_ratio.value,
            "min_recent_turns": CONVERSATION_COMPACTION_DEFAULTS.min_recent_turns.value,
            "max_recent_turns": CONVERSATION_COMPACTION_DEFAULTS.max_recent_turns.value,
        },
    }
