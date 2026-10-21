"""The agent access tools (access plan section 10).

Every agent holds these through its role's standard tool set
(``agent_runtime``): what it can use, asking for access, and, for the team
lead, restricting workers and revoking what it gave, both only in the request
it works on and for that request's person (``AccessRequests.restrict`` and
``revoke``). Searching the catalog
(``find_tools``) is in ``custom_tools.providers.tool_loading``, because it
works in both authorization modes. There is no tool
to give a worker access: workers get it for each task, from what the team lead
may hand on, when their call needs it ("access follows the work",
``AccessService.hand_on_for_task``). Denials from the access decision point
name the exact ``request_access`` call to make.

``request_access`` pauses the task with a Strands interrupt when a person must
decide. The interrupt waits in this process (``ACCESS_REQUEST_INTERRUPT_KIND``)
and is answered when the gateway reports the decision; the answer is only a
wake-up signal, and the tool reads the decision from the access store. A
resumed call finds its request by the tool call's id, so it never asks twice.

With authorization off these tools are not given to agents (decision D8), and
a call that still arrives says so.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_control.decisions import HandDownRefused
from access_control.folders import FolderIdError, grantable_folder_id
from access_control.identity import IdentityRejected
from access_control.requests import LEVEL_WORDS, AccessRequests
from access_control.standard_grants import is_standard_grant
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessRequestV1,
    GrantKind,
    GrantLifetime,
    GrantV1,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.task_v1 import ACCESS_REQUEST_INTERRUPT_KIND
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import get_agent_ref

logger = logging.getLogger(__name__)

ACCESS_REQUEST_INTERRUPT_NAME = "access_request"
LEVELS_HIGH_FIRST = (AccessLevel.FULL, AccessLevel.WRITE, AccessLevel.READ)
STATUS_REFUSED = "refused"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _refused(reason: str) -> str:
    return _json({"status": STATUS_REFUSED, "reason": reason})


def _target(tool_id: str, operation: str = "", folder: str = "") -> TargetRefV1:
    """A tool (``fn:heartbeat``), one of its operations, a tool set (``standard``),
    or a folder (``team/reports``, ``data/<name>/…``; access_control.folders)."""

    tool_id = str(tool_id or "").strip()
    operation = str(operation or "").strip()
    folder = str(folder or "").strip()
    if folder:
        if tool_id or operation:
            raise ValueError("Name either a tool or a folder, not both.")
        return TargetRefV1(kind=TargetKind.FOLDER, id=grantable_folder_id(folder))
    if not tool_id:
        raise ValueError(
            "Name the tool, for example tool_id='cli:google', or the folder, for example "
            "folder='team/reports'; find_tools lists tools."
        )
    if operation:
        return TargetRefV1(kind=TargetKind.OPERATION, id=f"{tool_id}#{operation}")
    return TargetRefV1(kind=TargetKind.TOOL if ":" in tool_id else TargetKind.TOOL_SET, id=tool_id)


def _level(level: str) -> AccessLevel:
    try:
        return AccessLevel(str(level or "").strip().lower())
    except ValueError as exc:
        raise ValueError("level is read, write or full.") from exc


def _lifetime(lifetime: str) -> GrantLifetime:
    try:
        return GrantLifetime(str(lifetime or "until_revoked").strip().lower())
    except ValueError as exc:
        raise ValueError("lifetime is one_time, task, until (with hours) or until_revoked.") from exc


def _context() -> tuple[Any, AccessRequests, Any, Any, str | None]:
    """The agent, its request service, its principal, its person and task id."""

    from custom_agents.access_runtime import runtime_access, runtime_requests, task_access

    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    requests = runtime_requests(agent)
    principal, person, task_id = task_access(agent, runtime_access(agent))
    return agent, requests, principal, person, task_id


def _guarded(call: Callable[[], str]) -> str:
    from custom_agents.access_runtime import AuthorizationOff

    try:
        return call()
    except InterruptException:
        raise
    except AuthorizationOff as exc:
        return _json({"status": "not_needed", "reason": str(exc)})
    except (HandDownRefused, IdentityRejected, FolderIdError, ValueError) as exc:
        return _refused(str(exc))


def _chain_text(requests: AccessRequests, grant: GrantV1) -> str:
    if is_standard_grant(grant.grant_id):
        return "standard for your role"
    names = [requests.subject_name(link.granted_by) for link in requests.service.chain(grant)]
    return " → ".join([*names, requests.subject_name(grant.subject)])


def _grant_view(requests: AccessRequests, grant: GrantV1) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "target": grant.target.id,
        "name": requests.target_name(grant.target),
        "level": grant.level.value,
        "lifetime": grant.lifetime.value,
        "expires_at_utc": grant.expires_at_utc,
        "can_hand_on": grant.delegation_depth > 0,
        "from": _chain_text(requests, grant),
    }


@tool
def list_my_access() -> str:
    """List the tools you may use: target, level, lifetime and who gave it.

    Standard access comes with your role. Restrictions list what you are
    blocked from even when a grant covers it. shared_with_your_person lists
    files and folders other people shared with the person you work for; use
    their paths with the file tools and in code.
    """

    def run() -> str:
        agent, requests, principal, person, _task_id = _context()
        # Access for one task is bound to the request the task works on.
        task_id = principal.work_id
        grants = requests.service.agent_grants(principal, task_id=task_id, person_id=person.person_id if person else None)
        return _json(
            {
                "agent_type": principal.agent_type,
                "role": principal.role,
                "working_for": person.person_id if person else None,
                "access": [_grant_view(requests, g) for g in grants if g.kind is GrantKind.ACCESS],
                "restrictions": [_grant_view(requests, g) for g in grants if g.kind is GrantKind.BLOCK],
                "shared_with_your_person": _shared_view(agent, requests, principal, person, task_id),
                "note": "This is what you hold while working for this person; for another person you hold "
                "what they authorized. You can use a tool only when the person you work for can use it too.",
            }
        )

    return _guarded(run)


def _shared_view(agent: Any, requests: AccessRequests, principal: Any, person: Any, task_id: str | None) -> list[dict[str, Any]]:
    """What other people shared with the person this task works for (decision D15), with the path to use."""

    from custom_tools.workspace_scope import workspaces_root

    root = workspaces_root(agent)
    return [
        {
            "path": str(root / grant.target.id),
            "is_file": grant.target.kind is TargetKind.FILE,
            "level": grant.level.value,
            "shared_by": grant.granted_by.id,
            "expires_at_utc": grant.expires_at_utc,
        }
        for grant in requests.service.shared_with(principal, person, task_id)
    ]


def _held_level(
    requests: AccessRequests,
    principal: Any,
    target: TargetRefV1,
    task_id: str | None,
    grants: list[GrantV1],
    person_id: str | None,
) -> AccessLevel | None:
    for level in LEVELS_HIGH_FIRST:
        if requests.service.holds(principal, target, level, task_id=task_id, person_id=person_id, grants=grants):
            return level
    return None


def _request_result(request: AccessRequestV1, requests: AccessRequests) -> str:
    name = requests.target_name(request.target)
    words = LEVEL_WORDS[request.level]
    if request.status is AccessRequestStatus.GRANTED:
        message = f"Access granted: {words} access to {name}. Retry the call you needed it for."
        if request.decision is not None and request.decision.value == "approve_read":
            message = f"Approved for reading only: read access to {name}."
        elif request.decision is not None and request.decision.value == "approve_task":
            message = f"Approved for this task only: {words} access to {name}."
    elif request.status is AccessRequestStatus.DENIED:
        message = f"Denied: {words} access to {name}. Do not retry; find another way or tell the person."
    elif request.status is AccessRequestStatus.WITHDRAWN:
        message = "The request was withdrawn."
    else:
        message = f"Still waiting for a decision on {words} access to {name}. Do not call the tool yet."
    return _json(
        {
            "status": request.status.value,
            "request_id": request.request_id,
            "decision": request.decision.value if request.decision else None,
            "note": request.decision_note or None,
            "grant_ids": list(request.grant_ids),
            "message": message,
        }
    )


@tool(context=True)
def request_access(
    level: str,
    reason: str,
    tool_context: ToolContext,
    tool_id: str = "",
    folder: str = "",
    lifetime: str = "until_revoked",
    hours: int = 0,
    hand_on: bool = False,
) -> str:
    """Ask for access to a tool, a tool set, or a folder, at a level (read, write or full).

    Use the tool_id (or folder) and level a denial named; access is to the
    whole tool at that level, never to one operation. Folders are named below
    team/, agents/ or data/<name>/, for example folder='data/reports/2026'; a
    folder covers everything inside it. When TeamLead may hand the access on,
    you get it at once for this task; otherwise your task pauses until a
    person decides, then this call returns the decision. lifetime is
    until_revoked (default), task (this task only), one_time (one use in this
    task) or until (with hours). Workers keep nothing between tasks: what a
    person keeps goes to TeamLead, which hands it on to later tasks, or to the
    schedule during a scheduled run. TeamLead: pass hand_on=true for access a
    worker will use, so it may hand it on to its workers' tasks. Say plainly
    why you need it; people read the reason.
    """

    def run() -> str:
        agent, requests, principal, person, task_id = _context()
        if not str(reason or "").strip():
            raise ValueError("Give the reason people will read.")
        tool_use_id = str(tool_context.tool_use.get("toolUseId") or "") or None
        request = (
            requests.service.store.request_for_tool_use(
                team_id=requests.service.team_id, agent_id=principal.agent_id, task_id=task_id, tool_use_id=tool_use_id
            )
            if tool_use_id
            else None
        )
        if request is None:
            _meta_task, meta = _active_meta(agent)
            outcome = requests.submit(
                requester=principal,
                person=person,
                target=_target(tool_id, "", folder),
                level=_level(level),
                reason=str(reason).strip(),
                lifetime=_lifetime(lifetime),
                hours=int(hours) or None,
                task_id=task_id,
                conversation_id=getattr(meta, "conversation_id", None),
                tool_use_id=tool_use_id,
                hand_on=bool(hand_on),
            )
            if outcome.request is None:
                return _json({"status": "already_held", "message": outcome.message})
            request = outcome.request
        if request.status is AccessRequestStatus.WAITING:
            tool_context.interrupt(
                ACCESS_REQUEST_INTERRUPT_NAME,
                reason={
                    "kind": ACCESS_REQUEST_INTERRUPT_KIND,
                    "access_request_id": request.request_id,
                    "summary": f"Waiting for {LEVEL_WORDS[request.level]} access to "
                    f"{requests.target_name(request.target)}.",
                },
            )
            request = requests.service.store.get_request(request.request_id) or request
        return _request_result(request, requests)

    return _guarded(run)


def _active_meta(agent: Any) -> tuple[str | None, Any]:
    from custom_tools.runtime_context import active_task_context

    return active_task_context(agent)


@tool
def restrict_access(agent_type: str, reason: str, tool_id: str = "", folder: str = "", operation: str = "") -> str:
    """Block a worker from a tool, one operation, or a folder you could have given it, in this request.

    The restriction counts only in the request you are working on and ends
    with it; the person's other requests and other people's work are not
    affected. While it lasts it wins over any grant, including ones from
    people. Remove it with revoke_access and the grant_id this returns. For a
    restriction on every request, propose a block with propose_access_change.
    """

    def run() -> str:
        _agent, requests, principal, person, _task_id = _context()
        block = requests.restrict(
            principal,
            recipient_agent_type=str(agent_type or "").strip(),
            target=_target(tool_id, operation, folder),
            reason=str(reason or "").strip() or "No reason given.",
            # The request this task works on; the restriction is bound to it.
            task_id=principal.work_id,
            person_id=person.person_id if person else None,
        )
        return _json(
            {
                "status": "restricted",
                "grant_id": block.grant_id,
                "message": f"{agent_type} can no longer use {requests.target_name(block.target)} in this request.",
            }
        )

    return _guarded(run)


@tool
def revoke_access(grant_id: str) -> str:
    """Revoke access or a restriction your agent type gave for the request you are working on.

    Use the grant_id returned by restrict_access to remove a restriction.
    What you gave for another request, or for another person's work, is not
    yours to end here. Lasting scheduled-job access changes through the job's
    requirements or a person's access controls.
    """

    def run() -> str:
        _agent, requests, principal, person, _task_id = _context()
        revocation = requests.revoke(
            principal,
            str(grant_id or "").strip(),
            task_id=principal.work_id,
            person_id=person.person_id if person else None,
        )
        result: dict[str, Any] = {"status": "revoked", "grant_ids": revocation.revoked}
        # The whole entry goes, except parts a person gave (AccessRequests.revoke).
        if revocation.kept_by_people:
            result["still_given_by_a_person"] = revocation.kept_by_people
            result["message"] = (
                "A person also gave this agent type access to the same tool, so it still has that access. "
                "Tell the person you work for if it should be removed."
            )
        return _json(result)

    return _guarded(run)


ACCESS_TOOLS = (list_my_access, request_access, restrict_access, revoke_access)
ACCESS_TOOL_NAMES = frozenset(("list_my_access", "request_access", "restrict_access", "revoke_access"))
PROVIDER = ToolProvider("access", ACCESS_TOOLS)

__all__ = [
    "ACCESS_REQUEST_INTERRUPT_NAME",
    "ACCESS_TOOLS",
    "ACCESS_TOOL_NAMES",
    "PROVIDER",
]
