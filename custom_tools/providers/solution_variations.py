"""Advisory solution-variation tools for TeamArchitect."""

from __future__ import annotations

import json
from typing import Any

from strands import tool

from custom_messages.solution_variation import SolutionVariationStatus
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._runtime import _require_manager_tool
from custom_tools.runtime_context import active_task_context, get_agent_ref


def _json_list(raw: str, *, field_name: str) -> list[Any]:
    if not str(raw or "").strip():
        return []
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must decode to a JSON list.")
    return value


def _current_task_id(agent: Any) -> str:
    task_id, _ = active_task_context(agent)
    if not task_id:
        raise ValueError("No task currently in progress.")
    return task_id


def _manager_agent(tool_name: str):
    agent = get_agent_ref()
    if agent is None:
        return None, {"error": "Agent reference not set"}
    manager_error = _require_manager_tool(agent, tool_name)
    if manager_error:
        return None, {"error": manager_error, "manager_only": True}
    return agent, None


def _bundle_payload(bundle: Any) -> dict[str, Any]:
    return {
        "run": bundle.run.model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in bundle.candidates],
        "evaluations": [item.model_dump(mode="json") for item in bundle.evaluations],
        "evaluation_authority": "agent_authored_advisory",
        "artifact_application_tracked": False,
    }


@tool
def start_solution_variation(
    purpose: str,
    evaluation_criteria_json: str,
    constraints_json: str = "",
    candidate_limit: int = 3,
    resource_budget: str = "",
    stop_condition: str = "",
) -> str:
    """Optionally open a bounded comparison when materially different solutions merit testing.

    Use this at TeamArchitect's discretion. Skip it when the direct path is clear
    or comparison cost exceeds likely value. This records reasoning lineage only;
    it does not create isolated workspaces, execute candidates, or grant authority
    for their effects.
    """
    agent, error = _manager_agent("start_solution_variation")
    if error:
        return json.dumps(error)
    try:
        run, created = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.start,
            _current_task_id(agent),
            purpose=purpose,
            evaluation_criteria=_json_list(
                evaluation_criteria_json,
                field_name="evaluation_criteria_json",
            ),
            constraints=_json_list(
                constraints_json,
                field_name="constraints_json",
            ),
            candidate_limit=candidate_limit,
            resource_budget=resource_budget or None,
            stop_condition=stop_condition or None,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        return json.dumps(
            {
                "created": created,
                "run": run.model_dump(mode="json"),
                "required": False,
                "workspace_isolation_created": False,
            },
            default=str,
        )
    except (ValueError, PermissionError, RuntimeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def record_solution_candidate(
    candidate_key: str,
    title: str,
    hypothesis: str,
    approach: str,
    artifact_refs_json: str = "",
    parent_candidate_key: str = "",
) -> str:
    """Record one bounded candidate hypothesis in the current optional comparison.

    Artifact references are pointers only. Create or inspect artifacts through the
    normal authorized tools. In a shared mutable workspace, compare before editing
    or use a genuinely isolated facility; this tool itself provides no isolation.
    """
    agent, error = _manager_agent("record_solution_candidate")
    if error:
        return json.dumps(error)
    try:
        candidate, created = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.record_candidate,
            _current_task_id(agent),
            candidate_key=candidate_key,
            title=title,
            hypothesis=hypothesis,
            approach=approach,
            artifact_refs=_json_list(
                artifact_refs_json,
                field_name="artifact_refs_json",
            ),
            parent_candidate_key=parent_candidate_key or None,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        return json.dumps(
            {
                "created": created,
                "candidate": candidate.model_dump(mode="json"),
                "artifact_refs_are_advisory": True,
            },
            default=str,
        )
    except (ValueError, PermissionError, RuntimeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def record_solution_evaluation(
    candidate_key: str,
    evaluation_key: str,
    outcome: str,
    summary: str,
    hard_constraints_passed: bool,
    metrics_json: str = "",
    evidence_refs_json: str = "",
    limitations: str = "",
) -> str:
    """Record an agent-authored evaluation for a candidate.

    outcome is supports, rejects, or inconclusive. Metrics and evidence references
    help compare candidates but are not runtime-verified evidence. Re-verify the
    integrated result through the ordinary workflow after choosing a candidate.
    """
    agent, error = _manager_agent("record_solution_evaluation")
    if error:
        return json.dumps(error)
    try:
        evaluation, created = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.record_evaluation,
            _current_task_id(agent),
            candidate_key=candidate_key,
            evaluation_key=evaluation_key,
            outcome=outcome,
            summary=summary,
            hard_constraints_passed=hard_constraints_passed,
            metrics=_json_list(metrics_json, field_name="metrics_json"),
            evidence_refs=_json_list(
                evidence_refs_json,
                field_name="evidence_refs_json",
            ),
            limitations=limitations or None,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        return json.dumps(
            {
                "created": created,
                "evaluation": evaluation.model_dump(mode="json"),
                "evaluation_authority": "agent_authored_advisory",
            },
            default=str,
        )
    except (ValueError, PermissionError, RuntimeError, json.JSONDecodeError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def inspect_solution_variation(include_closed: bool = False) -> str:
    """Inspect the active, or most recent, solution variation for the current task or Plan."""
    agent, error = _manager_agent("inspect_solution_variation")
    if error:
        return json.dumps(error)
    try:
        bundle = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.inspect,
            _current_task_id(agent),
            include_closed=include_closed,
        )
        return json.dumps(_bundle_payload(bundle), default=str)
    except (ValueError, PermissionError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def select_solution_candidate(
    candidate_key: str,
    evaluation_key: str,
    rationale: str,
) -> str:
    """Record a preferred candidate, citing one supporting evaluation.

    Selection is a recommendation only. It does not apply artifacts, mutate Task,
    Plan, or Objective status, or authorize any external effect.
    """
    agent, error = _manager_agent("select_solution_candidate")
    if error:
        return json.dumps(error)
    try:
        run, selected = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.select,
            _current_task_id(agent),
            candidate_key=candidate_key,
            evaluation_key=evaluation_key,
            rationale=rationale,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        return json.dumps(
            {
                "selection_recorded": selected,
                "run": run.model_dump(mode="json"),
                "artifact_applied": False,
                "plan_status_changed": False,
                "objective_status_changed": False,
                "authority_granted": False,
            },
            default=str,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def close_solution_variation(
    outcome: str,
    summary: str,
) -> str:
    """Close the current comparison as completed, no_selection, abandoned, or superseded.

    Closing is bookkeeping only and never completes the Task, Plan, or Objective.
    Use completed only after applying and verifying the selected route through the
    normal authorized workflow.
    """
    agent, error = _manager_agent("close_solution_variation")
    if error:
        return json.dumps(error)
    try:
        parsed_outcome = SolutionVariationStatus(str(outcome).strip().lower())
        run, closed = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.close,
            _current_task_id(agent),
            outcome=parsed_outcome,
            summary=summary,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        bundle = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.solution_variation_coordinator.inspect,
            _current_task_id(agent),
            include_closed=True,
        )
        return json.dumps(
            {
                "closed": closed,
                **_bundle_payload(bundle),
                "task_status_changed": False,
                "plan_status_changed": False,
                "objective_status_changed": False,
            },
            default=str,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        return json.dumps({"error": str(exc)})


PROVIDER = ToolProvider(
    name="solution_variations",
    tools=(
        start_solution_variation,
        record_solution_candidate,
        record_solution_evaluation,
        inspect_solution_variation,
        select_solution_candidate,
        close_solution_variation,
    ),
)
