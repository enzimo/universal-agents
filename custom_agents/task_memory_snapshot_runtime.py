"""Build durable task-state snapshots and case memories from derived deltas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from custom_agents.memory_graph_entity_resolution import build_text_embedding
from custom_agents.task_memory_delta_runtime import DerivedTaskTurnDelta
from storage.memory_graph_repository import MemoryGraphRepository, TaskStateSnapshotRow


@dataclass(frozen=True)
class PersistedTaskMemoryProjection:
    delta_id: str
    snapshot: TaskStateSnapshotRow
    handoff_event_id: Optional[str]
    case_memory_id: Optional[str]


def persist_task_memory_projection(
    *,
    repository: MemoryGraphRepository,
    source_payload: Dict[str, Any],
    derived_delta: DerivedTaskTurnDelta,
    embed_text_fn: Optional[Callable[[str], str]] = None,
) -> PersistedTaskMemoryProjection:
    turn_index = source_payload.get("turn_index")
    delta_row = repository.upsert_task_turn_delta(
        task_id=str(source_payload.get("task_id") or ""),
        conversation_id=source_payload.get("conversation_id"),
        turn_id=_build_turn_id(source_payload),
        turn_index=int(turn_index) if turn_index is not None else None,
        source_kind="conversation_turn",
        actor_id=None,
        actor_name=str(source_payload.get("owner_named_agent_type") or ""),
        actor_type="agent_role",
        turn_kind=derived_delta.turn_kind,
        delta=derived_delta.delta,
        delta_summary=derived_delta.delta_summary,
        extractor_version="task_memory_v1",
        batch_id=f"task_mem_batch_{source_payload.get('conversation_id') or source_payload.get('task_id') or 'unknown'}",
    )

    snapshot = repository.create_task_state_snapshot(
        task_id=str(source_payload.get("task_id") or ""),
        conversation_id=source_payload.get("conversation_id"),
        through_turn_index=int(turn_index) if turn_index is not None else None,
        status=str(derived_delta.delta.get("status") or ""),
        assignee_id=str(source_payload.get("owner_named_agent_type") or "") or None,
        priority=None,
        due_at=None,
        active_blockers=list(derived_delta.delta.get("blockers") or []),
        active_dependencies=list(derived_delta.delta.get("dependencies") or []),
        open_questions=list(derived_delta.delta.get("open_questions") or []),
        next_actions=list(derived_delta.delta.get("next_actions") or []),
        current_summary=str(
            derived_delta.delta.get("current_focus")
            or derived_delta.delta.get("plan_summary")
            or derived_delta.delta_summary
        ),
        state=derived_delta.delta,
    )

    handoff_event_id: Optional[str] = None
    if derived_delta.handoff is not None:
        handoff = repository.add_task_handoff_event(
            task_id=str(source_payload.get("task_id") or ""),
            conversation_id=source_payload.get("conversation_id"),
            turn_id=_build_turn_id(source_payload),
            assigner_id=derived_delta.handoff.get("assigner_id"),
            assigner_name=derived_delta.handoff.get("assigner_name"),
            assignee_id=derived_delta.handoff.get("assignee_id"),
            assignee_name=derived_delta.handoff.get("assignee_name"),
            handoff_kind=str(derived_delta.handoff.get("handoff_kind") or "handoff"),
            rationale=derived_delta.handoff.get("rationale"),
            handoff_notes=derived_delta.handoff.get("handoff_notes"),
        )
        handoff_event_id = handoff.handoff_event_id

    case_memory_id: Optional[str] = None
    latest_status = str(derived_delta.delta.get("status") or "").lower()
    case_type: Optional[str] = None
    outcome_quality: Optional[str] = None
    if latest_status in {"completed", "failed", "cancelled"}:
        case_type = "completed_task" if latest_status == "completed" else "failed_task"
        outcome_quality = "successful" if latest_status == "completed" else "degraded"
    elif str(source_payload.get("turn_summary") or "").strip() or str(source_payload.get("final_answer_text") or "").strip():
        case_type = "checkpoint"
        outcome_quality = "in_progress"

    if case_type is not None:
        case_memory = repository.upsert_case_memory(
            task_id=str(source_payload.get("task_id") or ""),
            conversation_id=source_payload.get("conversation_id"),
            project_key=None,
            case_type=case_type,
            summary=str(source_payload.get("turn_summary") or derived_delta.delta_summary),
            resolution=str(source_payload.get("final_answer_text") or ""),
            outcome_quality=outcome_quality,
            key_blockers=list(derived_delta.delta.get("blockers") or []),
            dependencies=list(derived_delta.delta.get("dependencies") or []),
            tools_used=_extract_tools_used(source_payload),
            lessons_learned=list(derived_delta.delta.get("lessons") or []),
            embedding=(embed_text_fn or build_text_embedding)(
                " ".join(
                    [
                        str(source_payload.get("request_text") or ""),
                        str(source_payload.get("turn_summary") or ""),
                        str(source_payload.get("final_answer_text") or ""),
                    ]
                )
            ),
        )
        case_memory_id = case_memory.case_memory_id

    return PersistedTaskMemoryProjection(
        delta_id=delta_row.delta_id,
        snapshot=snapshot,
        handoff_event_id=handoff_event_id,
        case_memory_id=case_memory_id,
    )


def _extract_tools_used(source_payload: Dict[str, Any]) -> List[str]:
    answer = source_payload.get("task_answer")
    if isinstance(answer, dict):
        return [str(item) for item in list(answer.get("tools_used") or []) if str(item).strip()]
    return []


def _build_turn_id(source_payload: Dict[str, Any]) -> Optional[str]:
    task_id = str(source_payload.get("task_id") or "").strip()
    turn_index = source_payload.get("turn_index")
    if not task_id:
        return None
    if turn_index is None:
        return f"{task_id}:turn"
    return f"{task_id}:turn:{turn_index}"
