from __future__ import annotations

from copy import deepcopy
from typing import Any
from runtime_settings.memory import validate_memory_plugin_config


MEMORY_NOTIFICATION_DELIVERY_TARGET_DEFAULT = "default"
MEMORY_NOTIFICATION_DELIVERY_TARGETS = {
    "default",
    "current_thread",
}

DEFAULT_MEMORY_CONFIG: dict[str, Any] = {
    "plugin_manager": {
        "enabled": True,
        "registry_mode": "builtin",
        "fail_open": True,
        "emit_plugin_warnings": True,
    },
    "plugins": {
        "order": [
            "coremem",
            "usermem",
            "domain_topic_recall",
            "knowledge_recall",
        ],
        "coremem": {"enabled": True, "priority": 34, "pinned": True, "degradable": False},
        "usermem": {"enabled": True, "priority": 35, "pinned": True, "degradable": False},
        "domain_topic_recall": {"enabled": True, "priority": 44, "pinned": False, "degradable": True},
        "knowledge_recall": {"enabled": True, "priority": 45, "pinned": False, "degradable": True, "max_pages": 5},
    },
    "coremem": {
        "enabled": True,
        "max_facts_per_team": 200,
        "max_metadata_chars": 2000,
        "max_tags": 20,
        "max_tag_chars": 64,
        "hydrate_into_context": True,
        "automatic_creation_enabled": True,
        "agent_write_tools_enabled": True,
    },
    "usermem": {
        "enabled": True,
        "max_facts_per_user": 500,
        "require_canonical_user_id": True,
        "max_metadata_chars": 2000,
        "max_tag_chars": 64,
        "max_tags": 20,
        "hydrate_into_context": True,
        "automatic_creation_enabled": True,
        "agent_write_tools_enabled": True,
    },
    "review": {
        "enabled": True,
        "mode": "cue_plus_periodic",
        "periodic_turn_interval": 15,
        "sliding_window_overlap_turns": 5,
        "explicit_cue_immediate": True,
        "max_turn_text_chars": 4000,
        "stale_conversation_after_seconds": 86400,
        "max_stale_conversations_per_run": 20,
        "stale_conversation_review_interval_seconds": 3600,
        "coremem_audit_interval_seconds": 86400,
        "usermem_audit_interval_seconds": 86400,
        "claim_ttl_seconds": 300,
        "memreview_agent_count": 2,
        "review_task_ttl_seconds": 14400,
    },
    "notifications": {
        "quiet_hours_aware": True,
        "active_engagement_grace_seconds": 300,
        "delivery_target": MEMORY_NOTIFICATION_DELIVERY_TARGET_DEFAULT,
    },
    "domain": {
        "enabled": True,
        "auto_tag_threads": True,
        "auto_tag_turns": True,
        "visible_notices": True,
        "max_tags_per_thread": 8,
        "max_tags_per_turn": 5,
        "allow_switch_by_tag": True,
    },
}


def normalize_memory_notification_delivery_target(value: Any) -> str:
    rendered = str(value or MEMORY_NOTIFICATION_DELIVERY_TARGET_DEFAULT).strip().lower()
    aliases = {
        "preferred": "default",
        "preferred_profile": "default",
        "default_target": "default",
        "current": "current_thread",
        "thread": "current_thread",
        "source_thread": "current_thread",
    }
    normalized = aliases.get(rendered, rendered)
    if normalized not in MEMORY_NOTIFICATION_DELIVERY_TARGETS:
        allowed = ", ".join(sorted(MEMORY_NOTIFICATION_DELIVERY_TARGETS))
        raise ValueError(
            "agent.memory.notifications.delivery_target must be one of "
            f"{allowed}; got {value!r}."
        )
    return normalized


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def normalize_memory_config(config: dict[str, Any] | None) -> dict[str, Any]:
    normalized = deep_merge_dicts(DEFAULT_MEMORY_CONFIG, dict(config or {}))
    manager = dict(normalized.get("plugin_manager") or {})
    if str(manager.get("registry_mode") or "builtin") != "builtin":
        raise ValueError("agent.memory.plugin_manager.registry_mode supports only 'builtin'.")
    validate_memory_plugin_config(normalized)
    notifications = dict(normalized.get("notifications") or {})
    notifications["delivery_target"] = normalize_memory_notification_delivery_target(
        notifications.get("delivery_target", MEMORY_NOTIFICATION_DELIVERY_TARGET_DEFAULT)
    )
    normalized["notifications"] = notifications
    return normalized


def memory_notification_delivery_target(config: dict[str, Any] | None) -> str:
    normalized = normalize_memory_config(config)
    notifications = dict(normalized.get("notifications") or {})
    return normalize_memory_notification_delivery_target(
        notifications.get("delivery_target", MEMORY_NOTIFICATION_DELIVERY_TARGET_DEFAULT)
    )


def plugin_settings(config: dict[str, Any], plugin_id: str) -> dict[str, Any]:
    return dict(((config.get("plugins") or {}).get(plugin_id)) or {})


def memory_bank_settings(config: dict[str, Any] | None, bank: str) -> dict[str, Any]:
    normalized = normalize_memory_config(config)
    return dict(normalized.get(str(bank or "").strip().lower()) or {})


def bank_automatic_creation_enabled(config: dict[str, Any] | None, bank: str) -> bool:
    return bool(memory_bank_settings(config, bank).get("automatic_creation_enabled", True))


def bank_agent_write_tools_enabled(config: dict[str, Any] | None, bank: str) -> bool:
    return bool(memory_bank_settings(config, bank).get("agent_write_tools_enabled", True))
