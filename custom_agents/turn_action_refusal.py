"""An action of a turn that trusted code refuses for good (``TurnActionRefused``).

Why a refusal is terminal: the outbox's model recovery runs the task again with
a recovery note, and each such turn records a new action batch. The recovery
limit counts attempts per batch, so it never stopped a refusal that the next
turn's same decision meets again: a task whose final answer the access
workflow refused kept running model turns for as long as the model kept
answering the same way (found 2026-11-16 while fixing the held-answer stop).

What happens: the outbox records the refused action ``failed_terminal``
without automatic retry, model recovery or isolated recovery
(``turn_action_outbox_runtime._schedule_turn_action_recovery``), and the error
ends the turn. Every place that runs a turn's actions ends the task through
``turn_failure_runtime.fail_task_after_turn_error``: the turn runners and the
outbox's retry of an interrupted batch. The task fails, its conversation turn
closes, and its requester reads ``requester_message``.

Raise it only when another attempt would meet the same refusal
(``access_workflow_runtime.WorkflowCompletionRefused``).
"""

from __future__ import annotations


class TurnActionRefused(RuntimeError):
    """Trusted code refused this turn action for good; another model turn would meet the same refusal.

    ``str(error)`` is the plain statement kept in records; ``requester_message``
    is what the task's requester reads, the same statement unless a subclass
    adds to it.
    """

    @property
    def requester_message(self) -> str:
        return str(self)


__all__ = ["TurnActionRefused"]
