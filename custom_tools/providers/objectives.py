"""The team lead's Objective tools (catalog tool ``fn:objectives``).

``objectives``, ``objective_work_sessions`` and ``objective_work_profiles``
each take an ``action`` and replace several functions (owner's decision
2026-10-10: a model request may carry only so many tool definitions, and
these were nineteen). The catalog keeps a level per action (``argument:
action``) and each tool checks which inputs its action takes
(``custom_tools.action_inputs``); each action runs the function it replaced,
unchanged (``_get_objective``, ``_enable_sessions``, ...). Functions with many
inputs of their own stay separate: ``create_objective``,
``start_objective_task``, ``propose_objective_work_profile``,
``record_objective_work_checkpoint``, ``activate_plan_execution`` and
``start_isolated_recovery``. The work-session and profile tools live in
``custom_tools.providers.objective_work``; this module's provider lists them.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

from strands import tool

from access_control.ownership import PRIVATE_READ, PRIVATE_UPDATE
from custom_agents.schedule_management import (
    ScheduleMutationOperation,
    is_system_managed_schedule,
)
from custom_agents.task_manager_objectives import ObjectiveSaveFailed
from custom_agents.schedule_store import (
    schedule_claim_scope_for_agent,
)
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ObjectiveHealthDisposition,
    ObjectiveStatus,
    PlanActionType,
    PlanStatus,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_tools.action_inputs import actions, input_refusal
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.objective_work import (
    objective_work_profiles,
    objective_work_sessions,
    propose_objective_work_profile,
)
from custom_tools.runtime_context import (
    get_agent_ref,
)
from id_generation import (
    new_id,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


from custom_tools.providers._runtime import (
    _get_current_task_info,
    _link_to_objective,
    _named_task_refusal,
    _new_conversation_id,
    _objective_in_view,
    _objective_view,
    _require_manager_tool,
    _resolve_objective_id_for_tool,
    _schedule_store,
)
from custom_tools.providers.diagnostics import (
    _build_initial_recovery_context,
    _coerce_tool_bool,
    _parse_json_list,
    _parse_objective_completion_policy,
    _parse_objective_horizon,
    _parse_objective_horizon_list,
    _parse_objective_priority,
    _parse_objective_status,
    _parse_objective_status_list,
    _parse_plan_action_status,
    _parse_plan_action_type,
    _parse_plan_item_status,
    _parse_unknown_status,
)
from custom_tools.providers.heartbeat import (
    _objective_heartbeat_store_for_cascade,
)

logger = logging.getLogger(__name__)


































@tool
def record_objective_work_checkpoint(
    observe: str,
    orient: str,
    decide: str,
    act: str,
    health: str,
    evidence_delta_json: str = "[]",
    completed_subgoals_json: str = "[]",
    next_subgoal: str = "",
    blockers_json: str = "[]",
    in_flight_effects_json: str = "[]",
    artifacts_json: str = "[]",
) -> str:
    """Persist the required OODA continuation checkpoint for the active bounded run."""
    agent = get_agent_ref()
    denial = _require_manager_tool(agent, "record_objective_work_checkpoint")
    if denial:
        return json.dumps(denial)
    current = _get_current_task_info(agent)
    session_id = getattr(
        getattr(current, "meta", None), "objective_work_session_id", None
    )
    if not session_id:
        return json.dumps(
            {"error": "The active task is not part of an Objective work session."}
        )
    try:
        checkpoint = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_work_coordinator.checkpoint,
            session_id,
            observe=observe,
            orient=orient,
            decide=decide,
            act=act,
            health=ObjectiveHealthDisposition(health.strip().lower()),
            evidence_delta=_parse_json_list(
                evidence_delta_json, field_name="evidence_delta_json"
            ),
            completed_subgoals=_parse_json_list(
                completed_subgoals_json, field_name="completed_subgoals_json"
            ),
            next_subgoal=next_subgoal.strip() or None,
            blockers=_parse_json_list(blockers_json, field_name="blockers_json"),
            in_flight_effects=_parse_json_list(
                in_flight_effects_json, field_name="in_flight_effects_json"
            ),
            artifacts=_parse_json_list(artifacts_json, field_name="artifacts_json"),
        )
        return checkpoint.model_dump_json()
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": str(exc)})


def _plan_in_view(agent: "UniversalAgent", plan_id: str) -> bool:
    """Whether the active task may change one Plan: its Objective's, or its root task's person.

    Missing and inaccessible Plans both fail without revealing which case
    occurred.
    """

    plan = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_plan, plan_id
    )
    if plan is None:
        return False
    if plan.objective_id:
        return _objective_in_view(agent, plan.objective_id, action=PRIVATE_UPDATE)
    if plan.root_task_id:
        return _named_task_refusal(agent, plan.root_task_id) is None
    logger.warning(
        "Plan with neither an Objective nor a root task was refused. "
        "event=plan_owner_unknown plan_id=%s",
        plan_id,
    )
    return False


def _resolve_plan_id_for_tool(
    agent: "UniversalAgent",
    *,
    plan_id: str,
    objective_id: str,
    task_id: str,
) -> str:
    if plan_id.strip():
        return plan_id.strip()
    resolved_objective_id = _resolve_objective_id_for_tool(
        agent,
        objective_id,
        task_id,
        "",
        action=PRIVATE_UPDATE,
    )
    objective = (
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_objective, resolved_objective_id
        )
        if resolved_objective_id
        else None
    )
    if objective is not None and objective.current_plan_id:
        return objective.current_plan_id
    if objective_id.strip():
        # A named Objective means its own plan, never the task's instead.
        return ""
    return (
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_plan_id, task_id
        )
        or ""
    )


@tool
def create_objective(
    title: str,
    desired_outcome: str,
    why: str,
    horizon: str = "finite",
    priority: str = "normal",
    completion_policy: str = "manager_decides",
    success_criteria_json: str = "",
    constraints_json: str = "",
    non_goals_json: str = "",
    requires_user_confirmation: bool = False,
    current_focus: str = "",
    progress_summary: str = "",
    creation_mode: str = "promote_task",
    task_id: str = "",
) -> str:
    """
    Create a durable Objective from the current manager task, or a specified manager task.

    Use this only when a normal Task or Task+Plan is not enough: long-horizon,
    recurring, standing, research, analysis, or user-visible tracking work.

    creation_mode controls lineage:
    - promote_task: the source task becomes the Objective root work.
    - spawn_task: the source task creates a separate Objective root task.

    A Plan already owned by an Objective cannot be promoted again. Use
    spawn_task for separate work with its own Plan.

    Objective creation always ensures at least one Objective-owned task and one
    durable plan. Use activate_plan_execution later when the manager decides the
    plan needs staged fresh-context execution.

    The Objective belongs to the person the source task works for, so a
    specified task must work for the same person as the current one.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "create_objective")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})

    resolved_task_id = task_id or agent.task_manager.current_task_id
    if not resolved_task_id:
        return json.dumps(
            {"error": "No task_id provided and no task currently in progress"}
        )
    task_refusal = _named_task_refusal(agent, resolved_task_id)
    if task_refusal:
        return json.dumps({"error": task_refusal})
    # A person's task can share a team Objective's Plan. Creating an
    # Objective from it must not move that Plan into personal ownership.
    source_objective_id = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_objective_id, resolved_task_id
    )
    source_plan_id = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_plan_id, resolved_task_id
    )
    if (
        source_objective_id
        and not _objective_in_view(agent, source_objective_id, action=PRIVATE_UPDATE)
    ) or (source_plan_id and not _plan_in_view(agent, source_plan_id)):
        return json.dumps({"error": "No objective could be resolved from the supplied context."})

    try:
        success_criteria = _parse_json_list(
            success_criteria_json, field_name="success_criteria_json"
        )
        constraints = _parse_json_list(constraints_json, field_name="constraints_json")
        non_goals = _parse_json_list(non_goals_json, field_name="non_goals_json")
        objective = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.create_objective,
            task_id=resolved_task_id,
            title=title,
            desired_outcome=desired_outcome,
            why=why,
            creation_mode=creation_mode,
            horizon=_parse_objective_horizon(horizon),
            priority=_parse_objective_priority(priority),
            completion_policy=_parse_objective_completion_policy(completion_policy),
            success_criteria=success_criteria,
            constraints=[str(item) for item in constraints],
            non_goals=[str(item) for item in non_goals],
            requires_user_confirmation=_coerce_tool_bool(requires_user_confirmation),
            current_focus=current_focus or None,
            progress_summary=progress_summary or None,
        )
    except (ValueError, json.JSONDecodeError, ObjectiveSaveFailed) as exc:
        return json.dumps({"error": str(exc)})

    if objective is None:
        return json.dumps(
            {"error": f"Failed to create objective for task {resolved_task_id}"}
        )
    returned_creation_mode = (
        "promote_task" if objective.promoted_from_task_id else "spawn_task"
    )
    return json.dumps(
        {
            "task_id": objective.related_task_ids[0]
            if objective.related_task_ids
            else resolved_task_id,
            "created_by_task_id": objective.created_by_task_id,
            "promoted_from_task_id": objective.promoted_from_task_id,
            "creation_mode": returned_creation_mode,
            "objective_id": objective.objective_id,
            "plan_id": objective.current_plan_id,
            "objective": objective.model_dump(mode="json"),
            "markdown": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.render_objective_markdown, objective.objective_id
            ),
        },
        default=str,
    )


def _get_objective(objective_id: str = "", task_id: str = "", plan_id: str = "") -> str:
    """
    Inspect a durable Objective by objective_id, task_id, plan_id, or current task context.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "objectives")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})

    resolved_objective_id = _resolve_objective_id_for_tool(
        agent, objective_id, task_id, plan_id, action=PRIVATE_READ
    )
    if not resolved_objective_id:
        return json.dumps(
            {
                "objective": None,
                "error": "No objective could be resolved from the supplied context.",
            }
        )
    objective = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_objective, resolved_objective_id
    )
    if objective is None:
        return json.dumps({"objective_id": resolved_objective_id, "objective": None})
    return json.dumps(
        {
            "objective_id": objective.objective_id,
            "objective": objective.model_dump(mode="json"),
            "markdown": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.render_objective_markdown, objective.objective_id
            ),
        },
        default=str,
    )


def _list_objectives(
    limit: int = 20,
    statuses: str = "",
    horizons: str = "",
    include_closed: bool = False,
) -> str:
    """
    List the Objectives this task may see: the team's and those of the person it works for.

    statuses and horizons are optional comma-separated enum values.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "objectives")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})

    view = _objective_view(agent)
    if view is None:
        return json.dumps({"returned": 0, "objectives": []})
    rows = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.list_objectives,
        view=view,
        limit=limit,
        statuses=_parse_objective_status_list(statuses),
        horizons=_parse_objective_horizon_list(horizons),
        include_closed=_coerce_tool_bool(include_closed),
    )
    return json.dumps(
        {
            "returned": len(rows),
            "objectives": [row.__dict__ for row in rows],
        },
        default=str,
    )


def _update_progress(
    objective_id: str = "",
    progress_summary: str = "",
    current_focus: str = "",
    criterion_updates_json: str = "",
    task_id: str = "",
    plan_id: str = "",
    user_visible: bool = True,
) -> str:
    """
    Update a durable Objective's progress summary, focus, or success criteria.

    criterion_updates_json must be a JSON list of criterion update objects.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "objectives")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})

    task_refusal = _named_task_refusal(agent, task_id)
    if task_refusal:
        return json.dumps({"error": task_refusal})
    resolved_objective_id = _resolve_objective_id_for_tool(
        agent, objective_id, task_id, plan_id, action=PRIVATE_UPDATE
    )
    if not resolved_objective_id:
        return json.dumps(
            {"error": "No objective could be resolved from the supplied context."}
        )
    try:
        criterion_updates = _parse_json_list(
            criterion_updates_json,
            field_name="criterion_updates_json",
        )
        objective = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.update_objective_progress,
            resolved_objective_id,
            progress_summary=progress_summary if progress_summary else None,
            current_focus=current_focus if current_focus else None,
            criterion_updates=criterion_updates,
            task_id=task_id or agent.task_manager.current_task_id,
            plan_id=plan_id or None,
            user_visible=_coerce_tool_bool(user_visible),
        )
    except (ValueError, json.JSONDecodeError, ObjectiveSaveFailed) as exc:
        return json.dumps({"error": str(exc)})

    if objective is None:
        return json.dumps(
            {"error": f"Failed to update objective {resolved_objective_id}"}
        )
    return json.dumps(
        {
            "objective_id": objective.objective_id,
            "objective": objective.model_dump(mode="json"),
            "markdown": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.render_objective_markdown, objective.objective_id
            ),
        },
        default=str,
    )


def _update_status(
    status: str,
    objective_id: str = "",
    summary: str = "",
    user_confirmation_summary: str = "",
    satisfied_by_current_task: bool = False,
    task_id: str = "",
    plan_id: str = "",
    user_visible: bool = True,
) -> str:
    """
    Change a durable Objective lifecycle status.

    Marking a user-confirmed Objective satisfied requires user_confirmation_summary.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "objectives")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})

    parsed_status = _parse_objective_status(status)
    if parsed_status is None:
        return json.dumps(
            {
                "error": f"Unknown objective status: {status}",
                "allowed_statuses": [item.value for item in ObjectiveStatus],
            }
        )
    task_refusal = _named_task_refusal(agent, task_id)
    if task_refusal:
        return json.dumps({"error": task_refusal})
    current_task_id = agent.task_manager.current_task_id
    resolved_task_id = task_id or current_task_id
    resolved_objective_id = _resolve_objective_id_for_tool(
        agent, objective_id, resolved_task_id or "", plan_id, action=PRIVATE_UPDATE
    )
    if not resolved_objective_id:
        return json.dumps(
            {"error": "No objective could be resolved from the supplied context."}
        )
    try:
        objective = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.update_objective_status,
            resolved_objective_id,
            parsed_status,
            summary=summary or None,
            user_confirmation_summary=user_confirmation_summary or None,
            satisfied_by_task_id=(
                resolved_task_id
                if _coerce_tool_bool(satisfied_by_current_task)
                else None
            ),
            task_id=resolved_task_id or None,
            plan_id=plan_id or None,
            user_visible=_coerce_tool_bool(user_visible),
        )
    except (ValueError, ObjectiveSaveFailed) as exc:
        return json.dumps({"error": str(exc)})

    if objective is None:
        return json.dumps(
            {"error": f"Failed to update objective {resolved_objective_id}"}
        )
    objective, lifecycle_warnings = _apply_objective_lifecycle_cascade(agent, objective)
    payload = {
        "objective_id": objective.objective_id,
        "objective": objective.model_dump(mode="json"),
        "markdown": agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.render_objective_markdown, objective.objective_id
        ),
    }
    if lifecycle_warnings:
        payload["lifecycle_warnings"] = list(lifecycle_warnings)
    return json.dumps(payload, default=str)


@tool
def start_objective_task(
    work_brief: str,
    objective_id: str = "",
    current_focus: str = "",
    source_task_id: str = "",
    user_visible: bool = True,
) -> str:
    """Queue a replaceable Task that continues a durable Objective and its current Plan."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "start_objective_task")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})
    if not work_brief or not work_brief.strip():
        return json.dumps({"error": "work_brief is required"})

    resolved_source_task_id = source_task_id or agent.task_manager.current_task_id
    if not resolved_source_task_id:
        return json.dumps({"error": "No source task is currently in progress"})
    # The new task works for the source task's person.
    task_refusal = _named_task_refusal(agent, resolved_source_task_id)
    if task_refusal:
        return json.dumps({"error": task_refusal})
    resolved_objective_id = _resolve_objective_id_for_tool(
        agent,
        objective_id,
        resolved_source_task_id,
        "",
        action=PRIVATE_UPDATE,
    )
    if not resolved_objective_id:
        return json.dumps(
            {"error": "No objective could be resolved from the supplied context."}
        )

    try:
        task_info = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.objective_coordinator.start_objective_task,
            resolved_objective_id,
            source_task_id=resolved_source_task_id,
            work_brief=work_brief,
            current_focus=current_focus or None,
            user_visible=_coerce_tool_bool(user_visible),
        )
    except (ValueError, ObjectiveSaveFailed) as exc:
        return json.dumps({"error": str(exc)})
    if task_info is None:
        return json.dumps(
            {
                "error": f"Failed to start a continuation Task for Objective {resolved_objective_id}"
            }
        )

    objective = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_objective, resolved_objective_id
    )
    return json.dumps(
        {
            "queued": True,
            "task_id": task_info.task.task_id,
            "objective_id": resolved_objective_id,
            "plan_id": objective.current_plan_id if objective is not None else None,
            "task_payload_has_plan_id": "plan_id" in task_info.task.model_dump(),
        },
        default=str,
    )


@tool
def update_execution_plan_status(
    status: str,
    summary: str,
    plan_id: str = "",
    objective_id: str = "",
    task_id: str = "",
) -> str:
    """Explicitly transition a durable Plan independently from any Task lifecycle."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    manager_error = _require_manager_tool(agent, "update_execution_plan_status")
    if manager_error:
        return json.dumps({"error": manager_error, "manager_only": True})
    if not summary or not summary.strip():
        return json.dumps({"error": "summary is required"})
    try:
        parsed_status = PlanStatus(str(status).strip().lower())
    except ValueError:
        return json.dumps(
            {
                "error": f"Unknown plan status: {status}",
                "allowed_statuses": [item.value for item in PlanStatus],
            }
        )

    task_refusal = _named_task_refusal(agent, task_id)
    if task_refusal:
        return json.dumps({"error": task_refusal})
    current_task_id = agent.task_manager.current_task_id
    resolved_task_id = task_id or current_task_id
    resolved_plan_id = _resolve_plan_id_for_tool(
        agent,
        plan_id=plan_id,
        objective_id=objective_id,
        task_id=resolved_task_id or "",
    )
    if not resolved_plan_id or not _plan_in_view(agent, resolved_plan_id):
        return json.dumps(
            {"error": "No plan could be resolved from the supplied context."}
        )

    try:
        plan = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.plan_coordinator.update_plan_status,
            resolved_plan_id,
            parsed_status,
            summary=summary,
            task_id=resolved_task_id or None,
            agent_name=agent.name,
            agent_id=agent.id,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if plan is None:
        return json.dumps({"error": f"Failed to update Plan {resolved_plan_id}"})
    return json.dumps(
        {
            "plan_id": plan.plan_id,
            "objective_id": plan.objective_id,
            "plan": plan.model_dump(mode="json"),
        },
        default=str,
    )


@tool
def set_execution_plan(
    plan_summary: str,
    goal_summary: str = "",
    current_focus: str = "",
    rationale: str = "",
) -> str:
    """
    Update the durable high-level execution plan summary for the current task.

    Use this for the living top-level plan, not for ephemeral notes.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        return json.dumps({"error": "No task currently in progress"})

    digest = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.set_task_plan,
        current_task_id,
        plan_summary,
        goal_summary=goal_summary or None,
        current_focus=current_focus or None,
        rationale=rationale or None,
        agent_name=agent.name,
        agent_id=agent.id,
    )
    if digest is None:
        return json.dumps(
            {"error": f"Failed to update execution plan for task {current_task_id}"}
        )

    return json.dumps(
        {
            "task_id": current_task_id,
            "plan_id": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_plan_id, current_task_id
            ),
            "digest": digest.model_dump(mode="json"),
        },
        default=str,
    )


@tool
def update_execution_plan_step(
    title: str,
    rationale: str,
    step_id: str = "",
    status: str = "planned",
    status_detail: str = "",
    resolution_summary: str = "",
) -> str:
    """
    Create or update one durable plan step for the current task.

    Every plan step must include rationale so the manager can understand why it exists.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        return json.dumps({"error": "No task currently in progress"})

    try:
        item = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.upsert_execution_plan_item,
            current_task_id,
            title=title,
            rationale=rationale,
            step_id=step_id or None,
            status=_parse_plan_item_status(status),
            status_detail=status_detail or None,
            resolution_summary=resolution_summary or None,
            agent_name=agent.name,
            agent_id=agent.id,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if item is None:
        return json.dumps(
            {
                "error": f"Failed to update execution plan step for task {current_task_id}"
            }
        )

    return json.dumps(
        {
            "task_id": current_task_id,
            "plan_id": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_plan_id, current_task_id
            ),
            "step": item.model_dump(mode="json"),
        },
        default=str,
    )


@tool
def update_execution_unknown(
    summary: str,
    unknown_id: str = "",
    status: str = "open",
    why_it_matters: str = "",
    resolution_plan: str = "",
    resolution_summary: str = "",
) -> str:
    """
    Create or update one durable tracked unknown for the current task.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        return json.dumps({"error": "No task currently in progress"})

    item = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.upsert_execution_unknown,
        current_task_id,
        summary=summary,
        unknown_id=unknown_id or None,
        status=_parse_unknown_status(status),
        why_it_matters=why_it_matters or None,
        resolution_plan=resolution_plan or None,
        resolution_summary=resolution_summary or None,
        agent_name=agent.name,
        agent_id=agent.id,
    )
    if item is None:
        return json.dumps(
            {"error": f"Failed to update execution unknown for task {current_task_id}"}
        )

    return json.dumps(
        {
            "task_id": current_task_id,
            "plan_id": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_plan_id, current_task_id
            ),
            "unknown": item.model_dump(mode="json"),
        },
        default=str,
    )


@tool
def record_plan_action(
    action_type: str,
    summary: str,
    reason: str = "",
    status: str = "recorded",
    related_task_id: str = "",
    recovery_task_id: str = "",
    plan_step_id: str = "",
    branch_id: str = "",
    user_visible: bool = False,
    details_json: str = "",
) -> str:
    """
    Record a manager-owned recovery or orchestration action on the current plan.

    Use this when the manager diverges from the happy path: checkpointing,
    recording a failure, starting inline or isolated recovery, asking the user,
    pausing/resuming work, or abandoning a branch. This does not mutate core task
    lifecycle state and is only meaningful for TeamArchitect tasks.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "record_plan_action is manager-only; specialists should use normal task history/digest tools.",
                "manager_only": True,
            }
        )

    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        return json.dumps({"error": "No task currently in progress"})

    parsed_action_type = _parse_plan_action_type(action_type)
    if parsed_action_type is None:
        return json.dumps(
            {
                "error": f"Unknown plan action type: {action_type}",
                "allowed_action_types": [item.value for item in PlanActionType],
            }
        )
    if not summary or not summary.strip():
        return json.dumps({"error": "summary is required"})

    details: Dict[str, Any] = {}
    if details_json.strip():
        try:
            parsed_details = json.loads(details_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"details_json must be valid JSON: {exc}"})
        if not isinstance(parsed_details, dict):
            return json.dumps({"error": "details_json must decode to a JSON object"})
        details = parsed_details

    try:
        action = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.record_plan_action,
            current_task_id,
            parsed_action_type,
            summary,
            status=_parse_plan_action_status(status),
            reason=reason or None,
            related_task_id=related_task_id or None,
            recovery_task_id=recovery_task_id or None,
            plan_step_id=plan_step_id or None,
            branch_id=branch_id or None,
            user_visible=_coerce_tool_bool(user_visible),
            details=details,
            agent_name=agent.name,
            agent_id=agent.id,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if action is None:
        return json.dumps(
            {"error": f"Failed to record plan action for task {current_task_id}"}
        )

    return json.dumps(
        {
            "task_id": current_task_id,
            "plan_id": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_plan_id, current_task_id
            ),
            "plan_action": action.model_dump(mode="json"),
        },
        default=str,
    )


@tool
def activate_plan_execution(
    steps_json: str,
    final_review: bool = True,
) -> str:
    """
    Promote the current manager task's plan into runtime-owned step execution.

    `steps_json` must be a JSON list of objects with semantic fields only:
    `title`, `objective`, and optional `rationale`. The runtime creates and
    persists all task, plan, step, conversation, and dependency identifiers.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "activate_plan_execution is manager-only; specialists should execute their assigned Task directly.",
                "manager_only": True,
            }
        )

    try:
        current_task_info = _get_current_task_info(agent)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    try:
        parsed_steps = json.loads(steps_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"steps_json must be valid JSON: {exc}"})
    if not isinstance(parsed_steps, list):
        return json.dumps({"error": "steps_json must decode to a JSON list"})

    semantic_steps: List[Dict[str, Any]] = []
    for index, raw_step in enumerate(parsed_steps):
        if not isinstance(raw_step, dict):
            return json.dumps({"error": f"Step {index + 1} must be a JSON object"})
        title = str(raw_step.get("title") or "").strip()
        objective = str(
            raw_step.get("objective") or raw_step.get("description") or ""
        ).strip()
        rationale = str(raw_step.get("rationale") or "").strip()
        if not title or not objective:
            return json.dumps(
                {"error": f"Step {index + 1} requires title and objective"}
            )
        semantic_steps.append(
            {
                "title": title,
                "objective": objective,
                "rationale": rationale,
            }
        )

    try:
        plan = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.activate_plan_execution,
            current_task_info.task.task_id,
            steps=semantic_steps,
            final_review=_coerce_tool_bool(final_review),
            agent_name=agent.name,
            agent_id=agent.id,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if plan is None:
        return json.dumps({"error": "Failed to activate runtime plan execution"})

    return json.dumps(
        {
            "plan_execution_started": True,
            "step_count": len(plan.plan_steps),
            "metadata_hidden": True,
            "ids_available_to_model": False,
            "next_action": (
                "Stop working in this invocation. Return control so the runtime can execute "
                "the first internal plan step in a fresh context."
            ),
        },
        default=str,
    )


@tool
def start_isolated_recovery(
    failure_type: str,
    failure_summary: str,
    recovery_brief: str,
    affected_step_id: str = "",
    affected_subtask_id: str = "",
    notify_user: bool = True,
    details_json: str = "",
    checkpoint_delay_seconds: int = 300,
) -> str:
    """
    Queue a fresh-context manager recovery task linked to the current plan.

    The new task receives only the compact recovery brief. Correlation and
    resume linkage are tracked in manager-owned runtime metadata; task, plan,
    conversation, and routing identifiers are intentionally not returned to the
    model.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "start_isolated_recovery is manager-only; specialists should return their task result or ask a TaskQuery.",
                "manager_only": True,
            }
        )
    if not failure_type.strip():
        return json.dumps({"error": "failure_type is required"})
    if not failure_summary.strip():
        return json.dumps({"error": "failure_summary is required"})
    if not recovery_brief.strip():
        return json.dumps({"error": "recovery_brief is required"})

    try:
        current_task_info = _get_current_task_info(agent)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    details: Dict[str, Any] = {}
    if details_json.strip():
        try:
            parsed_details = json.loads(details_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"details_json must be valid JSON: {exc}"})
        if not isinstance(parsed_details, dict):
            return json.dumps({"error": "details_json must decode to a JSON object"})
        details = parsed_details

    recovery_task_id = new_id("recovery_task")
    branch_id = new_id("recovery_branch")
    original_header = current_task_info.header
    original_meta = current_task_info.meta
    response_route = (
        original_header.response_routing_key
        or original_header.from_agent_id
        or agent.id
    )
    recovery_header = MessageHeader(
        from_agent_id=original_header.from_agent_id or agent.id,
        to_routing_path=agent.id,
        response_routing_key=response_route,
        agent_signature=original_header.agent_signature,
    )
    recovery_meta = original_meta.model_copy(deep=True)
    recovery_meta.msg_correlation_id = f"recovery_corr_{recovery_task_id}"
    recovery_meta.conversation_id = _new_conversation_id()
    recovery_meta.followup_to_task_id = None
    recovery_meta.requester_type = (
        original_meta.requester_type or RequesterType.AGENT_REQUESTER
    )
    recovery_meta.is_response_requested = _coerce_tool_bool(notify_user)
    recovery_meta.to_agent_name = agent.name
    recovery_meta.to_named_agent_type = agent.named_agent_type
    plan_id = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_plan_id, current_task_info.task.task_id
    )
    objective_id = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_objective_id, current_task_info.task.task_id
    )
    recovery_meta.objective_id = objective_id
    link_preview = SimpleNamespace(
        original_task_id=current_task_info.task.task_id,
        original_plan_id=plan_id or "",
        recovery_task_id=recovery_task_id,
        branch_id=branch_id,
        failure_type=failure_type.strip(),
        failure_summary=failure_summary.strip(),
        affected_step_id=affected_step_id or None,
        affected_subtask_id=affected_subtask_id or None,
        checkpoint_schedule_id=None,
    )
    initial_recovery_context = _build_initial_recovery_context(agent, link_preview)

    try:
        recovery_task_info = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.start_isolated_recovery_task,
            current_task_info.task.task_id,
            failure_type=failure_type,
            failure_summary=failure_summary,
            recovery_brief=recovery_brief,
            initial_recovery_context=initial_recovery_context,
            header=recovery_header,
            meta=recovery_meta,
            affected_step_id=affected_step_id or None,
            affected_subtask_id=affected_subtask_id or None,
            recovery_task_id=recovery_task_id,
            branch_id=branch_id,
            notify_user=_coerce_tool_bool(notify_user),
            details=details,
            agent_name=agent.name,
            agent_id=agent.id,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    if recovery_task_info is None:
        return json.dumps({"error": "Failed to queue isolated recovery task"})

    checkpoint_schedule = None
    checkpoint_link_fields: dict[str, str] = {}
    if _coerce_tool_bool(notify_user) and checkpoint_delay_seconds > 0:
        try:
            checkpoint_schedule_id = new_id("recov_ckpt")
            checkpoint_schedule = ScheduledMessage(
                schedule_id=checkpoint_schedule_id,
                schedule_name=(
                    "isolated_recovery_checkpoint:"
                    f"{current_task_info.task.task_id}:{recovery_task_id}"
                ),
                management_class=ScheduleManagementClass.SYSTEM,
                system_schedule_kind=SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT,
                objective_id=objective_id,
                payload=Task(
                    task_id=new_id("recov_ckpt_task"),
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=(
                        "Internal isolated-recovery checkpoint. If the recovery task is still active, "
                        "ask the user whether to continue troubleshooting, stop and return partial findings, "
                        "or abort the original task."
                    ),
                ),
                payload_header=MessageHeader(
                    from_agent_id=agent.id,
                    to_routing_path=agent.id,
                    response_routing_key=response_route,
                    agent_signature=original_header.agent_signature,
                ),
                payload_meta=original_meta.model_copy(deep=True),
                repeat_count=1,
                next_fire_at=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=checkpoint_delay_seconds)
                ).isoformat(),
            )
            checkpoint_schedule.payload_meta.requester_type = (
                RequesterType.TIMER_REQUESTER
            )
            checkpoint_schedule.payload_meta.is_response_requested = True
            checkpoint_schedule.payload_meta.msg_correlation_id = (
                f"checkpoint_corr_{checkpoint_schedule_id}"
            )
            checkpoint_schedule.payload_meta.followup_to_task_id = (
                current_task_info.task.task_id
            )
            checkpoint_schedule.payload_meta.objective_id = objective_id
            _schedule_store(agent).add(
                checkpoint_schedule,
                **schedule_claim_scope_for_agent(agent),
            )
            checkpoint_link_fields = _link_to_objective(
                agent,
                "schedule",
                objective_id,
                checkpoint_schedule_id,
                managed_by_objective=True,
                user_visible=False,
                reason="Isolated recovery checkpoint schedule.",
            )
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.set_recovery_checkpoint_schedule_id,
                recovery_task_id,
                checkpoint_schedule_id,
            )
            if getattr(agent, "_scheduler_wake", None):
                agent._scheduler_wake.set()
        except Exception as exc:
            checkpoint_schedule = {"error": str(exc)}

    return json.dumps(
        {
            "queued": True,
            "recovery_started": True,
            "metadata_hidden": True,
            "recovery_task_payload_has_plan_id": "plan_id"
            in recovery_task_info.task.model_dump(),
            "user_notice_recorded": bool(_coerce_tool_bool(notify_user)),
            "checkpoint_schedule": (
                {"scheduled": True, **checkpoint_link_fields}
                if isinstance(checkpoint_schedule, ScheduledMessage)
                else checkpoint_schedule
            ),
        },
        default=str,
    )


def _save_cascaded_links(agent: "UniversalAgent", objective: Any) -> tuple[str, ...]:
    """Save which links a cascade paused or resumed; a warning when the Objective could not.

    The schedules and heartbeat items already changed. An Objective that does
    not remember pausing them does not restart them when it resumes.
    """

    try:
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager._save_objective, objective
        )
    except ObjectiveSaveFailed:
        return (
            f"Objective {objective.objective_id} could not record which schedules and heartbeat "
            "items it paused or resumed, so resuming it will not restart the ones it paused. "
            "Tell the person.",
        )
    return ()


def _apply_objective_lifecycle_cascade(
    agent: "UniversalAgent", objective: Any
) -> tuple[Any, tuple[str, ...]]:
    """Apply Objective pause/resume/terminal state to managed schedules and heartbeat items."""
    disable_statuses = {
        ObjectiveStatus.PAUSED,
        ObjectiveStatus.SATISFIED,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
        ObjectiveStatus.ABANDONED,
    }
    should_disable = objective.status in disable_statuses
    should_resume = objective.status == ObjectiveStatus.ACTIVE
    changed = False

    schedule_store = getattr(agent, "_schedule_store", None)
    if schedule_store is not None:
        for link in getattr(objective, "schedule_links", []) or []:
            if not getattr(link, "managed_by_objective", True):
                continue
            if should_disable and not getattr(
                link, "disabled_by_objective_pause", False
            ):
                linked_schedule = schedule_store.get_scheduled_message(link.schedule_id)
                if is_system_managed_schedule(linked_schedule):
                    getattr(agent, "_logger", logger).warning(
                        "Objective lifecycle cascade skipped protected system schedule. "
                        "event=objective_system_schedule_mutation_skipped objective_id=%s "
                        "schedule_id=%s requested_operation=disable",
                        objective.objective_id,
                        link.schedule_id,
                    )
                    continue
                updated = schedule_store.set_enabled(
                    link.schedule_id,
                    False,
                    operation=ScheduleMutationOperation.DISABLE,
                )
                if updated:
                    link.disabled_by_objective_pause = True
                    changed = True
            elif should_resume and getattr(link, "disabled_by_objective_pause", False):
                linked_schedule = schedule_store.get_scheduled_message(link.schedule_id)
                if is_system_managed_schedule(linked_schedule):
                    getattr(agent, "_logger", logger).warning(
                        "Objective lifecycle cascade skipped protected system schedule. "
                        "event=objective_system_schedule_mutation_skipped objective_id=%s "
                        "schedule_id=%s requested_operation=enable",
                        objective.objective_id,
                        link.schedule_id,
                    )
                    continue
                updated = schedule_store.set_enabled(
                    link.schedule_id,
                    True,
                    operation=ScheduleMutationOperation.ENABLE,
                )
                if updated:
                    link.disabled_by_objective_pause = False
                    changed = True
        if changed and getattr(agent, "_scheduler_wake", None):
            agent._scheduler_wake.set()

    heartbeat_links = getattr(objective, "heartbeat_links", []) or []
    heartbeat_store, warnings = _objective_heartbeat_store_for_cascade(
        agent, objective, heartbeat_links
    )
    if heartbeat_store is not None:
        for link in heartbeat_links:
            if not getattr(link, "managed_by_objective", True):
                continue
            if should_disable and not getattr(
                link, "disabled_by_objective_pause", False
            ):
                heartbeat_store.set_item_enabled(link.heartbeat_item_id, False)
                link.disabled_by_objective_pause = True
                changed = True
            elif should_resume and getattr(link, "disabled_by_objective_pause", False):
                heartbeat_store.set_item_enabled(link.heartbeat_item_id, True)
                link.disabled_by_objective_pause = False
                changed = True

    if changed:
        warnings = (*warnings, *_save_cascaded_links(agent, objective))
        refreshed = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_objective, objective.objective_id
        )
        return refreshed or objective, tuple(warnings)
    return objective, tuple(warnings)


# -- action tools ----------------------------------------------------------------------

ObjectivesAction = Literal["get", "list", "update_progress", "update_status"]

_OBJECTIVE_REF = ("objective_id", "task_id", "plan_id")
OBJECTIVES_ACTIONS = actions(
    get=((), _OBJECTIVE_REF),
    list=((), ("limit", "statuses", "horizons", "include_closed")),
    update_progress=((), (*_OBJECTIVE_REF, "progress_summary", "current_focus", "criterion_updates_json", "user_visible")),
    update_status=(
        ("status",),
        (*_OBJECTIVE_REF, "summary", "user_confirmation_summary", "satisfied_by_current_task", "user_visible"),
    ),
)


@tool
def objectives(
    action: ObjectivesAction,
    objective_id: Optional[str] = None,
    task_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    limit: Optional[int] = None,
    statuses: Optional[str] = None,
    horizons: Optional[str] = None,
    include_closed: bool = False,
    progress_summary: Optional[str] = None,
    current_focus: Optional[str] = None,
    criterion_updates_json: Optional[str] = None,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    user_confirmation_summary: Optional[str] = None,
    satisfied_by_current_task: bool = False,
    user_visible: Optional[bool] = None,
) -> str:
    """See and update durable Objectives (manager-only).

    A task sees the team's Objectives and those of the person it works for, never another
    person's. The team's Objectives change only in work for no person or for an administrator.
    An Objective is found by `objective_id`, `task_id` or `plan_id`, or else from the current task.

    Actions:
    - `get`: one Objective, with its markdown summary.
    - `list`: Objectives (up to `limit`, 20 by default); `statuses` and `horizons` are optional
      comma-separated values, and closed ones are left out unless `include_closed`.
    - `update_progress`: its `progress_summary`, `current_focus`, or success criteria
      (`criterion_updates_json`, a JSON list of criterion update objects).
    - `update_status`: change its lifecycle `status`, with a `summary`. Marking a user-confirmed
      Objective satisfied needs `user_confirmation_summary`; `satisfied_by_current_task` records this
      task as what satisfied it.

    Updates are shown to the person unless `user_visible` is false.

    Args:
        action: What to do; see Actions.
        objective_id: The Objective.
        task_id: A task linked to the Objective.
        plan_id: A plan linked to the Objective.
        limit: How many Objectives to list at most.
        statuses: Statuses to list, comma-separated.
        horizons: Horizons to list, comma-separated.
        include_closed: List closed Objectives too.
        progress_summary: What progress was made.
        current_focus: What the work focuses on now.
        criterion_updates_json: A JSON list of success criterion updates.
        status: The new lifecycle status.
        summary: Why the status changes.
        user_confirmation_summary: What the person confirmed, to mark a user-confirmed Objective satisfied.
        satisfied_by_current_task: This task satisfied the Objective.
        user_visible: Whether the person sees the update (true by default).
    """

    reference = {"objective_id": objective_id, "task_id": task_id, "plan_id": plan_id}
    inputs = {
        **reference,
        "limit": limit,
        "statuses": statuses,
        "horizons": horizons,
        "include_closed": include_closed,
        "progress_summary": progress_summary,
        "current_focus": current_focus,
        "criterion_updates_json": criterion_updates_json,
        "status": status,
        "summary": summary,
        "user_confirmation_summary": user_confirmation_summary,
        "satisfied_by_current_task": satisfied_by_current_task,
        "user_visible": user_visible,
    }
    refusal = input_refusal("objectives", action, OBJECTIVES_ACTIONS, inputs)
    if refusal:
        return refusal
    found = {key: value or "" for key, value in reference.items()}
    visible = True if user_visible is None else user_visible
    if action == "get":
        return _get_objective(**found)
    if action == "list":
        return _list_objectives(
            limit=limit or 20, statuses=statuses or "", horizons=horizons or "", include_closed=include_closed
        )
    if action == "update_progress":
        return _update_progress(
            **found,
            progress_summary=progress_summary or "",
            current_focus=current_focus or "",
            criterion_updates_json=criterion_updates_json or "",
            user_visible=visible,
        )
    return _update_status(
        str(status),
        **found,
        summary=summary or "",
        user_confirmation_summary=user_confirmation_summary or "",
        satisfied_by_current_task=satisfied_by_current_task,
        user_visible=visible,
    )






PROVIDER = ToolProvider(
    name="objectives",
    tools=(
        objectives,
        objective_work_sessions,
        objective_work_profiles,
        propose_objective_work_profile,
        record_objective_work_checkpoint,
        create_objective,
        start_objective_task,
        activate_plan_execution,
        start_isolated_recovery,
    ),
)
