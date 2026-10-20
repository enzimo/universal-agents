"""Stop a task that keeps waiting and resuming, and tell the person (``MAX_WAITS_PER_TASK``).

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
its requester with ``wait_limit_message``. A scheduled run's requester is the
scheduler, whose failure reaches the person only per the schedule's notify
setting, so for a top-level scheduled run the person also gets one chat
message (``tell_person_about_wait_limit``). A subtask's requester is the agent
that delegated it, which receives the failure and answers its own requester.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import RequesterType
from id_generation import operational_id

logger = logging.getLogger(__name__)

# Waits one task may start (owner's decision of 2026-10-19). A fixed value, not
# a setting: a configurable limit changes the team transfer's settings format
# (a new section version that older releases' files cannot skip), so it stays
# fixed until someone needs to adjust it.
MAX_WAITS_PER_TASK = 5

WAIT_LIMIT_NOTICE_REASON = "task_wait_limit_reached"
WAIT_LIMIT_NOTICE_ID_PREFIX = "wait_limit"


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


def _chat_route(agent: Any, task_info: Any) -> str | None:
    """The person's chat route for a top-level scheduled run, or None."""

    task = getattr(task_info, "task", None)
    meta = getattr(task_info, "meta", None)
    if getattr(task, "parent_task_id", None) is not None:
        return None
    if getattr(meta, "requester_type", None) != RequesterType.TIMER_REQUESTER:
        return None
    if getattr(meta, "gateway_context", None) is None:
        return None
    route = getattr(getattr(task_info, "header", None), "response_routing_key", None)
    return route if route and route != getattr(agent, "id", None) else None


async def tell_person_about_wait_limit(agent: Any, task_info: Any, error: TaskWaitLimitReached) -> bool:
    """One chat message for a scheduled run stopped at the limit; False when none was sent."""

    route = _chat_route(agent, task_info)
    if route is None:
        return False
    notice_id = operational_id(WAIT_LIMIT_NOTICE_ID_PREFIX, error.task_id)
    # A standalone notice: never the run's own result.
    context = task_info.meta.gateway_context.model_copy(
        update={"scheduled_job_id": None, "schedule_owner_agent_id": None, "delivery_mode": "autonomous"},
        deep=True,
    )
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            notification_id=notice_id,
            text="A scheduled run was stopped\n\n" + str(error),
            urgency="normal",
            reason=WAIT_LIMIT_NOTICE_REASON,
            source_task_id=error.task_id,
            source_agent_id=agent.id,
            source_agent_name=getattr(agent, "name", None),
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            msg_correlation_id=notice_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=context,
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=route,
            response_routing_key=agent.id,
            idempotency_key=notice_id,
        ),
    )
    try:
        await agent.team_comms.send_message(message=envelope, routing_key=route)
    except Exception:  # noqa: BLE001 - the task is already failed and its requester told
        logger.exception(
            "Could not tell the person that a scheduled run was stopped at its wait limit. "
            "event=task_wait_limit_notice_failed agent_id=%s task_id=%s",
            agent.id,
            error.task_id,
        )
        return False
    logger.info(
        "Told the person that a scheduled run was stopped at its wait limit. "
        "event=task_wait_limit_notice_sent agent_id=%s task_id=%s route=%s",
        agent.id,
        error.task_id,
        route,
    )
    return True


__all__ = [
    "MAX_WAITS_PER_TASK",
    "TaskWaitLimitReached",
    "WAIT_LIMIT_NOTICE_REASON",
    "tell_person_about_wait_limit",
    "wait_limit_message",
]
