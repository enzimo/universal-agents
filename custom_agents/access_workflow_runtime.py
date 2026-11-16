"""Keep a task open while its published access workflow has unfinished steps.

Runs in the TaskManager operations lane when a top-level task gives a final
answer (``turn_action_outbox_runtime``). A task whose open workflow still has
unfinished steps is not allowed to report success:

- The first final answer for each distinct saved state (steps and request
  outcomes) is held back and not sent. The task is queued at the front and
  runs again at once with a continuation note (``WORKFLOW_CONTINUATION_NOTE``)
  to finish or report the steps; guidance the person sends meanwhile is read
  on that turn. The end-of-turn handling keeps the queued state
  (``task_message_runtime._keep_held_answer_state``). It used to mark the
  task WAITING instead; on 2026-11-14 that dropped a held task from the
  queue, so the task stopped for good and nothing told the person.
- A repeated final answer without progress raises
  ``WorkflowCompletionRefused``, a ``TurnActionRefused``: the outbox does not
  ask the model to recover (that ran a new model turn on every repeat), the
  task fails, and the person is told in plain words, followed by the agent's
  last reply, labelled as given before the open steps were finished so it is
  never read as a completed result. Administrators' notices never quote it.

A short preview of each held answer stays in the task's history
(``attempted_answer_preview``), so a held answer can be recovered.

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
from custom_agents.tool_call_limits import compact_debug_preview
from custom_agents.turn_action_refusal import TurnActionRefused
from custom_messages.contracts.access_v2 import contract_digest
from custom_messages.contracts.access_workflow_v2 import CLOSED_STEP_STATES, WorkflowEventV2, utc_now
from custom_messages.enums import ProgressState, TaskHistoryImportance, TaskHistoryType
from storage.access_workflow_repository import AccessWorkflowRepository

logger = logging.getLogger(__name__)
CLOSED_STATE_VALUES = frozenset(state.value for state in CLOSED_STEP_STATES)
# How the note a held task's next turn reads begins (``_continue``).
WORKFLOW_CONTINUATION_NOTE = "Continue the published access workflow in this same task."
# The agent's last reply follows this label in the refused task's message (lead's decision, 2026-11-16).
LAST_REPLY_LABEL = (
    "The agent's last reply is below. It was given before the plan's open steps were finished, "
    "so it is not a completed result:"
)
# Characters of a held answer kept in the task's history.
ANSWER_PREVIEW_CHARS = 600


class WorkflowCompletionRefused(TurnActionRefused):
    """A task tried to finish again without progress on its workflow's unfinished steps."""

    def __init__(self, *, workflow_id: str, task_id: str, title: str, attempted_answer: str = "") -> None:
        self.workflow_id = workflow_id
        self.task_id = task_id
        self.attempted_answer = attempted_answer
        super().__init__(
            f'This task was stopped because its plan "{title}" still has unfinished steps and it tried to '
            "finish again without making progress on them. Results it already finished are kept. Reply here "
            "to go over the remaining work with the team lead, and check uncertain results before trying again."
        )

    @property
    def requester_message(self) -> str:
        """The plain statement, then the agent's last reply, quoted and labelled (module docstring)."""

        reply = self.attempted_answer.strip()
        if not reply:
            return str(self)
        quoted = "\n".join(f"> {line}" if line else ">" for line in reply.splitlines())
        return f"{self}\n\n{LAST_REPLY_LABEL}\n\n{quoted}"


def _service(manager: Any) -> AccessWorkflowService:
    return AccessWorkflowService(AccessWorkflowRepository(manager.storage_settings))


def _continue(manager: Any, record: Any, evidence: list[dict[str, Any]]) -> None:
    # Restart recovery may already have left the task a note its next turn has
    # not read: startup records the restart before the scheduler processes a
    # final answer saved before it. Add to that note; never replace it
    # (``access_request_runtime`` does the same when a request is decided).
    manager.append_runtime_resume_directive(
        record.source_task_id,
        f"{WORKFLOW_CONTINUATION_NOTE} Keep finished results. Read "
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


def _refuse_completion(
    manager: Any, service: AccessWorkflowService, record: Any, evidence: list[dict[str, Any]], attempted_answer: str
) -> WorkflowCompletionRefused:
    """Record a repeated final answer without progress, and the refusal that ends its task."""

    unfinished = [item["step_id"] for item in evidence if item["state"] not in CLOSED_STATE_VALUES]
    service.repository.append_event(
        WorkflowEventV2(
            workflow_id=record.workflow_id,
            kind="error",
            code="source_task_completion_rejected",
            message="The agent tried to finish again without resolving the open workflow steps.",
        )
    )
    manager.append_history_entry(
        record.source_task_id,
        "The task tried to finish again without progress on its workflow's unfinished steps; it is stopped.",
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": "workflow_completion_refused",
            "workflow_id": record.workflow_id,
            "unfinished_step_ids": unfinished,
            "attempted_answer_preview": compact_debug_preview(attempted_answer, max_chars=ANSWER_PREVIEW_CHARS),
        },
    )
    logger.warning(
        "A task tried to finish again without progress on its workflow; it is stopped. "
        "event=workflow_completion_refused workflow_id=%s task_id=%s unfinished_step_ids=%s",
        record.workflow_id,
        record.source_task_id,
        unfinished,
    )
    return WorkflowCompletionRefused(
        workflow_id=record.workflow_id,
        task_id=record.source_task_id,
        title=record.title,
        attempted_answer=attempted_answer,
    )


def _reconcile_open_work(
    manager: Any, service: AccessWorkflowService, record: Any, evidence: list[dict[str, Any]], attempted_answer: str
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
        raise _refuse_completion(manager, service, record, evidence, attempted_answer)
    manager.append_history_entry(
        task_id,
        "The published workflow has unfinished steps; continue it before finishing the task.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": "workflow_completion_deferred",
            "workflow_id": record.workflow_id,
            "workflow_reconciliation_signature": signature,
            "attempted_answer_preview": compact_debug_preview(attempted_answer, max_chars=ANSWER_PREVIEW_CHARS),
        },
    )
    _continue(manager, record, evidence)


def defer_workflow_completion(manager: Any, *, task_id: str, team_id: str, attempted_answer: str = "") -> bool:
    """Return true when a premature final answer must be held back (see the module docstring).

    Raises ``WorkflowCompletionRefused`` for a repeated answer without progress.
    """

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
        _reconcile_open_work(manager, service, record, evidence, attempted_answer)
        service.repository.append_event(
            WorkflowEventV2(
                workflow_id=record.workflow_id,
                kind="progress",
                code="source_task_reconciliation_requested",
                message="The task tried to finish with unfinished steps. It runs again at once to finish or report them.",
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


__all__ = [
    "LAST_REPLY_LABEL",
    "WORKFLOW_CONTINUATION_NOTE",
    "WorkflowCompletionRefused",
    "defer_workflow_completion",
]
