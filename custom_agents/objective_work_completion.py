"""Close a checkpointed session using durable evidence and notification policy."""

from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.schedule_observations import record_run_observation
from custom_agents.schedule_store import (
    OwnerGuard,
    ScheduleStore,
    ScheduleWriteConflict,
    owner_canonical_user_id_for_schedule,
)
from custom_agents.structured_llm_messages import (
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, SystemScheduleKind
from custom_messages.records.objectives import (
    ObjectiveWorkCheckpoint,
    ObjectiveWorkSession,
)


class ObjectiveWorkCompletionError(RuntimeError):
    """A saved session cannot be finalized safely; preserve its durable work."""

    def __init__(
        self,
        reason: str,
        detail: str,
        session: ObjectiveWorkSession,
        schedule_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.session_id = session.session_id
        self.objective_id = session.objective_id
        self.schedule_id = schedule_id
        super().__init__(
            f"This Objective work session could not be finalized: {detail} "
            "Saved checkpoints were kept. "
            "Review the Objective's work policy and recurring schedule before resuming it."
        )


def _owned_session_schedule(
    agent: Any,
    task: Task,
    session: ObjectiveWorkSession,
) -> ScheduledMessage:
    # The policy owns the recurring schedule link. Gateway job identity is
    # delivery metadata and may be absent or inherited from the enabling task.
    policy = agent.task_manager.objective_work_repository.get_policy(
        session.objective_id,
        owner_agent_id=session.owner_agent_id,
    )
    if policy is None or policy.policy_id != session.policy_id:
        raise ObjectiveWorkCompletionError(
            "policy_mismatch",
            "the saved work policy no longer matches this session.",
            session,
        )
    schedule_id = policy.schedule_id
    store: ScheduleStore | None = agent._schedule_store
    schedule = (
        store.get_scheduled_message(schedule_id) if store and schedule_id else None
    )
    if schedule is None:
        raise ObjectiveWorkCompletionError(
            "schedule_missing",
            "the linked recurring schedule is unavailable.",
            session,
            schedule_id,
        )
    # The person the session's task verifiably works for, recorded at its
    # admission (TaskInfo.owner_person_id); never the person its message names.
    task_info = agent.task_manager.get_task_info(task.task_id)
    owner = task_info.owner_person_id if task_info is not None else None
    checks = (
        (
            schedule.objective_id == session.objective_id,
            "objective_mismatch",
            "the recurring schedule belongs to a different Objective.",
        ),
        (
            schedule.effective_system_schedule_kind
            == SystemScheduleKind.OBJECTIVE_WORK_SESSION,
            "schedule_kind_mismatch",
            "the linked schedule is not an Objective work schedule.",
        ),
        (
            schedule.claim_team_id == agent.team_id,
            "team_mismatch",
            "the recurring schedule belongs to a different team.",
        ),
        (
            bool(owner) and owner_canonical_user_id_for_schedule(schedule) == owner,
            "owner_mismatch",
            "the recurring schedule's owner does not match this task.",
        ),
        (
            not policy.owner_canonical_user_id
            or policy.owner_canonical_user_id == owner,
            "policy_owner_mismatch",
            "the saved work policy belongs to a different owner.",
        ),
    )
    for valid, reason, detail in checks:
        if not valid:
            raise ObjectiveWorkCompletionError(reason, detail, session, schedule_id)
    return schedule


def _count_checkpoint(memory: ScheduleTaskMemory, checkpoint: ObjectiveWorkCheckpoint, answer: str) -> bool:
    """Record one checkpoint in the run notes once (a repeat returns its first answer); whether to notify."""

    previous = next(
        (
            run
            for run in memory.ledger.recent_runs
            if run.get("evidence", {}).get("checkpoint_id") == checkpoint.checkpoint_id
        ),
        None,
    )
    if previous is not None:
        return bool(previous["should_notify"])
    changed = bool(checkpoint.evidence_delta or checkpoint.completed_subgoals)
    outcome = "changed" if changed else "no_change"
    if checkpoint.degraded or checkpoint.blockers:
        outcome = "inconclusive"
    now = datetime.now(timezone.utc).isoformat()
    should_notify = record_run_observation(
        memory,
        outcome=outcome,
        state_changed=changed,
        summary=answer,
        evidence={
            "checkpoint_id": checkpoint.checkpoint_id,
            "session_id": checkpoint.session_id,
        },
        error="; ".join(checkpoint.blockers),
        at=now,
    )
    memory.state.summary = answer
    memory.state.last_observed_at = now
    if changed:
        memory.state.last_changed_at = now
    return should_notify


def _record_checkpoint_outcome(
    store: ScheduleStore,
    schedule: ScheduledMessage,
    checkpoint: ObjectiveWorkCheckpoint,
    answer: str,
    session: ObjectiveWorkSession,
) -> bool:
    """Count one checkpoint once in the schedule's run notes; whether to notify.

    Only the run notes change (``ScheduleStore.edit_schedule_memory``), so the
    schedule's due time, count, controls and claim stay as they are now.
    """

    record = partial(_count_checkpoint, checkpoint=checkpoint, answer=answer)
    try:
        should_notify = store.edit_schedule_memory(
            schedule.schedule_id,
            record,
            owner_guard=OwnerGuard(owner_canonical_user_id_for_schedule(schedule)),
        )
    except ScheduleWriteConflict as exc:
        raise ObjectiveWorkCompletionError(
            "owner_mismatch",
            "the recurring schedule changed hands while the session finished.",
            session,
            schedule.schedule_id,
        ) from exc
    if should_notify is None:
        raise ObjectiveWorkCompletionError(
            "schedule_missing",
            "the linked recurring schedule was removed while the session finished.",
            session,
            schedule.schedule_id,
        )
    return bool(should_notify)


def prepare_objective_work_completion(
    agent: Any,
    task: Task,
    meta: MessageMeta,
    session: ObjectiveWorkSession,
) -> StructuredAgentTurnDecision:
    """Run in the serialized task lane; the action outbox owns final dispatch."""
    checkpoints = agent.task_manager.objective_work_repository.list_checkpoints(
        session.objective_id,
        owner_agent_id=session.owner_agent_id,
        limit=500,
    )
    checkpoint = next(
        (item for item in checkpoints if item.checkpoint_id == session.checkpoint_id),
        None,
    )
    if checkpoint is None or checkpoint.session_id != session.session_id:
        raise ObjectiveWorkCompletionError(
            "checkpoint_mismatch",
            "the session's saved checkpoint could not be verified.",
            session,
        )
    answer = (
        f"This Objective work session finished with checkpoint {checkpoint.checkpoint_id}. "
        f"{checkpoint.observe} Next: {checkpoint.decide}"
    )
    if checkpoint.blockers:
        answer += " Blocked by: " + "; ".join(checkpoint.blockers)
    suppress = False
    if meta.requester_type == RequesterType.TIMER_REQUESTER:
        schedule = _owned_session_schedule(agent, task, session)
        suppress = not _record_checkpoint_outcome(
            agent._schedule_store, schedule, checkpoint, answer, session
        )
    return StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_id=task.task_id,
                    task_type=MessageMetaType.RESPONSE,
                    answer=answer,
                ),
                suppress_outbound_dispatch=suppress,
            )
        ]
    )
