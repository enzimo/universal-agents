"""Maintain a runnable queue independently of model execution budgets."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from custom_messages.enums import ProgressState, TaskHistoryImportance, TaskHistoryType

QUEUE_REPAIR_EVENT = "task_queue_nonrunnable_removed"
QUEUE_NOTICE_EVENT = "task_queue_safety_notice_sent"
FINAL_REVIEW_WAIT_EVENT = "final_review_wait_detected"
FINAL_REVIEW_STALLED_EVENT = "final_review_stalled"
SAFETY_EVENTS = frozenset(
    {QUEUE_REPAIR_EVENT, FINAL_REVIEW_WAIT_EVENT, FINAL_REVIEW_STALLED_EVENT}
)


def is_ready(manager: Any, task_id: str) -> bool:
    info = manager.get_task_info(task_id)
    return bool(
        info is not None
        and manager.is_locally_executable_task(task_id)
        and manager.is_runnable_state(info.state)
        and task_id not in manager.task_state.human_interaction_blockers
        and manager.get_pending_recoverable_pause(task_id) is None
    )


def remove_queue_entry(manager: Any, task_id: str) -> None:
    """Called within the state transition's persistence boundary."""
    manager.task_state.task_queue = deque(
        item for item in manager.task_state.task_queue if item != task_id
    )


def reconcile_queue(manager: Any) -> None:
    """Remove invalid entries once instead of polling them forever.

    Retain task and blocker records. Only their authoritative resolution path
    may make the task runnable again. This also handles old persisted queues.
    """
    for task_id in tuple(dict.fromkeys(manager.task_state.task_queue)):
        if is_ready(manager, task_id):
            continue
        info = manager.get_task_info(task_id)
        remove_queue_entry(manager, task_id)
        manager._logger.warning(
            "Removed non-runnable task from execution queue. event=%s task_id=%s state=%s",
            QUEUE_REPAIR_EVENT,
            task_id,
            info.state.value if info else "missing",
        )
        if info is not None and not manager.is_terminal_state(info.state):
            manager.append_history_entry(
                task_id,
                "Removed a stale execution-queue entry. The task remains tracked and cannot block other ready work.",
                entry_type=TaskHistoryType.ERROR_EVENT,
                importance=TaskHistoryImportance.HIGH,
                details={"event": QUEUE_REPAIR_EVENT, "state": info.state.value},
            )
        manager._persist_runtime_state()


def next_ready_task(manager: Any) -> str | None:
    if not manager.can_start_new_task():
        return None
    reconcile_queue(manager)
    if not manager.task_state.task_queue:
        return None
    task_id = manager.task_state.task_queue.popleft()
    try:
        manager._persist_runtime_state()
    except Exception:
        manager.task_state.task_queue.appendleft(task_id)
        raise
    manager._logger.info("Next task to process: %s", task_id)
    manager._check_queue_backlog_warning()
    return str(task_id)


def _inspect_final_review(manager: Any, info: Any) -> None:
    task_id = info.task.task_id
    if (
        not manager.is_final_review_pending(task_id)
        or info.state != ProgressState.WAITING
    ):
        return
    if manager.get_active_runtime_lease(task_id):
        return
    waiting = bool(
        task_id in manager.task_state.human_interaction_blockers
        or manager.is_waiting_on_task_query(task_id)
        or manager.has_pending_subtasks(task_id)
        or manager.is_plan_execution_root_active(task_id)
        or manager.get_pending_recoverable_pause(task_id)
    )
    event = FINAL_REVIEW_WAIT_EVENT if waiting else FINAL_REVIEW_STALLED_EVENT
    if any(
        (entry.details or {}).get("event") == event
        for entry in info.task.execution_history
    ):
        return
    if not waiting:
        manager.update_main_task_state(
            task_id, ProgressState.PAUSED, datetime.now(timezone.utc).isoformat()
        )
    summary = (
        "The final response is held for review while this task waits for a decision or unfinished work."
        if waiting
        else "The final review stopped without a recorded reason to wait. The runtime paused the task instead of retrying it."
    )
    manager._logger.warning("event=%s task_id=%s", event, task_id)
    manager.append_history_entry(
        task_id,
        summary,
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={"event": event},
    )


def pending_safety_notices(manager: Any) -> list[tuple[Any, Any]]:
    """Run on the scheduler's existing tick, even when no model turn can run."""
    reconcile_queue(manager)
    notices: list[tuple[Any, Any]] = []
    for info in manager.get_locally_executable_task_infos():
        if manager.is_terminal_state(info.state):
            continue
        _inspect_final_review(manager, info)
        history = info.task.execution_history
        delivered = {
            (entry.details or {}).get("source_entry_id")
            for entry in history
            if (entry.details or {}).get("event") == QUEUE_NOTICE_EVENT
        }
        notices.extend(
            (info, entry)
            for entry in history
            if (entry.details or {}).get("event") in SAFETY_EVENTS
            and entry.entry_id not in delivered
        )
    return notices
