"""``apply_change``: apply a staged change once it is approved (access_control.changes).

Plan tools stage exact changes and return a ``change_id``. This tool:

- with authorization on, applies at once when the agent and its person
  already hold the change's level (team-owned or administrator-only changes,
  such as team skills and model choice); otherwise it
  stores a change request, pauses the task with the same local interrupt as
  ``request_access``, and, after a person approves the exact plan, claims the
  approval once and applies the plan in this process;
- with authorization off, applies when the person agreed in conversation
  (``confirmed=true``).

Resumed calls find their request by the tool call's id, as ``request_access``
does; a request made by another call for the same exact plan (a workflow's
batch of approvals, ``request_access_workflow_approvals``) is found by its
change id, so the batch's approval is applied and never asked for twice. Each
service checks that its plan can still apply (not applied yet,
sources unchanged) before a person is asked and again when applying; a plan
that changed or went stale is refused, and the agent stages it again.

The person's note from the Inbox (``decision_note``) is returned with every
decided outcome: applied, denied, or refused because the approved plan could
no longer apply. A restart ends this process before its result is read, so
the restart service also keeps the note in its receipt for the resumed task
(``custom_agents.runtime_restart_service``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_control.changes import ChangeAdapter, ChangeApproval
from access_control.decisions import HandDownRefused
from access_control.identity import IdentityRejected
from access_control.requests import AccessRequests
from custom_agents.change_followups import after_saved_change
from custom_messages.contracts.access_v2 import (
    AccessRequestStatus,
    AccessRequestV1,
    ChangeApprovalMode,
    ChangeProposalV1,
)
from custom_messages.contracts.task_v1 import ACCESS_REQUEST_INTERRUPT_KIND
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.access import ACCESS_REQUEST_INTERRUPT_NAME
from custom_tools.runtime_context import active_task_context, get_agent_ref
from custom_tools.tool_results import failed

logger = logging.getLogger(__name__)


# Refusals reported to the agent as ``status: refused`` rather than raised.
_REFUSALS: tuple[type[Exception], ...] = (
    HandDownRefused,
    IdentityRejected,
    FileExistsError,  # already applied
    FileNotFoundError,  # no such plan
    PermissionError,
    RuntimeError,
    ValueError,
)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


@dataclass
class _Applied:
    """A change trusted code applied; the adapter's follow-up step runs next."""

    agent: Any
    adapter: ChangeAdapter
    approval: ChangeApproval
    result: dict[str, Any]


def _applied(result: dict[str, Any], approval: ChangeApproval) -> str | dict[str, Any]:
    payload = {
        "status": "failed" if result.get("error") else "applied",
        "approval": approval.record(),
        "note": approval.note or None,
        "result": result,
    }
    return failed(payload) if result.get("error") else _json(payload)


def _agreement_needed(proposal: ChangeProposalV1) -> dict[str, Any]:
    return {
        "status": "agreement_required",
        "title": proposal.title,
        "effect": list(proposal.effect),
        "message": (
            "Authorization is off. Explain this change to the person and get their agreement "
            "in the conversation, then call apply_change again with confirmed=true."
        ),
    }


def _request_for_change(
    requests: AccessRequests, agent_id: str, task_id: str | None, proposal: ChangeProposalV1
) -> AccessRequestV1 | None:
    """A request another call of this agent and task made for this exact plan.

    A workflow's batch of approvals (``request_access_workflow_approvals``)
    submits one change request per change; the agent then applies each with
    ``apply_change``. The latest request for the same change id and plan
    digest counts when it still waits, is approved and not yet applied, or
    was denied (reported, never asked again). A withdrawn or already used one
    does not, and a new request is made.
    """

    if not task_id:
        return None
    candidates = [
        request
        for request in requests.service.store.change_requests_for_task(
            team_id=requests.service.team_id, agent_id=agent_id, task_id=task_id, change_id=proposal.change_id
        )
        if request.change is not None and request.change.plan_digest == proposal.plan_digest
    ]
    latest = candidates[-1] if candidates else None
    if latest is None or latest.status is AccessRequestStatus.WITHDRAWN:
        return None
    if latest.status is AccessRequestStatus.GRANTED and latest.applied_at_utc is not None:
        return None
    logger.info(
        "Found the request for this change made by another call. event=change_request_found_by_change_id "
        "request_id=%s change_id=%s status=%s",
        latest.request_id,
        proposal.change_id,
        latest.status.value,
    )
    return latest


def _waiting_request(
    agent: Any, requests: AccessRequests, proposal: ChangeProposalV1, reason: str, tool_context: ToolContext
) -> AccessRequestV1 | ChangeApproval:
    """The change request for this tool call, or an approval when access is already held.

    Found by the tool call's id first (a resumed call), then by the change id
    (a request a workflow's batch made); otherwise a new request is made.
    """

    from custom_agents.access_runtime import runtime_access, task_access

    principal, person, task_id = task_access(agent, runtime_access(agent))
    tool_use_id = str(tool_context.tool_use.get("toolUseId") or "") or None
    if tool_use_id:
        existing = requests.service.store.request_for_tool_use(
            team_id=requests.service.team_id, agent_id=principal.agent_id, task_id=task_id, tool_use_id=tool_use_id
        )
        if existing is not None:
            return existing
    found = _request_for_change(requests, principal.agent_id, task_id, proposal)
    if found is not None:
        return found
    _task, meta = active_task_context(agent)
    outcome = requests.submit_change(
        requester=principal,
        person=person,
        proposal=proposal,
        reason=reason,
        task_id=task_id,
        conversation_id=getattr(meta, "conversation_id", None),
        tool_use_id=tool_use_id,
    )
    if outcome.already_held or outcome.request is None:
        return ChangeApproval(ChangeApprovalMode.HELD_ACCESS, principal.agent_type, datetime.now(timezone.utc))
    return outcome.request


def _apply_approved(
    agent: Any, adapter: ChangeAdapter, requests: AccessRequests, request: AccessRequestV1, proposal: ChangeProposalV1
) -> dict[str, Any] | _Applied:
    change = request.change
    assert change is not None
    note = request.decision_note or None
    if change.change_id != proposal.change_id or change.plan_digest != proposal.plan_digest:
        return {
            "status": "refused",
            "reason": "The plan changed after it was approved. Stage it again and call apply_change for the new change_id.",
            "note": note,
        }
    claimed = requests.claim_change(request.request_id)
    approval = ChangeApproval(
        ChangeApprovalMode.PERSON_APPROVAL,
        claimed.decided_by.id if claimed.decided_by else "unknown",
        claimed.decided_at_utc or datetime.now(timezone.utc),
        request_id=claimed.request_id,
        note=claimed.decision_note,
    )
    try:
        result = adapter.apply(agent, proposal.change_id, approval)
    except Exception as exc:
        requests.service.store.record_change_error(request.request_id, str(exc))
        logger.exception(
            "An approved change failed to apply. event=change_apply_failed request_id=%s change_id=%s",
            request.request_id,
            proposal.change_id,
        )
        if isinstance(exc, _REFUSALS):
            return {"status": "refused", "reason": str(exc), "note": note}
        raise
    if result.get("error"):
        requests.service.store.record_change_error(request.request_id, str(result["error"]))
    return _Applied(agent, adapter, approval, result)


def _decided(request: AccessRequestV1) -> dict[str, Any]:
    if request.status is AccessRequestStatus.DENIED:
        message = "The person denied this change. Do not apply it; tell them what will not happen."
    elif request.status is AccessRequestStatus.WITHDRAWN:
        message = "The request was withdrawn."
    else:
        message = "Still waiting for a person to decide. Do not stage the change again."
    return {
        "status": request.status.value,
        "request_id": request.request_id,
        "note": request.decision_note or None,
        "message": message,
    }


def _close_stale_request(agent: Any, tool_context: ToolContext, change_id: str, why: str) -> str | None:
    """A person approved this call's change (or its batch's), but it can no longer apply.

    Uses up the approval and records why, as a failure while applying does,
    so the request shows it was not applied and the approval cannot be reused.
    Never hides the original refusal. Returns the approver's note, if any.
    """

    from custom_agents.access_runtime import agent_principal, runtime_access, runtime_requests, task_person

    tool_use_id = str(tool_context.tool_use.get("toolUseId") or "")
    try:
        access = runtime_access(agent)
        if not access.enabled:
            return None
        requests = runtime_requests(agent)
        _person, task_id = task_person(agent, access)
        agent_id = agent_principal(agent).agent_id
        request = (
            requests.service.store.request_for_tool_use(
                team_id=requests.service.team_id, agent_id=agent_id, task_id=task_id, tool_use_id=tool_use_id
            )
            if tool_use_id
            else None
        )
        if request is None and task_id:
            batch = requests.service.store.change_requests_for_task(
                team_id=requests.service.team_id, agent_id=agent_id, task_id=task_id, change_id=change_id
            )
            request = batch[-1] if batch else None
        if request is None or request.status is not AccessRequestStatus.GRANTED or request.applied_at_utc is not None:
            return None
        requests.claim_change(request.request_id)
        requests.service.store.record_change_error(request.request_id, why)
        logger.warning(
            "An approved change could no longer apply. event=change_stale_after_approval request_id=%s reason=%s",
            request.request_id,
            why,
        )
        return request.decision_note or None
    except Exception:
        logger.exception("Could not record why an approved change did not apply. event=change_stale_record_failed")
        return None


def _decide_and_apply(
    change_id: str, reason: str, tool_context: ToolContext, confirmed: bool
) -> dict[str, Any] | _Applied:
    """The blocking part (plan checks, store, Cedar), run in a worker thread."""

    from custom_agents.access_runtime import runtime_access, runtime_requests
    from custom_agents.change_approvals import change_adapter

    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    adapter = change_adapter(change_id)
    try:
        proposal = adapter.describe(agent, change_id)
    except (FileExistsError, FileNotFoundError, PermissionError, ValueError) as exc:
        note = _close_stale_request(agent, tool_context, change_id, str(exc))
        if note is None:
            raise
        return {"status": "refused", "reason": str(exc), "note": note}
    if not runtime_access(agent).enabled:
        if proposal.agreement_required and not confirmed:
            return _agreement_needed(proposal)
        approval = ChangeApproval(ChangeApprovalMode.AGREEMENT, "agreement", datetime.now(timezone.utc))
        return _Applied(agent, adapter, approval, adapter.apply(agent, change_id, approval))
    requests = runtime_requests(agent)
    found = _waiting_request(agent, requests, proposal, str(reason or "").strip() or proposal.title, tool_context)
    if isinstance(found, ChangeApproval):
        return _Applied(agent, adapter, found, adapter.apply(agent, change_id, found))
    request = found
    if request.status is AccessRequestStatus.WAITING:
        tool_context.interrupt(
            ACCESS_REQUEST_INTERRUPT_NAME,
            reason={
                "kind": ACCESS_REQUEST_INTERRUPT_KIND,
                "access_request_id": request.request_id,
                "summary": f"Waiting for a person to approve: {proposal.title}.",
            },
        )
        request = requests.service.store.get_request(request.request_id) or request
    if request.status is AccessRequestStatus.GRANTED:
        return _apply_approved(agent, adapter, requests, request, proposal)
    return _decided(request)


@tool(context=True)
async def apply_change(
    change_id: str, reason: str, tool_context: ToolContext, confirmed: bool = False
) -> str | dict[str, Any]:
    """Apply a staged change: the change_id a plan tool returned.

    With authorization on, a person approves the exact change in the Inbox:
    your task pauses until they decide, then the change is applied and this
    returns the result. `note` is what the person wrote with their decision;
    follow it. A change you already hold access for (such as a
    model choice for an administrator who gave you model choice) applies at
    once. A change already approved through
    request_access_workflow_approvals is applied without asking again.
    `confirmed` does not count as approval then. With
    authorization off, explain the change, get the person's agreement in the
    conversation, and call with confirmed=true. Give a plain reason people
    will read.
    """

    from custom_agents.access_runtime import AuthorizationOff

    refusals: tuple[type[Exception], ...] = (AuthorizationOff, *_REFUSALS)
    try:
        outcome = await asyncio.to_thread(_decide_and_apply, change_id, reason, tool_context, confirmed)
    except InterruptException:
        raise
    except refusals as exc:
        return failed({"status": "refused", "reason": str(exc)})
    if isinstance(outcome, dict):
        # Staging already succeeded in the plan tool. This call did not
        # apply the change, so later calls chosen on that assumption stop.
        return failed(outcome)
    result = outcome.result
    follow_up = getattr(outcome.adapter, "after_apply", None)
    if callable(follow_up) and not result.get("error"):
        with after_saved_change(result, record_id=change_id, step="notifying agents after the change", logger=logger):
            # The adapter already applied the plan. A failed notification or
            # reload must preserve that receipt, even if it returns an error.
            followed = await follow_up(outcome.agent, dict(result))
            if followed.get("error"):
                raise RuntimeError(str(followed["error"]))
            result = followed
    return _applied(result, outcome.approval)


PROVIDER = ToolProvider("changes", (apply_change,))

__all__ = ["PROVIDER", "apply_change"]
