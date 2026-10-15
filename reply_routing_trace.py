"""Shared helpers for opt-in reply routing diagnostics."""

from __future__ import annotations

from typing import Any


_TRUTHY_STRINGS = {"1", "true", "yes", "on"}


def is_reply_routing_trace_enabled(debug_config: Any) -> bool:
    """Return whether reply-routing trace logging is enabled in agent debug config."""

    if not isinstance(debug_config, dict):
        return False

    raw_config = debug_config.get("reply_routing_trace")
    if isinstance(raw_config, dict):
        return bool(raw_config.get("enabled", False))
    if isinstance(raw_config, bool):
        return raw_config
    if raw_config is None:
        return False
    return str(raw_config).strip().lower() in _TRUTHY_STRINGS


def compact_gateway_context(gateway_context: Any) -> dict[str, Any]:
    """Extract non-secret gateway/session fields useful for reply routing traces."""

    if gateway_context is None:
        return {}
    return {
        "gateway_instance_name": getattr(gateway_context, "gateway_instance_name", None),
        "session_key": getattr(gateway_context, "session_key", None),
        "channel_name": getattr(gateway_context, "channel_name", None),
        "user_id": getattr(gateway_context, "user_id", None),
        "canonical_user_id": getattr(gateway_context, "canonical_user_id", None),
        "chat_id": getattr(gateway_context, "chat_id", None),
        "thread_id": getattr(gateway_context, "thread_id", None),
        "delivery_mode": getattr(gateway_context, "delivery_mode", None),
        "scheduled_job_id": getattr(gateway_context, "scheduled_job_id", None),
        "heartbeat_source_id": getattr(gateway_context, "heartbeat_source_id", None),
        "original_message_id": getattr(gateway_context, "original_message_id", None),
    }


def compact_content_context(content: Any) -> dict[str, Any]:
    """Extract stable content identifiers without rendering user-visible text."""

    content_id: Any = None
    get_id = getattr(content, "get_id", None)
    if callable(get_id):
        try:
            content_id = get_id()
        except Exception:
            content_id = None
    return {
        "content_type": content.__class__.__name__ if content is not None else None,
        "content_id": content_id,
        "task_id": getattr(content, "task_id", None),
        "parent_task_id": getattr(content, "parent_task_id", None),
        "task_type": getattr(content, "task_type", None),
    }
