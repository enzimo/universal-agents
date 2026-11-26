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
  takeover, strip any held-answer mark if present, then atomically save the
  durable resume directive, remove the wait record, append wait and state-change
  history, move the task to ``ACCEPTED``, and enqueue it at the front in one
  persisted write (``task_state_transitions.resume_wait_from_note``), waking the
  drain on success. A failed write rolls back completely so recovery tries again
  without duplicating notes or stranding the task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_human_interaction_blockers import (
    mark_answer_held,
    remove_answer_mark,
)
from custom_agents.task_state_transitions import resume_wait_from_note
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import TaskHistoryImportance, TaskHistoryType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitLogSpec:
    """Logging names for one kind of locally-ended wait (setup run or timed wait)."""

    label: str
    dropped_event: str


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


def _steering_interrupt_id(steering: TaskSteering) -> str | None:
    """Extract the first non-empty interrupt id carried by ``steering``."""

    for item in steering.interrupt_responses or ():
        interrupt_id = str(item.get("interrupt_id") or "").strip()
        if interrupt_id:
            return interrupt_id
    return None


async def hand_answer_to_paused_turn(
    agent: Any,
    *,
    task_id: str,
    kind: str,
    wait_id: str,
    steering: TaskSteering,
    interrupt_id: str | None = None,
    replace_pending: bool = True,
    at: str | None = None,
) -> bool:
    """Mark the record, then hand ``steering`` to the paused turn; it runs when the task's lane is free.

    The mark is saved before the answer exists in memory, so the answer is
    never held without its record; the resumed turn removes the record
    (``task_steering_runtime``). After saving the mark, re-checks that the
    paused turn is still waiting (so a concurrent stop or turn drop never
    leaves an orphan answer) and, when ``replace_pending`` is False, that no
    other answer arrived meanwhile; otherwise removes the mark and returns
    False.
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
    target_interrupt_id = interrupt_id or _steering_interrupt_id(steering)
    turn_still_waits = (
        paused_turn_waits(agent, task_id, target_interrupt_id)
        if target_interrupt_id
        else task_id in getattr(agent, "_interrupted_turn_sessions", {})
    )
    if not turn_still_waits or (not replace_pending and task_id in agent._pending_interrupt_responses):
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
    wake: bool = True,
    at: str | None = None,
) -> bool:
    """Queue the task at the front with a note its next turn reads; True when this call removed the record.

    The caller holds the wait's lock. No paused turn waits on this wait here, so
    if the record still carries a held-answer mark it is stripped first (a zero-write
    no-op when already unmarked) so a failed resume never leaves the task looking
    as if its answer were held. Then ``resume_wait_from_note`` commits the note,
    blocker removal, history entries, ``ACCEPTED`` transition, and front-of-queue
    enqueue in a single persisted write, rolling back cleanly on failure.
    """

    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    record = await run_ops(partial(remove_answer_mark, manager), task_id, kind, wait_id)
    if record is None:
        return False  # read under the lock; only the task ending removes it meanwhile
    resumed = bool(
        await run_ops(
            partial(resume_wait_from_note, manager),
            task_id,
            kind=kind,
            wait_id=wait_id,
            note=note,
            history=history,
            history_details=history_details,
            timestamp=at or utc_now_iso(),
            front=True,
        )
    )
    if resumed and wake:
        schedule_pending_work(agent)
    return resumed


__all__ = [
    "WaitLogSpec",
    "append_wait_history",
    "drop_paused_turn",
    "hand_answer_to_paused_turn",
    "paused_turn_waits",
    "resume_from_note",
    "utc_now_iso",
]
