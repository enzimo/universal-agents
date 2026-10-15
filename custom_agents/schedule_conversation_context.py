"""Conversation-thread helpers for user-facing scheduled work."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ProgressState, RequesterType
from id_generation import (
    SCHEDULE_CONVERSATION_SEED_ID_PREFIX,
    new_id,
    operational_id,
)
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.system_schedules import (
    internal_delivery_modes,
    internal_schedule_name_prefixes,
)
from storage.conversation_repository import TURN_MEMORY_REVIEW_IGNORED

def schedule_conversation_seed_task_id(schedule_id: str) -> str:
    safe_schedule_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(schedule_id or "unknown")).strip("_")
    return operational_id(
        SCHEDULE_CONVERSATION_SEED_ID_PREFIX,
        safe_schedule_id[:96] or "unknown",
    )


def is_user_facing_recurring_schedule(scheduled_message: ScheduledMessage) -> bool:
    payload = getattr(scheduled_message, "payload", None)
    if not isinstance(payload, Task) or payload.parent_task_id is not None:
        return False
    if not (
        scheduled_message.interval_ms is not None
        or bool(scheduled_message.cron_expression)
        or scheduled_message.repeat_count is None
        or int(scheduled_message.repeat_count or 0) > 1
    ):
        return False

    # Internal maintenance (the system schedule table's ``internal`` kinds) is
    # never user-facing: neither a schedule named in one of their name families
    # nor one whose runs carry their delivery mode.
    schedule_name = str(scheduled_message.schedule_name or "")
    if schedule_name.startswith(internal_schedule_name_prefixes()):
        return False

    gateway_context = _gateway_context(scheduled_message)
    delivery_mode = str(getattr(gateway_context, "delivery_mode", "") or "")
    if delivery_mode in internal_delivery_modes():
        return False
    return True


def ensure_dedicated_schedule_conversation(
    agent: Any,
    scheduled_message: ScheduledMessage,
    *,
    source_header: Optional[MessageHeader] = None,
    source_meta: Optional[MessageMeta] = None,
    source_task: Optional[Task] = None,
    logger: Optional[logging.Logger] = None,
) -> tuple[ScheduledMessage, Optional[dict[str, Any]]]:
    """Fork user-facing recurring schedules into a dedicated conversation thread."""
    if not is_user_facing_recurring_schedule(scheduled_message):
        return scheduled_message, None

    conversation_store = getattr(agent, "_conversation_store", None)
    log = logger or getattr(agent, "_logger", None) or logging.getLogger(__name__)
    if conversation_store is None:
        log.warning(
            "[%s] ScheduledMessage %s is recurring but no ConversationStore is available; "
            "using existing payload_meta.conversation_id.",
            getattr(agent, "id", None),
            scheduled_message.schedule_id,
        )
        return scheduled_message, None

    normalized = scheduled_message.model_copy(deep=True)
    meta = (
        normalized.payload_meta.model_copy(deep=True)
        if normalized.payload_meta is not None
        else (source_meta.model_copy(deep=True) if source_meta is not None else MessageMeta())
    )
    seed_task_id = schedule_conversation_seed_task_id(normalized.schedule_id)
    existing_seed = conversation_store.get_turn_by_task_id(seed_task_id)
    if existing_seed:
        meta.conversation_id = existing_seed.get("conversation_id")
        meta.followup_to_task_id = seed_task_id
        normalized.payload_meta = meta
        return normalized, {
            "conversation_id": meta.conversation_id,
            "seed_task_id": seed_task_id,
            "reused_existing_seed": True,
        }

    source_conversation_id = (
        getattr(source_meta, "conversation_id", None)
        or getattr(normalized.payload_meta, "conversation_id", None)
        or getattr(meta, "conversation_id", None)
    )
    preallocated_conversation_id = getattr(meta, "conversation_id", None)
    target_conversation_id = (
        preallocated_conversation_id
        if preallocated_conversation_id and preallocated_conversation_id != source_conversation_id
        else new_id("conversation_id")
    )
    source_task_id = (
        getattr(meta, "followup_to_task_id", None)
        or getattr(source_task, "task_id", None)
        or getattr(source_meta, "followup_to_task_id", None)
    )

    title = _schedule_conversation_title(normalized)
    seed_problem = _schedule_seed_problem_text(
        scheduled_message=normalized,
        source_conversation_id=source_conversation_id,
        source_task_id=source_task_id,
        source_task=source_task,
    )
    seed_answer = (
        f"Dedicated conversation for recurring scheduled job {normalized.schedule_id}. "
        "Future fires of this job should use this thread to remember prior scheduled reports."
    )
    # The schedule's owner as its record states it (the store projects the
    # row's owner there; the schedule tools require the task's verified person).
    owner_canonical_user_id = owner_canonical_user_id_for_schedule(normalized)

    thread = conversation_store.ensure_thread(
        conversation_id=target_conversation_id,
        team_id=getattr(agent, "team_id", None),
        owner_canonical_user_id=owner_canonical_user_id,
        owner_agent_id=getattr(agent, "id", None),
        owner_agent_name=getattr(agent, "name", None) or getattr(agent, "named_agent_type", None),
        requester_name=getattr(source_meta, "from_agent_name", None) if source_meta is not None else getattr(meta, "from_agent_name", None),
        requester_id=getattr(source_header, "from_agent_id", None) if source_header is not None else None,
        root_task_id=seed_task_id,
        subject_preview=title[:160],
    )
    seed_turn = conversation_store.append_turn(
        conversation_id=target_conversation_id,
        task_id=seed_task_id,
        followup_to_task_id=None,
        owner_agent_id=getattr(agent, "id", None),
        owner_named_agent_type=getattr(agent, "named_agent_type", None),
        requester_name=getattr(source_meta, "from_agent_name", None) if source_meta is not None else getattr(meta, "from_agent_name", None),
        requester_id=getattr(source_header, "from_agent_id", None) if source_header is not None else None,
        correlation_id=getattr(meta, "msg_correlation_id", None),
        problem_text=seed_problem,
        requester_type=_seed_requester_type(source_meta),
        delivery_mode=getattr(_gateway_context(normalized), "delivery_mode", None),
        delivery_state="not_applicable",
        gateway_context=_gateway_context_dict(normalized),
        task_status=ProgressState.COMPLETED.value,
        memory_review_status=TURN_MEMORY_REVIEW_IGNORED,
        owner_canonical_user_id=owner_canonical_user_id,
    )
    updated_seed = conversation_store.update_turn_completion(
        task_id=seed_task_id,
        task_status=ProgressState.COMPLETED.value,
        final_answer_text=seed_answer,
        turn_summary_text=seed_answer,
        user_request_summary=f"Create recurring scheduled job: {title}",
        assistant_response_summary=seed_answer,
        context_summary_text=(
            f"Recurring scheduled job {normalized.schedule_id} was forked from "
            f"conversation {source_conversation_id or 'unknown'}"
            + (f" task {source_task_id}." if source_task_id else ".")
        ),
    )

    meta.conversation_id = target_conversation_id
    meta.followup_to_task_id = seed_task_id
    normalized.payload_meta = meta
    return normalized, {
        "conversation_id": target_conversation_id,
        "source_conversation_id": source_conversation_id,
        "source_task_id": source_task_id,
        "seed_task_id": seed_task_id,
        "thread": thread,
        "seed_turn": updated_seed or seed_turn,
        "reused_existing_seed": False,
    }


def _gateway_context(scheduled_message: ScheduledMessage) -> Optional[GatewayContext]:
    meta = getattr(scheduled_message, "payload_meta", None)
    gateway_context = getattr(meta, "gateway_context", None) if meta is not None else None
    return gateway_context if isinstance(gateway_context, GatewayContext) else None


def _gateway_context_dict(scheduled_message: ScheduledMessage) -> Optional[dict[str, Any]]:
    gateway_context = _gateway_context(scheduled_message)
    return gateway_context.model_dump(mode="json") if gateway_context is not None else None


def _schedule_conversation_title(scheduled_message: ScheduledMessage) -> str:
    if scheduled_message.schedule_name:
        return str(scheduled_message.schedule_name)
    problem = str(getattr(scheduled_message.payload, "problem_statement", "") or "").strip()
    return problem[:120] or f"Scheduled job {scheduled_message.schedule_id}"


def _schedule_seed_problem_text(
    *,
    scheduled_message: ScheduledMessage,
    source_conversation_id: Optional[str],
    source_task_id: Optional[str],
    source_task: Optional[Task],
) -> str:
    lines = [
        "Create a dedicated conversation for this recurring scheduled job.",
        f"Schedule id: {scheduled_message.schedule_id}",
        f"Schedule name: {scheduled_message.schedule_name or '(none)'}",
        f"Source conversation id: {source_conversation_id or '(unknown)'}",
        f"Source task id: {source_task_id or '(unknown)'}",
        "",
        "Scheduled instruction:",
        str(getattr(scheduled_message.payload, "problem_statement", "") or "").strip() or "(no instruction stored)",
    ]
    source_text = str(getattr(source_task, "problem_statement", "") or "").strip()
    if source_text:
        lines.extend(["", "Source task request:", source_text])
    return "\n".join(lines)


def _seed_requester_type(source_meta: Optional[MessageMeta]) -> str:
    requester_type = getattr(source_meta, "requester_type", None) if source_meta is not None else None
    if requester_type == RequesterType.TIMER_REQUESTER:
        return RequesterType.AGENT_REQUESTER.value
    if hasattr(requester_type, "value"):
        return requester_type.value
    return requester_type or RequesterType.AGENT_REQUESTER.value
