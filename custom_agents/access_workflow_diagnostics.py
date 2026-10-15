"""Allowlisted explanations for exported workflow and task lifecycle evidence."""

import json
from typing import Any


EVENT_EXPLANATIONS = {
    "plan_submitted": "The workflow was saved. Its steps have not necessarily run.",
    "request_waiting": "An access or change request was linked to this step and waits for a person.",
    "request_granted_at_once": "A request was linked to this step and granted at once from access the team lead may hand on.",
    "request_not_on_step": "A request from this task matched no single running step. It is still decided in the Inbox; the page lists it separately.",
    "step_expectations_corrected": "The publishing task corrected what an unfinished step without requests needs. Results and requests were kept; no access changed.",
    "agent_reported_running": "The agent reported that work on this step started.",
    "agent_reported_waiting": "The agent reported that this step is waiting.",
    "agent_reported_complete": "The agent reported the step complete after its requests were approved and its changes applied.",
    "agent_reported_omitted": "The agent left out this optional step.",
    "agent_reported_failed": "The agent reported that the step failed and gave a recovery step.",
    "setup_waiting": "A setup page (sign-in, account link, team sign-in app or outside step) was opened for this step and waits for the person.",
    "setup_completed": "The person finished a setup page of this step.",
    "setup_expired": "A setup page of this step expired; trusted code offers a fresh one, at most three.",
    "setup_declined": "The person closed a setup page of this step without finishing; it is not asked for again.",
    "setup_unavailable": "A setup page of this step could not be used; the step says why.",
    "setup_not_on_step": "A setup page matched no single step; the page lists it separately.",
    "access_requested": "Trusted code asked for the access this step needs, when a tool was being set up.",
    "access_approved": "Trusted code closed this step: every access it needed was approved or handed on.",
    "agent_reported_outcome_unknown": "The agent reported that the step's external result is uncertain; it must be checked before retrying.",
    "source_task_reconciliation_requested": "The task tried to finish with unfinished steps. It was kept open to finish or report them.",
    "source_task_completion_rejected": "The task tried to finish again without changing the unfinished steps. The runtime's action recovery handles the failure.",
    "step_denied": "The person denied a request for this step. The step is closed and will not be asked for again.",
    "decision_recorded": "A person decided a request from the workflow page.",
    "decision_not_confirmed": "A decision from the workflow page could not be confirmed; its request keeps its current status.",
}


def archived_task_evidence(task_json: str, *, limit: int) -> dict[str, Any]:
    """Export known system lifecycle codes and counts, never arbitrary details."""
    task = json.loads(task_json)
    history = task.get("execution_history") or []
    result = []
    for entry in history:
        item = _lifecycle_event(entry)
        if item is not None:
            result.append(item)
    return {
        "events": result[-limit:],
        "truncated": len(result) > limit,
        "scope": "Known system lifecycle events and counts only; omitted events cannot be inferred absent.",
    }


def _lifecycle_event(entry: dict[str, Any]) -> dict[str, Any] | None:
    details = entry.get("details") or {}
    code = str(
        details.get("action") or details.get("event") or details.get("reason") or ""
    )
    if entry.get("source", "system") != "system":
        return None
    explanation = {
        "enter_finalizing_plan": "A terminal task response was generated and local plan finalization began.",
        "complete_finalizing_plan": "The local task plan finished finalization. Its item counts do not describe the separately saved workflow steps.",
        "tool_result_budget_exceeded": "Oversized tool results were offloaded to protect model context. This event does not establish that an external operation failed.",
        "workflow_completion_deferred": "The task attempted to finish while workflow steps remained open.",
        "terminal_task_response_deferred": "The task attempted to finish with outstanding delegated work or a clarification question.",
    }.get(code)
    if explanation is None:
        return None
    item = {
        "timestamp": entry.get("timestamp"),
        "code": code,
        "explanation": explanation,
    }
    if code == "complete_finalizing_plan":
        item["local_plan_counts"] = {
            key.removesuffix("_ids") + "_count": len(details.get(key) or [])
            for key in (
                "open_plan_item_ids",
                "completed_plan_item_ids",
                "dropped_plan_item_ids",
                "open_unknown_ids",
            )
        }
    if code == "tool_result_budget_exceeded":
        item["offloaded_result_count"] = len(details.get("events") or [])
        item["offloaded_results"] = [
            {
                key: event[key]
                for key in (
                    "result_tokens",
                    "result_chars",
                    "max_tool_result_tokens_per_call",
                )
                if type(event.get(key)) is int and event[key] >= 0
            }
            for event in details.get("events") or []
        ]
    return item
