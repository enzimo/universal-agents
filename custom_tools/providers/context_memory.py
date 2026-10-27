"""Context memory support for the trusted Strands tool surface."""

import json
import logging
from typing import Any, Dict, Optional

from strands import tool

from access_control.ownership import PRIVATE_UPDATE
from custom_agents.after_action_report_knowledge import (
    KnowledgeLesson,
    LessonRefused,
    lesson_problem,
    save_review_lesson,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.enums import AfterActionReportReviewStatus
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._runtime import (
    _new_conversation_id,
)
from custom_tools.providers._user_scope import (
    _conversation_owner_denial,
    _current_private_history_owner,
    _private_user_scope,
)
from custom_tools.runtime_context import (
    get_agent_ref,
)
from custom_tools.tool_results import failed
from id_generation import (
    new_id,
)

logger = logging.getLogger(__name__)


@tool
def list_archived_tasks(limit: int = 10, status: str = "") -> str:
    """
    List recently archived tasks from the task-history database.

    Use this when you need prior task IDs or summaries but no task is currently
    in progress. After finding a relevant ID, call `get_task_history_window`
    with that explicit task_id to inspect detailed history.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    current_owner, denial = _private_user_scope(
        agent, resource_name="Archived task history"
    )
    if denial is not None:
        return json.dumps(denial)

    archived_tasks = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.list_archived_tasks,
        limit=limit,
        status=status or None,
        owner_canonical_user_id=current_owner,
    )
    return json.dumps(
        {
            "db_path": agent.task_manager._db_path,
            "returned": len(archived_tasks),
            "tasks": archived_tasks,
        },
        default=str,
    )


@tool
def list_conversations(limit: int = 10) -> str:
    """
    List recently active conversation threads known to this agent.

    Use this when you need durable multi-turn conversation identifiers rather
    than individual archived task ids.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})

    current_owner = _current_private_history_owner(agent)
    if not current_owner:
        return json.dumps(
            {
                "error": "conversation_history_requires_registered_user",
                "message": "Private conversation history is available only for registered users.",
            }
        )
    conversations = agent._conversation_store.list_conversations(
        limit=limit,
        owner_canonical_user_id=current_owner,
    )
    return json.dumps(
        {
            "db_path": agent.task_manager._db_path,
            "returned": len(conversations),
            "conversations": conversations,
        },
        default=str,
    )


@tool
def get_conversation_summary(conversation_id: str) -> str:
    """
    Retrieve the stored synopsis and metadata for one conversation thread.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})
    denial = _conversation_owner_denial(agent, conversation_id=conversation_id)
    if denial is not None:
        return json.dumps(denial)

    return json.dumps(
        agent._conversation_store.get_conversation_summary(conversation_id),
        default=str,
    )


@tool
def get_conversation_turns(
    conversation_id: str,
    start_turn: int = 1,
    limit: int = 10,
    created_after: str = "",
    created_before: str = "",
) -> str:
    """
    Retrieve a chronological slice of turns from one conversation thread.

    `created_after` is an inclusive lower bound. `created_before` is an
    exclusive upper bound. Both accept ISO-8601 timestamps or `YYYY-MM-DD`
    dates interpreted in UTC.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})
    denial = _conversation_owner_denial(agent, conversation_id=conversation_id)
    if denial is not None:
        return json.dumps(denial)
    try:
        turns = agent._conversation_store.get_conversation_turns(
            conversation_id=conversation_id,
            start_turn=start_turn,
            limit=limit,
            created_after=created_after or None,
            created_before=created_before or None,
        )
        return json.dumps(
            {
                "conversation_id": conversation_id,
                "start_turn": start_turn,
                "limit": limit,
                "created_after": created_after or None,
                "created_before": created_before or None,
                "returned": len(turns),
                "turns": turns,
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def get_history_overview(limit: int = 20) -> str:
    """
    See how much chat history and how many finished tasks the person you work for has stored.

    Lists their conversations (newest first, with ids, the start of the first
    request as a title, turns, dates and size) and how much is older than 30,
    90, 180 and 365 days. Use it before `plan_history_cleanup` and to tell the
    person what they have. It only reads, and only this person's history.
    """
    from access_control.identity import IdentityRejected
    from custom_agents.history_cleanup import HistoryCleanupRefused, history_overview

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(history_overview(agent, limit=limit), default=str)
    except (HistoryCleanupRefused, IdentityRejected) as exc:
        return json.dumps({"error": str(exc)})


@tool
def plan_history_cleanup(what: str, older_than: str = "", conversation_ids: list[str] | None = None) -> str | dict[str, Any]:
    """
    Stage a clean-up of the person's own chat history; it deletes nothing yet.

    Only when the person asks you to delete their history. `what` is
    `conversations` (their chats everywhere, with summaries and saved tool
    outputs), `tasks` (finished tasks) or `both`. Give `older_than` (a date,
    YYYY-MM-DD: everything from before it) and/or `conversation_ids` (from
    `get_history_overview`); with both, only those conversations' older part.
    It always works on the history of the person this task is for. The
    conversation you are answering in and conversations with work still
    running are kept. Then show the person the result's `effect` and call
    `apply_change(change_id, reason)`: it applies at once if they let you
    clean up their history without asking (Settings > Access), otherwise they
    approve this exact clean-up in the Inbox. Remembered facts are kept.
    """
    from access_control.identity import IdentityRejected
    from custom_agents.history_cleanup import HistoryCleanupRefused, stage_history_cleanup

    agent = get_agent_ref()
    if agent is None:
        return failed({"error": "Agent reference not set"})
    try:
        return json.dumps(
            stage_history_cleanup(
                agent, what=what, older_than=older_than, conversation_ids=list(conversation_ids or ())
            ),
            default=str,
        )
    except (HistoryCleanupRefused, IdentityRejected) as exc:
        return failed({"status": "refused", "reason": str(exc)})


def _conversation_turn_summary_payload(turn: dict[str, Any]) -> dict[str, Any]:
    user_text = str(turn.get("problem_text") or "")
    user_summary = str(turn.get("user_request_summary") or user_text[:600])
    assistant_summary = str(
        turn.get("assistant_response_summary")
        or turn.get("context_summary_text")
        or turn.get("turn_summary_text")
        or turn.get("final_answer_text")
        or ""
    )
    artifacts = turn.get("tool_artifact_manifest") or []
    artifact_summary = turn.get("artifact_summary") or []
    return {
        "turn_index": turn.get("turn_index"),
        "task_id": turn.get("task_id"),
        "followup_to_task_id": turn.get("followup_to_task_id"),
        "task_status": turn.get("task_status"),
        "user_request_summary": user_summary[:1200],
        "assistant_summary": assistant_summary[:1200],
        "context_summary": str(
            turn.get("context_summary_text") or turn.get("turn_summary_text") or ""
        )[:1600],
        "summary_generator": turn.get("summary_generator") or "",
        "summary_generated_at": turn.get("summary_generated_at"),
        "artifact_summary": artifact_summary[:10]
        if isinstance(artifact_summary, list)
        else [],
        "has_full_user_text": bool(user_text),
        "has_full_assistant_text": bool(turn.get("final_answer_text")),
        "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
    }


def _parse_turn_index_filter(raw_value: str) -> Optional[set[int]]:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "source_turn_indices_json must be a JSON array of turn indexes."
        ) from exc
    if decoded in (None, []):
        return None
    if not isinstance(decoded, list):
        raise ValueError(
            "source_turn_indices_json must be a JSON array of turn indexes."
        )
    indexes: set[int] = set()
    for item in decoded:
        try:
            parsed = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "source_turn_indices_json must contain only integers."
            ) from exc
        if parsed < 1:
            raise ValueError(
                "source_turn_indices_json must contain positive turn indexes."
            )
        indexes.add(parsed)
    return indexes


def _focused_conversation_summary_markdown(
    *,
    title: str,
    summary: str,
    source_conversation_id: str,
    selected_turns: list[dict[str, Any]],
) -> str:
    lines = [
        f"# {title or 'Focused Conversation'}",
        "",
        summary.strip() or "Focused context copied from an earlier conversation.",
        "",
        f"Source conversation: `{source_conversation_id}`",
    ]
    if selected_turns:
        lines.extend(["", "Relevant source turns:"])
        for turn in selected_turns:
            payload = _conversation_turn_summary_payload(turn)
            user_summary = str(payload.get("user_request_summary") or "").strip()
            assistant_summary = str(payload.get("assistant_summary") or "").strip()
            lines.append(
                f"- Turn {payload.get('turn_index')} task={payload.get('task_id')}: "
                f"user={user_summary[:240]!r}; assistant={assistant_summary[:240]!r}"
            )
    return "\n".join(lines).strip()


@tool
def create_focused_conversation(
    title: str,
    summary: str,
    source_conversation_id: str = "",
    source_turn_indices_json: str = "[]",
    max_recent_turns: int = 8,
) -> str:
    """
    Create a fresh conversation thread seeded with selected context from another thread.

    This does not move or rewrite prior turns. It creates a new conversation_id
    with a context epoch that summarizes the relevant source turns, so future
    follow-ups can stay focused while the original conversation remains intact.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})

    current_task_id = (
        agent.task_manager.current_task_id if agent.task_manager is not None else None
    )
    current_task_info = (
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_info, current_task_id
        )
        if current_task_id
        else None
    )
    current_conversation_id = getattr(
        getattr(current_task_info, "meta", None), "conversation_id", None
    )
    resolved_source_conversation_id = (
        source_conversation_id or current_conversation_id or ""
    ).strip()
    if not resolved_source_conversation_id:
        return json.dumps(
            {
                "error": "source_conversation_id is required when no active conversation task exists"
            }
        )
    denial = _conversation_owner_denial(
        agent, conversation_id=resolved_source_conversation_id
    )
    if denial is not None:
        return json.dumps(denial)

    try:
        selected_indexes = _parse_turn_index_filter(source_turn_indices_json)
        safe_limit = max(1, min(int(max_recent_turns or 8), 25))
        source_turns = agent._conversation_store.get_conversation_turns(
            conversation_id=resolved_source_conversation_id,
            start_turn=1,
            limit=200,
        )
        source_turns = [
            turn for turn in source_turns if turn.get("task_id") != current_task_id
        ]
        if selected_indexes is not None:
            selected_turns = [
                turn
                for turn in source_turns
                if int(turn.get("turn_index") or 0) in selected_indexes
            ]
        else:
            selected_turns = source_turns[-safe_limit:]
        selected_turns = selected_turns[:25]

        resolved_title = str(title or "").strip() or "Focused Conversation"
        resolved_summary = str(summary or "").strip()
        summary_markdown = _focused_conversation_summary_markdown(
            title=resolved_title,
            summary=resolved_summary,
            source_conversation_id=resolved_source_conversation_id,
            selected_turns=selected_turns,
        )
        synopsis = agent._conversation_store.default_synopsis()
        synopsis["goals"] = [resolved_title]
        synopsis["current_state"] = resolved_summary or summary_markdown
        synopsis["important_facts"] = [
            {
                "source_conversation_id": resolved_source_conversation_id,
                "turn_index": turn.get("turn_index"),
                "task_id": turn.get("task_id"),
                "user_request_summary": _conversation_turn_summary_payload(turn).get(
                    "user_request_summary"
                ),
                "assistant_summary": _conversation_turn_summary_payload(turn).get(
                    "assistant_summary"
                ),
            }
            for turn in selected_turns
        ]
        synopsis["summary_markdown"] = summary_markdown

        new_conversation_id = _new_conversation_id()
        seed_task_id = new_id("focus_conv_seed")
        thread = agent._conversation_store.ensure_thread(
            conversation_id=new_conversation_id,
            team_id=getattr(agent, "team_id", None),
            owner_canonical_user_id=_current_private_history_owner(agent),
            owner_agent_id=getattr(agent, "id", None),
            owner_agent_name=getattr(agent, "named_agent_type", None),
            requester_name=getattr(
                getattr(current_task_info, "meta", None), "from_agent_name", None
            ),
            requester_id=None,
            root_task_id=seed_task_id,
            subject_preview=resolved_title[:160],
        )
        agent._conversation_store.update_thread_compaction(
            new_conversation_id,
            synopsis_json=synopsis,
            synopsis_markdown=summary_markdown,
            summarized_through_turn_index=0,
            compactor_type="focused_conversation_seed",
            compaction_state={
                "source_conversation_id": resolved_source_conversation_id,
                "source_turn_indices": [
                    turn.get("turn_index") for turn in selected_turns
                ],
            },
            latest_task_id=seed_task_id,
        )
        epoch = agent._conversation_store.create_context_epoch(
            conversation_id=new_conversation_id,
            trigger="focused_conversation_seed",
            through_turn_index=0,
            preserved_suffix_start_turn=1,
            latest_task_id=seed_task_id,
            summary_json=synopsis,
            summary_markdown=summary_markdown,
            rehydration_manifest={
                "source_conversation_id": resolved_source_conversation_id,
                "source_turn_indices": [
                    turn.get("turn_index") for turn in selected_turns
                ],
                "source_task_ids": [turn.get("task_id") for turn in selected_turns],
                "current_task_id": current_task_id,
            },
            compactor_type="focused_conversation_seed",
            created_by_agent_id=getattr(agent, "id", None),
        )
        return json.dumps(
            {
                "status": "success",
                "conversation_id": new_conversation_id,
                "source_conversation_id": resolved_source_conversation_id,
                "source_turn_indices": [
                    turn.get("turn_index") for turn in selected_turns
                ],
                "context_epoch_id": epoch.get("context_epoch_id"),
                "thread": thread,
                "next_step_hint": (
                    "Use this conversation_id in the generated site spec when publishing a website. "
                    "Keep the original gateway session_key so browser chat can attach this focused thread to the user session."
                ),
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def get_conversation_context_slice(
    conversation_id: str,
    start_turn: int = 1,
    limit: int = 10,
    mode: str = "summary",
) -> str:
    """
    Retrieve a bounded slice of one conversation thread for context recovery.

    `mode="summary"` returns compact per-turn summaries and identifiers.
    `mode="full"` returns the full stored turn rows. The limit is capped at 50.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})
    denial = _conversation_owner_denial(agent, conversation_id=conversation_id)
    if denial is not None:
        return json.dumps(denial)

    normalized_mode = (mode or "summary").strip().lower()
    if normalized_mode not in {"summary", "full"}:
        return json.dumps({"error": "mode must be either 'summary' or 'full'"})

    safe_start = max(1, int(start_turn or 1))
    safe_limit = max(1, min(int(limit or 10), 50))
    try:
        rows = agent._conversation_store.get_conversation_turns(
            conversation_id=conversation_id,
            start_turn=safe_start,
            limit=safe_limit + 1,
        )
        has_more = len(rows) > safe_limit
        visible_rows = rows[:safe_limit]
        next_start_turn = None
        if has_more and visible_rows:
            next_start_turn = int(visible_rows[-1].get("turn_index") or safe_start) + 1
        payload_turns = (
            [_conversation_turn_summary_payload(turn) for turn in visible_rows]
            if normalized_mode == "summary"
            else visible_rows
        )
        return json.dumps(
            {
                "conversation_id": conversation_id,
                "start_turn": safe_start,
                "limit": safe_limit,
                "mode": normalized_mode,
                "returned": len(payload_turns),
                "has_more": has_more,
                "next_start_turn": next_start_turn,
                "turns": payload_turns,
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def get_context_status() -> str:
    """
    Return current outer-loop context pressure, latest context epoch, and source accounting.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    assembly = agent.turn_executor.last_context_assembly
    current_task_id = (
        agent.task_manager.current_task_id if agent.task_manager is not None else None
    )
    task_info = (
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_info, current_task_id
        )
        if current_task_id
        else None
    )
    conversation_id = getattr(getattr(task_info, "meta", None), "conversation_id", None)
    latest_epoch = None
    if conversation_id and getattr(agent, "_conversation_store", None):
        latest_epoch = agent._conversation_store.get_latest_context_epoch(
            conversation_id
        )
    return json.dumps(
        {
            "current_task_id": current_task_id,
            "conversation_id": conversation_id,
            "context_window_tokens": getattr(assembly, "max_context_tokens", None),
            "estimated_tokens": getattr(assembly, "estimated_tokens", None),
            "hard_limit_tokens": getattr(assembly, "hard_limit_tokens", None),
            "soft_limit_tokens": getattr(assembly, "soft_limit_tokens", None),
            "context_fill_ratio": getattr(assembly, "context_fill_ratio", None),
            "context_epoch_id": getattr(assembly, "context_epoch_id", None),
            "recovery_attempt": getattr(assembly, "recovery_attempt", 0),
            "degraded_sections": getattr(assembly, "degraded_sections", []),
            "warnings": getattr(assembly, "warnings", []),
            "source_lengths": getattr(assembly, "source_lengths", []),
            "latest_epoch": latest_epoch,
        },
        default=str,
    )


@tool
def compact_conversation_context(
    conversation_id: str = "", reason: str = "manual"
) -> str:
    """
    Compact a conversation thread and create a context epoch when thresholds require it.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None) or not getattr(
        agent, "_conversation_compactor", None
    ):
        return json.dumps(
            {"error": "Conversation context components are not initialized"}
        )
    current_task_id = (
        agent.task_manager.current_task_id if agent.task_manager is not None else None
    )
    task_info = (
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_info, current_task_id
        )
        if current_task_id
        else None
    )
    resolved_conversation_id = conversation_id or getattr(
        getattr(task_info, "meta", None), "conversation_id", None
    )
    if not resolved_conversation_id:
        return json.dumps(
            {
                "error": "conversation_id is required when no active conversation task exists"
            }
        )
    denial = _conversation_owner_denial(
        agent,
        conversation_id=resolved_conversation_id,
        action=PRIVATE_UPDATE,
    )
    if denial is not None:
        return json.dumps(denial)
    try:
        result = agent._conversation_compactor.compact_if_needed(
            store=agent._conversation_store,
            conversation_id=resolved_conversation_id,
            latest_request_text="",
            exclude_task_id=current_task_id,
        )
        if not getattr(result, "compacted", False) and reason == "manual":
            thread = (
                agent._conversation_store.get_thread(resolved_conversation_id) or {}
            )
            turns = agent._conversation_store.get_recent_turns(
                resolved_conversation_id,
                include_pending_delivery=False,
            )
            latest_turn = max(
                [int(turn.get("turn_index") or 0) for turn in turns] or [0]
            )
            if latest_turn > 0:
                epoch = agent._conversation_store.create_context_epoch(
                    conversation_id=resolved_conversation_id,
                    trigger="manual",
                    through_turn_index=int(
                        thread.get("summarized_through_turn_index") or latest_turn
                    ),
                    preserved_suffix_start_turn=latest_turn + 1,
                    latest_task_id=thread.get("latest_task_id"),
                    summary_json=thread.get("synopsis_json") or {},
                    summary_markdown=thread.get("synopsis_markdown") or "",
                    rehydration_manifest={
                        "source_turn_range": [1, latest_turn],
                        "source_summary_fields": [
                            "user_request_summary",
                            "assistant_response_summary",
                            "context_summary_text",
                            "artifact_summary_json",
                            "tool_artifact_manifest_json",
                        ],
                        "summary_layering": "manual_epoch_rollup; lower-level turn summaries are not overwritten",
                    },
                    pre_compact_estimated_tokens=getattr(
                        result, "token_estimate", None
                    ),
                    post_compact_estimated_tokens=getattr(
                        result, "token_estimate", None
                    ),
                    compactor_type=getattr(
                        agent._conversation_compactor, "compactor_type", None
                    ),
                    created_by_agent_id=getattr(agent, "id", None),
                )
                return json.dumps(
                    {
                        "conversation_id": resolved_conversation_id,
                        "compaction": result.__dict__,
                        "manual_epoch": epoch,
                    },
                    default=str,
                )
        return json.dumps(
            {
                "conversation_id": resolved_conversation_id,
                "compaction": result.__dict__,
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def list_context_epochs(conversation_id: str, limit: int = 20) -> str:
    """
    List durable context epochs for a conversation.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})
    denial = _conversation_owner_denial(agent, conversation_id=conversation_id)
    if denial is not None:
        return json.dumps(denial)
    try:
        epochs = agent._conversation_store.list_context_epochs(
            conversation_id, limit=limit
        )
        return json.dumps(
            {
                "conversation_id": conversation_id,
                "returned": len(epochs),
                "epochs": epochs,
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def get_context_epoch(context_epoch_id: str, mode: str = "summary") -> str:
    """
    Retrieve one context epoch. Summary mode omits large source accounting arrays.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not getattr(agent, "_conversation_store", None):
        return json.dumps({"error": "ConversationStore not initialized"})
    normalized_mode = (mode or "summary").strip().lower()
    if normalized_mode not in {"summary", "full"}:
        return json.dumps({"error": "mode must be 'summary' or 'full'"})
    try:
        epoch = agent._conversation_store.get_context_epoch(context_epoch_id)
        if not epoch:
            return json.dumps({"error": f"Context epoch {context_epoch_id} not found"})
        denial = _conversation_owner_denial(
            agent, conversation_id=str(epoch.get("conversation_id") or "")
        )
        if denial is not None:
            return json.dumps({"error": f"Context epoch {context_epoch_id} not found"})
        if normalized_mode == "summary":
            epoch = dict(epoch)
            epoch.pop("source_lengths_before", None)
            epoch.pop("source_lengths_after", None)
        return json.dumps({"mode": normalized_mode, "epoch": epoch}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# The statuses a review is resolved with.
_RESOLUTION_STATUSES = tuple(
    item for item in AfterActionReportReviewStatus if item != AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW
)


def _parse_after_action_report_review_status(
    status: str,
) -> Optional[AfterActionReportReviewStatus]:
    normalized = (status or "").strip().lower()
    for item in AfterActionReportReviewStatus:
        if normalized == item.value:
            return item
    return None


def _render_after_action_report_review(review: Any) -> Dict[str, Any]:
    return {
        "review_id": review.review_id,
        "report_id": review.report_id,
        "batch_id": getattr(review, "batch_id", None),
        "status": review.status.value,
        "summary": review.summary,
        "recommended_importance": review.recommended_importance.value,
        "source_agent_name": review.source_agent_name,
        "source_named_agent_type": review.source_named_agent_type,
        "durable_context_candidates": list(review.durable_context_candidates or []),
        "risks_or_followups": list(review.risks_or_followups or []),
        "evidence_refs": list(review.evidence_refs or []),
        "decision_summary": review.decision_summary,
        "review_rationale": review.review_rationale,
        "stale_reason": getattr(review, "stale_reason", None),
        "created_at": review.created_at,
        "updated_at": review.updated_at,
        "reviewed_at": review.reviewed_at,
    }


@tool
def list_pending_after_action_reports(
    limit: int = 10, status: str = "pending_manager_review"
) -> str:
    """
    List manager-owned AfterActionReport reviews awaiting or recently given a decision.

    Use this to inspect specialist lessons-learned reports. Resolve each useful
    report with resolve_after_action_report_review after deciding whether to
    ignore it, keep it in task history, summarize it, promote it to memory, or
    mark it as requiring follow-up. Only reports about the work of the person
    this task works for are listed.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "list_pending_after_action_reports is manager-only.",
                "manager_only": True,
            }
        )
    parsed_status = _parse_after_action_report_review_status(status)
    if parsed_status is None:
        return json.dumps(
            {
                "error": f"Unknown AfterActionReport review status: {status}",
                "allowed_statuses": [
                    item.value for item in AfterActionReportReviewStatus
                ],
            }
        )
    try:
        # Reports about other people's work stay out of view: the person comes
        # from the task's verified identity (custom_agents.after_action_report_identity).
        reviews = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.list_after_action_report_reviews,
            person_id=_current_private_history_owner(agent),
            status=parsed_status,
            limit=limit,
        )
        return json.dumps(
            {
                "status": parsed_status.value,
                "returned": len(reviews),
                "reviews": [
                    _render_after_action_report_review(review) for review in reviews
                ],
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def resolve_after_action_report_review(
    review_id: str = "",
    status: str = "",
    decision_summary: str = "",
    review_rationale: str = "",
    report_id: str = "",
    knowledge_page: str = "",
    knowledge_title: str = "",
    knowledge_summary: Optional[str] = None,
    knowledge_body: str = "",
    knowledge_base_revision: int = 0,
) -> str:
    """
    Resolve one manager-owned AfterActionReport review.

    Prefer `report_id` when resolving a scheduled late-AAR review task because
    the runtime injects report ids directly into that task context. `review_id`
    remains supported for manual/debug workflows.

    Valid statuses are ignored, kept_in_task_history, summarized_to_task_history,
    promoted_to_memory, saved_to_knowledge, and needs_followup. If using
    promoted_to_memory, store the short durable fact with
    `memory(action="create", bank="coremem")` (or bank="usermem") first or
    immediately after, then record that promotion decision here.

    For a longer lesson, use saved_to_knowledge with knowledge_page (such as
    "team/release-lessons"), knowledge_title, knowledge_summary and
    knowledge_body: the page is saved first, and the review is resolved only
    when that worked. In a review of a person's work a team page is proposed
    for the Knowledge Curator, who publishes it without the person's private
    details; in a review of work for nobody it is published at once. To add
    to an existing page, read it and pass its revision as
    knowledge_base_revision with the whole new text. Omit knowledge_summary
    to keep the page's existing summary, or send "" to clear it. Never put passwords,
    keys or the person's private details in it. Only reports about the work
    of the person this task works for can be resolved.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "resolve_after_action_report_review is manager-only.",
                "manager_only": True,
            }
        )
    parsed_status = _parse_after_action_report_review_status(status)
    if parsed_status is None or parsed_status == AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW:
        return json.dumps(
            {
                "error": f"Invalid resolution status: {status}",
                "allowed_statuses": [item.value for item in _RESOLUTION_STATUSES],
            }
        )
    if not (review_id or report_id):
        return json.dumps({"error": "Either review_id or report_id is required."})
    lesson = KnowledgeLesson(
        page=knowledge_page,
        title=knowledge_title,
        summary=knowledge_summary,
        body=knowledge_body,
        base_revision=int(knowledge_base_revision or 0),
    )
    saved = _lesson_first(agent, parsed_status, lesson, decision_summary, review_id=review_id, report_id=report_id)
    if "error" in saved:
        return json.dumps(saved, default=str)
    saved_note = saved.get("note", "")
    decision_summary = saved.get("decision_summary", decision_summary)
    try:
        review = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.resolve_after_action_report_review,
            review_id or None,
            person_id=_current_private_history_owner(agent),
            report_id=report_id or None,
            status=parsed_status,
            decision_summary=decision_summary,
            review_rationale=review_rationale or None,
            agent_name=getattr(agent, "name", None),
            agent_id=getattr(agent, "id", None),
        )
    except Exception as exc:
        return json.dumps(_resolution_failed(exc, saved_note, review_id=review_id, report_id=report_id))
    if review is None:
        return json.dumps(
            {
                "error": "AfterActionReport review not found",
                "review_id": review_id or None,
                "report_id": report_id or None,
            }
        )
    return json.dumps(
        {
            "review": _render_after_action_report_review(review),
            "history_entry_id": review.resolution_history_entry_id,
        },
        default=str,
    )


def _lesson_first(
    agent: Any,
    status: AfterActionReportReviewStatus,
    lesson: KnowledgeLesson,
    decision_summary: str,
    *,
    review_id: str,
    report_id: str,
) -> Dict[str, Any]:
    """Before resolving: ``{}`` for most statuses; for saved_to_knowledge the saved lesson's note and the
    decision summary that records it, or an error."""

    saving = status is AfterActionReportReviewStatus.SAVED_TO_KNOWLEDGE
    problem = lesson_problem(saving, lesson)
    if problem:
        return {"error": problem}
    if not saving:
        return {}
    if not (decision_summary or "").strip():
        return {"error": "decision_summary is required."}
    saved = _save_lesson(agent, lesson, review_id=review_id, report_id=report_id)
    if "note" in saved:
        saved["decision_summary"] = f"{decision_summary.strip()} {saved['note']}"
    return saved


def _resolution_failed(exc: Exception, saved_note: str, *, review_id: str, report_id: str) -> Dict[str, Any]:
    if not saved_note:
        return {"error": str(exc)}
    # The page and its receipt are saved. Retrying the original lesson fields
    # resumes this resolution without another knowledge write, after a restart too.
    logger.warning(
        "Saved a review's lesson but could not resolve the review. "
        "event=aar_knowledge_saved_review_unresolved review_id=%s report_id=%s error=%s",
        review_id or None,
        report_id or None,
        type(exc).__name__,
    )
    return {"error": str(exc), "knowledge_saved": saved_note}


def _save_lesson(agent: Any, lesson: KnowledgeLesson, *, review_id: str, report_id: str) -> Dict[str, Any]:
    """Save a pending review's lesson; ``{"note": ...}`` to record on the review, or an error answer."""

    tasks = agent.task_manager
    person_id = _current_private_history_owner(agent)
    review = None
    if review_id:
        review = tasks.operations_runtime.run_sync(tasks.get_after_action_report_review, review_id, person_id=person_id)
    if review is None and report_id:
        review = tasks.operations_runtime.run_sync(
            tasks.get_after_action_report_review_by_report_id, report_id, person_id=person_id
        )
    if review is None:
        return {"error": "AfterActionReport review not found", "review_id": review_id or None, "report_id": report_id or None}
    if review.status is not AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW:
        return {"error": f"This review is already resolved ({review.status.value}); a lesson is saved once."}
    try:
        return {"note": save_review_lesson(agent, review, lesson)}
    except LessonRefused as refused:
        return {"error": str(refused), **refused.payload}


# Two providers by who needs them, matching the catalog tools fn:own_context
# (every agent) and fn:conversations (the team lead) in
# ua_definitions/access/tools/functions.yaml; fn:memory is
# custom_tools.providers.memory.
OWN_CONTEXT_PROVIDER = ToolProvider(
    name="own_context",
    tools=(
        list_archived_tasks,
        get_context_status,
        compact_conversation_context,
        list_context_epochs,
        get_context_epoch,
    ),
)

CONVERSATIONS_PROVIDER = ToolProvider(
    name="conversations",
    tools=(
        list_conversations,
        get_conversation_summary,
        get_conversation_turns,
        create_focused_conversation,
        get_conversation_context_slice,
        list_pending_after_action_reports,
        resolve_after_action_report_review,
        get_history_overview,
        plan_history_cleanup,
    ),
)
