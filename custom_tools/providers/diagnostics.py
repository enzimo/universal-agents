"""Diagnostics support for the trusted Strands tool surface."""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, TypedDict

from strands import tool

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.token_estimation import count_message_tokens, count_text_tokens
from custom_messages.contracts.envelope_v1 import CommsMessage
from custom_messages.enums import (
    ExecutionPlanItemStatus,
    ExecutionUnknownStatus,
    MessageMetaType,
    ObjectiveCompletionPolicy,
    ObjectiveHorizon,
    ObjectivePriority,
    ObjectiveStatus,
    PlanActionStatus,
    PlanActionType,
    TaskHistoryImportance,
)
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import (
    get_agent_ref,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


_RECOVERY_CONTEXT_DETAIL_LEVELS = {
    "summary",
    "plan_state",
    "recent_history",
    "failure_window",
    "all",
}

logger = logging.getLogger(__name__)


def _compact_recovery_text(value: Any, *, max_chars: int = 2400) -> str:
    if value is None:
        return ""
    if hasattr(value, "answer"):
        value = getattr(value, "answer", "")
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _recovery_redaction_values(agent: "UniversalAgent", link: Any) -> Dict[str, str]:
    values: Dict[str, str] = {
        getattr(link, "original_task_id", ""): "[original-task]",
        getattr(link, "recovery_task_id", ""): "[recovery-task]",
        getattr(link, "original_plan_id", ""): "[plan]",
        getattr(link, "branch_id", ""): "[recovery-branch]",
        getattr(link, "affected_step_id", ""): "[affected-step]",
        getattr(link, "affected_subtask_id", ""): "[affected-subtask]",
        getattr(link, "checkpoint_schedule_id", ""): "[checkpoint]",
    }
    for task_id in (
        getattr(link, "original_task_id", None),
        getattr(link, "recovery_task_id", None),
    ):
        if not task_id:
            continue
        task_info = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_info, task_id
        )
        if task_info is None:
            archived = agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_from_db, task_id
            )
            if archived:
                task = archived["main_task"]
                for state_entry in getattr(task, "task_state", []) or []:
                    if isinstance(state_entry, (list, tuple)) and len(state_entry) >= 2:
                        values[str(state_entry[1])] = "[timestamp]"
                continue
            continue
        for candidate, replacement in (
            (getattr(task_info.meta, "conversation_id", None), "[conversation]"),
            (getattr(task_info.meta, "msg_correlation_id", None), "[correlation]"),
            (getattr(task_info.meta, "followup_to_task_id", None), "[followup-task]"),
            (getattr(task_info.header, "message_id", None), "[message]"),
            (
                getattr(task_info.header, "response_routing_key", None),
                "[response-route]",
            ),
        ):
            if candidate:
                values[str(candidate)] = replacement
    return {key: value for key, value in values.items() if key}


def _sanitize_recovery_value(value: Any, redactions: Dict[str, str]) -> Any:
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.endswith("_id") or key_text in {
                "id",
                "task_id",
                "parent_task_id",
                "plan_id",
                "conversation_id",
                "msg_correlation_id",
                "correlation_id",
                "message_id",
                "schedule_id",
                "branch_id",
                "checkpoint_schedule_id",
            }:
                continue
            sanitized[key_text] = _sanitize_recovery_value(item, redactions)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_recovery_value(item, redactions) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_recovery_value(item, redactions) for item in value]
    if isinstance(value, str):
        sanitized = value
        for raw, replacement in sorted(
            redactions.items(), key=lambda item: len(item[0]), reverse=True
        ):
            sanitized = sanitized.replace(raw, replacement)
        sanitized = re.sub(
            r"\b(?:task|subtask|recovery_task|original|plan_id|plan|msg|corr|conversation)_[A-Za-z0-9_-]+\b",
            "[runtime-id]",
            sanitized,
        )
        return sanitized
    return value


def _recovery_history_entry_payload(
    entry: Any, redactions: Dict[str, str]
) -> Dict[str, Any]:
    details = _sanitize_recovery_value(getattr(entry, "details", {}) or {}, redactions)
    return {
        "timestamp": getattr(entry, "timestamp", None),
        "source": getattr(
            getattr(entry, "source", None), "value", getattr(entry, "source", None)
        ),
        "type": getattr(
            getattr(entry, "entry_type", None),
            "value",
            getattr(entry, "entry_type", None),
        ),
        "importance": getattr(
            getattr(entry, "importance", None),
            "value",
            getattr(entry, "importance", None),
        ),
        "summary": _sanitize_recovery_value(getattr(entry, "summary", ""), redactions),
        "agent_name": _sanitize_recovery_value(
            getattr(entry, "agent_name", None), redactions
        ),
        "details": details,
    }


def _recovery_plan_state_payload(
    agent: "UniversalAgent", original_task_id: str, redactions: Dict[str, str]
) -> Dict[str, Any]:
    digest = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_execution_digest, original_task_id
    )
    if digest is None:
        return {"active": False}
    return {
        "active": agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.has_active_execution_digest, original_task_id
        ),
        "goal_summary": _sanitize_recovery_value(digest.goal_summary, redactions),
        "plan_summary": _sanitize_recovery_value(digest.plan_summary, redactions),
        "current_focus": _sanitize_recovery_value(digest.current_focus, redactions),
        "plan_items": [
            {
                "title": _sanitize_recovery_value(item.title, redactions),
                "rationale": _sanitize_recovery_value(item.rationale, redactions),
                "status": item.status.value,
                "status_detail": _sanitize_recovery_value(
                    item.status_detail, redactions
                ),
                "resolution_summary": _sanitize_recovery_value(
                    item.resolution_summary, redactions
                ),
            }
            for item in digest.plan_items
        ],
        "unknowns": [
            {
                "summary": _sanitize_recovery_value(item.summary, redactions),
                "why_it_matters": _sanitize_recovery_value(
                    item.why_it_matters, redactions
                ),
                "resolution_plan": _sanitize_recovery_value(
                    item.resolution_plan, redactions
                ),
                "status": item.status.value,
                "resolution_summary": _sanitize_recovery_value(
                    item.resolution_summary, redactions
                ),
            }
            for item in digest.unknowns
        ],
        "last_updated_at": digest.last_updated_at,
    }


def _recovery_summary_payload(
    agent: "UniversalAgent",
    link: Any,
    redactions: Dict[str, str],
    *,
    include_problem_statement: bool = True,
) -> Dict[str, Any]:
    original_info = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_info, link.original_task_id
    )
    archived = (
        None
        if original_info
        else agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_from_db, link.original_task_id
        )
    )
    original_task = (
        original_info.task
        if original_info
        else (archived["main_task"] if archived else None)
    )
    state = (
        original_info.state.value
        if original_info
        else (archived["status"] if archived else "unknown")
    )
    pending_subtasks = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_pending_subtasks, link.original_task_id
    )
    pending_query = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_latest_pending_task_query, link.original_task_id
    )
    original_task_payload: Dict[str, Any] = {
        "active": original_info is not None,
        "state": state,
        "has_errors": bool(getattr(original_task, "errors", None)),
        "error_count": len(getattr(original_task, "errors", None) or []),
        "waiting_on_user": bool(pending_query),
        "latest_user_question": (
            _sanitize_recovery_value(
                _compact_recovery_text(
                    getattr(pending_query, "message", None), max_chars=800
                ),
                redactions,
            )
            if pending_query
            else None
        ),
        "pending_subtask_count": len(pending_subtasks),
    }
    if include_problem_statement:
        original_task_payload["problem_statement"] = _sanitize_recovery_value(
            _compact_recovery_text(
                getattr(original_task, "problem_statement", None), max_chars=1800
            ),
            redactions,
        )
    else:
        original_task_payload["problem_statement_available_on_request"] = True

    return {
        "failure": {
            "type": _sanitize_recovery_value(link.failure_type, redactions),
            "summary": _sanitize_recovery_value(link.failure_summary, redactions),
        },
        "original_task": original_task_payload,
    }


def _recovery_recent_history_payload(
    agent: "UniversalAgent",
    original_task_id: str,
    redactions: Dict[str, str],
    *,
    max_entries: int,
) -> Dict[str, Any]:
    entries = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_history_entries, original_task_id, limit=None
    )
    selected = entries[-max_entries:]
    return {
        "returned": len(selected),
        "entries": [
            _recovery_history_entry_payload(entry, redactions) for entry in selected
        ],
    }


def _recovery_failure_window_payload(
    agent: "UniversalAgent",
    original_task_id: str,
    redactions: Dict[str, str],
    *,
    max_entries: int,
    include_general_high_importance: bool = True,
) -> Dict[str, Any]:
    entries = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_history_entries, original_task_id, limit=None
    )
    failure_entries = []
    for entry in entries:
        entry_type = getattr(getattr(entry, "entry_type", None), "value", "")
        importance = getattr(getattr(entry, "importance", None), "value", "")
        summary = str(getattr(entry, "summary", "") or "")
        if (
            entry_type in {"error_event", "subtask_event", "plan_event"}
            or (include_general_high_importance and importance in {"high", "critical"})
            or re.search(
                r"\b(?:fail|error|timeout|stuck|recover|blocked|cancel)\b",
                summary,
                flags=re.IGNORECASE,
            )
        ):
            failure_entries.append(entry)
    selected = failure_entries[-max_entries:]
    original_task = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task, original_task_id
    )
    if original_task is None:
        archived = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_from_db, original_task_id
        )
        original_task = archived["main_task"] if archived else None
    return {
        "returned": len(selected),
        "entries": [
            _recovery_history_entry_payload(entry, redactions) for entry in selected
        ],
        "errors": _sanitize_recovery_value(
            getattr(original_task, "errors", None) or [],
            redactions,
        ),
    }


def _build_initial_recovery_context(agent: "UniversalAgent", link: Any) -> str:
    """
    Build the bounded context included in the first isolated recovery prompt.

    This starter context is intentionally narrower than `get_recovery_context`.
    It gives the recovery loop enough failure evidence to begin while avoiding
    automatic replay of the original problem statement or broad history.
    """
    redactions = _recovery_redaction_values(agent, link)
    payload = {
        "summary": _recovery_summary_payload(
            agent,
            link,
            redactions,
            include_problem_statement=False,
        ),
        "plan_state": _recovery_plan_state_payload(
            agent, link.original_task_id, redactions
        ),
        "failure_window": _recovery_failure_window_payload(
            agent,
            link.original_task_id,
            redactions,
            max_entries=6,
            include_general_high_importance=False,
        ),
        "more_context_available": "Call get_recovery_context with a narrow detail_level if this starter context is insufficient.",
    }
    rendered = json.dumps(
        _sanitize_recovery_value(payload, redactions), indent=2, default=str
    )
    if len(rendered) > 7000:
        return rendered[:6997].rstrip() + "..."
    return rendered


@tool
def get_recovery_context(detail_level: str = "summary", max_entries: int = 8) -> str:
    """
    Return sanitized context for the original task of the current isolated recovery.

    This tool is only available to the TeamArchitect while executing an
    isolated recovery task. It infers the original task from runtime metadata, so
    the model does not need task ids, plan ids, conversation ids, or routing
    identifiers.

    detail_level may be one of: summary, plan_state, recent_history,
    failure_window, or all.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "get_recovery_context is manager-only and only works inside isolated recovery tasks.",
                "manager_only": True,
            }
        )
    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        return json.dumps({"error": "No task currently in progress"})
    link = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_recovery_link, current_task_id
    )
    if link is None:
        return json.dumps(
            {
                "error": "Current task is not an isolated recovery task.",
                "recovery_task_only": True,
                "allowed_detail_levels": sorted(_RECOVERY_CONTEXT_DETAIL_LEVELS),
            }
        )

    normalized_level = (detail_level or "summary").strip().lower().replace("-", "_")
    if normalized_level not in _RECOVERY_CONTEXT_DETAIL_LEVELS:
        return json.dumps(
            {
                "error": f"Unknown detail_level: {detail_level}",
                "allowed_detail_levels": sorted(_RECOVERY_CONTEXT_DETAIL_LEVELS),
            }
        )
    safe_max_entries = max(1, min(int(max_entries or 8), 30))
    redactions = _recovery_redaction_values(agent, link)

    payload: Dict[str, Any] = {
        "recovery_context": True,
        "detail_level": normalized_level,
        "metadata_hidden": True,
        "ids_available_to_model": False,
    }
    if normalized_level in {"summary", "all"}:
        payload["summary"] = _recovery_summary_payload(agent, link, redactions)
    if normalized_level in {"plan_state", "all"}:
        payload["plan_state"] = _recovery_plan_state_payload(
            agent, link.original_task_id, redactions
        )
    if normalized_level in {"recent_history", "all"}:
        payload["recent_history"] = _recovery_recent_history_payload(
            agent,
            link.original_task_id,
            redactions,
            max_entries=safe_max_entries,
        )
    if normalized_level in {"failure_window", "all"}:
        payload["failure_window"] = _recovery_failure_window_payload(
            agent,
            link.original_task_id,
            redactions,
            max_entries=safe_max_entries,
        )

    return json.dumps(payload, default=str)


def _parse_history_importance(raw_value: str) -> TaskHistoryImportance:
    normalized = (raw_value or "").strip().lower()
    mapping = {
        "low": TaskHistoryImportance.LOW,
        "normal": TaskHistoryImportance.NORMAL,
        "high": TaskHistoryImportance.HIGH,
        "critical": TaskHistoryImportance.CRITICAL,
    }
    return mapping.get(normalized, TaskHistoryImportance.NORMAL)


def _parse_plan_item_status(raw_value: str) -> ExecutionPlanItemStatus:
    normalized = (raw_value or "").strip().lower()
    mapping = {
        "planned": ExecutionPlanItemStatus.PLANNED,
        "todo": ExecutionPlanItemStatus.PLANNED,
        "in_progress": ExecutionPlanItemStatus.IN_PROGRESS,
        "active": ExecutionPlanItemStatus.IN_PROGRESS,
        "blocked": ExecutionPlanItemStatus.BLOCKED,
        "completed": ExecutionPlanItemStatus.COMPLETED,
        "done": ExecutionPlanItemStatus.COMPLETED,
        "dropped": ExecutionPlanItemStatus.DROPPED,
        "cancelled": ExecutionPlanItemStatus.DROPPED,
    }
    return mapping.get(normalized, ExecutionPlanItemStatus.PLANNED)


def _parse_unknown_status(raw_value: str) -> ExecutionUnknownStatus:
    normalized = (raw_value or "").strip().lower()
    mapping = {
        "open": ExecutionUnknownStatus.OPEN,
        "resolved": ExecutionUnknownStatus.RESOLVED,
        "closed": ExecutionUnknownStatus.RESOLVED,
    }
    return mapping.get(normalized, ExecutionUnknownStatus.OPEN)


def _parse_plan_action_type(raw_value: str) -> Optional[PlanActionType]:
    normalized = (raw_value or "").strip().lower().replace("-", "_")
    mapping = {item.value: item for item in PlanActionType}
    return mapping.get(normalized)


def _parse_plan_action_status(raw_value: str) -> PlanActionStatus:
    normalized = (raw_value or "").strip().lower().replace("-", "_")
    mapping = {item.value: item for item in PlanActionStatus}
    return mapping.get(normalized, PlanActionStatus.RECORDED)


def _parse_objective_horizon(raw_value: str) -> ObjectiveHorizon:
    normalized = (raw_value or "").strip().lower().replace("-", "_")
    mapping = {item.value: item for item in ObjectiveHorizon}
    return mapping.get(normalized, ObjectiveHorizon.FINITE)


def _parse_objective_priority(raw_value: str) -> ObjectivePriority:
    normalized = (raw_value or "").strip().lower().replace("-", "_")
    mapping = {item.value: item for item in ObjectivePriority}
    return mapping.get(normalized, ObjectivePriority.NORMAL)


def _parse_objective_completion_policy(raw_value: str) -> ObjectiveCompletionPolicy:
    normalized = (raw_value or "").strip().lower().replace("-", "_")
    mapping = {item.value: item for item in ObjectiveCompletionPolicy}
    return mapping.get(normalized, ObjectiveCompletionPolicy.MANAGER_DECIDES)


def _parse_objective_status(raw_value: str) -> Optional[ObjectiveStatus]:
    normalized = (raw_value or "").strip().lower().replace("-", "_")
    mapping = {item.value: item for item in ObjectiveStatus}
    return mapping.get(normalized)


def _parse_objective_status_list(raw_value: str) -> Optional[List[str]]:
    values = [
        item.strip().lower().replace("-", "_") for item in (raw_value or "").split(",")
    ]
    statuses = [
        item for item in values if item in {status.value for status in ObjectiveStatus}
    ]
    return statuses or None


def _parse_objective_horizon_list(raw_value: str) -> Optional[List[str]]:
    values = [
        item.strip().lower().replace("-", "_") for item in (raw_value or "").split(",")
    ]
    horizons = [
        item
        for item in values
        if item in {horizon.value for horizon in ObjectiveHorizon}
    ]
    return horizons or None


def _parse_json_list(raw_value: str, *, field_name: str) -> List[Any]:
    if not raw_value or not raw_value.strip():
        return []
    parsed = json.loads(raw_value)
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must decode to a JSON list.")
    return parsed


def _coerce_tool_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(raw_value)


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _task_age_seconds(task_info: Any) -> Optional[int]:
    timestamps: list[datetime] = []
    task = getattr(task_info, "task", None)
    header = getattr(task_info, "header", None)
    for state_entry in getattr(task, "task_state", []) or []:
        if isinstance(state_entry, (list, tuple)) and len(state_entry) >= 2:
            parsed = _parse_iso_datetime(state_entry[1])
            if parsed is not None:
                timestamps.append(parsed)
    header_timestamp = _parse_iso_datetime(getattr(header, "timestamp", None))
    if header_timestamp is not None:
        timestamps.append(header_timestamp)
    if not timestamps:
        return None
    return max(0, int((datetime.now(timezone.utc) - min(timestamps)).total_seconds()))


def _token_counter_for_agent(agent: "UniversalAgent"):
    return getattr(agent, "_strands_model", None) or getattr(
        getattr(agent, "_strands_agent", None), "model", None
    )


def _estimate_tokens_from_text(text: str, model: Any) -> Dict[str, Any]:
    char_count = len(text or "")
    word_count = len(re.findall(r"\S+", text or ""))
    estimated_tokens = count_text_tokens(model, text) if text else 0
    return {
        "char_count": char_count,
        "word_count": word_count,
        "estimated_tokens": estimated_tokens,
        "method": "strands_model_count_tokens",
    }


def _safe_call_string(func: Any, *args: Any, **kwargs: Any) -> str:
    if not callable(func):
        return ""
    try:
        value = func(*args, **kwargs)
    except Exception:
        return ""
    return value if isinstance(value, str) else str(value or "")


class RuntimePressureSourceLength(TypedDict):
    """Contribution of one prompt source to a runtime pressure estimate."""

    source: str
    chars: int
    tokens: int


def _build_runtime_pressure_material(
    agent: "UniversalAgent",
    task_info: Any,
    model: Any,
) -> tuple[str, list[RuntimePressureSourceLength]]:
    task = task_info.task
    parts: list[tuple[str, str]] = []

    team_routing_context = _safe_call_string(
        getattr(agent, "_build_team_routing_prompt_context", None)
    )
    if team_routing_context:
        parts.append(
            ("team_routing_context", f"Team Routing Context:\n{team_routing_context}")
        )

    parts.append(
        (
            "incoming_message",
            "--- Incoming Message ---\n"
            + CommsMessage(msg_content=task, msg_meta=task_info.meta).model_dump_json(),
        )
    )

    if task.task_type == MessageMetaType.REQUEST:
        execution_digest = _safe_call_string(
            agent.task_manager.render_execution_digest_markdown,
            task_id=task.task_id,
        )
        if execution_digest:
            parts.append(("execution_digest", f"Execution Digest:\n{execution_digest}"))

        history_summary = _safe_call_string(
            agent.task_manager.summarize_task_history_markdown,
            task_id=task.task_id,
            include_execution_digest=False,
        )
        if history_summary:
            parts.append(("task_history_summary", f"Task History:\n{history_summary}"))

    strands_messages = getattr(getattr(agent, "_strands_agent", None), "messages", None)
    if strands_messages:
        parts.append(
            (
                "strands_message_buffer",
                f"Current Strands Message Buffer:\n{_safe_json_dumps(strands_messages)}",
            )
        )

    source_lengths = [
        {
            "source": name,
            "chars": len(value),
            "tokens": count_text_tokens(model, value) if value else 0,
        }
        for name, value in parts
    ]
    return "\n\n".join(value for _, value in parts), source_lengths


def _resolve_context_window_tokens(agent: "UniversalAgent") -> int:
    resolver = getattr(agent, "_resolved_context_window_tokens", None)
    if callable(resolver):
        try:
            return max(1024, int(resolver()))
        except Exception:
            pass
    env_value = os.getenv("LLM_CONTEXT_WINDOW_TOKENS")
    if env_value:
        try:
            return max(1024, int(env_value))
        except ValueError:
            pass
    configured = getattr(agent, "context_window_tokens", None)
    if configured:
        try:
            return max(1024, int(configured))
        except (TypeError, ValueError):
            pass
    from custom_agents.strands_model_factory import resolve_context_window_tokens

    return resolve_context_window_tokens(
        provider=getattr(agent, "model_provider", None), model_id=getattr(agent, "model_id", None)
    )


def _fill_ratio(used: Optional[int], limit: Optional[int]) -> Optional[float]:
    if used is None or not limit or limit <= 0:
        return None
    return round(max(0.0, float(used) / float(limit)), 4)


def _pressure_level(fill_ratio: Optional[float]) -> str:
    if fill_ratio is None:
        return "unknown"
    if fill_ratio >= 0.90:
        return "critical"
    if fill_ratio >= 0.75:
        return "high"
    if fill_ratio >= 0.50:
        return "moderate"
    return "low"


def _resolved_tool_call_limit(agent: "UniversalAgent") -> Optional[int]:
    resolver = getattr(agent, "_resolved_max_tool_calls_per_task", None)
    if callable(resolver):
        try:
            return resolver()
        except Exception:
            pass
    raw = getattr(agent, "max_tool_calls_per_task", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _tool_usage_from_value(value: Any, model: Any) -> tuple[int, int]:
    tool_call_count = 0
    tool_result_tokens = 0

    def visit(node: Any) -> None:
        nonlocal tool_call_count, tool_result_tokens
        if isinstance(node, dict):
            if "toolUse" in node:
                tool_call_count += 1
            if "toolResult" in node:
                tool_result = node.get("toolResult")
                if isinstance(tool_result, dict):
                    tool_result_tokens += count_message_tokens(
                        model,
                        [{"role": "user", "content": [{"toolResult": tool_result}]}],
                    )
                else:
                    tool_result_tokens += count_text_tokens(
                        model, _safe_json_dumps(tool_result)
                    )
            for child in node.values():
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)
    return tool_call_count, tool_result_tokens


def _recent_failure_count(task_manager: Any, task_id: str) -> int:
    count = 0
    task_info = task_manager.get_task_info(task_id)
    task = getattr(task_info, "task", None)
    count += len(getattr(task, "errors", []) or [])

    failure_terms = (
        "fail",
        "failed",
        "failure",
        "error",
        "timeout",
        "cancelled",
        "budget",
    )
    for entry in task_manager.get_task_history_entries(task_id, limit=50):
        entry_type = getattr(
            getattr(entry, "entry_type", None),
            "value",
            str(getattr(entry, "entry_type", "")),
        )
        summary = (getattr(entry, "summary", "") or "").lower()
        details_text = _safe_json_dumps(getattr(entry, "details", {}) or {}).lower()
        if entry_type == "error_event" or any(
            term in summary or term in details_text for term in failure_terms
        ):
            count += 1

    for subtask_info in task_manager.get_subtasks(task_id):
        state_value = getattr(
            getattr(subtask_info, "state", None),
            "value",
            str(getattr(subtask_info, "state", "")),
        )
        if state_value in {"failed", "cancelled", "abandoned"}:
            count += 1
        count += len(getattr(subtask_info.task, "errors", []) or [])

    return count


def build_runtime_pressure_snapshot(agent: "UniversalAgent") -> Dict[str, Any]:
    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        raise ValueError("No task currently in progress")
    task_info = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_info, current_task_id
    )
    if task_info is None:
        raise ValueError(f"Current task info missing for task_id={current_task_id}")

    model = _token_counter_for_agent(agent)
    prompt_material, source_lengths = _build_runtime_pressure_material(
        agent, task_info, model
    )
    estimate = _estimate_tokens_from_text(prompt_material, model)
    context_window_tokens = _resolve_context_window_tokens(agent)
    context_fill_ratio = _fill_ratio(
        estimate["estimated_tokens"], context_window_tokens
    )

    strands_messages = (
        getattr(getattr(agent, "_strands_agent", None), "messages", []) or []
    )
    tool_call_budget_used, tool_result_tokens_observed = _tool_usage_from_value(
        strands_messages, model
    )
    inner_loop_context_tokens = count_message_tokens(model, strands_messages)
    tool_call_budget_limit = _resolved_tool_call_limit(agent)
    inner_loop_context_cfg = getattr(agent, "inner_loop_context", {}) or {}
    inner_loop_context_limit_tokens = None
    if isinstance(inner_loop_context_cfg, dict) and inner_loop_context_cfg.get(
        "enabled", True
    ):
        try:
            inner_loop_context_limit_tokens = (
                int(inner_loop_context_cfg.get("max_total_tokens") or 0) or None
            )
        except (TypeError, ValueError):
            inner_loop_context_limit_tokens = None

    return {
        "current_task_id": current_task_id,
        "task_age_seconds": _task_age_seconds(task_info),
        "context_window_tokens": context_window_tokens,
        "estimated_context_tokens": estimate["estimated_tokens"],
        "context_fill_ratio": context_fill_ratio,
        "context_pressure_level": _pressure_level(context_fill_ratio),
        "tool_call_budget_used": tool_call_budget_used,
        "tool_call_budget_limit": tool_call_budget_limit,
        "tool_call_budget_fill_ratio": _fill_ratio(
            tool_call_budget_used, tool_call_budget_limit
        ),
        "tool_result_tokens_observed": tool_result_tokens_observed,
        "inner_loop_context_tokens": inner_loop_context_tokens,
        "inner_loop_context_limit_tokens": inner_loop_context_limit_tokens,
        "inner_loop_context_fill_ratio": _fill_ratio(
            inner_loop_context_tokens, inner_loop_context_limit_tokens
        ),
        "pending_subtask_count": len(
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_pending_subtasks, current_task_id
            )
        ),
        "recent_failure_count": _recent_failure_count(
            agent.task_manager, current_task_id
        ),
        "context_estimate": {
            "char_count": estimate["char_count"],
            "word_count": estimate["word_count"],
            "method": estimate["method"],
            "sources": source_lengths,
        },
    }


@tool
def log_task_history_entry(
    note: str,
    importance: str = "normal",
    plan_step_id: str = "",
    tracks_to_plan: bool = False,
) -> str:
    """
    Log a freeform execution note against the current task.

    Use this when you discover something important that the runtime cannot infer
    automatically, such as a strategic pivot, a blocker, or a key conclusion.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        return json.dumps({"error": "No task currently in progress"})

    entry = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.log_agent_note,
        current_task_id,
        note=note,
        agent_name=agent.name,
        agent_id=agent.id,
        importance=_parse_history_importance(importance),
        plan_step_id=plan_step_id or None,
        tracks_to_plan=tracks_to_plan,
    )
    if entry is None:
        return json.dumps(
            {"error": f"Failed to append history entry for task {current_task_id}"}
        )

    return json.dumps(
        {
            "task_id": current_task_id,
            "entry_id": entry.entry_id,
            "timestamp": entry.timestamp,
            "importance": entry.importance.value,
            "summary": entry.summary,
        }
    )


@tool
def get_execution_digest(task_id: str = "") -> str:
    """
    Retrieve the pinned durable execution digest for a task.

    If task_id is omitted, the current in-progress task is used.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    resolved_task_id = task_id or agent.task_manager.current_task_id
    if not resolved_task_id:
        return json.dumps(
            {"error": "No task_id provided and no task currently in progress"}
        )

    digest = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_execution_digest, resolved_task_id
    )
    if digest is None:
        return json.dumps({"task_id": resolved_task_id, "digest": None})

    return json.dumps(
        {
            "task_id": resolved_task_id,
            "plan_id": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_plan_id, resolved_task_id
            ),
            "objective_id": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_objective_id, resolved_task_id
            ),
            "active": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.has_active_execution_digest, resolved_task_id
            ),
            "digest": digest.model_dump(mode="json"),
            "recent_plan_actions": [
                action.model_dump(mode="json")
                for action in agent.task_manager.operations_runtime.run_sync(
                    agent.task_manager.get_plan_actions, resolved_task_id, limit=10
                )
            ],
            "markdown": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.render_execution_digest_markdown,
                resolved_task_id,
                only_if_active=False,
            ),
        },
        default=str,
    )


def _task_history_window_tool():
    # Defined with the heartbeat tools; it reads the agent's own task history.
    from custom_tools.providers.heartbeat import get_task_history_window

    return get_task_history_window


PROVIDER = ToolProvider(
    name="diagnostics",
    tools=(
        get_recovery_context,
        log_task_history_entry,
        get_execution_digest,
        _task_history_window_tool(),
    ),
)
