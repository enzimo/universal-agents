"""Helpers for request-scoped prompt context assembly."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.enums import RequesterType
from custom_messages.system_schedules import internal_delivery_modes


def _looks_like_cross_conversation_recall_request(text: Optional[str]) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return False

    direct_patterns = (
        "what have we talked about",
        "what did we talk about",
        "have we talked about",
        "did we talk about",
        "in the past",
        "in previous conversations",
        "in our previous conversations",
        "other conversations",
        "other threads",
        "previous threads",
        "past threads",
        "older conversations",
        "before this thread",
    )
    if any(pattern in normalized for pattern in direct_patterns):
        return True

    recall_tokens = ("remember", "recall", "discussed", "talked", "conversation", "thread")
    time_tokens = ("before", "previous", "past", "earlier", "other")
    return any(token in normalized for token in recall_tokens) and any(token in normalized for token in time_tokens)


def _allow_cross_conversation_recall_hint(msg_meta: Optional[MessageMeta]) -> bool:
    if msg_meta is None:
        return False

    requester_type = getattr(msg_meta, "requester_type", None)
    requester_value = getattr(requester_type, "value", requester_type)
    if requester_value in {
        RequesterType.AGENT_REQUESTER.value,
        RequesterType.SELF_REQUESTER.value,
        RequesterType.TIMER_REQUESTER.value,
    }:
        return False

    correlation_id = str(getattr(msg_meta, "msg_correlation_id", "") or "")
    if correlation_id.startswith(("memory_review_", "schedule_corr_")):
        return False

    gateway_context = getattr(msg_meta, "gateway_context", None)
    delivery_mode = getattr(gateway_context, "delivery_mode", None) if gateway_context is not None else None
    if isinstance(gateway_context, dict):
        delivery_mode = gateway_context.get("delivery_mode")
    # Internal maintenance runs (heartbeat, memory review; custom_messages.system_schedules).
    if delivery_mode in internal_delivery_modes():
        return False

    return True


def normalize_request_timezone_name(
    timezone_name: Optional[str],
    *,
    logger: Optional[logging.Logger] = None,
    agent_id: Optional[str] = None,
) -> Optional[str]:
    if not timezone_name:
        return None
    try:
        return ZoneInfo(str(timezone_name)).key
    except ZoneInfoNotFoundError:
        if logger is not None:
            prefix = f"[{agent_id}] " if agent_id else ""
            logger.warning("%sIgnoring invalid request timezone '%s'.", prefix, timezone_name)
        return None


def detect_system_request_timezone(
    *,
    logger: Optional[logging.Logger] = None,
    agent_id: Optional[str] = None,
) -> Optional[str]:
    local_tz = datetime.now().astimezone().tzinfo
    zone_key = getattr(local_tz, "key", None)
    return normalize_request_timezone_name(zone_key, logger=logger, agent_id=agent_id)


def effective_default_request_timezone(
    default_request_timezone: Optional[str],
    *,
    logger: Optional[logging.Logger] = None,
    agent_id: Optional[str] = None,
) -> Optional[str]:
    configured = normalize_request_timezone_name(
        default_request_timezone,
        logger=logger,
        agent_id=agent_id,
    )
    if configured:
        return configured
    return detect_system_request_timezone(logger=logger, agent_id=agent_id)


def ensure_request_timezone(
    msg_meta: MessageMeta,
    *,
    top_level_request: bool,
    default_request_timezone: Optional[str],
    logger: Optional[logging.Logger] = None,
    agent_id: Optional[str] = None,
) -> MessageMeta:
    normalized_meta = msg_meta.model_copy(deep=True) if msg_meta else MessageMeta()
    normalized_timezone = normalize_request_timezone_name(
        getattr(normalized_meta, "request_timezone", None),
        logger=logger,
        agent_id=agent_id,
    )
    if normalized_timezone:
        normalized_meta.request_timezone = normalized_timezone
        return normalized_meta
    if top_level_request:
        default_timezone = effective_default_request_timezone(
            default_request_timezone,
            logger=logger,
            agent_id=agent_id,
        )
        if default_timezone:
            normalized_meta.request_timezone = default_timezone
    return normalized_meta


def build_request_time_context(
    msg_meta: Optional[MessageMeta],
    *,
    logger: Optional[logging.Logger] = None,
    agent_id: Optional[str] = None,
) -> str:
    request_timezone = normalize_request_timezone_name(
        getattr(msg_meta, "request_timezone", None) if msg_meta else None,
        logger=logger,
        agent_id=agent_id,
    )
    if not request_timezone:
        return ""
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ZoneInfo(request_timezone))
    return (
        "Request Time Context:\n"
        f"- Interpret relative dates like `today`, `tomorrow`, `yesterday`, and `latest as of now` in `{request_timezone}`.\n"
        f"- Current UTC time: {now_utc.isoformat()}\n"
        f"- Current requester-local time: {local_now.isoformat()}\n"
        f"- Current requester-local weekday: {local_now.strftime('%A')}\n"
        f"- Current requester-local date: {local_now.date().isoformat()}"
    )


def build_request_conversation_context(
    msg_meta: Optional[MessageMeta],
    *,
    request_text: Optional[str] = None,
) -> str:
    if msg_meta is None:
        return ""

    conversation_id = getattr(msg_meta, "conversation_id", None)
    followup_to_task_id = getattr(msg_meta, "followup_to_task_id", None)
    gateway_context = getattr(msg_meta, "gateway_context", None)
    session_key = getattr(gateway_context, "session_key", None) if gateway_context else None

    if not conversation_id and not session_key and not followup_to_task_id:
        return ""

    lines = ["Request Conversation Context:"]
    if session_key:
        lines.append(f"- Current session_key: {session_key}")
    if conversation_id:
        lines.append(f"- Current conversation_id: {conversation_id}")
    if followup_to_task_id:
        lines.append(
            f"- This request is a follow-up within the current conversation thread, linked to prior task {followup_to_task_id}."
        )
    elif conversation_id:
        lines.append("- This request is the first top-level turn currently recorded for this thread.")
        lines.append(
            "- No earlier turns from this thread should be assumed unless an explicit Conversation Thread Context block is present below."
        )
        lines.append(
            "- Treat pinned memory, archived tasks, and summaries of older interactions as cross-thread or cross-session context unless the current conversation context explicitly links them to this conversation_id."
        )
    if _allow_cross_conversation_recall_hint(msg_meta) and _looks_like_cross_conversation_recall_request(request_text):
        lines.append(
            "- This request appears to ask about prior discussions beyond the active thread. If the active thread context is insufficient, inspect other conversations explicitly with list_conversations, get_conversation_summary, and get_conversation_context_slice before answering."
        )
    return "\n".join(lines)
