"""Persistence-aware task blockers for tasks waiting on a person.

The only blocker kind is an access request: a task paused by
``request_access`` (or a workflow's batch of approvals) waits in its own
process until people decide in the Inbox. The blocker records what it waits
on so recovery, the task queue and runtime-work summaries can tell a waiting
task from a stuck one; it never grants anything.
"""

from __future__ import annotations

from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_INTERRUPT_KIND,
    access_interrupt_request_ids,
)
from typing import Any, Protocol
from custom_agents.task_runtime_state import TaskRuntimeState

# A task blocked on a review records the same kind as its interrupt.
ACCESS_REQUEST_BLOCKER = ACCESS_REQUEST_INTERRUPT_KIND


def blocker_request_ids(blocker: dict[str, Any]) -> list[str]:
    """The access requests a blocker waits on (several for one batch pause)."""

    if str(blocker.get("kind") or "") != ACCESS_REQUEST_BLOCKER:
        return []
    return access_interrupt_request_ids(blocker)


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


def clear_answered_access_blocker(
    host: TaskHumanInteractionBlockerHost,
    task_id: str,
    answers: list[tuple[str, str]],
) -> bool:
    """Consume only the answered wait; this does not grant tool authority.

    ``answers`` pairs an interrupt id with the access request it answered.
    Run before resuming the SDK so a new interruption can install its own
    blocker without later cleanup deleting it.
    """
    blocker = host.task_state.human_interaction_blockers.get(task_id)
    if blocker is None or blocker.get("kind") != ACCESS_REQUEST_BLOCKER:
        return False
    interrupt_id = blocker.get("interrupt_id")
    waited = set(blocker_request_ids(blocker))
    if not interrupt_id or not any(
        answered_interrupt == interrupt_id and request_id in waited for answered_interrupt, request_id in answers
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
