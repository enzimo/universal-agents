"""Saved records of a task that waits for people or for trusted code it started.

A task has at most one record (``TaskRuntimeState.human_interaction_blockers``),
of one of three kinds:

- ``access_request``: a task paused by ``request_access`` (or a workflow's
  batch of approvals) waits in its own process until people decide in the
  Inbox (``custom_agents.access_request_runtime``).
- ``tool_setup_run``: TeamLead's turn paused on ``set_up_tools`` waits until
  trusted code ends the run (``custom_agents.tool_setup_run_runtime``).
- ``timed_wait``: a turn paused on ``wait`` or ``long_wait`` waits until its
  saved UTC resume time passes (``custom_agents.timed_wait_runtime``).

For both waits trusted code ends in this process (``tool_setup_run`` and
``timed_wait``), when the wait ends while the paused turn is still in memory,
its answer waits in memory for the task's lane, which can be busy with another
turn or paused. The record then stays, marked with who holds the answer and
since when (``mark_answer_held``), until the turn really resumes. Until
2026-11-19 a setup-run record was deleted when the run ended, so a task whose
answer waited for a busy lane had no saved reason to wait and was reported as
stuck.

The mark exists only while a paused turn of this process waits on the
interrupt with its answer held or being resumed: a failed save of the mark
puts the unmarked record back, the mark is removed wherever the turn is
dropped or the task resumes from a note instead (``remove_answer_mark``), and
agent start removes every mark (``clear_answer_marks``), because no paused
turn survives a restart. The stuck-task check excuses a marked task, so a mark
without its answer would hide a stranded task.

A record is removed only by what ends its wait: the answer a resumed turn
takes (``clear_answered_blocker``, before the turn runs, so a new pause in it
keeps its own record), a resume from a note (``clear_wait_record``), or the
task ending. Recovery, the task queue, runtime-work summaries and the
stuck-task check (``custom_agents.task_wait_reasons``) read the record to tell
a waiting task from a stuck one; it never grants anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from custom_agents.task_runtime_state import TaskRuntimeState
from custom_agents.timed_waits import TIMED_WAIT_KIND
from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_KIND, TOOL_SETUP_RUN_RESPONSE_KIND
from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_ID_KEY,
    ACCESS_REQUEST_INTERRUPT_KIND,
    access_interrupt_request_ids,
)

# A task blocked on a wait records the same kind as its interrupt.
ACCESS_REQUEST_BLOCKER = ACCESS_REQUEST_INTERRUPT_KIND
SETUP_RUN_BLOCKER = TOOL_SETUP_RUN_KIND
TIMED_WAIT_BLOCKER = TIMED_WAIT_KIND
# Waits trusted code ends in this process, mapped to the record field that
# identifies one wait of that kind.
_WAIT_ID_KEYS: Mapping[str, str] = {
    SETUP_RUN_BLOCKER: "run_id",
    TIMED_WAIT_BLOCKER: "interrupt_id",
}
# The mark of a record whose answer waits in memory for the task's lane: when
# (UTC, ISO 8601) and which agent process holds the answer. Agent ids are new
# for each process, so a mark from another id is left over from before a
# restart (``custom_agents.task_wait_reasons``).
ANSWER_HELD_AT_KEY = "answer_held_at"
ANSWER_HELD_BY_KEY = "answer_held_by"


def wait_id_key(kind: str) -> str:
    """The record and log field that identifies one locally-ended wait of ``kind``."""

    return _WAIT_ID_KEYS[kind]


def blocker_request_ids(blocker: dict[str, Any]) -> list[str]:
    """The access requests a blocker waits on (several for one batch pause)."""

    if str(blocker.get("kind") or "") != ACCESS_REQUEST_BLOCKER:
        return []
    return access_interrupt_request_ids(blocker)


def is_wait_blocker(blocker: Mapping[str, Any] | None, kind: str, wait_id: str | None = None) -> bool:
    """A record of a wait trusted code ends (``tool_setup_run`` or ``timed_wait``); with ``wait_id``, only that wait."""

    id_key = _WAIT_ID_KEYS.get(kind)
    if not id_key or not blocker or blocker.get("kind") != kind:
        return False
    return wait_id is None or blocker.get(id_key) == wait_id


def is_setup_run_blocker(blocker: Mapping[str, Any] | None, run_id: str | None = None) -> bool:
    """A ``set_up_tools`` record; with ``run_id``, only the one for that run."""

    return is_wait_blocker(blocker, SETUP_RUN_BLOCKER, run_id)


def is_timed_wait_blocker(blocker: Mapping[str, Any] | None, interrupt_id: str | None = None) -> bool:
    """A ``wait`` / ``long_wait`` record; with ``interrupt_id``, only the one for that interrupt."""

    return is_wait_blocker(blocker, TIMED_WAIT_BLOCKER, interrupt_id)


def answer_held(blocker: Mapping[str, Any] | None, holder_id: str | None = None) -> bool:
    """A setup-run or timed-wait record whose answer waits for the task's lane; with ``holder_id``, held by that agent."""

    if not blocker or blocker.get("kind") not in _WAIT_ID_KEYS or not blocker.get(ANSWER_HELD_AT_KEY):
        return False
    return holder_id is None or blocker.get(ANSWER_HELD_BY_KEY) == holder_id


class TaskHumanInteractionBlockerHost(Protocol):
    """Minimal TaskManager state needed to maintain blocker projections."""

    task_state: TaskRuntimeState

    def _persist_runtime_state(self) -> None: ...


def record_human_interaction_blocker(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
    blocker: dict[str, Any],
) -> None:
    """Persist the non-authoritative reason a task must remain waiting."""

    if task_id not in host.task_state.tasks:
        raise KeyError(f"Task {task_id} is not tracked")
    host.task_state.human_interaction_blockers[task_id] = dict(blocker)
    host._persist_runtime_state()


def clear_human_interaction_blocker(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
) -> bool:
    removed = host.task_state.human_interaction_blockers.pop(task_id, None) is not None
    if removed:
        host._persist_runtime_state()
    return removed


def mark_answer_held(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
    kind: str,
    wait_id: str,
    *,
    holder_id: str,
    at: str,
) -> bool:
    """Mark the wait record: its answer now waits for the task's lane.

    True only for the first caller (compare-and-set on the operations lane),
    so the answer is handed to the paused turn once.
    """

    blocker = host.task_state.human_interaction_blockers.get(task_id)
    if blocker is None or not is_wait_blocker(blocker, kind, wait_id) or answer_held(blocker):
        return False
    host.task_state.human_interaction_blockers[task_id] = {
        **blocker,
        ANSWER_HELD_AT_KEY: at,
        ANSWER_HELD_BY_KEY: holder_id,
    }
    try:
        host._persist_runtime_state()
    except Exception:
        # No answer will be held: the unmarked record goes back, so recovery
        # answers the turn on its next pass (module docstring).
        host.task_state.human_interaction_blockers[task_id] = blocker
        raise
    return True


def _unmarked(blocker: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in blocker.items() if key not in (ANSWER_HELD_AT_KEY, ANSWER_HELD_BY_KEY)}


def remove_answer_mark(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
    kind: str,
    wait_id: str,
) -> dict[str, Any] | None:
    """Remove the mark from the wait's record, keeping the record; the record, or None when it is not that wait's.

    For when no paused turn waits on this wait any more. Memory changes first:
    if the save then fails, the unmarked record, which cannot hide the task,
    is what the next save writes.
    """

    blocker = host.task_state.human_interaction_blockers.get(task_id)
    if blocker is None or not is_wait_blocker(blocker, kind, wait_id):
        return None
    unmarked = _unmarked(blocker)
    if unmarked != blocker:
        host.task_state.human_interaction_blockers[task_id] = unmarked
        host._persist_runtime_state()
    return dict(unmarked)


def clear_answer_marks(host: TaskHumanInteractionBlockerHost) -> list[str]:
    """Remove every mark (agent start: no paused turn survives a restart); the tasks whose mark was removed."""

    blockers = host.task_state.human_interaction_blockers
    marked = [task_id for task_id, blocker in blockers.items() if answer_held(blocker)]
    for task_id in marked:
        blockers[task_id] = _unmarked(blockers[task_id])
    if marked:
        host._persist_runtime_state()
    return marked


def clear_wait_record(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
    kind: str,
    wait_id: str,
) -> bool:
    """Remove the task's record only while it is this wait's (True when this call removed it)."""

    if not is_wait_blocker(host.task_state.human_interaction_blockers.get(task_id), kind, wait_id):
        return False
    return clear_human_interaction_blocker(host, task_id)


def _answers(blocker: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
    """Whether a structured answer is the one this record waits for (the interrupt id is checked by the caller)."""

    kind = blocker.get("kind")
    if kind == ACCESS_REQUEST_BLOCKER:
        request_id = response.get(ACCESS_REQUEST_ID_KEY)
        return bool(request_id) and str(request_id) in blocker_request_ids(dict(blocker))
    if kind == SETUP_RUN_BLOCKER:
        return response.get("kind") == TOOL_SETUP_RUN_RESPONSE_KIND and response.get("run_id") == blocker.get("run_id")
    return False


def should_clear_answered_blocker(
    blocker: Mapping[str, Any] | None,
    answers: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    resumed_interrupt_ids: Iterable[str] = (),
    answered_interrupt_ids: Iterable[str] = (),
) -> bool:
    """Whether ``blocker`` is consumed by the answers resuming the paused turn."""

    interrupt_id = blocker.get("interrupt_id") if blocker else None
    if blocker is None or not interrupt_id:
        return False
    answer_list = list(answers)
    answered = any(answered_id == interrupt_id and _answers(blocker, response) for answered_id, response in answer_list)
    if not answered and is_timed_wait_blocker(blocker):
        answered = interrupt_id in set(answered_interrupt_ids) or any(
            answered_id == interrupt_id for answered_id, _ in answer_list
        )
    return answered or (answer_held(blocker) and interrupt_id in set(resumed_interrupt_ids))


def clear_answered_blocker(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
    answers: Iterable[tuple[str, Mapping[str, Any]]],
    resumed_interrupt_ids: Iterable[str] = (),
    answered_interrupt_ids: Iterable[str] = (),
) -> bool:
    """Consume only the wait an answer belongs to; this does not grant tool authority.

    ``answers`` pairs an interrupt id with the structured answer the turn gets.
    An access record is answered when the interrupt id matches and the answer
    names one of its requests; a setup-run record when the interrupt id
    matches and the answer is its run's; a timed-wait record when the resumed
    turn answers its interrupt (any answer, via ``answered_interrupt_ids`` or
    ``answers``), so a stray answer cannot leave a stale record that resumes
    the task a second time. A marked setup-run or timed-wait record also ends
    when the resumed turn holds its interrupt (``resumed_interrupt_ids``),
    whatever answer resumes it: the wait has ended and another answer may have
    replaced the held one. An unmarked setup-run record stays then: its run
    still waits and must resume the task later. Run before resuming the SDK,
    so a new interruption can install its own record without later cleanup
    deleting it.
    """
    blocker = host.task_state.human_interaction_blockers.get(task_id)
    if not should_clear_answered_blocker(
        blocker,
        answers,
        resumed_interrupt_ids=resumed_interrupt_ids,
        answered_interrupt_ids=answered_interrupt_ids,
    ):
        return False
    return clear_human_interaction_blocker(host, task_id)


def has_pending_human_interaction(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
) -> bool:
    return task_id in host.task_state.human_interaction_blockers


def get_human_interaction_blocker(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
) -> dict[str, Any] | None:
    blocker = host.task_state.human_interaction_blockers.get(task_id)
    return dict(blocker) if blocker is not None else None
