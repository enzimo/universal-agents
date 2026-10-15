"""Keep a task open while its published access workflow has unfinished steps.

Runs in the TaskManager operations lane when a top-level task gives a final
answer (``turn_action_outbox_runtime``). A task whose open workflow still has
unfinished steps is not allowed to report success: it gets one continuation
turn per distinct saved state to finish or report the steps, and a repeated
final answer without progress becomes an explicit failure instead of an
endless loop or a false success.

Waiting for people is not handled here. Every access or change request pauses
its own task with a local Strands interrupt until a person decides
(``custom_agents.access_request_runtime``), so a workflow never parks a task
itself. A request still waiting when the task tries to finish is unexpected;
it is logged and included in the continuation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from custom_agents.access_workflows import AccessWorkflowService
from custom_messages.contracts.access_v2 import contract_digest
from custom_messages.contracts.access_workflow_v2 import CLOSED_STEP_STATES, WorkflowEventV2, utc_now
from custom_messages.enums import ProgressState, TaskHistoryImportance, TaskHistoryType
from storage.access_workflow_repository import AccessWorkflowRepository

logger = logging.getLogger(__name__)
CLOSED_STATE_VALUES = frozenset(state.value for state in CLOSED_STEP_STATES)


def _service(manager: Any) -> AccessWorkflowService:
    return AccessWorkflowService(AccessWorkflowRepository(manager.storage_settings))


def _continue(manager: Any, record: Any, evidence: list[dict[str, Any]]) -> None:
    # Restart recovery may already have left the task a note its next turn has
    # not read: startup records the restart before the scheduler processes a
    # final answer saved before it. Add to that note; never replace it
    # (``access_request_runtime`` does the same when a request is decided).
    manager.append_runtime_resume_directive(
        record.source_task_id,
        "Continue the published access workflow in this same task. Keep finished results. Read "
        "get_access_workflow, check each request's current status, then do only what is allowed now and "
        "report each step. An approval is not completion: apply approved changes with apply_change and check "
        "the work. Never ask again for anything the person denied. If work cannot continue, report the step "
        "as failed or outcome_unknown with a concrete recovery step. Never repeat an uncertain external "
        "operation without checking its result. "
        + json.dumps(
            {"workflow_id": record.workflow_id, "review_url": f"/workflows/{record.workflow_id}", "steps": evidence},
            sort_keys=True,
            default=str,
        ),
    )
    manager.update_main_task_state(record.source_task_id, ProgressState.ACCEPTED, utc_now().isoformat())
    if not manager.enqueue_task(record.source_task_id, front=True):
        logger.warning(
            "The workflow's task was not queued to continue. event=workflow_resume_enqueue_rejected "
            "workflow_id=%s task_id=%s",
            record.workflow_id,
            record.source_task_id,
        )


def _reconcile_open_work(
    manager: Any, service: AccessWorkflowService, record: Any, evidence: list[dict[str, Any]]
) -> None:
    task_id = record.source_task_id
    signature = contract_digest(
        {"steps": [{"step_id": item["step_id"], "state": item["state"], "requests": item["requests"]} for item in evidence]}
    )
    info = manager.get_task_info(task_id)
    repeated = any(
        (entry.details or {}).get("workflow_reconciliation_signature") == signature
        for entry in info.task.execution_history or []
    )
    if repeated:
        service.repository.append_event(
            WorkflowEventV2(
                workflow_id=record.workflow_id,
                kind="error",
                code="source_task_completion_rejected",
                message="The agent tried to finish again without resolving the open workflow steps.",
            )
        )
        raise RuntimeError(
            f"Workflow {record.workflow_id} still has unfinished steps after reconciliation. "
            "The task cannot report success. Check the saved steps and request outcomes before retrying."
        )
    manager.append_history_entry(
        task_id,
        "The published workflow has unfinished steps; continue it before finishing the task.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": "workflow_completion_deferred",
            "workflow_id": record.workflow_id,
            "workflow_reconciliation_signature": signature,
        },
    )
    _continue(manager, record, evidence)


def defer_workflow_completion(manager: Any, *, task_id: str, team_id: str) -> bool:
    """Return true when a premature final answer must be held back (see the module docstring)."""

    service = _service(manager)
    for record in service.repository.for_task(task_id=task_id, team_id=team_id):
        if record.terminal or record.phase.value == "failed":
            continue
        evidence = service.evidence(record)
        waiting = [request_id for step in evidence for request_id in step["pending_request_ids"]]
        if waiting:
            logger.warning(
                "A task tried to finish while requests of its workflow still wait for a person. "
                "event=workflow_completion_with_waiting_requests workflow_id=%s task_id=%s request_ids=%s",
                record.workflow_id,
                task_id,
                ",".join(waiting),
            )
        _reconcile_open_work(manager, service, record, evidence)
        service.repository.append_event(
            WorkflowEventV2(
                workflow_id=record.workflow_id,
                kind="progress",
                code="source_task_reconciliation_requested",
                message="The task tried to finish with unfinished steps. It was kept open to finish or report them.",
            )
        )
        logger.warning(
            "A task's final answer was held back for its workflow. event=workflow_completion_deferred "
            "workflow_id=%s task_id=%s unfinished_step_ids=%s",
            record.workflow_id,
            task_id,
            [item["step_id"] for item in evidence if item["state"] not in CLOSED_STATE_VALUES],
        )
        return True
    return False


__all__ = ["defer_workflow_completion"]
