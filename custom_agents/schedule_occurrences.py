"""Derive execution identity from durable schedules and verified runtime custody."""

from __future__ import annotations

from typing import Any

from custom_agents.schedule_execution_identity import execution_digest

from custom_agents.schedule_store import owner_canonical_user_id_for_schedule

from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleExecutionScopeV1,
    ScheduleOccurrenceV1,
    occurrence_identity,
)


def prepare_occurrence(
    agent: Any, schedule: Any, claim_token: str
) -> ScheduleOccurrenceV1 | None:
    # Every typed root task needs a durable receipt, including runtime-managed
    # root timers that are not eligible for automatic local worker startup.
    # Child timers keep their existing correlation. Specialized launchers use
    # their own admission path rather than admitting the root payload.
    if schedule.payload.parent_task_id or not schedule.claim_named_agent_type:
        return None
    occurrence_id = occurrence_identity(schedule.schedule_id, schedule.next_fire_at)
    binding = agent._schedule_store.worker_records.get_binding(
        schedule.schedule_id, team_id=agent.team_id, active=True
    )
    definition = binding.definition_digest if binding else ""
    return ScheduleOccurrenceV1(
        occurrence_id=occurrence_id,
        schedule_id=schedule.schedule_id,
        due_at=schedule.next_fire_at,
        task_id="task_" + occurrence_id,
        team_id=agent.team_id,
        owner_canonical_user_id=owner_canonical_user_id_for_schedule(schedule) or "",
        named_agent_type=agent.named_agent_type,
        execution_digest=execution_digest(schedule, definition),
        agent_id=agent.id,
        claim_token=claim_token,
        policy_revision=binding.revision if binding else None,
        launch_id=getattr(getattr(agent, "_worker_launch", None), "launch_id", None),
    )


def trusted_execution_scope(
    agent: Any, task_id: str | None
) -> ScheduleExecutionScopeV1 | None:
    """Never infer schedule authority from model-authored message metadata."""
    if not task_id:
        return None
    store = getattr(agent, "_schedule_store", None)
    if store is None:
        return None
    record = store.worker_records.for_task(task_id, agent.team_id)
    if (
        record is None
        or record.agent_id != agent.id
        or record.named_agent_type != agent.named_agent_type
    ):
        return None
    schedule = store.get_scheduled_message(record.schedule_id)
    binding = store.worker_records.get_binding(
        record.schedule_id, team_id=agent.team_id, active=True
    )
    if (
        schedule is None
        or binding is None
        or record.execution_digest
        != execution_digest(schedule, binding.definition_digest)
    ):
        return None
    from custom_agents.agent_instantiation_service import AgentInstantiationService
    from custom_agents.schedule_definition_identity import definition_is_current

    definition, _ = AgentInstantiationService(agent).validate_type(
        record.named_agent_type, automatic=False
    )
    if not definition_is_current(binding, definition):
        return None
    return ScheduleExecutionScopeV1(
        schedule_id=record.schedule_id, execution_digest=record.execution_digest
    )


def invocation_schedule_context(agent: Any, task_id: str) -> dict[str, Any]:
    scope = trusted_execution_scope(agent, task_id)
    return {"schedule_execution": scope.model_dump(mode="json")} if scope else {}
