"""Trusted default local startup policy and narrowly recognized system timers.

The default policy is a standing permission to start the specialist types that
ship with the code, not to invoke their tools. Shipped types change only with a
deploy, which is the operator's approval, so the rule is evaluated against the
current catalog instead of a saved snapshot. Team-written types need an explicit
eligible_agent_types entry and lead-configured policy.

The only system timers an on-demand local worker runs are the kinds the system
schedule table marks ``local_worker`` (``custom_messages.system_schedules``,
today memory review): ``local_system_timer_spec`` finds their row.
"""

from typing import Any

from custom_agents.agent_instantiation_service import default_type_allowed
from custom_agents.schedule_definition_identity import (
    authored_definition_digest,
    build_definition_record,
)
from custom_agents.schedule_execution_identity import execution_digest
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    build_schedule_mutation_permit,
    schedule_control_state_digest,
)
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
)
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import (
    SystemScheduleKindSpec,
    system_schedule_spec,
    system_schedule_spec_for_name,
)

# Who saved a default startup policy: the team's trusted local schedule
# runtime, never an agent or a person.
LOCAL_SCHEDULE_RUNTIME = "local_schedule_runtime"


def local_schedule_service_id(team_id: str) -> str:
    return f"{LOCAL_SCHEDULE_RUNTIME}:{team_id}"


def local_system_timer_spec(schedule: Any) -> SystemScheduleKindSpec | None:
    """The table row of a system root timer that an on-demand local worker runs; ``None`` for any other schedule.

    Recognized by name, as before kinds existed, so an unclassified timer of a
    ``local_worker`` kind still counts. A stored kind must belong to the same
    agent type as the name's row. The timer may still be claimed by another
    agent type (an old memory timer of TeamArchitect); ``supports_local_worker``
    checks the claim.
    """

    if schedule.management_class != ScheduleManagementClass.SYSTEM or schedule.payload.parent_task_id:
        return None
    spec = system_schedule_spec_for_name(schedule.schedule_name)
    if spec is None or not spec.local_worker:
        return None
    if schedule.system_schedule_kind in (None, SystemScheduleKind.UNCLASSIFIED):
        return spec
    stored = system_schedule_spec(schedule.system_schedule_kind)
    if stored is None or not stored.local_worker or stored.agent_type != spec.agent_type:
        return None
    return spec


def is_memory_review_timer(schedule: Any) -> bool:
    """A local system timer of MemReviewAgent; the one-time upgrade moves old ones off TeamArchitect."""

    spec = local_system_timer_spec(schedule)
    return spec is not None and spec.agent_type == MEM_REVIEW_AGENT_TYPE


def supports_local_worker(schedule: Any) -> bool:
    if schedule.payload.parent_task_id:
        return False
    if schedule.management_class == ScheduleManagementClass.USER:
        return True
    spec = local_system_timer_spec(schedule)
    return spec is not None and schedule.claim_named_agent_type == spec.agent_type


def install_schedule_default(
    agent: Any,
    schedule: Any,
    *,
    require_unclaimed: bool = False,
    previous_schedule: Any = None,
) -> bool:
    records = agent._schedule_store.worker_records
    if not supports_local_worker(schedule) or schedule.claim_agent_id:
        return False
    if records.get_binding(schedule.schedule_id, team_id=agent.team_id) is not None:
        return False
    owner = owner_canonical_user_id_for_schedule(schedule)
    definition = agent._local_team_runtime.resolve_definition(
        schedule.claim_named_agent_type
    )
    if not owner or schedule.claim_team_id != agent.team_id or definition is None:
        return False
    if not default_type_allowed(agent, schedule.claim_named_agent_type, definition):
        return False
    digest = authored_definition_digest(definition)
    binding = ScheduleWorkerBindingV1(
        schedule_id=schedule.schedule_id,
        team_id=agent.team_id,
        owner_canonical_user_id=owner,
        target_agent_type=schedule.claim_named_agent_type,
        definition_digest=digest,
        execution_digest=execution_digest(schedule, digest),
        revision=1,
        policy=ScheduleWorkerPolicySpecV1(),
        actor_principal_id=local_schedule_service_id(agent.team_id),
        authority_mode="runtime_default",
    )
    previous = previous_schedule or schedule
    permit = build_schedule_mutation_permit(
        schedule=previous,
        operation="update",
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id=binding.actor_principal_id,
        owner_canonical_user_id=owner,
        team_id=agent.team_id,
        candidate=schedule,
    )
    records.configure(
        binding,
        schedule,
        expected_revision=0,
        expected_control_digest=schedule_control_state_digest(previous),
        system_mutation_permit=permit,
        require_unclaimed=require_unclaimed,
        definition_record=build_definition_record(
            definition,
            schedule_id=schedule.schedule_id,
            team_id=agent.team_id,
            policy_revision=binding.revision,
        ),
    )
    agent._logger.info(
        "Configured default local schedule startup. event=local_schedule_default_configured schedule_id=%s team_id=%s agent_type=%s",
        schedule.schedule_id,
        agent.team_id,
        schedule.claim_named_agent_type,
    )
    return True
