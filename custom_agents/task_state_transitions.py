"""Shared task history and atomic state-transition helpers outside the TaskManager facade.

Extracted so single-write persistence paths (atomic timed-wait staging, atomic
note-based wait resume, and atomic live-turn timed-wait blocker clearing) share
the exact same task-link resolution and in-memory state-transition rules as
``TaskManager`` without growing the facade class.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from custom_messages.contracts.task_v1 import TaskHistoryEntry
from custom_messages.enums import TaskHistoryImportance, TaskHistorySource, TaskHistoryType


def build_task_history_entry(
    manager: Any,
    task_id: str,
    summary: str,
    *,
    source: TaskHistorySource = TaskHistorySource.SYSTEM,
    entry_type: TaskHistoryType = TaskHistoryType.TASK_EVENT,
    importance: TaskHistoryImportance = TaskHistoryImportance.NORMAL,
    timestamp: Optional[str] = None,
    agent_name: Optional[str] = None,
    agent_id: Optional[str] = None,
    related_task_id: Optional[str] = None,
    plan_step_id: Optional[str] = None,
    tracks_to_plan: Optional[bool] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Optional[TaskHistoryEntry]:
    """Build a task-linked history entry without mutating or persisting state."""

    task_info = manager.get_task_info(task_id)
    if not task_info:
        return None
    plan_id = manager._ensure_task_plan_link(task_info, create=False)
    objective_id = manager._get_task_objective_id_from_context(task_id)
    if objective_id is None and plan_id:
        objective = manager._get_objective_for_plan_id(plan_id)
        objective_id = objective.objective_id if objective else None
    entry: TaskHistoryEntry = manager._build_history_entry(
        summary,
        source=source,
        entry_type=entry_type,
        importance=importance,
        timestamp=timestamp,
        agent_name=agent_name,
        agent_id=agent_id,
        related_task_id=related_task_id,
        plan_step_id=plan_step_id,
        tracks_to_plan=tracks_to_plan,
        details=details,
    )
    entry.objective_id = objective_id
    entry.plan_id = plan_id
    return entry


def record_blocker_with_history(
    host: Any,
    task_id: str,
    blocker: dict[str, Any],
    *,
    history_summary: str,
    history_details: dict[str, Any],
    entry_type: TaskHistoryType = TaskHistoryType.QUERY_EVENT,
    importance: TaskHistoryImportance = TaskHistoryImportance.HIGH,
) -> bool:
    """Persist a task history entry and blocker record together in one runtime-state write.

    Rolls back both in-memory mutations if persistence raises (while this agent
    still holds the same ``TaskInfo``), and returns ``False`` if custody of this
    task was released during the write (``TaskCustodyTakenOver``).
    """

    task_info = host.get_task_info(task_id)
    if task_info is None:
        raise KeyError(f"Task {task_id} is not tracked")
    entry = build_task_history_entry(
        host,
        task_id,
        history_summary,
        entry_type=entry_type,
        importance=importance,
        details=history_details,
    )
    if entry is None:
        raise KeyError(f"Task {task_id} is not tracked")

    had_blocker = task_id in host.task_state.human_interaction_blockers
    previous_blocker = host.task_state.human_interaction_blockers.get(task_id)
    task_info.task.execution_history.append(entry)
    host.task_state.human_interaction_blockers[task_id] = dict(blocker)
    try:
        host._persist_runtime_state()
    except Exception:
        if host.get_task_info(task_id) is task_info:
            if entry in task_info.task.execution_history:
                task_info.task.execution_history.remove(entry)
            if had_blocker and previous_blocker is not None:
                host.task_state.human_interaction_blockers[task_id] = previous_blocker
            else:
                host.task_state.human_interaction_blockers.pop(task_id, None)
        raise
    return bool(host.get_task_info(task_id) is task_info)


__all__ = [
    "build_task_history_entry",
    "record_blocker_with_history",
]
