"""Owner-scoped diagnostics for leads and workers holding a scheduled task."""

from custom_tools.runtime_context import bound_task_id
from datetime import datetime, timedelta
from typing import Any

from custom_agents.agent_instantiation_service import (
    AgentInstantiationService,
    current_worker_limits,
    effective_worker_rules,
)
from custom_agents.memory_review_runtime import is_memory_review_schedule_name
from custom_agents.schedule_definition_identity import definition_changes
from custom_agents.schedule_repair_guidance import (
    AGENT_DEFINITION_CHANGED,
    RESAVE_REPAIR_REASONS,
    repair_instructions,
)
from custom_messages.contracts.runtime_v1 import (
    MEM_REVIEW_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import occurrence_identity
from custom_messages.enums import ScheduleManagementClass
from runtime_settings.schedule_workers import SCHEDULE_WORKER_ELIGIBILITY_PATH


def _eligibility_diagnostics(agent: Any, agent_type: str | None) -> dict[str, Any]:
    """Expose the exact repair even when no binding could be installed yet."""
    limits = current_worker_limits(agent)
    runtime = getattr(agent, "_local_team_runtime", None)
    registered = bool(runtime and agent_type and runtime.resolve_definition(agent_type))
    denied = agent_type in limits.denied_agent_types
    manager = agent_type == TEAM_ARCHITECT_AGENT_TYPE
    listed = agent_type in limits.eligible_agent_types
    from custom_agents.agent_instantiation_service import default_type_allowed

    default_allowed = bool(runtime and agent_type and registered and default_type_allowed(agent, agent_type, runtime.resolve_definition(agent_type)))
    steps = []
    change = None
    if denied:
        steps.append(
            "The operator explicitly denied this type. Retarget to a suitable "
            "allowed specialist or ask the operator to review the denial; "
            "do not remove or bypass it."
        )
    elif manager:
        steps.append(
            "Select an ordinary specialist; automatic TeamArchitect creation is forbidden."
        )
    elif not registered:
        steps.append(
            "Select an exact registered specialist from get_available_agent_types. "
            "A specialist created with specialist_definitions(action='plan') is registered as "
            "soon as its change is applied, with no restart; if it is missing, check "
            "specialist_definitions(action='status')."
        )
    elif not listed and not default_allowed:
        change = {
            "path": SCHEDULE_WORKER_ELIGIBILITY_PATH,
            "value": [*limits.eligible_agent_types, agent_type],
            "target_scope": "team",
        }
        steps.append(
            "As TeamLead, pass suggested_settings_change to plan_runtime_setting_change, "
            "then apply_change with its change_id. Preserve the other "
            "candidates. This does not create a schedule policy or grant access."
        )
    if not limits.enabled:
        steps.append(
            "Automatic startup is disabled by the operator. Request operator review of "
            "agent.schedule_workers.enabled; adding eligibility cannot override it."
        )
    if not agent.is_team_lead:
        change = None
        if steps:
            steps = ["Ask TeamLead to inspect and repair this schedule."]
    return {
        "agent_type": agent_type,
        "registered": registered,
        "listed": listed,
        "allowed_by_default_policy": default_allowed,
        "explicitly_denied": denied,
        "automatic_launch_enabled": limits.enabled,
        "eligible": registered
        and (listed or default_allowed)
        and not denied
        and not manager
        and limits.enabled,
        "eligible_agent_types": limits.eligible_agent_types,
        "suggested_settings_change": change,
        "next_steps": steps,
    }


# Prompt text shown in a definition diff is capped so one large prompt cannot
# crowd out the rest of the inspection; the cap is stated in the output.
_PROMPT_DIFF_LIMIT = 6000


def _bounded_prompt(text: str | None) -> str | None:
    if text is None or len(text) <= _PROMPT_DIFF_LIMIT:
        return text
    hidden = len(text) - _PROMPT_DIFF_LIMIT
    return f"{text[:_PROMPT_DIFF_LIMIT]}\n[{hidden} more characters not shown]"


def _definition_inspection(agent: Any, binding: Any) -> dict[str, Any]:
    """Compare the saved target definition with the current one for review."""
    runtime = getattr(agent, "_local_team_runtime", None)
    definition = runtime.resolve_definition(binding.target_agent_type) if runtime else None
    if definition is None:
        return {"status": "unavailable"}
    record = agent._schedule_store.worker_records.get_definition_record(
        binding.schedule_id, binding.team_id
    )
    changes = definition_changes(binding, record, definition)
    rendered = changes.model_dump(mode="json")
    rendered["own_prompt_before"] = _bounded_prompt(changes.own_prompt_before)
    rendered["own_prompt_after"] = _bounded_prompt(changes.own_prompt_after)
    return {
        "status": "changed" if changes.requires_review else "current",
        "source": changes.source_after,
        "source_path": definition.get("agent_definition_source_path"),
        "recorded_at_utc": record.recorded_at_utc.isoformat() if record else None,
        "last_release_change": record.last_release_change.model_dump(mode="json")
        if record and record.last_release_change
        else None,
        "changes": rendered,
    }


def _repair_plan(
    agent: Any, binding: Any, definition: dict[str, Any], reason: str | None
) -> tuple[str, dict[str, Any] | None]:
    """Instructions for the current blocker plus a ready re-save when it applies."""
    blocker = binding.blocked_reason or reason
    if definition.get("status") == "changed":
        blocker = AGENT_DEFINITION_CHANGED
    if blocker not in RESAVE_REPAIR_REASONS:
        return repair_instructions(blocker), None
    if not agent.is_team_lead:
        return "Ask TeamLead to inspect and repair this schedule.", None
    return repair_instructions(blocker), {
        "tool": "configure_scheduled_worker",
        "schedule_id": binding.schedule_id,
        "target_agent_type": binding.target_agent_type,
        "expected_revision": binding.revision,
        "worker_policy_json": "",
    }


def _protected_schedule_inspection(schedule: ScheduledMessage) -> dict[str, Any]:
    """Describe the owning dispatcher, without claiming its workers are healthy.

    System timers do not use ordinary scheduled-worker bindings. Computing their
    manager's specialist eligibility would prescribe a mutation configure rejects.
    """
    memory_review = (
        schedule.management_class == ScheduleManagementClass.SYSTEM
        and is_memory_review_schedule_name(schedule.schedule_name)
    )
    execution: dict[str, Any] = {
        "mode": "system_schedule"
        if schedule.management_class == ScheduleManagementClass.SYSTEM
        else "task_owned_schedule",
        "dispatcher_agent_type": schedule.claim_named_agent_type,
        "health_verified": False,
    }
    instructions = (
        "This timer uses its owning system or task workflow. Do not retarget it "
        "with configure_scheduled_worker or create a duplicate user-owned timer. "
        "Inspect that workflow's configuration, dispatch and task results."
    )
    if memory_review:
        execution.update(
            {
                "mode": "memory_review_dispatch",
                "worker_agent_type": MEM_REVIEW_AGENT_TYPE,
                "worker_start": "upgrade_required",
                "transport": "local_database",
            }
        )
        instructions = (
            "This memory-review timer needs the explicit local schedule upgrade. "
            "Have the operator drain review work, stop the team runtimes and run "
            "scripts/upgrade_local_schedules.py. Do not duplicate the timer or "
            "retarget it through an agent tool. The upgrade preserves its ID and timing."
        )
    return {
        "schedule_id": schedule.schedule_id,
        "management_class": schedule.management_class.value,
        "worker_configuration_supported": False,
        "worker_configuration_reason": "automatic_workers_require_user_owned_root_schedule",
        "target_agent_type": schedule.claim_named_agent_type,
        "specific_agent_id": schedule.claim_agent_id,
        "schedule_enabled": schedule.enabled,
        "automation_enabled": False,
        "policy_revision": 0,
        "eligibility": None,
        "execution": execution,
        "repair_instructions": instructions,
    }


def inspect_schedule_worker(agent: Any, schedule_id: str = "") -> dict[str, Any]:
    from custom_agents.schedule_worker_service import ScheduleWorkerService

    from access_control.ownership import PRIVATE_READ
    from custom_agents.access_runtime import verified_person_id

    records = agent._schedule_store.worker_records
    person_id = verified_person_id(agent)
    if agent.is_team_lead:
        schedule = ScheduleWorkerService(agent).owned_schedule(schedule_id, action=PRIVATE_READ)
    else:
        task_id = bound_task_id()
        occurrence = records.for_task(task_id, agent.team_id) if task_id else None
        if (
            occurrence is None
            or occurrence.agent_id != agent.id
            or not person_id
            or occurrence.owner_canonical_user_id != person_id
            or (schedule_id and schedule_id != occurrence.schedule_id)
        ):
            raise PermissionError("No current scheduled task in this worker's custody")
        schedule_id = occurrence.schedule_id
        schedule = agent._schedule_store.get_scheduled_message(schedule_id)
    if schedule is None:
        raise ValueError("Schedule has been removed")
    # Apply the same supported-schedule boundary as configure, after owner checks.
    from custom_agents.local_schedule_policy import supports_local_worker

    if not supports_local_worker(schedule):
        return _protected_schedule_inspection(schedule)
    binding = records.get_binding(
        schedule_id, team_id=agent.team_id, owner_id=person_id
    )
    eligibility = _eligibility_diagnostics(
        agent, binding.target_agent_type if binding else schedule.claim_named_agent_type
    )
    if binding is None:
        return {
            "schedule_id": schedule_id,
            "management_class": schedule.management_class.value,
            "worker_configuration_supported": True,
            "automation_enabled": False,
            "target_agent_type": schedule.claim_named_agent_type,
            "specific_agent_id": schedule.claim_agent_id,
            "policy_revision": 0,
            "schedule_enabled": schedule.enabled,
            "eligibility": eligibility,
            "repair_instructions": (
                "Resolve eligibility.next_steps, preview with prepare_scheduled_job_access, "
                "then save the target and requirements with configure_scheduled_worker "
                "using expected_revision=0 and activate_schedule=false. Give the missing "
                "access with authorize_scheduled_job_access for the returned revision, "
                "recheck readiness and activate. "
                "Exact agent-ID targets require explicit conversion. Verify admission "
                "and completion; a registered or manually started worker does not "
                "enable future automatic starts."
            ),
        }
    launcher = AgentInstantiationService(agent)
    reason = None
    readiness = None
    inherited = None
    try:
        launcher.validate_type(binding.target_agent_type, automatic=True)
        rules = effective_worker_rules(
            agent, binding.target_agent_type, binding.policy.rules
        )
        inherited = effective_worker_rules(agent, binding.target_agent_type, {})
        from custom_agents.schedule_readiness import (
            effective_requirements,
            prepare_schedule_access,
        )

        preview = schedule.model_copy(
            update={
                "claim_named_agent_type": binding.target_agent_type,
                "claim_agent_id": None,
            }
        )
        from custom_agents.schedule_access import ScheduleAccessCheck

        readiness = prepare_schedule_access(
            agent,
            preview,
            effective_requirements(binding),
            binding=binding,
            # TeamLead inspects for the owner, so it also sees missing access;
            # a worker in a run has its calls decided one by one instead.
            access=ScheduleAccessCheck.for_agent(
                agent, worker_type=binding.target_agent_type, schedule_id=binding.schedule_id
            )
            if agent.is_team_lead
            else None,
        )
    except (ValueError, PermissionError) as exc:
        rules, reason = None, str(exc)
    definition = _definition_inspection(agent, binding)
    instructions, suggested_refresh = _repair_plan(agent, binding, definition, reason)
    occurrence_id = (
        occurrence_identity(schedule_id, schedule.next_fire_at)
        if schedule.next_fire_at
        else None
    )
    attempt = records.attempt(occurrence_id, agent.team_id) if occurrence_id else None
    launch = (
        launcher.repository.get(attempt["launch_id"], agent.team_id)
        if attempt and attempt.get("launch_id")
        else None
    )
    latest = records.latest_occurrence(schedule_id, agent.team_id)
    next_action_at = None
    if rules and schedule.next_fire_at:
        next_action_at = datetime.fromisoformat(schedule.next_fire_at) + timedelta(
            seconds=rules.claim_wait_seconds
        )
        if launch:
            next_action_at = (
                launch.ready_at_utc + timedelta(seconds=rules.admission_timeout_seconds)
                if launch.ready_at_utc
                else launch.created_at_utc
                + timedelta(seconds=rules.startup_timeout_seconds)
            )
    return {
        "schedule_id": schedule_id,
        "management_class": schedule.management_class.value,
        "worker_configuration_supported": True,
        "target_agent_type": schedule.claim_named_agent_type,
        "desired_agent_type": binding.target_agent_type,
        "policy_revision": binding.revision,
        "policy": binding.policy.model_dump(mode="json"),
        "automation_enabled": binding.policy.enabled,
        "schedule_enabled": schedule.enabled,
        "authority_mode": binding.authority_mode,
        "eligibility": eligibility,
        "eligibility_reason": reason,
        "effective_rules": rules.model_dump(mode="json") if rules else None,
        "requested_rules": binding.policy.rules,
        "inherited_rules": inherited.model_dump(mode="json") if inherited else None,
        "operator_limits": current_worker_limits(agent).model_dump(mode="json"),
        "readiness": readiness.model_dump(mode="json") if readiness else None,
        "requirements": binding.capabilities.model_dump(mode="json"),
        "observed_requirements": [
            item.model_dump(mode="json") for item in binding.observed_capabilities
        ],
        "block_reason": binding.blocked_reason,
        "next_action_at_utc": next_action_at.isoformat() if next_action_at else None,
        "launch_attempt": attempt,
        "launch": launch.model_dump(mode="json") if launch else None,
        "last_occurrence": latest.model_dump(mode="json") if latest else None,
        "spawn_to_ready_seconds": (
            launch.ready_at_utc - launch.created_at_utc
        ).total_seconds()
        if launch and launch.ready_at_utc
        else None,
        "due_to_admission_seconds": (
            latest.admitted_at_utc - datetime.fromisoformat(latest.due_at)
        ).total_seconds()
        if latest and latest.admitted_at_utc
        else None,
        "blocked_since_utc": binding.blocked_since_utc.isoformat()
        if binding.blocked_since_utc
        else None,
        "definition": definition,
        "suggested_refresh": suggested_refresh,
        "repair_instructions": instructions,
    }
