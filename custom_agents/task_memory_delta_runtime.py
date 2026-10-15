"""Derive structured per-turn task deltas from archived review-job payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class DerivedTaskTurnDelta:
    turn_kind: str
    delta: Dict[str, Any]
    delta_summary: str
    handoff: Optional[Dict[str, Any]] = None


def derive_task_turn_delta(source_payload: Dict[str, Any]) -> DerivedTaskTurnDelta:
    task_id = str(source_payload.get("task_id") or "")
    task_state = list(source_payload.get("task_state") or [])
    execution_digest = dict(source_payload.get("execution_digest") or {})
    history_entries = list(source_payload.get("execution_history_entries") or [])
    request_text = str(source_payload.get("request_text") or "")
    turn_summary = str(source_payload.get("turn_summary") or "")
    final_answer_text = str(source_payload.get("final_answer_text") or "")
    errors = list(source_payload.get("task_errors") or [])

    latest_status = _resolve_latest_status(task_state)
    blockers = _collect_blockers(history_entries, errors)
    dependencies = _collect_dependencies(history_entries)
    open_questions = _collect_open_questions(history_entries, request_text, turn_summary)
    next_actions = _collect_next_actions(execution_digest, history_entries)
    decisions = _collect_decisions(history_entries, request_text)
    lessons = _collect_lessons(history_entries, turn_summary, final_answer_text)
    handoff = _detect_handoff(history_entries)

    delta = {
        "task_id": task_id,
        "status": latest_status,
        "goal_summary": execution_digest.get("goal_summary"),
        "plan_summary": execution_digest.get("plan_summary"),
        "current_focus": execution_digest.get("current_focus"),
        "plan_items": list(execution_digest.get("plan_items") or []),
        "unknowns": list(execution_digest.get("unknowns") or []),
        "blockers": blockers,
        "dependencies": dependencies,
        "open_questions": open_questions,
        "next_actions": next_actions,
        "decisions": decisions,
        "lessons": lessons,
        "errors": errors,
        "final_answer_text": final_answer_text,
    }
    if handoff is not None:
        delta["handoff"] = handoff

    status_label = latest_status or "unknown"
    parts = [f"Task {task_id or 'unknown'} status is {status_label}."]
    if blockers:
        parts.append(f"{len(blockers)} blocker(s) tracked.")
    if next_actions:
        parts.append(f"Next action: {next_actions[0]}")
    elif decisions:
        parts.append(f"Decision noted: {decisions[0]}")
    elif final_answer_text:
        parts.append(f"Outcome: {final_answer_text[:120]}")

    turn_kind = "handoff" if handoff is not None else "status_update"
    if final_answer_text:
        turn_kind = "decision" if decisions else "comment"
    return DerivedTaskTurnDelta(
        turn_kind=turn_kind,
        delta=delta,
        delta_summary=" ".join(parts).strip(),
        handoff=handoff,
    )


def _resolve_latest_status(task_state: List[Any]) -> Optional[str]:
    if not task_state:
        return None
    latest = task_state[-1]
    if isinstance(latest, (list, tuple)) and latest:
        value = latest[0]
        return value.value if hasattr(value, "value") else str(value)
    return str(latest)


def _collect_blockers(history_entries: List[Dict[str, Any]], errors: List[str]) -> List[Dict[str, Any]]:
    blockers: List[Dict[str, Any]] = []
    for error in errors:
        if str(error).strip():
            blockers.append({"description": str(error).strip(), "source": "task_error"})
    for entry in history_entries:
        summary = str(entry.get("summary") or "").strip()
        entry_type = str(entry.get("entry_type") or "")
        lowered = summary.lower()
        if entry_type == "error_event" or "block" in lowered or "waiting" in lowered:
            blockers.append({"description": summary, "source": entry_type or "history"})
    return _dedupe_dicts(blockers)


def _collect_dependencies(history_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dependencies: List[Dict[str, Any]] = []
    for entry in history_entries:
        related_task_id = entry.get("related_task_id")
        details = dict(entry.get("details") or {})
        if related_task_id:
            dependencies.append(
                {
                    "depends_on_task_id": str(related_task_id),
                    "kind": str(details.get("dependency_kind") or "related_task"),
                }
            )
    return _dedupe_dicts(dependencies)


def _collect_open_questions(history_entries: List[Dict[str, Any]], request_text: str, turn_summary: str) -> List[str]:
    questions: List[str] = []
    for entry in history_entries:
        summary = str(entry.get("summary") or "").strip()
        if str(entry.get("entry_type") or "") == "query_event" or "?" in summary:
            questions.append(summary)
    if "?" in request_text:
        questions.append(request_text.strip())
    if "?" in turn_summary:
        questions.append(turn_summary.strip())
    return _dedupe_strings(questions)


def _collect_next_actions(execution_digest: Dict[str, Any], history_entries: List[Dict[str, Any]]) -> List[str]:
    next_actions: List[str] = []
    for item in list(execution_digest.get("plan_items") or []):
        status = str(item.get("status") or "").lower()
        if status in {"planned", "in_progress", "blocked"}:
            title = str(item.get("title") or "").strip()
            detail = str(item.get("status_detail") or "").strip()
            if title:
                next_actions.append(f"{title}: {detail}".strip(": "))
    for entry in history_entries:
        details = dict(entry.get("details") or {})
        for action in list(details.get("next_actions") or []):
            if str(action).strip():
                next_actions.append(str(action).strip())
    return _dedupe_strings(next_actions)


def _collect_decisions(history_entries: List[Dict[str, Any]], request_text: str) -> List[str]:
    decisions: List[str] = []
    for entry in history_entries:
        summary = str(entry.get("summary") or "").strip()
        if str(entry.get("entry_type") or "") == "plan_event":
            continue
        lowered = summary.lower()
        if "decid" in lowered or "going forward" in lowered or "from now on" in lowered:
            decisions.append(summary)
    request_lower = request_text.lower()
    if "from now on" in request_lower or "we decided" in request_lower:
        decisions.append(request_text.strip())
    return _dedupe_strings(decisions)


def _collect_lessons(history_entries: List[Dict[str, Any]], turn_summary: str, final_answer_text: str) -> List[str]:
    lessons: List[str] = []
    for entry in history_entries:
        summary = str(entry.get("summary") or "").strip()
        lowered = summary.lower()
        if "lesson" in lowered or "remember to" in lowered or "avoid " in lowered:
            lessons.append(summary)
    for text in (turn_summary, final_answer_text):
        lowered = text.lower()
        if "lesson learned" in lowered or "remember to" in lowered or "avoid " in lowered:
            lessons.append(text.strip())
    return _dedupe_strings(lessons)


def _detect_handoff(history_entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for entry in reversed(history_entries):
        summary = str(entry.get("summary") or "").strip()
        details = dict(entry.get("details") or {})
        lowered = summary.lower()
        if "handoff" in lowered or "delegate" in lowered or "reassign" in lowered:
            return {
                "assigner_id": details.get("assigner_id"),
                "assigner_name": details.get("assigner_name"),
                "assignee_id": details.get("assignee_id"),
                "assignee_name": details.get("assignee_name"),
                "handoff_kind": str(details.get("handoff_kind") or _infer_handoff_kind(lowered)),
                "rationale": details.get("rationale") or summary,
                "handoff_notes": details.get("handoff_notes"),
            }
    return None


def _infer_handoff_kind(summary_lower: str) -> str:
    if "reassign" in summary_lower:
        return "reassign"
    if "delegate" in summary_lower:
        return "delegate"
    if "resume" in summary_lower:
        return "resume"
    return "handoff"


def _dedupe_strings(values: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        normalized = " ".join(str(value or "").strip().split()).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(" ".join(str(value or "").strip().split()))
    return result


def _dedupe_dicts(values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []
    for value in values:
        key = tuple(sorted((str(k), str(v)) for k, v in value.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
