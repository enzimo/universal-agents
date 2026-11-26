"""Shared task history and state-transition helpers outside the TaskManager facade.

Extracted so single-write persistence helpers (such as atomic timed-wait staging
and atomic note-based wait resume) share the exact same task-link resolution and
in-memory transition logic as ``TaskManager`` without growing the facade class.
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


__all__ = ["build_task_history_entry"]
