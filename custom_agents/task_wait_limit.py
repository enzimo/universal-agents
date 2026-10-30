"""Stop a task that keeps waiting and resuming (``MAX_WAITS_PER_TASK``).

Why: on 2026-10-19 one scheduled knowledge-base audit escalated its findings,
waited for the team lead, and ran the whole audit again on each resume: four
passes for one occurrence, and nothing bounded it except a pass that found
nothing to report. The causes are fixed (``custom_agents.turn_work_notes``,
``custom_agents.worker_result_reports``); this limit is the backstop for any
other loop of the same shape, by the owner's decision of 2026-10-19 (limit 5).

What counts: a turn of the task that sent something out and waited for it (a
delegation, a manager escalation, a question to the requester, a credential
form or signup request), as recorded by the task's ``turn_work_before_wait``
notes with ``started_wait``. A check-back wake that only keeps waiting and a
worker's report (no reply) do not count, so long delegated work with
check-backs keeps running.

What happens: when a turn would start the task's wait number limit + 1, the
turn executor raises ``TaskWaitLimitReached`` before the outbox sends anything,
so the last request never goes out. The failed-turn path
(``turn_failure_runtime.fail_task_after_turn_error``) fails the task and tells
its requester with ``wait_limit_message``. A subtask's requester is the agent
that delegated it, which receives the failure and answers its own requester.
A top-level scheduled run's requester is the scheduler; its person hears of
the failed run, like any other, from the team lead's failing-schedule notice
(``custom_agents.scheduled_run_failures``). This module used to send its own
chat message for such runs too, so the person got two (removed 2026-10-30).
"""

from __future__ import annotations

# Waits one task may start (owner's decision of 2026-10-19). A fixed value, not
# a setting: a configurable limit changes the team transfer's settings format
# (a new section version that older releases' files cannot skip), so it stays
# fixed until someone needs to adjust it.
MAX_WAITS_PER_TASK = 5


class TaskWaitLimitReached(RuntimeError):
    """A task's turn would wait once more than ``MAX_WAITS_PER_TASK`` allows."""

    def __init__(self, *, task_id: str, waits: int, limit: int) -> None:
        self.task_id = task_id
        self.waits = waits
        self.limit = limit
        super().__init__(wait_limit_message(waits=waits, limit=limit))


def wait_limit_message(*, waits: int, limit: int) -> str:
    """What the requester and the person read (plain language, no internal names)."""

    return (
        f"This task was stopped because it had already waited {waits} times for work it sent "
        f"out (the limit for one task is {limit}) and was about to wait again, so it was "
        "probably repeating itself. Nothing more was sent. Changes it already made are kept. "
        "Check what it did before running it again."
    )


__all__ = [
    "MAX_WAITS_PER_TASK",
    "TaskWaitLimitReached",
    "wait_limit_message",
]
