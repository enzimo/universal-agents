"""Shared pause/resume helpers for waits that trusted code ends in this process.

Both ``set_up_tools`` runs (``custom_agents.tool_setup_run_runtime``) and timed
waits (``custom_agents.timed_wait_runtime``) pause a turn with a local Strands
interrupt and save one record in ``TaskRuntimeState.human_interaction_blockers``.
When the wait ends, the task resumes in one of two ways:

- **Live paused turn** (``hand_answer_to_paused_turn``): mark the wait record
  as holding an answer first, place the ``TaskSteering`` answer in
  ``_pending_interrupt_responses``, and wake the pending-work drain. The
  resumed turn removes the record just before it runs
  (``task_steering_runtime``).
- **No live turn** (``resume_from_note``): after a restart, a dropped turn, or
  takeover, remove any held-answer mark, save a durable resume directive,
  conditionally remove the wait record, append a history entry, move the task
  to ``ACCEPTED``, enqueue it at the front, and wake the drain. If anything
  after removing the record fails, the record is put back so recovery tries
  again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_human_interaction_blockers import (
    clear_wait_record,
    mark_answer_held,
    remove_answer_mark,
    restore_wait_record,
    wait_id_key,
)
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import ProgressState, TaskHistoryImportance, TaskHistoryType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitLogSpec:
    """Logging names for one kind of locally-ended wait (setup run or timed wait)."""

    label: str
    dropped_event: str
    restored_event: str
    restore_failed_event: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def append_wait_history(agent: Any, task_id: str, text: str, details: dict[str, Any]) -> None:
    """Append a high-importance wait lifecycle entry to the task's history on the operations lane."""

    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        text,
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details=details,
    )


def paused_turn_waits(agent: Any, task_id: str, interrupt_id: str | None) -> bool:
    """The task's paused turn is still in memory and waits on ``interrupt_id``."""

    if not interrupt_id:
        return False
    session = getattr(agent, "_interrupted_turn_sessions", {}).get(task_id)
    return session is not None and any(item.interrupt_id == interrupt_id for item in session.interrupts)


def drop_paused_turn(
    agent: Any,
    task_id: str,
    interrupt_id: str | None,
    *,
    why: str,
    log: WaitLogSpec,
) -> bool:
    """Forget the task's paused turn when it waits on ``interrupt_id`` but can never take its answer (logged).

    Its Strands agent is not reused (``replace_before_next_turn``); the task's
    next turn starts afresh from its note. Left in place, a later answer
    would be handed to it again.
    """

    if not interrupt_id:
        return False
    sessions = getattr(agent, "_interrupted_turn_sessions", {})
    session = sessions.get(task_id)
    if session is None or not any(item.interrupt_id == interrupt_id for item in session.interrupts):
        return False
    sessions.pop(task_id, None)
    session.strands.replace_before_next_turn = True
    logger.warning(
        "Dropped a paused turn that cannot take its %s answer. event=%s task_id=%s interrupt_id=%s why=%s",
        log.label,
        log.dropped_event,
        task_id,
        interrupt_id,
        why,
    )
    return True


async def hand_answer_to_paused_turn(
    agent: Any,
    *,
    task_id: str,
    kind: str,
    wait_id: str,
    steering: TaskSteering,
    replace_pending: bool = True,
    at: str | None = None,
) -> bool:
    """Mark the record, then hand ``steering`` to the paused turn; it runs when the task's lane is free.

    The mark is saved before the answer exists in memory, so the answer is
    never held without its record; the resumed turn removes the record
    (``task_steering_runtime``). When ``replace_pending`` is False and another
    answer is already pending for the task, leaves the record unmarked and
    returns False so the next pass tries again.
    """

    if not replace_pending and task_id in agent._pending_interrupt_responses:
        return False
    manager = agent.task_manager
    marked = await manager.operations_runtime.run(
        partial(mark_answer_held, manager),
        task_id,
        kind,
        wait_id,
        holder_id=str(agent.id),
        at=at or utc_now_iso(),
    )
    if not marked:
        return False
    if not replace_pending and task_id in agent._pending_interrupt_responses:
        await manager.operations_runtime.run(partial(remove_answer_mark, manager), task_id, kind, wait_id)
        return False
    agent._pending_interrupt_responses[task_id] = steering
    schedule_pending_work(agent)
    return True


async def resume_from_note(
    agent: Any,
    *,
    task_id: str,
    kind: str,
    wait_id: str,
    note: str,
    history: str,
    history_details: dict[str, Any],
    log: WaitLogSpec,
    wake: bool = True,
    at: str | None = None,
) -> bool:
    """Queue the task at the front with a note its next turn reads; True when this call removed the record.

    The caller holds the wait's lock. No paused turn waits on this wait here, so
    the record loses any mark first: should this fail, the task must not look
    as if its answer were held. The note is saved before the record is
    removed: a restart between the two finds the record and saves the same
    note again, which is not added twice (``append_runtime_resume_directive``).
    The record goes before the task is queued, because the queue refuses a
    task with a record (``task_queue_safety.is_ready``); if anything after the
    removal fails, the record is put back and the error raised, so recovery
    tries again. Repeating is safe: the note is not added twice and the state
    may be set again; only a second history line is added.
    """

    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    record = await run_ops(partial(remove_answer_mark, manager), task_id, kind, wait_id)
    if record is None:
        return False  # read under the lock; only the task ending removes it meanwhile
    await run_ops(manager.append_runtime_resume_directive, task_id, note)
    try:
        if not await run_ops(partial(clear_wait_record, manager), task_id, kind, wait_id):
            return False
        await append_wait_history(agent, task_id, history, history_details)
        await run_ops(manager.update_main_task_state, task_id, ProgressState.ACCEPTED, at or utc_now_iso())
        await run_ops(manager.enqueue_task, task_id, front=True)
    except Exception:
        await _put_record_back(agent, task_id, record, kind=kind, wait_id=wait_id, log=log)
        raise
    if wake:
        schedule_pending_work(agent)
    return True


async def _put_record_back(
    agent: Any,
    task_id: str,
    record: dict[str, Any],
    *,
    kind: str,
    wait_id: str,
    log: WaitLogSpec,
) -> None:
    """After a failed resume from a note, the record goes back so recovery finds the task again (logged)."""

    manager = agent.task_manager
    id_key = wait_id_key(kind)
    try:
        restored = await manager.operations_runtime.run(partial(restore_wait_record, manager), task_id, record)
    except Exception as exc:  # noqa: BLE001 - the record is back in memory; the next save writes it
        logger.warning(
            "Could not save the %s record put back after a failed resume. "
            "event=%s task_id=%s %s=%s error_type=%s",
            log.label,
            log.restore_failed_event,
            task_id,
            id_key,
            wait_id,
            type(exc).__name__,
        )
        return
    logger.warning(
        "A resume from a note failed after the task's %s record was removed; the record is back. "
        "event=%s task_id=%s %s=%s restored=%s",
        log.label,
        log.restored_event,
        task_id,
        id_key,
        wait_id,
        restored,
    )


__all__ = [
    "WaitLogSpec",
    "append_wait_history",
    "drop_paused_turn",
    "hand_answer_to_paused_turn",
    "paused_turn_waits",
    "resume_from_note",
    "utc_now_iso",
]
