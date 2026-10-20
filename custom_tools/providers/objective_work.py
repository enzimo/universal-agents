"""Bounded Objective work sessions and their execution profiles (catalog tool ``fn:objectives``).

``objective_work_sessions`` and ``objective_work_profiles`` take an action
to group related operations within providers' tool-count limits. Each call
validates its action's inputs and required level; grants cover the whole
tool. ``propose_objective_work_profile`` stays its own function because it
has many inputs. This module holds the sessions' schedule
installation and rollback too; ``custom_tools.providers.objectives`` lists
these tools in the objectives provider.
"""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

from strands import tool

from access_control.ownership import PRIVATE_READ, PRIVATE_UPDATE
from custom_agents.schedule_management import (
    ScheduleMutationOperation,
    agent_tool_authority_mode,
    build_schedule_mutation_permit,
    is_system_managed_schedule,
)
from custom_agents.schedule_store import (
    schedule_claim_scope_for_agent,
    schedule_record_digest,
    with_cron_start,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ObjectiveWorkPolicyStatus,
    ObjectiveWorkProfileProposalStatus,
    ObjectiveWorkProfileScope,
    ObjectiveWorkProfileSource,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.records.objectives import (
    OWNER_CADENCE_TIMEZONE,
    ObjectiveWorkBudget,
    ObjectiveWorkProfile,
    ObjectiveWorkProfileProposal,
)
from custom_tools.action_inputs import actions, input_refusal
from custom_tools.tool_results import failed
from custom_agents.change_followups import after_saved_change
from custom_tools.runtime_context import (
    get_agent_ref,
)
from id_generation import (
    new_id,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


from custom_tools.providers._objective_profile_export import export_profile
from custom_tools.providers._objective_work_skills import missing_objective_work_skills
from custom_agents.pending_work_runtime import wake_scheduler
from custom_tools.providers._runtime import (
    _get_current_task_info,
    _require_manager_tool,
    _resolve_objective_id_for_tool,
    _schedule_store,
)
from custom_tools.providers._user_scope import _private_user_scope
from custom_tools.providers.diagnostics import (
    _coerce_tool_bool,
)

logger = logging.getLogger(__name__)

# The reply when the Objective is unknown or out of this task's view
# (``ownership.objective_access``); the same words as the ``objectives`` tool.
_NO_OBJECTIVE = {"error": "No objective could be resolved from the supplied context."}


def _build_objective_work_schedule(
    agent: "UniversalAgent",
    *,
    current: Any,
    policy: Any,
    schedule_id: str,
) -> ScheduledMessage:
    schedule_meta = current.meta.model_copy(deep=True)
    schedule_meta.requester_type = RequesterType.TIMER_REQUESTER
    schedule_meta.is_response_requested = False
    schedule_meta.objective_id = policy.objective_id
    schedule_meta.objective_work_session_id = None
    # The zone and first run are set here, not by the store, so the permit binds
    # the schedule as saved; "owner" is the installing task's request time zone.
    return with_cron_start(
        ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=f"objective_work_session:{policy.objective_id}",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
            objective_id=policy.objective_id,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=(
                    "Resume this Objective for one bounded work session. Load the durable Objective, current Plan, "
                    "latest checkpoint, and recent evidence. Revalidate authority and in-flight effects; activate "
                    "long-horizon-plan-execution; advance the highest-value admissible subgoal; then checkpoint."
                ),
            ),
            payload_header=current.header.model_copy(deep=True),
            payload_meta=schedule_meta,
            cron_expression=policy.cadence_cron,
            cron_timezone=None if policy.cadence_timezone == OWNER_CADENCE_TIMEZONE else policy.cadence_timezone,
            repeat_count=None,
            **schedule_claim_scope_for_agent(agent),
        ),
        getattr(agent, "instance_timezone", None),
    )


def _require_matching_objective_work_schedule(
    existing_schedule: Any,
    *,
    objective_id: str,
) -> None:
    if existing_schedule is None:
        return
    if (
        not is_system_managed_schedule(existing_schedule)
        or existing_schedule.system_schedule_kind
        != SystemScheduleKind.OBJECTIVE_WORK_SESSION
        or existing_schedule.objective_id != objective_id
    ):
        raise PermissionError(
            "The existing schedule id is not the protected schedule for this Objective."
        )


def _objective_work_schedule_permit(
    agent: "UniversalAgent",
    *,
    schedule: Any,
    operation: ScheduleMutationOperation,
    owner_canonical_user_id: str,
    candidate: Any = None,
) -> Any:
    # The exact permit for the Objective's own work-session schedule. The
    # access hook already allowed this tool call for the agent and the person
    # its task works for (held access; agreement with authorization off), and
    # the owner is that verified person.
    if schedule is None:
        return None
    return build_schedule_mutation_permit(
        schedule=schedule,
        operation=operation,
        authority_mode=agent_tool_authority_mode(agent),
        actor_principal_id=agent.id,
        owner_canonical_user_id=owner_canonical_user_id,
        team_id=agent.team_id,
        candidate=candidate,
    )


def _rollback_objective_work_schedule(
    agent: "UniversalAgent",
    *,
    store: Any,
    schedule_id: str,
    existing_schedule: Any,
    installed_schedule: ScheduledMessage,
    owner_canonical_user_id: str,
) -> None:
    # Only undo this operation's write. A fresh read could already include
    # somebody else's stop or edit and must never authorize undoing it.
    if existing_schedule is None:
        delete_permit = _objective_work_schedule_permit(
            agent,
            schedule=installed_schedule,
            operation=ScheduleMutationOperation.DELETE,
            owner_canonical_user_id=owner_canonical_user_id,
        )
        if not store.remove(
            schedule_id,
            system_mutation_permit=delete_permit,
            expected_record_digest=schedule_record_digest(installed_schedule),
        ):
            raise RuntimeError(
                "Failed to remove a partially installed Objective work schedule."
            )
        return
    rollback_permit = _objective_work_schedule_permit(
        agent,
        schedule=installed_schedule,
        operation=ScheduleMutationOperation.UPDATE,
        owner_canonical_user_id=owner_canonical_user_id,
        candidate=existing_schedule,
    )
    store.update(
        existing_schedule,
        base=installed_schedule,
        system_mutation_permit=rollback_permit,
        system_mutation_operation=ScheduleMutationOperation.UPDATE,
    )


def _install_objective_work_schedule(
    agent: "UniversalAgent",
    *,
    current: Any,
    policy: Any,
    owner_canonical_user_id: str,
    followup_result: dict[str, Any],
) -> Any:
    """Install or exactly replace one Objective's protected recurring schedule."""

    schedule_id = policy.schedule_id or new_id("objwork_sched")
    schedule = _build_objective_work_schedule(
        agent,
        current=current,
        policy=policy,
        schedule_id=schedule_id,
    )
    store = _schedule_store(agent)
    existing_schedule = store.get_scheduled_message(schedule_id)
    _require_matching_objective_work_schedule(
        existing_schedule,
        objective_id=policy.objective_id,
    )
    if existing_schedule is not None:
        # Reconfiguration changes the definition, not a run's notes. Equal
        # memory in candidate/base makes update keep the latest stored notes.
        schedule.schedule_memory = existing_schedule.schedule_memory
    permit = _objective_work_schedule_permit(
        agent,
        schedule=existing_schedule,
        operation=ScheduleMutationOperation.UPDATE,
        owner_canonical_user_id=owner_canonical_user_id,
        candidate=schedule,
    )
    schedule_persisted = False
    try:
        if existing_schedule is None:
            store.add(schedule, system_mutation_permit=permit)
        else:
            store.update(schedule, base=existing_schedule, system_mutation_permit=permit)
        schedule_persisted = True
        policy = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.set_schedule,
            policy.objective_id,
            schedule_id=schedule_id,
            next_run_at=schedule.next_fire_at,
        )
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.link_schedule_to_objective,
            policy.objective_id,
            schedule_id,
            managed_by_objective=False,
            user_visible=True,
            reason=(
                "Protected recurring Objective work-session schedule installed or updated "
                "after explicit acceptance."
            ),
        )
    except Exception:
        if schedule_persisted:
            try:
                _rollback_objective_work_schedule(
                    agent,
                    store=store,
                    schedule_id=schedule_id,
                    existing_schedule=existing_schedule,
                    installed_schedule=schedule,
                    owner_canonical_user_id=owner_canonical_user_id,
                )
            except Exception as rollback_exc:
                getattr(agent, "_logger", logger).error(
                    "Objective work schedule rollback failed. schedule_id=%s objective_id=%s error=%s",
                    schedule_id,
                    policy.objective_id,
                    rollback_exc,
                )
        raise
    with after_saved_change(followup_result, record_id=schedule_id, step="waking the scheduler", logger=logger):
        wake_scheduler(agent)
    return policy


def _restore_or_disable_objective_work_policy(
    agent: "UniversalAgent",
    *,
    policy: Any,
    previous_policy: Any,
) -> None:
    if previous_policy is not None:
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_repository.save_policy, previous_policy
        )
        return
    policy.status = ObjectiveWorkPolicyStatus.DISABLED
    policy.updated_at = datetime.now(timezone.utc).isoformat()
    agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.objective_work_repository.save_policy, policy
    )


def _apply_reusable_objective_work_profile(
    coordinator: Any,
    *,
    current: Any,
    proposal: Any,
    acceptance_summary: str,
) -> str:
    installed = coordinator.install_reusable_profile(
        proposal,
        accepted_by_task_id=current.task.task_id,
        acceptance_summary=acceptance_summary,
    )
    applied = coordinator.mark_profile_proposal_applied(
        proposal,
        accepted_by_task_id=current.task.task_id,
        acceptance_summary=acceptance_summary,
        installed_profile_id=installed.installed_profile_id,
    )
    return json.dumps(
        {
            "proposal": applied.model_dump(mode="json"),
            "installed_profile": installed.model_dump(mode="json"),
        }
    )


def _suggest_session(
    objective_id: str, estimated_sessions: int = 3
) -> str:
    """Return an inert time-boxing suggestion for a large Objective; never enables it."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_sessions")
    if denial:
        return json.dumps(denial)
    owner, owner_denial = _private_user_scope(
        agent, resource_name="Objective work sessions"
    )
    if owner_denial is not None:
        return json.dumps(owner_denial)
    resolved_objective_id = _resolve_objective_id_for_tool(
        agent, objective_id.strip(), "", "", action=PRIVATE_READ
    )
    if not resolved_objective_id:
        return json.dumps(_NO_OBJECTIVE)
    try:
        return json.dumps(
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.objective_work_coordinator.suggest,
                resolved_objective_id,
                estimated_sessions=max(1, int(estimated_sessions)),
                owner_canonical_user_id=owner,
            )
        )
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


def _enable_sessions(
    objective_id: str,
    confirmation_summary: str,
    profile_name: str = "",
    confirmed: bool = False,
) -> str | dict[str, Any]:
    """Enable recurring bounded execution only after explicit user acceptance."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_sessions")
    if denial:
        return failed({"error": denial})
    if not _coerce_tool_bool(confirmed):
        return failed(
            {
                "error": "Explicit user confirmation is required before enabling bounded execution."
            }
        )
    current = _get_current_task_info(agent)
    if current is None:
        return failed({"error": "No active task context is available."})
    owner, owner_denial = _private_user_scope(
        agent, resource_name="Objective work sessions"
    )
    if owner_denial is not None:
        return failed(owner_denial)
    if current.meta.requester_type != RequesterType.USER_REQUESTER:
        return failed(
            {
                "error": "direct_user_request_required",
                "message": "Enable bounded Objective execution only in the authenticated user request that accepted it.",
            }
        )
    resolved_objective_id = _resolve_objective_id_for_tool(
        agent, objective_id, current.task.task_id, "", action=PRIVATE_UPDATE
    )
    if not resolved_objective_id:
        return failed(_NO_OBJECTIVE)
    policy = None
    previous_policy = None
    try:
        unavailable = missing_objective_work_skills(agent)
        if unavailable:
            return failed(
                {
                    "error": "Required Objective work-session skills are unavailable.",
                    "missing_skills": unavailable,
                }
            )
        previous_policy = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_repository.get_policy,
            resolved_objective_id,
            owner_agent_id=agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.durable_manager_scope_id
            ),
        )
        if previous_policy is not None:
            previous_policy = previous_policy.model_copy(deep=True)
        policy = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.enable_policy,
            resolved_objective_id,
            profile_name=profile_name.strip() or None,
            accepted_by_task_id=current.task.task_id,
            acceptance_summary=confirmation_summary,
            owner_canonical_user_id=owner,
        )
        followup_result: dict[str, Any] = {}
        policy = _install_objective_work_schedule(
            agent,
            current=current,
            policy=policy,
            owner_canonical_user_id=owner,
            followup_result=followup_result,
        )
        return json.dumps({**policy.model_dump(mode="json"), **followup_result})
    except Exception as exc:
        if policy is not None:
            _restore_or_disable_objective_work_policy(
                agent,
                policy=policy,
                previous_policy=previous_policy,
            )
            getattr(agent, "_logger", logger).error(
                "Objective work schedule installation failed; restored or disabled policy %s for Objective %s: %s",
                policy.policy_id,
                resolved_objective_id,
                exc,
            )
        return failed({"error": str(exc)})


def _list_profiles(include_proposals: bool = True) -> str:
    """List operator profiles, this user's installed profiles, proposal history, and bounds."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_profiles")
    if denial:
        return json.dumps(denial)
    owner, owner_denial = _private_user_scope(
        agent,
        resource_name="Objective work profiles",
    )
    if owner_denial is not None:
        return json.dumps(owner_denial)
    try:
        return json.dumps(
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.objective_work_coordinator.profile_catalog,
                owner_canonical_user_id=owner,
                include_proposals=_coerce_tool_bool(include_proposals),
            )
        )
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def propose_objective_work_profile(
    profile_name: str,
    cadence_cron: str,
    active_seconds: int,
    checkpoint_reserve_seconds: int,
    checkpoint_reserve_turns: int,
    checkpoint_reserve_tokens: int,
    checkpoint_reserve_tool_calls: int,
    turns: int,
    total_tokens: int,
    tool_calls: int,
    rationale: str,
    scope: str = "objective",
    objective_id: str = "",
    cadence_timezone: str = OWNER_CADENCE_TIMEZONE,
    replace_existing: bool = False,
) -> str:
    """Stage an inert exact one-time or reusable profile after discussing its bounds."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "propose_objective_work_profile")
    if denial:
        return json.dumps(denial)
    current = _get_current_task_info(agent)
    if current.meta.requester_type != RequesterType.USER_REQUESTER:
        return json.dumps({"error": "direct_user_request_required"})
    owner, owner_denial = _private_user_scope(
        agent,
        resource_name="Objective work profiles",
    )
    if owner_denial is not None:
        return json.dumps(owner_denial)
    try:
        normalized_scope = ObjectiveWorkProfileScope(scope.strip().lower())
        resolved_objective_id = None
        if normalized_scope == ObjectiveWorkProfileScope.OBJECTIVE:
            resolved_objective_id = _resolve_objective_id_for_tool(
                agent,
                objective_id,
                current.task.task_id,
                "",
                action=PRIVATE_UPDATE,
            )
            if not resolved_objective_id:
                return json.dumps(_NO_OBJECTIVE)
        proposal = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.propose_profile,
            owner_canonical_user_id=owner,
            source_task_id=current.task.task_id,
            scope=normalized_scope,
            objective_id=resolved_objective_id,
            profile_name=profile_name,
            profile=ObjectiveWorkProfile(
                cadence_cron=cadence_cron.strip(),
                cadence_timezone=cadence_timezone.strip(),
                budget=ObjectiveWorkBudget(
                    active_seconds=int(active_seconds),
                    checkpoint_reserve_seconds=int(checkpoint_reserve_seconds),
                    checkpoint_reserve_turns=int(checkpoint_reserve_turns),
                    checkpoint_reserve_tokens=int(checkpoint_reserve_tokens),
                    checkpoint_reserve_tool_calls=int(checkpoint_reserve_tool_calls),
                    turns=int(turns),
                    total_tokens=int(total_tokens),
                    tool_calls=int(tool_calls),
                ),
            ),
            rationale=rationale,
            replace_existing=_coerce_tool_bool(replace_existing),
        )
        return proposal.model_dump_json()
    except (PermissionError, RuntimeError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


def _profile_objective_in_view(agent: "UniversalAgent", proposal: ObjectiveWorkProfileProposal) -> bool:
    """Reusable profiles are personal; one-time profiles need current Objective authority."""

    if proposal.scope == ObjectiveWorkProfileScope.REUSABLE:
        return True
    if not proposal.objective_id:
        return False
    return _resolve_objective_id_for_tool(
        agent, proposal.objective_id, "", "", action=PRIVATE_UPDATE
    ) is not None


def _apply_profile_proposal(
    proposal_id: str,
    proposal_digest: str,
    confirmation_summary: str,
    confirmed: bool = False,
) -> str | dict[str, Any]:
    """Apply one exact accepted proposal: install reusable or enable its target Objective."""
    agent = get_agent_ref()
    if agent is None:
        return failed({"error": "Agent reference not set"})
    denial = _require_manager_tool(agent, "objective_work_profiles")
    if denial:
        return failed({"error": denial})
    if not _coerce_tool_bool(confirmed):
        return failed({"error": "Explicit user confirmation is required."})
    current = _get_current_task_info(agent)
    if current.meta.requester_type != RequesterType.USER_REQUESTER:
        return failed({"error": "direct_user_request_required"})
    owner, owner_denial = _private_user_scope(
        agent,
        resource_name="Objective work profiles",
    )
    if owner_denial is not None:
        return failed(owner_denial)
    summary = confirmation_summary.strip()
    if not summary:
        return failed({"error": "An explicit user acceptance summary is required."})
    coordinator = agent.task_manager.objective_work_coordinator
    policy = None
    previous_policy = None
    try:
        proposal = agent.task_manager.operations_runtime.run_sync(
            coordinator.require_profile_proposal,
            proposal_id,
            proposal_digest=proposal_digest,
            owner_canonical_user_id=owner,
        )
        # A proposal records the exact bounds, not lasting authority over
        # its Objective. The person's roles may have changed since staging.
        if not _profile_objective_in_view(agent, proposal):
            return failed(_NO_OBJECTIVE)
        if proposal.status == ObjectiveWorkProfileProposalStatus.APPLIED:
            return json.dumps(
                {
                    "proposal": proposal.model_dump(mode="json"),
                    "already_applied": True,
                }
            )
        if proposal.scope == ObjectiveWorkProfileScope.REUSABLE:
            from functools import partial

            return agent.task_manager.operations_runtime.run_sync(
                partial(_apply_reusable_objective_work_profile, coordinator),
                current=current,
                proposal=proposal,
                acceptance_summary=summary,
            )

        unavailable = missing_objective_work_skills(agent)
        if unavailable:
            return failed(
                {
                    "error": "Required Objective work-session skills are unavailable.",
                    "missing_skills": unavailable,
                }
            )
        repository = agent.task_manager.objective_work_repository
        if repository is None:
            raise RuntimeError("Bounded Objective execution is unavailable.")
        objective_id = proposal.objective_id or ""
        previous_policy = agent.task_manager.operations_runtime.run_sync(
            repository.get_policy,
            objective_id,
            owner_agent_id=agent.task_manager.operations_runtime.run_sync(
                coordinator._require_owner
            ),
        )
        if previous_policy is not None:
            previous_policy = previous_policy.model_copy(deep=True)
        policy = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.enable_policy,
            objective_id,
            profile_name=proposal.profile_name,
            accepted_by_task_id=current.task.task_id,
            acceptance_summary=summary,
            owner_canonical_user_id=owner,
            profile_override=proposal.profile.model_copy(deep=True),
            profile_source=ObjectiveWorkProfileSource.ONE_TIME,
            profile_proposal_id=proposal.proposal_id,
        )
        followup_result: dict[str, Any] = {}
        policy = _install_objective_work_schedule(
            agent,
            current=current,
            policy=policy,
            owner_canonical_user_id=owner,
            followup_result=followup_result,
        )
        applied = agent.task_manager.operations_runtime.run_sync(
            coordinator.mark_profile_proposal_applied,
            proposal,
            accepted_by_task_id=current.task.task_id,
            acceptance_summary=summary,
            applied_policy_id=policy.policy_id,
        )
        return json.dumps(
            {
                "proposal": applied.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                **followup_result,
            }
        )
    except Exception as exc:
        if policy is not None:
            _restore_or_disable_objective_work_policy(
                agent,
                policy=policy,
                previous_policy=previous_policy,
            )
        return failed({"error": str(exc)})


def _remove_profile(
    profile_name: str,
    confirmation_summary: str,
    confirmed: bool = False,
) -> str:
    """Remove a reusable profile; existing Objective policy snapshots are unchanged."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_profiles")
    if denial:
        return json.dumps(denial)
    if not _coerce_tool_bool(confirmed):
        return json.dumps({"error": "Explicit user confirmation is required."})
    current = _get_current_task_info(agent)
    if current.meta.requester_type != RequesterType.USER_REQUESTER:
        return json.dumps({"error": "direct_user_request_required"})
    owner, owner_denial = _private_user_scope(
        agent,
        resource_name="Objective work profiles",
    )
    if owner_denial is not None:
        return json.dumps(owner_denial)
    try:
        removed = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.remove_installed_profile,
            profile_name,
            owner_canonical_user_id=owner,
            removed_by_task_id=current.task.task_id,
            removal_summary=confirmation_summary,
        )
        return removed.model_dump_json()
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


def _session_status(objective_id: str = "") -> str:
    """Get policy, sessions, and checkpoints for one owned Objective."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_sessions")
    if denial:
        return json.dumps(denial)
    owner, owner_denial = _private_user_scope(
        agent, resource_name="Objective work sessions"
    )
    if owner_denial is not None:
        return json.dumps(owner_denial)
    current = _get_current_task_info(agent)
    task_id = current.task.task_id if current else ""
    resolved = _resolve_objective_id_for_tool(
        agent, objective_id, task_id, "", action=PRIVATE_READ
    )
    if not resolved:
        return json.dumps(_NO_OBJECTIVE)
    try:
        return json.dumps(
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.objective_work_coordinator.snapshot,
                resolved,
                owner_canonical_user_id=owner,
            )
        )
    except (PermissionError, RuntimeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


def _disable_sessions(
    objective_id: str = "", confirmed: bool = False
) -> str | dict[str, Any]:
    """Disable an Objective policy; its protected schedule remains an inert auditable record."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_sessions")
    if denial:
        return failed({"error": denial})
    if not _coerce_tool_bool(confirmed):
        return failed({"error": "Explicit user confirmation is required."})
    current = _get_current_task_info(agent)
    if current is None or current.meta.requester_type != RequesterType.USER_REQUESTER:
        return failed({"error": "direct_user_request_required"})
    owner, owner_denial = _private_user_scope(
        agent, resource_name="Objective work sessions"
    )
    if owner_denial is not None:
        return failed(owner_denial)
    resolved = _resolve_objective_id_for_tool(
        agent, objective_id, current.task.task_id, "", action=PRIVATE_UPDATE
    )
    if not resolved:
        return failed(_NO_OBJECTIVE)
    try:
        policy = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.disable_policy,
            resolved,
            owner_canonical_user_id=owner,
        )
        return policy.model_dump_json()
    except (PermissionError, RuntimeError, ValueError) as exc:
        return failed({"error": str(exc)})


def _start_session(objective_id: str = "") -> str | dict[str, Any]:
    """Open one explicitly requested work window on the current Objective-linked task."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "objective_work_sessions")
    if denial:
        return failed({"error": denial})
    current = _get_current_task_info(agent)
    if current is None:
        return failed({"error": "No active task context is available."})
    owner, owner_denial = _private_user_scope(
        agent, resource_name="Objective work sessions"
    )
    if owner_denial is not None:
        return failed(owner_denial)
    if current.meta.requester_type != RequesterType.USER_REQUESTER:
        return failed({"error": "direct_user_request_required"})
    resolved = _resolve_objective_id_for_tool(
        agent, objective_id, current.task.task_id, "", action=PRIVATE_UPDATE
    )
    if not resolved:
        return failed(_NO_OBJECTIVE)
    try:
        session = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.start_session,
            resolved,
            task_id=current.task.task_id,
            owner_canonical_user_id=owner,
        )
        return session.model_dump_json()
    except (PermissionError, RuntimeError, ValueError) as exc:
        return failed({"error": str(exc)})


ObjectiveWorkSessionsAction = Literal["suggest", "enable", "disable", "status", "start"]


ObjectiveWorkProfilesAction = Literal["list", "export", "apply", "remove"]


OBJECTIVE_WORK_SESSIONS_ACTIONS = actions(
    suggest=(("objective_id",), ("estimated_sessions",)),
    enable=(("objective_id", "confirmation_summary"), ("profile_name", "confirmed")),
    disable=((), ("objective_id", "confirmed")),
    status=((), ("objective_id",)),
    start=((), ("objective_id",)),
)


OBJECTIVE_WORK_PROFILES_ACTIONS = actions(
    list=((), ("include_proposals",)),
    export=((), ("profile_name", "objective_id", "make_default", "save", "confirmed", "replace_existing")),
    apply=(("proposal_id", "proposal_digest", "confirmation_summary"), ("confirmed",)),
    remove=(("profile_name", "confirmation_summary"), ("confirmed",)),
)


@tool
def objective_work_sessions(
    action: ObjectiveWorkSessionsAction,
    objective_id: Optional[str] = None,
    estimated_sessions: Optional[int] = None,
    confirmation_summary: Optional[str] = None,
    profile_name: Optional[str] = None,
    confirmed: bool = False,
) -> str | dict[str, Any]:
    """Bounded, recurring work sessions on one Objective (manager-only).

    An Objective is `objective_id`, or else the current task's.

    Actions:
    - `suggest`: an inert time-boxing suggestion for a large Objective (`estimated_sessions`, 3 by
      default); it never enables anything.
    - `enable`: enable recurring bounded sessions, only after the person explicitly accepted them in
      this request: `confirmation_summary` says what they accepted, `confirmed` must be true, and
      `profile_name` picks the execution profile.
    - `disable`: disable the sessions (`confirmed` must be true); the protected schedule remains as an
      inert, auditable record.
    - `status`: the Objective's session policy, sessions and checkpoints.
    - `start`: open one work window the person asked for, on the current Objective-linked task.

    Args:
        action: What to do; see Actions.
        objective_id: The Objective.
        estimated_sessions: How many sessions the work may take.
        confirmation_summary: What the person accepted.
        profile_name: The execution profile to use.
        confirmed: True once the person explicitly agreed.
    """

    inputs = {
        "objective_id": objective_id,
        "estimated_sessions": estimated_sessions,
        "confirmation_summary": confirmation_summary,
        "profile_name": profile_name,
        "confirmed": confirmed,
    }
    refusal = input_refusal("objective_work_sessions", action, OBJECTIVE_WORK_SESSIONS_ACTIONS, inputs)
    if refusal:
        return refusal
    objective = objective_id or ""
    if action == "suggest":
        return _suggest_session(objective, estimated_sessions=3 if estimated_sessions is None else estimated_sessions)
    if action == "enable":
        return _enable_sessions(
            objective, str(confirmation_summary), profile_name=profile_name or "", confirmed=confirmed
        )
    if action == "disable":
        return _disable_sessions(objective, confirmed=confirmed)
    if action == "status":
        return _session_status(objective)
    return _start_session(objective)


@tool
def objective_work_profiles(
    action: ObjectiveWorkProfilesAction,
    profile_name: Optional[str] = None,
    objective_id: Optional[str] = None,
    include_proposals: Optional[bool] = None,
    make_default: bool = False,
    save: bool = False,
    replace_existing: bool = False,
    proposal_id: Optional[str] = None,
    proposal_digest: Optional[str] = None,
    confirmation_summary: Optional[str] = None,
    confirmed: bool = False,
) -> str | dict[str, Any]:
    """The execution profiles that bound Objective work sessions (manager-only).

    Stage a new one-time or reusable profile with propose_objective_work_profile.

    Actions:
    - `list`: operator profiles, this person's installed profiles, the proposal history (unless
      `include_proposals` is false) and the bounds.
    - `export`: render one profile (`profile_name`, or the one `objective_id` uses) as an inert YAML
      policy layer; with `save` (and `confirmed`) write it to the workspace, `replace_existing` to
      overwrite. It applies only once added to workspace.objective_execution_paths and the manager restarts.
    - `apply`: apply one exact accepted proposal (`proposal_id`, `proposal_digest`): install a reusable
      profile, or enable its Objective's sessions. Needs `confirmation_summary` and `confirmed` true.
    - `remove`: remove reusable profile `profile_name` (needs `confirmation_summary` and `confirmed`
      true); Objectives that use it keep their copy.

    Args:
        action: What to do; see Actions.
        profile_name: The profile.
        objective_id: The Objective whose profile to export.
        include_proposals: List proposals too (true by default).
        make_default: Make the exported layer the default profile.
        save: Save the exported layer to the workspace.
        replace_existing: Overwrite a saved layer of the same name.
        proposal_id: The accepted proposal.
        proposal_digest: The digest of the exact proposal the person accepted.
        confirmation_summary: What the person accepted.
        confirmed: True once the person explicitly agreed.
    """

    inputs = {
        "profile_name": profile_name,
        "objective_id": objective_id,
        "include_proposals": include_proposals,
        "make_default": make_default,
        "save": save,
        "replace_existing": replace_existing,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "confirmation_summary": confirmation_summary,
        "confirmed": confirmed,
    }
    refusal = input_refusal("objective_work_profiles", action, OBJECTIVE_WORK_PROFILES_ACTIONS, inputs)
    if refusal:
        return refusal
    if action == "list":
        return _list_profiles(include_proposals=True if include_proposals is None else include_proposals)
    if action == "export":
        return export_profile(
            profile_name=profile_name or "",
            objective_id=objective_id or "",
            make_default=make_default,
            save=save,
            confirmed=confirmed,
            replace_existing=replace_existing,
        )
    if action == "apply":
        return _apply_profile_proposal(
            str(proposal_id), str(proposal_digest), str(confirmation_summary), confirmed=confirmed
        )
    return _remove_profile(str(profile_name), str(confirmation_summary), confirmed=confirmed)
