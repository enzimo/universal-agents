"""The access workflow tools: the team lead's plan for a goal, shown to its person.

These tools coordinate and grant nothing, so they are part of the team lead's
standard tool set (``team_lead_runtime``). A workflow is a view over access
requests (``custom_agents.access_workflows``): each step names the kinds of
change and the tools it needs, requests made while it runs are shown on it,
and people decide every request in the Inbox as usual.

``request_access_workflow_approvals`` asks a person to approve several staged
changes together: one change request per change (bound to that change's
exact plan), then one pause until every one is decided. Afterwards the agent
applies each approved change with ``apply_change``, which finds the approval
by the change id. With authorization off there are no requests, so that tool
is not given to agents (``custom_agents.team_config_helpers``); publishing
and following a plan still works.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_control.decisions import HandDownRefused
from access_control.identity import IdentityRejected
from custom_agents.access_workflows import (
    AccessWorkflowService,
    WorkflowRequester,
    review_label,
    workflow_service_for,
)
from custom_messages.contracts.access_v2 import AccessRequestStatus, ChangeProposalV1
from custom_messages.contracts.access_workflow_v2 import (
    OPEN_STEP_STATES,
    AccessWorkflowV2,
    WorkflowProgressV2,
    WorkflowProposalV2,
)
from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_ID_KEY,
    ACCESS_REQUEST_IDS_KEY,
    ACCESS_REQUEST_INTERRUPT_KIND,
)
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.access import ACCESS_REQUEST_INTERRUPT_NAME
from custom_tools.runtime_context import active_task_context, get_agent_ref
from storage.access_workflow_repository import WorkflowConflict

logger = logging.getLogger(__name__)

MAX_BATCH_CHANGES = 20
STATUS_REFUSED = "refused"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _review_url(workflow_id: str) -> str:
    return f"/workflows/{workflow_id}"


def _agent() -> Any:
    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    return agent


def _service(agent: Any) -> AccessWorkflowService:
    """The agent's workflow service with the configured limits (loaded once)."""

    service = workflow_service_for(agent)
    if not getattr(agent, "_access_workflow_limits_loaded", False):
        from assistant_gateway.config import GatewayInstanceConfig

        settings = GatewayInstanceConfig.load_for_process(str(agent.instance_config_path)).access_workflows
        service.max_steps = settings.max_steps
        service.diagnostic_limit = settings.diagnostic_event_limit
        agent._access_workflow_limits_loaded = True
    return service


def _requester(agent: Any) -> tuple[WorkflowRequester, str]:
    """The publishing agent and its task, from the task's verified signed context."""

    from custom_agents.access_runtime import acting_agent, verified_context_id

    acting = acting_agent(agent)
    if acting.person is None or not acting.task_id:
        raise PermissionError("A workflow is shown to the person a task works for; this task works for nobody.")
    requester = WorkflowRequester(
        agent_id=acting.agent.agent_id,
        agent_type=acting.agent.agent_type,
        team_id=acting.agent.team_id,
        person_id=acting.person.person_id,
        context_id=verified_context_id(agent),
    )
    return requester, acting.task_id


def _catalog(agent: Any) -> Any:
    from custom_agents.access_runtime import runtime_access

    return runtime_access(agent).catalog


def _record_view(service: AccessWorkflowService, record: AccessWorkflowV2) -> dict[str, Any]:
    evidence = service.evidence(record)
    return {
        **record.model_dump(mode="json"),
        "review_url": _review_url(record.workflow_id),
        "steps_now": [{**step, "status": review_label(step)} for step in evidence],
        "requests_not_on_a_step": service.unassigned_evidence(record),
    }


def _refused(exc: Exception, workflow_id: str | None = None) -> str:
    payload: dict[str, Any] = {"status": STATUS_REFUSED, "reason": str(exc)}
    if workflow_id:
        payload["review_url"] = _review_url(workflow_id)
    return _json(payload)


GUARDED_ERRORS = (HandDownRefused, IdentityRejected, PermissionError, RuntimeError, ValueError, WorkflowConflict)


@tool
def submit_access_workflow(proposal: dict[str, Any], idempotency_key: str) -> str:
    """Publish the complete goal as a dependency map before asking for any access.

    proposal has title, goal, target_agent_type and steps. Each step has
    step_id, title, reason, kind (account/capability/setup/work/review),
    success_criterion, depends_on (step ids), optional, and what it needs:
    change_kinds (kinds of staged change it applies with apply_change, such as
    agent_definition, runtime_restart, team_skill_install, or access_proposal
    for a propose_access_change or mcp_connections(action="propose") review) and tool_access
    ([{tool_id, level}], such as {"tool_id": "mcp:crawl4ai", "level": "write"};
    find_tools lists tools). Account steps need neither: the person links the
    account in the broker's form. Include the work itself and a final check;
    do not stop at setup. No secrets, credential keys or links with tokens.

    Publishing grants nothing. Mark a step running before asking for what it
    needs; requests made then are shown on it. Link the returned review_url.
    """

    try:
        agent = _agent()
        requester, task_id = _requester(agent)
        _task, meta = active_task_context(agent)
        gateway = getattr(meta, "gateway_context", None)
        record = _service(agent).submit(
            WorkflowProposalV2.model_validate(proposal),
            requester=requester,
            task_id=task_id,
            idempotency_key=idempotency_key,
            catalog=_catalog(agent),
            conversation_id=getattr(meta, "conversation_id", None),
            correlation_id=getattr(meta, "msg_correlation_id", None),
            session_key=getattr(gateway, "session_key", None),
        )
        return _json(
            {
                "workflow_id": record.workflow_id,
                "review_url": _review_url(record.workflow_id),
                "workflow": record.model_dump(mode="json"),
                "instructions": (
                    "Link the returned review_url in chat; it is the workflow page (a /docs/skills/ address is "
                    "documentation, not a workflow). Keep this task open until the workflow is complete or a "
                    "failure is reported. Mark each step running before asking for what it needs; requests "
                    "made then appear on it. Stage several changes, then ask for them together with "
                    "request_access_workflow_approvals. Never ask again for something the person denied. "
                    "Report failures with a recovery step, and check uncertain results before retrying."
                ),
            }
        )
    except GUARDED_ERRORS as exc:
        return _refused(exc)


@tool
def revise_access_workflow_step(
    workflow_id: str, step_id: str, change_kinds: list[str], tool_access: list[dict[str, Any]], reason: str
) -> str:
    """Correct what an unfinished step needs, before any request is linked to it.

    change_kinds and tool_access replace the step's lists (see
    submit_access_workflow). Use it when a step named the wrong kind of
    change, or a tool that exists only after an earlier step installed it.
    Keeps the workflow, its dependencies, results and task. Cannot change a
    finished, denied or uncertain step, or a step with linked requests.
    """

    try:
        agent = _agent()
        requester, task_id = _requester(agent)
        service = _service(agent)
        record = service.require_agent(workflow_id, requester=requester, task_id=task_id)
        updated = service.revise_step(
            record,
            step_id=step_id,
            change_kinds=list(change_kinds or []),
            tool_access=list(tool_access or []),
            reason=reason,
            catalog=_catalog(agent),
        )
        return _json({**updated.model_dump(mode="json"), "review_url": _review_url(workflow_id), "access_changed": False})
    except GUARDED_ERRORS as exc:
        return _refused(exc, workflow_id)


@tool
def update_access_workflow(workflow_id: str, progress: dict[str, Any]) -> str:
    """Report a step's progress after checking the actual results.

    progress has step_id, state (running/waiting/complete/omitted/failed/
    outcome_unknown), summary and recovery. Failed or uncertain steps need a
    recovery step. A step is complete only after its requests were approved
    and its changes applied. No secrets or private work content in summary.
    Prerequisites finish first. Never retry an uncertain external effect.
    """

    try:
        agent = _agent()
        requester, task_id = _requester(agent)
        service = _service(agent)
        record = service.require_agent(workflow_id, requester=requester, task_id=task_id)
        updated = service.progress(record, WorkflowProgressV2.model_validate(progress))
        return _json({**updated.model_dump(mode="json"), "review_url": _review_url(workflow_id)})
    except GUARDED_ERRORS as exc:
        return _refused(exc, workflow_id)


@tool
def get_access_workflow(workflow_id: str) -> str:
    """Read a workflow before continuing it: each step's state and its requests' current status."""

    try:
        agent = _agent()
        requester, task_id = _requester(agent)
        service = _service(agent)
        record = service.require_owner(workflow_id, owner_id=requester.person_id, team_id=requester.team_id)
        if record.requester_agent_type != requester.agent_type:
            raise PermissionError("Only the agent type that published this workflow may read it with this tool.")
        return _json(
            {
                **_record_view(service, record),
                "source_task_status": service.task_status(record),
                "can_update_from_current_task": record.source_task_id == task_id,
            }
        )
    except GUARDED_ERRORS as exc:
        return _refused(exc, workflow_id)


# -- asking for several changes together ---------------------------------------------


def _batch_result(service: AccessWorkflowService, record: AccessWorkflowV2, store: Any, entry: dict[str, Any]) -> dict[str, Any]:
    request_id = entry.get("request_id")
    if not request_id:
        return {**entry, "status": "may_apply_now", "message": "You may make this change: call apply_change for it."}
    request = store.get_request(request_id)
    status = request.status if request is not None else None
    messages = {
        AccessRequestStatus.GRANTED: "Approved. Call apply_change with this change_id to apply it.",
        AccessRequestStatus.DENIED: "Denied. Do not apply it or ask again; tell the person what will not happen.",
        AccessRequestStatus.WITHDRAWN: "Withdrawn; nothing was approved.",
        AccessRequestStatus.WAITING: "Still waiting for a person. Do not stage it again.",
    }
    if status is AccessRequestStatus.DENIED and any(request_id in step.request_ids for step in record.steps):
        service.record_denial(record.workflow_id, request_id)
    return {
        **entry,
        "status": status.value if status is not None else "unavailable",
        "note": (request.decision_note or None) if request is not None else None,
        "message": messages[status] if status is not None else "The request is no longer available.",
    }


def _check_batch_steps(service: AccessWorkflowService, record: AccessWorkflowV2, proposal: ChangeProposalV1) -> None:
    """Each change needs exactly one open step expecting its kind, and none the person denied."""

    expecting = [step for step in record.steps if proposal.kind in step.change_kinds]
    denied = service.closed_steps(record, expecting)
    usable = [step for step in expecting if step.state in OPEN_STEP_STATES and step not in denied]
    if denied and not usable:
        raise HandDownRefused(
            f"The person denied a request for the step \"{denied[0].title}\". Do not ask for "
            f"\"{proposal.title}\" again; tell the person what cannot happen."
        )
    if not usable:
        raise ValueError(
            f"No running step of this workflow expects {proposal.kind} (\"{proposal.title}\"). Mark its step running "
            "with update_access_workflow, or correct the step with revise_access_workflow_step."
        )
    if len(usable) > 1:
        names = ", ".join(step.step_id for step in usable)
        raise ValueError(f"Several running steps expect {proposal.kind} ({names}); run them one after another.")


@dataclass
class _Batch:
    """One call of the batch tool: the workflow, the described changes and who asks."""

    agent: Any
    requests: Any  # AccessRequests of the agent (with the workflow watcher)
    acting: Any  # custom_agents.access_runtime.ActingAgent
    service: AccessWorkflowService
    record: AccessWorkflowV2
    proposals: list[ChangeProposalV1]
    tool_use_id: str

    def call_id(self, change_id: str) -> str:
        """One tool-use key per change, so a resumed call finds each of its requests."""

        return f"{self.tool_use_id}:{change_id}"

    def made(self, change_id: str) -> Any:
        return self.requests.service.store.request_for_tool_use(
            team_id=self.requests.service.team_id,
            agent_id=self.acting.agent.agent_id,
            task_id=self.acting.task_id,
            tool_use_id=self.call_id(change_id),
        )


def _open_batch(workflow_id: str, change_ids: list[str], reason: str, tool_context: ToolContext) -> _Batch:
    from custom_agents.access_runtime import acting_agent, runtime_requests
    from custom_agents.change_approvals import change_adapter

    agent = _agent()
    requests = runtime_requests(agent)
    requester, task_id = _requester(agent)
    ids = list(dict.fromkeys(str(item or "").strip() for item in change_ids or [] if str(item or "").strip()))
    if not ids or len(ids) > MAX_BATCH_CHANGES:
        raise ValueError(f"List between 1 and {MAX_BATCH_CHANGES} change_id values that plan tools returned.")
    if not str(reason or "").strip():
        raise ValueError("Give the reason people will read.")
    tool_use_id = str(tool_context.tool_use.get("toolUseId") or "")
    if not tool_use_id:
        raise RuntimeError("This tool call has no id, so its requests cannot be found again.")
    service = _service(agent)
    record = service.require_agent(workflow_id, requester=requester, task_id=task_id)
    # Each service re-checks its plan before anyone is asked (not applied, unchanged).
    proposals = [change_adapter(change_id).describe(agent, change_id) for change_id in ids]
    return _Batch(agent, requests, acting_agent(agent), service, record, proposals, tool_use_id)


def _submit_changes(batch: _Batch, reason: str) -> list[dict[str, Any]]:
    """One change request per change (reused when this call already made it)."""

    made = {proposal.change_id: batch.made(proposal.change_id) for proposal in batch.proposals}
    for proposal in batch.proposals:
        if made[proposal.change_id] is None:
            _check_batch_steps(batch.service, batch.record, proposal)
    _task, meta = active_task_context(batch.agent)
    entries = []
    for proposal in batch.proposals:
        request = made[proposal.change_id] or batch.requests.submit_change(
            requester=batch.acting.agent,
            person=batch.acting.person,
            proposal=proposal,
            reason=str(reason).strip(),
            task_id=batch.acting.task_id,
            conversation_id=getattr(meta, "conversation_id", None),
            tool_use_id=batch.call_id(proposal.change_id),
        ).request
        entries.append(
            {
                "change_id": proposal.change_id,
                "kind": proposal.kind,
                "title": proposal.title,
                "request_id": request.request_id if request is not None else None,
            }
        )
    return entries


def _pause_until_decided(batch: _Batch, entries: list[dict[str, Any]], tool_context: ToolContext) -> None:
    """Pause once for every request that waits; the task resumes when all are decided."""

    store = batch.requests.service.store
    current = {entry["request_id"]: store.get_request(entry["request_id"]) for entry in entries if entry["request_id"]}
    waiting = [
        request_id
        for request_id, request in current.items()
        if request is not None and request.status is AccessRequestStatus.WAITING
    ]
    if not waiting:
        return
    logger.info(
        "A workflow's changes wait for a person together. event=workflow_batch_waiting workflow_id=%s request_ids=%s",
        batch.record.workflow_id,
        ",".join(waiting),
    )
    tool_context.interrupt(
        ACCESS_REQUEST_INTERRUPT_NAME,
        reason={
            "kind": ACCESS_REQUEST_INTERRUPT_KIND,
            ACCESS_REQUEST_ID_KEY: waiting[0],
            ACCESS_REQUEST_IDS_KEY: waiting,
            "summary": f"Waiting for a person to decide {len(waiting)} change(s) of the workflow "
            f"\"{batch.record.title}\".",
        },
    )


def _submit_batch(workflow_id: str, change_ids: list[str], reason: str, tool_context: ToolContext) -> dict[str, Any]:
    """The blocking part (plan checks, store, Cedar), run in a worker thread."""

    batch = _open_batch(workflow_id, change_ids, reason, tool_context)
    entries = _submit_changes(batch, reason)
    _pause_until_decided(batch, entries, tool_context)
    record = batch.service.repository.get(workflow_id) or batch.record
    store = batch.requests.service.store
    return {
        "workflow_id": workflow_id,
        "review_url": _review_url(workflow_id),
        "results": [_batch_result(batch.service, record, store, entry) for entry in entries],
        "next_step": (
            "Apply each approved change with apply_change(change_id, reason), check each result, and report the "
            "step's progress. Never apply or ask again for a denied change."
        ),
    }


@tool(context=True)
async def request_access_workflow_approvals(
    workflow_id: str, change_ids: list[str], reason: str, tool_context: ToolContext
) -> str:
    """Ask a person to approve several staged changes of a workflow together.

    change_ids are change_id values plan tools returned, each for a kind of
    change a running step of this workflow lists in change_kinds. One request
    per change goes to the Inbox, bound to that exact plan, and your task
    pauses once until a person decided all of them. Then call
    apply_change(change_id, reason) for each approved change; it finds the
    approval. Denied changes are reported: never apply or ask for them again.
    Changes that depend on a result that does not exist yet go in a later batch.
    """

    from custom_agents.access_runtime import AuthorizationOff

    try:
        outcome = await asyncio.to_thread(_submit_batch, workflow_id, change_ids, reason, tool_context)
    except InterruptException:
        raise
    except AuthorizationOff:
        return _json(
            {
                "status": "not_needed",
                "reason": "Authorization is off: explain each change to the person, and after they agree apply it "
                "with apply_change(change_id, reason, confirmed=true).",
            }
        )
    except (*GUARDED_ERRORS, FileExistsError, FileNotFoundError) as exc:
        return _refused(exc, workflow_id)
    return _json(outcome)


PROVIDER = ToolProvider(
    name="access_workflows",
    tools=(
        submit_access_workflow,
        update_access_workflow,
        revise_access_workflow_step,
        get_access_workflow,
        request_access_workflow_approvals,
    ),
)

__all__ = [
    "PROVIDER",
    "get_access_workflow",
    "request_access_workflow_approvals",
    "revise_access_workflow_step",
    "submit_access_workflow",
    "update_access_workflow",
]
