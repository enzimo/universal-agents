"""Transport-copy helpers for task payloads."""

from __future__ import annotations

from custom_messages.contracts.task_v1 import Task


def task_request_transport_copy(task: Task) -> Task:
    """
    Build the transport payload for a delegated task request.

    Manager-local task state, task history, execution digest, and plan
    annotations stay local to the manager runtime. Specialists receive only
    the executable task request.
    """
    transport_task = task.model_copy(deep=True)
    transport_task.task_state = []
    transport_task.execution_history = []
    transport_task.task_plan = None
    transport_task.execution_digest = None
    transport_task.thoughts_info = None
    transport_task.answer = None
    transport_task.errors = None
    return transport_task


def task_response_history_transport_copy(task: Task) -> list:
    """
    Copy returned task history across the transport boundary without plan links.

    A delegated worker may return useful execution-history entries, but
    first-class plan identifiers are manager-local sidecar state.
    """
    sanitized_history = []
    for entry in task.execution_history:
        entry_copy = entry.model_copy(deep=True)
        entry_copy.plan_id = None
        sanitized_history.append(entry_copy)
    return sanitized_history
