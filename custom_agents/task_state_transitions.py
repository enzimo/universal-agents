"""Shared task history and atomic state-transition helpers outside the TaskManager facade.

Extracted so single-write persistence paths (atomic timed-wait staging, atomic
note-based wait resume, and atomic live-turn timed-wait blocker clearing) share
the exact same task-link resolution and in-memory state-transition rules as
``TaskManager`` without growing the facade class.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Dict, Optional

from custom_agents import timed_waits as timed_waits_rules
from custom_agents.execution_lanes import lane_for_task
from custom_agents.task_human_interaction_blockers import (
    clear_human_interaction_blocker,
    is_timed_wait_blocker,
    is_wait_blocker,
    should_clear_answered_blocker,
    unmark_wait_record,
)
from custom_agents.task_queue_safety import is_ready
from custom_agents.task_runtime_state import (
    add_runtime_resume_directive,
    drop_resume_directive_blocks,
    restore_runtime_resume_directive,
    snapshot_runtime_resume_directive,
)
from custom_messages.contracts.task_v1 import TaskHistoryEntry
from custom_messages.enums import (
    ProgressState,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
)
from runtime_hosting.agent_execution import MAIN_LANE

logger = logging.getLogger(__name__)


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


def apply_task_state_tracking(
    manager: Any, task_info: Any, new_state: ProgressState, timestamp: str
) -> None:
    """Apply the local execution-slot and waiting-set updates for a state transition.

    Shared by ``TaskManager.update_main_task_state`` and atomic transitions in
    this module so queue/slot/waiting tracking cannot drift apart.
    """

    task_id = task_info.task.task_id
    is_top_level = task_info.task.parent_task_id is None
    is_locally_executable = manager.is_locally_executable_task(task_id)
    is_blocked = manager.is_blocked_state(new_state)
    if not is_locally_executable:
        if is_top_level and not is_blocked:
            manager.task_state.waiting_top_level_task_ids.discard(task_id)
        return
    if manager.is_executing_state(new_state):
        if new_state == ProgressState.IN_PROGRESS:
            manager.plan_state.final_review_pending_task_ids.discard(task_id)
        # A parallel request runs in its own lane beside the main slot
        # (custom_agents.execution_lanes): it neither takes nor preempts it.
        if lane_for_task(manager, task_id) == MAIN_LANE:
            manager._take_main_slot(task_id, timestamp)
        if is_top_level:
            manager.task_state.waiting_top_level_task_ids.discard(task_id)
        return
    if is_blocked:
        if is_top_level:
            manager.task_state.waiting_top_level_task_ids.add(task_id)
        if task_id == manager.task_state.current_in_progress_task_id:
            manager.task_state.current_in_progress_task_id = None
            manager._logger.info(
                f"Task {task_id} is {new_state.value.upper()}. Execution slot released."
            )
        return
    if task_id == manager.task_state.current_in_progress_task_id:
        manager.task_state.current_in_progress_task_id = None
        manager._logger.info(
            f"Task {task_id} finished processing. No task is currently IN_PROGRESS."
        )
        manager.task_state.waiting_top_level_task_ids.discard(task_id)
    elif is_top_level:
        manager.task_state.waiting_top_level_task_ids.discard(task_id)


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


def resume_wait_from_note(
    manager: Any,
    task_id: str,
    *,
    kind: str,
    wait_id: str,
    note: str,
    history: str,
    history_details: dict[str, Any],
    timestamp: str,
    front: bool = True,
) -> bool:
    """Atomically clear a wait record, save its note and history, move to ACCEPTED, and enqueue.

    Commits every mutation in a single ``_persist_runtime_state()`` write so a
    crash or transient DB failure leaves no partial state (no saved note with a
    still-present blocker, no removed blocker with an unqueued WAITING task, and
    no duplicate notes on retry). If the task is not ready to queue after
    removing the wait (for example, a pending recoverable pause), rolls back
    every mutation, logs, and returns ``False``. Also guards rollback and return
    value against both ``TaskCustodyTakenOver`` and ``RuntimeCustodyLost``.
    """

    task_info = manager.get_task_info(task_id)
    if task_info is None:
        return False
    blocker = manager.task_state.human_interaction_blockers.get(task_id)
    if blocker is None or not is_wait_blocker(blocker, kind, wait_id):
        return False
    if not manager.can_transition_task_state(task_id, ProgressState.ACCEPTED):
        return False

    previous_state = task_info.state
    wait_entry = build_task_history_entry(
        manager,
        task_id,
        history,
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        timestamp=timestamp,
        details=history_details,
    )
    state_entry = build_task_history_entry(
        manager,
        task_id,
        f"State changed from {previous_state.value} to {ProgressState.ACCEPTED.value}.",
        entry_type=TaskHistoryType.STATE_CHANGE,
        importance=TaskHistoryImportance.NORMAL,
        timestamp=timestamp,
        details={"from_state": previous_state.value, "to_state": ProgressState.ACCEPTED.value},
    )
    if wait_entry is None or state_entry is None:
        return False

    previous_blocker = dict(blocker)
    previous_directive = snapshot_runtime_resume_directive(manager.task_state, task_id)
    previous_states_count = len(task_info.task.task_state)
    previous_history_count = len(task_info.task.execution_history)
    previous_queue = deque(manager.task_state.task_queue)
    previous_waiting = set(manager.task_state.waiting_top_level_task_ids)
    previous_current = manager.task_state.current_in_progress_task_id
    previous_final_review = set(manager.plan_state.final_review_pending_task_ids)

    def _rollback() -> None:
        if manager.get_task_info(task_id) is not task_info:
            return
        manager.task_state.human_interaction_blockers[task_id] = previous_blocker
        restore_runtime_resume_directive(manager.task_state, task_id, previous_directive)
        task_info.state = previous_state
        del task_info.task.task_state[previous_states_count:]
        del task_info.task.execution_history[previous_history_count:]
        manager.task_state.task_queue = previous_queue
        manager.task_state.waiting_top_level_task_ids = previous_waiting
        manager.task_state.current_in_progress_task_id = previous_current
        manager.plan_state.final_review_pending_task_ids = previous_final_review

    try:
        manager.task_state.human_interaction_blockers.pop(task_id, None)
        add_runtime_resume_directive(manager.task_state, task_id, note)
        task_info.task.execution_history.append(wait_entry)
        task_info.update_state(ProgressState.ACCEPTED, timestamp)
        task_info.task.execution_history.append(state_entry)
        apply_task_state_tracking(manager, task_info, ProgressState.ACCEPTED, timestamp)
        if not is_ready(manager, task_id):
            _rollback()
            logger.warning(
                "Could not queue task after wait ended because another hold blocks readiness; "
                "rolled back resume. event=wait_resume_not_ready task_id=%s kind=%s wait_id=%s state=%s",
                task_id,
                kind,
                wait_id,
                previous_state.value,
            )
            return False
        if task_id not in manager.task_state.task_queue:
            if front:
                manager.task_state.task_queue.appendleft(task_id)
            else:
                manager.task_state.task_queue.append(task_id)
        manager._persist_runtime_state()
    except Exception:
        _rollback()
        raise
    if manager.get_task_info(task_id) is not task_info:
        return False
    try:
        manager._sync_plan_from_task(task_id, create=False)
    except Exception:
        logger.warning(
            "Task was resumed and queued, but plan sync failed. "
            "event=wait_resume_plan_sync_failed task_id=%s kind=%s wait_id=%s",
            task_id,
            kind,
            wait_id,
            exc_info=True,
        )
    return True


def clear_answered_blocker_with_resume_note(
    manager: Any,
    task_id: str,
    answers: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    resumed_interrupt_ids: Iterable[str] = (),
    answered_interrupt_ids: Iterable[str] = (),
    replace_directives: tuple[str, ...] = (),
    now: datetime | None = None,
) -> tuple[bool, Optional[str]]:
    """Consume an answered wait blocker; for a timed wait, save its resume note in the same write.

    For a live paused turn resuming after a timed wait, clearing the blocker and
    persisting ``resume_note(blocker, now=...)`` atomically closes both crash
    windows (between blocker clear and ``IN_PROGRESS``, and during the resumed
    turn before a decision batch is recorded): if the worker crashes before the
    resumed turn records a decision, restart recovery delivers the note so the
    next turn knows the wait already finished; when the live turn records its
    decision normally, ``TurnStartNotes`` releases that note cleanly.

    Any prior live-wait directives named in ``replace_directives`` are removed
    in the same write so repeated waits inside one turn keep only the latest
    wait note. If persistence raises, the directive slot is rolled back and the
    blocker is restored *unmarked* (matching ``mark_answer_held`` /
    ``remove_answer_mark``) because the caller already popped the pending
    answer from memory: leaving a held-answer mark without an answer would hide
    the task from both the next timed-wait pass and the stuck-task check.

    Returns ``(cleared, added_timed_wait_note)``.
    """

    blocker = manager.task_state.human_interaction_blockers.get(task_id)
    if not should_clear_answered_blocker(
        blocker,
        answers,
        resumed_interrupt_ids=resumed_interrupt_ids,
        answered_interrupt_ids=answered_interrupt_ids,
    ):
        return False, None
    assert blocker is not None
    if not is_timed_wait_blocker(blocker):
        return clear_human_interaction_blocker(manager, task_id), None

    task_info = manager.get_task_info(task_id)
    note = timed_waits_rules.resume_note(blocker, now=now or timed_waits_rules.utc_now())
    unmarked_blocker = unmark_wait_record(blocker)
    previous_directive = snapshot_runtime_resume_directive(manager.task_state, task_id)
    manager.task_state.human_interaction_blockers.pop(task_id, None)
    if replace_directives:
        drop_resume_directive_blocks(manager.task_state, task_id, replace_directives)
    added = add_runtime_resume_directive(manager.task_state, task_id, note)
    try:
        manager._persist_runtime_state()
    except Exception:
        if task_info is None or manager.get_task_info(task_id) is task_info:
            manager.task_state.human_interaction_blockers[task_id] = unmarked_blocker
            restore_runtime_resume_directive(manager.task_state, task_id, previous_directive)
        raise
    if task_info is not None and manager.get_task_info(task_id) is not task_info:
        return False, None
    return True, (note if added else None)


__all__ = [
    "apply_task_state_tracking",
    "build_task_history_entry",
    "clear_answered_blocker_with_resume_note",
    "record_blocker_with_history",
    "resume_wait_from_note",
]
