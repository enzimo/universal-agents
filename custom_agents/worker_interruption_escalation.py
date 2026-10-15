"""Stop retrying work that keeps losing its worker, and ask the TeamLead to look.

Each lost launch lease interrupts the run on that worker and the lead sends the
work out again (worker_handback_runtime). If the same work is interrupted
``max_consecutive_interruptions`` times in a row, retrying is not helping:

- ordinary work is counted per task id; the task is cancelled with the usual
  reply to its requester (a subtask the lead delegated itself fails instead, so
  its parent turn resumes and can decide what to do);
- scheduled work is counted per schedule, across its runs, since the last run
  that finished; the interrupted run is closed and not rerun, while the
  schedule itself is left alone because pausing it is an authorized decision.

The runtime then puts an investigation task on the TeamLead's own queue with the
evidence from every interruption. The TeamLead's instructions tell it to find
the likely cause, tell the owner, and pause the schedule if it judges that best.
Escalation repeats every ``max_consecutive_interruptions`` further interruptions
of an unbroken streak, so a still-failing schedule produces bounded reminders.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.control_runtime import send_task_cancel_response
from custom_messages.contracts.routing_v1 import (
    ManagerEscalationContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from custom_messages.enums import MessageMetaType, RequesterType
from storage.worker_handback_repository import WorkerHandbackRepository

INTERRUPTION_REVIEW_TASK_PREFIX = "interruption_review_"
_MAX_EVIDENCE_ENTRIES = 10


@dataclass(frozen=True)
class InterruptionStreak:
    """Consecutive lost-lease interruptions of one task or one schedule."""

    scope: str  # "task" or "schedule"
    scope_id: str
    count: int
    limit: int
    handbacks: tuple[WorkerTaskHandbackV1, ...]
    occurrence: Any = None

    @property
    def should_escalate(self) -> bool:
        return self.count >= self.limit and self.count % self.limit == 0


def interruption_streak(
    agent: Any,
    repository: WorkerHandbackRepository,
    handback: WorkerTaskHandbackV1,
    limit: int,
) -> InterruptionStreak:
    """Count the unbroken streak this interrupted handback belongs to."""

    task_id = handback.handed_back.task_id
    records = getattr(getattr(agent, "_schedule_store", None), "worker_records", None)
    occurrence = records.for_task(task_id, agent.team_id) if records is not None else None
    if records is None or occurrence is None:
        handbacks = repository.interrupted_for_tasks(agent.team_id, frozenset({task_id}))
        return InterruptionStreak("task", task_id, len(handbacks), limit, handbacks)
    runs = records.occurrences_for_schedule(occurrence.schedule_id, agent.team_id)
    # Any run that finished (even with an error) ends the streak; superseded or
    # coalesced occurrences did not finish a run of their own.
    finished = [
        _due(run.due_at)
        for run in runs
        if run.state == "finished" and run.coalesced_into_task_id is None
    ]
    reset_at = max(finished, default=None)
    streak_task_ids = frozenset(
        run.task_id for run in runs if reset_at is None or _due(run.due_at) > reset_at
    )
    handbacks = repository.interrupted_for_tasks(agent.team_id, streak_task_ids)
    return InterruptionStreak(
        "schedule", occurrence.schedule_id, len(handbacks), limit, handbacks, occurrence
    )


async def escalate_interruptions(
    agent: Any, handback: WorkerTaskHandbackV1, streak: InterruptionStreak
) -> None:
    """Stop retrying the interrupted work and give the TeamLead an investigation."""

    reason = (
        f"The work was interrupted {streak.count} times in a row because the worker "
        "running it lost its launch lease. The runtime stopped retrying it and asked "
        "the TeamLead to investigate."
    )
    if streak.scope == "schedule":
        records = agent._schedule_store.worker_records
        await agent.task_manager.operations_runtime.run(
            partial(records.supersede_run, handback.handed_back.task_id, agent.team_id),
            reason="interruption_limit_reached",
        )
    else:
        await _cancel_task(agent, handback, reason)
    await _open_investigation(agent, handback, streak)
    agent._logger.error(
        "Work interrupted too many times in a row; stopped retrying and opened a "
        "TeamLead investigation. event=worker_interruption_escalated agent_id=%s "
        "scope=%s scope_id=%s interruptions=%s limit=%s task_id=%s",
        agent.id,
        streak.scope,
        streak.scope_id,
        streak.count,
        streak.limit,
        handback.handed_back.task_id,
    )


async def _cancel_task(agent: Any, handback: WorkerTaskHandbackV1, reason: str) -> None:
    handed_back = handback.handed_back
    task_id = handed_back.task_id
    manager = agent.task_manager
    parent_id = await manager.operations_runtime.run(manager.get_parent_task_id, task_id)
    subtask = await manager.operations_runtime.run(manager.get_task_info, task_id)
    if parent_id is not None and subtask is not None:
        # The lead's own delegation: fail it so the parent turn resumes.
        await agent._fail_subtask_and_reconcile_parent(
            parent_task_id=parent_id,
            subtask_info=subtask,
            error_message=reason,
            resume_parent=True,
        )
        return
    now = datetime.now(timezone.utc).isoformat()
    gateway_context = getattr(handed_back.meta, "gateway_context", None)
    # The cancellation record stops a redelivered copy from running again.
    await manager.operations_runtime.run(
        manager.record_execution_graph_cancelled,
        root_task_id=task_id,
        task_ids=[task_id],
        canonical_user_id=getattr(gateway_context, "canonical_user_id", None),
        command_id=f"interruption_limit:{handback.handback_id}",
        reason=reason,
        cancelled_at=now,
    )
    await send_task_cancel_response(
        agent, handed_back.task, handed_back.header, handed_back.meta, reason
    )


async def _open_investigation(
    agent: Any, handback: WorkerTaskHandbackV1, streak: InterruptionStreak
) -> None:
    handed_back = handback.handed_back
    evidence = _evidence(agent, handback, streak)
    review_id = INTERRUPTION_REVIEW_TASK_PREFIX + hashlib.sha256(
        f"{streak.scope}\x1f{streak.scope_id}\x1f{handback.handback_id}".encode("utf-8")
    ).hexdigest()[:32]
    task = Task(
        task_id=review_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=_instructions(streak) + "\n\n" + json.dumps(evidence, indent=2, default=str),
    )
    gateway_context = getattr(handed_back.meta, "gateway_context", None)
    meta = MessageMeta(
        from_agent_name=getattr(agent, "name", None),
        requester_type=RequesterType.AGENT_REQUESTER,
        is_response_requested=False,
        # Same owner and channel for notify_user, but not the user's conversation
        # (the evidence is internal) and not the schedule's job id (the review
        # must not look like a run of that schedule to the overlap guard).
        gateway_context=(
            gateway_context.model_copy(update={"scheduled_job_id": None}, deep=True)
            if gateway_context
            else None
        ),
        manager_escalation=ManagerEscalationContext(
            escalation_type="manager_review",
            source_task_id=handed_back.task_id,
            source_schedule_id=streak.scope_id if streak.scope == "schedule" else None,
            source_agent_id=handback.worker_agent_id,
            source_named_agent_type=handback.worker_named_agent_type,
            original_requester=(
                "worker_schedule" if streak.scope == "schedule" else "worker_task"
            ),
        ),
    )
    header = MessageHeader(
        from_agent_id=agent.id, to_routing_path=agent.id, agent_signature=""
    )
    # Admitted directly: a lead-authored message on its own route would be
    # dropped as a self-message by the NATS intake.
    await agent._handle_task_request(task_request=task, msg_header=header, msg_meta=meta)


def _instructions(streak: InterruptionStreak) -> str:
    subject = (
        f"scheduled job {streak.scope_id}" if streak.scope == "schedule" else f"task {streak.scope_id}"
    )
    stopped = (
        "The interrupted run was closed and will not be rerun; the schedule itself is unchanged."
        if streak.scope == "schedule"
        else "The task was cancelled and its requester was told."
    )
    return (
        f"Worker interruption review for {subject}. The workers running it lost their "
        f"launch lease {streak.count} times in a row (limit {streak.limit}), usually "
        "because a worker could not renew its lease for the whole lease period "
        f"(for example a storage outage or a stalled worker). {stopped}\n\n"
        "Investigate using the evidence below: check the recent scheduler errors, the "
        "workers' launch records, and inspect_scheduled_worker for a scheduled job. "
        "Then tell the owner with notify_user what happened, the likely cause, and "
        "what you recommend. For a scheduled job you may pause it with the guarded "
        "schedule pause tool if you judge that further runs would fail the same way; "
        "if you pause it, say so in the notice. Do not rerun the work yourself."
    )


def _evidence(
    agent: Any, handback: WorkerTaskHandbackV1, streak: InterruptionStreak
) -> dict[str, Any]:
    occurrence = streak.occurrence
    return {
        "kind": "worker_interruption_review",
        "scope": streak.scope,
        "scope_id": streak.scope_id,
        "interruptions_in_a_row": streak.count,
        "limit": streak.limit,
        "worker_agent_type": handback.worker_named_agent_type,
        "latest_task_id": handback.handed_back.task_id,
        "schedule_run": (
            {"due_at": occurrence.due_at, "occurrence_id": occurrence.occurrence_id}
            if occurrence is not None
            else None
        ),
        "interruptions": [
            _interruption_entry(item) for item in streak.handbacks[-_MAX_EVIDENCE_ENTRIES:]
        ],
    }


def _interruption_entry(item: WorkerTaskHandbackV1) -> dict[str, Any]:
    evidence = item.evidence
    return {
        "task_id": item.handed_back.task_id,
        "worker_agent_id": item.worker_agent_id,
        "launch_id": evidence.launch_id,
        "lease_expires_at_utc": evidence.lease_expires_at_utc,
        "lease_lost_at_utc": evidence.lease_lost_at_utc,
        "reason": evidence.reason,
        "recent_scheduler_errors": list(evidence.recent_scheduler_errors),
        "actions_already_sent": [
            f"{note.action_type} ({note.status}): {note.summary}".rstrip(": ")
            for note in item.handed_back.dispatched_actions
        ],
    }


def _due(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def interruption_limit(agent: Any) -> int:
    from custom_agents.agent_instantiation_service import current_worker_limits

    return int(current_worker_limits(agent).max_consecutive_interruptions)

