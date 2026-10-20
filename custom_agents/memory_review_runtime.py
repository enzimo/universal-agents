"""Shared helpers for conversation and pinned-memory MemReview jobs.

Scheduled memory review has two scopes:

- Team schedule: the core-facts (coremem) audit, one per instance, because
  coremem belongs to the whole team.
- Per-person schedules: the personal-facts (usermem) audit and the
  stale-conversation review, one pair for each active person, owned by that
  person (``scope`` ``PERSON`` in ``custom_messages.system_schedules``). A run
  works for its owner and prepares jobs only from that person's facts and
  conversations
  (``custom_agents.scheduled_runtime.prepare_memory_review_schedule``), and the
  review tools act only on jobs for the task's person
  (``access_control.ownership.RESOURCE_MEMORY_REVIEW``). So one team-wide
  schedule owned by the first user would review only the first user; each
  person needs their own pair. The gateway installs the pairs
  (``assistant_gateway.application.maintenance_schedules``); when a person
  leaves the team, the manager deletes their per-person schedules with the
  rest of their schedules.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from access_control.ownership import PRIVATE_READ, RESOURCE_CONVERSATION, private_access
from custom_agents.memory_plugins.base import ReviewJobRef
from custom_agents.memory_plugins.config import (
    bank_automatic_creation_enabled,
    memory_notification_delivery_target,
    normalize_memory_config,
)
from custom_agents.pinned_memory_runtime import public_pinned_memory_fact
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
)
from custom_messages.system_schedules import (
    COREMEM_AUDIT_SCHEDULE_NAME_PREFIX,
    MEMORY_REVIEW_SCHEDULE_PREFIX,
    STALE_CONVERSATION_SCHEDULE_NAME_PREFIX,
    USERMEM_AUDIT_SCHEDULE_NAME_PREFIX,
)
from id_generation import new_id

CONVERSATION_WINDOW_REVIEW_KIND = "conversation_window"
COREMEM_AUDIT_REVIEW_KIND = "coremem_audit"
USERMEM_AUDIT_REVIEW_KIND = "usermem_audit"

def memory_review_notification_instruction(
    memory_config: dict[str, Any] | None,
) -> str:
    target = memory_notification_delivery_target(memory_config)
    if target == "current_thread":
        return (
            "If you create or change memory or topic metadata for the active source conversation, "
            'notify the user with notify_user(target="current_thread"); otherwise use '
            'notify_user(target="default").'
        )
    return (
        "If you create or change memory or topic metadata that should be user-visible, "
        'notify the user with notify_user(target="default") so the notice goes to the '
        "configured default notification channel, even when the job includes source-conversation "
        "gateway context."
    )


def memory_review_settings(memory_config: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_memory_config(memory_config)
    return dict(normalized.get("review") or {})


def review_enabled(memory_config: dict[str, Any] | None) -> bool:
    return bool(memory_review_settings(memory_config).get("enabled", True))


def pinned_memory_automatic_creation_enabled(memory_config: dict[str, Any] | None) -> bool:
    return (
        bank_automatic_creation_enabled(memory_config, "coremem")
        or bank_automatic_creation_enabled(memory_config, "usermem")
    )


def review_int_setting(settings: dict[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def review_interval_turns(settings: dict[str, Any]) -> int:
    return review_int_setting(settings, "periodic_turn_interval", 15, minimum=1)


def review_overlap_turns(settings: dict[str, Any]) -> int:
    return review_int_setting(settings, "sliding_window_overlap_turns", 5, minimum=0)


def review_max_turn_text_chars(settings: dict[str, Any]) -> int:
    return review_int_setting(settings, "max_turn_text_chars", 4000, minimum=500)


def stale_after_seconds(settings: dict[str, Any]) -> int:
    return review_int_setting(settings, "stale_conversation_after_seconds", 24 * 60 * 60, minimum=60)


def max_stale_conversations_per_run(settings: dict[str, Any]) -> int:
    return review_int_setting(settings, "max_stale_conversations_per_run", 20, minimum=1)


def explicit_cue_immediate_enabled(settings: dict[str, Any]) -> bool:
    return bool(settings.get("explicit_cue_immediate", True))


def is_memory_review_schedule_name(schedule_name: str | None) -> bool:
    return str(schedule_name or "").startswith(MEMORY_REVIEW_SCHEDULE_PREFIX)


def memory_review_schedule_kind(schedule_name: str | None) -> str | None:
    rendered = str(schedule_name or "")
    if rendered.startswith(COREMEM_AUDIT_SCHEDULE_NAME_PREFIX):
        return COREMEM_AUDIT_REVIEW_KIND
    if rendered.startswith(USERMEM_AUDIT_SCHEDULE_NAME_PREFIX):
        return USERMEM_AUDIT_REVIEW_KIND
    if rendered.startswith(STALE_CONVERSATION_SCHEDULE_NAME_PREFIX):
        return CONVERSATION_WINDOW_REVIEW_KIND
    return None


def _gateway_context_payload(meta: Any) -> dict[str, Any] | None:
    gateway_context = getattr(meta, "gateway_context", None) if meta is not None else None
    if gateway_context is None:
        return None
    if hasattr(gateway_context, "model_dump"):
        return gateway_context.model_dump(mode="json")
    if isinstance(gateway_context, dict):
        return dict(gateway_context)
    return None


def _canonical_user_from_meta(meta: Any) -> str | None:
    """The person a stored turn's gateway context names: a claim, used only to narrow.

    The stale review uses it to skip a conversation whose latest turn names
    someone other than the review schedule's owner; it never decides whom a
    job is made for.
    """

    gateway_context = getattr(meta, "gateway_context", None) if meta is not None else None
    value = getattr(gateway_context, "canonical_user_id", None) if gateway_context is not None else None
    normalized = str(value or "").strip()
    return normalized or None


def _has_explicit_memory_request(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().split()).lower()
    if not normalized:
        return False
    explicit_patterns = (
        "remember this",
        "remember that",
        "please remember",
        "call me ",
        "my name is ",
        "i prefer ",
        "i like ",
        "from now on",
        "we decided",
        "going forward",
        "always use",
        "never use",
        "store this in memory",
    )
    return any(pattern in normalized for pattern in explicit_patterns)


def _is_reviewable_completed_user_turn(task: Task, msg_meta: Optional[MessageMeta], *, task_status: str) -> bool:
    if task.parent_task_id is not None or task.task_type != MessageMetaType.REQUEST:
        return False
    if task_status != "completed":
        return False
    if msg_meta is None:
        return False
    requester_type = getattr(msg_meta, "requester_type", None)
    if requester_type == RequesterType.TIMER_REQUESTER:
        return False
    return requester_type == RequesterType.USER_REQUESTER or getattr(msg_meta, "gateway_context", None) is not None


def _truncate_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 80]}\n...[truncated {len(text) - max_chars + 80} chars]"


def _turn_payload(turn: dict[str, Any], *, max_text_chars: int) -> dict[str, Any]:
    return {
        "turn_index": turn.get("turn_index"),
        "task_id": turn.get("task_id"),
        "task_status": turn.get("task_status"),
        "memory_review_status": turn.get("memory_review_status"),
        "created_at": turn.get("created_at"),
        "updated_at": turn.get("updated_at"),
        "problem_text": _truncate_text(turn.get("problem_text"), max_chars=max_text_chars),
        "user_request_summary": _truncate_text(turn.get("user_request_summary"), max_chars=max_text_chars),
        "assistant_response_summary": _truncate_text(turn.get("assistant_response_summary"), max_chars=max_text_chars),
        "context_summary_text": _truncate_text(turn.get("context_summary_text"), max_chars=max_text_chars),
        "turn_summary_text": _truncate_text(turn.get("turn_summary_text"), max_chars=max_text_chars),
        "final_answer_text": _truncate_text(turn.get("final_answer_text"), max_chars=max_text_chars),
        "clarification_summary_text": _truncate_text(
            turn.get("clarification_summary_text"),
            max_chars=max_text_chars,
        ),
    }


def _pending_review_kind_exists(
    *,
    store: Any,
    team_id: str,
    review_kind: str,
    limit: int = 200,
    canonical_user_id: str | None = None,
) -> bool:
    for job in store.list_review_jobs(limit=limit, owner_team_id=team_id, owner_named_agent_type=MEM_REVIEW_AGENT_TYPE, pending_only=True):
        if getattr(job, "owner_team_id", None) != team_id:
            continue
        if getattr(job, "owner_named_agent_type", None) != MEM_REVIEW_AGENT_TYPE:
            continue
        if getattr(job, "status", None) not in {"queued", "claimed"}:
            continue
        payload = dict(getattr(job, "source_payload", None) or {})
        if payload.get("review_kind") == review_kind and (
            canonical_user_id is None or payload.get("canonical_user_id") == canonical_user_id
        ):
            return True
    return False


def enqueue_conversation_window_review_job(
    *,
    memory_review_store: Any,
    conversation_store: Any,
    team_id: str,
    named_agent_type: str | None,
    agent_id: str | None,
    task: Task,
    msg_meta: Optional[MessageMeta],
    canonical_user_id: str | None,
    task_status: str,
    memory_config: dict[str, Any] | None,
    trigger: str,
    allow_partial: bool = False,
    logger: logging.Logger | None = None,
) -> ReviewJobRef | None:
    """Queue a review of the conversation's unreviewed turns, made for ``canonical_user_id``.

    ``canonical_user_id`` is the person the turn verifiably works for (its
    signed context, ``memory_plugins.base``) or the review schedule's owner,
    never the gateway context's claim: the job holds the conversation's turns
    and the review tools show it only to that person. With no person, no job is
    made, since no one could review it; the turns stay unreviewed. (A claim
    that did not verify is already logged where the person is resolved,
    ``access_runtime.verified_meta_person_id``.)

    The stored conversation must belong to that person before any turns are
    read or queued; the unsigned message's conversation id proves no ownership.
    """

    if not review_enabled(memory_config):
        return None
    if not pinned_memory_automatic_creation_enabled(memory_config):
        return None
    if named_agent_type == MEM_REVIEW_AGENT_TYPE and trigger != "stale_conversation":
        return None
    if memory_review_store is None or conversation_store is None or not team_id:
        return None
    if not _is_reviewable_completed_user_turn(task, msg_meta, task_status=task_status):
        return None
    conversation_id = getattr(msg_meta, "conversation_id", None)
    if not conversation_id or not canonical_user_id:
        return None

    # Review tools trust the job's person, so its source must be that person's
    # conversation too. Refuse unknown ownership rather than assign it from a claim.
    thread = conversation_store.get_thread(conversation_id)
    if not private_access(
        person_id=canonical_user_id,
        owner_id=thread.get("owner_canonical_user_id") if thread else None,
        resource_type=RESOURCE_CONVERSATION,
        resource_id=conversation_id,
        action=PRIVATE_READ,
    ).allowed:
        return None

    settings = memory_review_settings(memory_config)
    explicit_cue = _has_explicit_memory_request(task.problem_statement or "")
    allow_now = allow_partial or (explicit_cue and explicit_cue_immediate_enabled(settings))
    window = conversation_store.get_memory_review_window(
        conversation_id=conversation_id,
        max_unreviewed_turns=review_interval_turns(settings),
        overlap_turns=review_overlap_turns(settings),
        allow_partial=allow_now,
    )
    if window is None:
        return None

    max_text_chars = review_max_turn_text_chars(settings)
    payload = {
        "review_kind": CONVERSATION_WINDOW_REVIEW_KIND,
        "plugin_id": "pinned_memory_review",
        "trigger": trigger,
        "source_owner_named_agent_type": named_agent_type,
        "source_agent_id": agent_id,
        "owner_team_id": team_id,
        "canonical_user_id": canonical_user_id,
        "gateway_context": _gateway_context_payload(msg_meta),
        "task_id": task.task_id,
        "conversation_id": conversation_id,
        "turn_status": task_status,
        "window_turn_range": {
            "start": window["window_start_turn"],
            "end": window["window_end_turn"],
            "overlap_turns": window["overlap_turns"],
        },
        "reviewed_turn_range": {
            "start": window["first_unreviewed_turn"],
            "end": window["last_unreviewed_turn"],
            "unreviewed_turn_indexes": list(window["unreviewed_turn_indexes"]),
        },
        "turns": [_turn_payload(turn, max_text_chars=max_text_chars) for turn in window["turns"]],
        "instructions": (
            "Review the conversation window. Create or replace coremem/usermem only for durable facts; "
            "to replace one, list the bank and pass the fact's record_digest. "
            "Use topic_tags with action tag_turn for one or more topic tags per turn when useful, and action "
            "tag_thread for thread-level topics. "
            "Resolve the memory review job when complete."
        ),
        "persisted": True,
    }
    job = memory_review_store.enqueue_review_job(
        owner_team_id=team_id,
        owner_named_agent_type=MEM_REVIEW_AGENT_TYPE,
        task_id=task.task_id,
        conversation_id=conversation_id,
        turn_index=int(window["last_unreviewed_turn"]),
        source_payload=payload,
    )
    conversation_store.mark_turns_memory_review_queued(
        conversation_id=conversation_id,
        start_turn=int(window["first_unreviewed_turn"]),
        end_turn=int(window["last_unreviewed_turn"]),
        review_job_id=job.review_job_id,
    )
    if logger is not None:
        logger.info(
            "Enqueued conversation window memory review job %s for conversation %s turns %s-%s.",
            job.review_job_id,
            conversation_id,
            window["first_unreviewed_turn"],
            window["last_unreviewed_turn"],
        )
    return ReviewJobRef(job_id=job.review_job_id, plugin_id="pinned_memory_review", payload=payload | {"review_job_id": job.review_job_id})


def enqueue_stale_conversation_review_jobs(
    *,
    memory_review_store: Any,
    conversation_store: Any,
    team_id: str,
    named_agent_type: str | None,
    agent_id: str | None,
    memory_config: dict[str, Any] | None,
    canonical_user_id: str,
    now_utc: str | None = None,
    logger: logging.Logger | None = None,
) -> list[ReviewJobRef]:
    """Queue reviews of ``canonical_user_id``'s stale conversations, each job made for them.

    ``canonical_user_id`` is the review schedule's owner, whom its run
    verifiably works for (``prepare_memory_review_schedule``).
    """

    if not review_enabled(memory_config):
        return []
    if memory_review_store is None or conversation_store is None or not team_id or not canonical_user_id:
        return []
    settings = memory_review_settings(memory_config)
    now_dt = datetime.fromisoformat((now_utc or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    stale_before = (now_dt.astimezone(timezone.utc) - timedelta(seconds=stale_after_seconds(settings))).isoformat()
    candidates = conversation_store.list_conversation_memory_review_candidates(
        team_id=team_id,
        stale_before_utc=stale_before,
        canonical_user_id=canonical_user_id,
        limit=max_stale_conversations_per_run(settings),
    )
    jobs: list[ReviewJobRef] = []
    for candidate in candidates:
        conversation_id = candidate["conversation_id"]
        last_unreviewed_turn = int(candidate.get("last_unreviewed_turn") or 0)
        if last_unreviewed_turn <= 0:
            continue
        turns = conversation_store.get_conversation_turns(
            conversation_id=conversation_id,
            start_turn=last_unreviewed_turn,
            limit=1,
        )
        turn = turns[0] if turns else None
        if not turn:
            continue
        latest_task_id = str(turn.get("task_id") or new_id("memory_review"))
        task = Task(
            task_id=latest_task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=str(turn.get("problem_text") or ""),
        )
        gateway_context = None
        raw_gateway_context = turn.get("gateway_context")
        if isinstance(raw_gateway_context, GatewayContext):
            gateway_context = raw_gateway_context
        elif isinstance(raw_gateway_context, dict) and raw_gateway_context:
            try:
                gateway_context = GatewayContext.model_validate(raw_gateway_context)
            except Exception:
                gateway_context = None
        meta = MessageMeta(
            conversation_id=conversation_id,
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=gateway_context,
        )
        if _canonical_user_from_meta(meta) != canonical_user_id:
            continue
        job_ref = enqueue_conversation_window_review_job(
            memory_review_store=memory_review_store,
            conversation_store=conversation_store,
            team_id=team_id,
            named_agent_type=named_agent_type,
            agent_id=agent_id,
            task=task,
            msg_meta=meta,
            canonical_user_id=canonical_user_id,
            task_status="completed",
            memory_config=memory_config,
            trigger="stale_conversation",
            allow_partial=True,
            logger=logger,
        )
        if job_ref is not None:
            jobs.append(job_ref)
    return jobs


def enqueue_pinned_memory_audit_job(
    *,
    memory_review_store: Any,
    pinned_memory_repository: Any,
    team_id: str,
    named_agent_type: str | None,
    agent_id: str | None,
    memory_config: dict[str, Any] | None,
    bank: str,
    canonical_user_id: str,
    now_utc: str | None = None,
    logger: logging.Logger | None = None,
) -> ReviewJobRef | None:
    """Queue an audit of the team's core facts or of one person's personal facts.

    ``canonical_user_id`` is the review schedule's owner, whom the run works
    for; the job is made for them, because the review tools show a job only to
    its person (``RESOURCE_MEMORY_REVIEW``). A personal-facts (usermem) audit
    holds only that person's facts. There is no audit of everyone's personal
    facts: such a job would hold every person's private facts and be made for
    nobody, so no review could read it. No job is made when there are no facts
    to audit.
    """

    if not review_enabled(memory_config):
        return None
    if memory_review_store is None or pinned_memory_repository is None or not team_id or not canonical_user_id:
        return None
    now = now_utc or datetime.now(timezone.utc).isoformat()
    normalized_bank = str(bank or "").strip().lower()
    if normalized_bank in {"coremem", "usermem"} and not bank_automatic_creation_enabled(
        memory_config,
        normalized_bank,
    ):
        if logger is not None:
            logger.info(
                "Skipping %s pinned-memory audit because automatic creation is disabled for that bank.",
                normalized_bank,
            )
        return None
    if normalized_bank == "coremem":
        if _pending_review_kind_exists(
            store=memory_review_store,
            team_id=team_id,
            review_kind=COREMEM_AUDIT_REVIEW_KIND,
        ):
            return None
        facts_payload = [
            public_pinned_memory_fact(row)
            for row in pinned_memory_repository.list_coremem(team_id=team_id, now_utc=now, sort="memory_id")
        ]
        review_kind = COREMEM_AUDIT_REVIEW_KIND
    elif normalized_bank == "usermem":
        if _pending_review_kind_exists(
            store=memory_review_store,
            team_id=team_id,
            review_kind=USERMEM_AUDIT_REVIEW_KIND,
            canonical_user_id=canonical_user_id,
        ):
            return None
        rows = pinned_memory_repository.list_usermem(
            canonical_user_id=canonical_user_id,
            now_utc=now,
            sort="memory_id",
        )
        # One group, for the job's person (the review prompt reads facts per person).
        facts_payload = (
            [{"canonical_user_id": canonical_user_id, "facts": [public_pinned_memory_fact(row) for row in rows]}]
            if rows
            else []
        )
        review_kind = USERMEM_AUDIT_REVIEW_KIND
    else:
        raise ValueError("bank must be 'coremem' or 'usermem'.")
    if not facts_payload:
        return None

    task_id = new_id("mem_audit")
    conversation_id = f"memory_audit:{team_id}:{normalized_bank}"
    payload = {
        "review_kind": review_kind,
        "plugin_id": "pinned_memory_review",
        "trigger": f"scheduled_{normalized_bank}_audit",
        "source_owner_named_agent_type": named_agent_type,
        "source_agent_id": agent_id,
        "owner_team_id": team_id,
        "task_id": task_id,
        "conversation_id": conversation_id,
        "canonical_user_id": canonical_user_id,
        "facts": facts_payload,
        "instructions": (
            f"Review active {normalized_bank} facts for validity, overlap, and conflicts. "
            "If a fact is outdated or invalid, use replace/delete tools so the pinned-memory event archive "
            "permanently records the fact text plus valid_from_utc and invalid_after_utc dates. "
            "Pass each fact's record_digest from these facts; a change refused as changed_since_read means "
            "the fact changed after this job was made, so list the bank again and review the current fact. "
            "Prompt the user with notify_user when validity cannot be decided safely."
        ),
        "persisted": True,
    }
    job = memory_review_store.enqueue_review_job(
        owner_team_id=team_id,
        owner_named_agent_type=MEM_REVIEW_AGENT_TYPE,
        task_id=task_id,
        conversation_id=conversation_id,
        turn_index=0,
        source_payload=payload,
    )
    if logger is not None:
        logger.info("Enqueued %s memory audit review job %s.", normalized_bank, job.review_job_id)
    return ReviewJobRef(job_id=job.review_job_id, plugin_id="pinned_memory_review", payload=payload | {"review_job_id": job.review_job_id})
