"""``propose_access_change`` and ``get_proposal_status``: TeamLead's access proposals (Phase 6).

When a service is missing from the tool catalog, or access is needed that
nobody may hand on, TeamLead proposes the whole change at once: new web API
tools, MCP servers to install, access for agent types, restrictions, data
folders to register, account links to set up and grants to remove. Trusted
code checks it and writes the review card (``access_control.proposals``); an
administrator approves it once in the Inbox or in chat, and the gateway
applies everything together. The task pauses while it waits, like
``request_access``, and this tool then returns what was applied. When the
proposal asks for account links, the approver chooses the credentials in
forms right after approving, and the task stays paused until those forms end
(``assistant_gateway.access_inbox``); the result then says which links exist.
With
authorization off there are no grants: the person agrees in conversation
(``confirmed=true``) and the new tools, MCP servers and data folders are added
directly; an installed MCP server's tools load on the next model call of each
agent it is meant for (``custom_agents.tool_sync``).

``submit_and_wait`` and ``apply_by_agreement`` are shared with
``mcp_connections(action="propose")`` (``custom_tools.providers.capabilities``), which
records a new MCP connection and proposes installing it in the same call.
"""

from __future__ import annotations

from access_control.cli_binaries import fetch_https
from access_control.proposal_clis import install_programs, remove_programs

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_catalog.instance_tools import write_instance_tool
from access_control.account_sharing import TeamAccountSharing
from access_control.data_folders import DataFolderRefused, DataFolderRegistry
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from access_control.identity import IdentityRejected
from access_control.proposals import (
    ACCESS_PROPOSAL_KIND,
    ProposalCompiler,
    ProposalRefused,
    proposal_summary,
    with_mcp_defaults,
)
from custom_messages.contracts.access_v2 import (
    AccessProposalV1,
    AccessRequestStatus,
    AccessRequestV1,
    ChangeApprovalMode,
    ChangeApprovalRecordV1,
    ChangeProposalV1,
    LinkOwnerKind,
)
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.task_v1 import ACCESS_REQUEST_INTERRUPT_KIND
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers.access import ACCESS_REQUEST_INTERRUPT_NAME
from custom_tools.runtime_context import active_task_context, get_agent_ref
from id_generation import new_id
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

MAX_EFFECT_LINES = 40
# Who approved a change made with authorization off (access_control.changes).
AGREEMENT_APPROVER = "agreement"
# The errors a proposal tool reports to the model as a refusal.
PROPOSAL_REFUSALS = (DataFolderRefused, HandDownRefused, IdentityRejected, ProposalRefused, RuntimeError, ValueError)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _proposal(
    title: str,
    reason: str,
    tools: list[dict] | None,
    mcp_servers: list[dict] | None,
    grants: list[dict] | None,
    blocks: list[dict] | None,
    account_links: list[dict] | None,
    data_folders: list[dict] | None,
    revoke_grant_ids: list[str] | None,
    clis: list[dict] | None = None,
) -> AccessProposalV1:
    return AccessProposalV1(
        title=title,
        reason=reason,
        tools=tuple(tools or ()),
        clis=tuple(clis or ()),
        mcp_servers=tuple(mcp_servers or ()),
        grants=tuple(grants or ()),
        blocks=tuple(blocks or ()),
        account_links=tuple(account_links or ()),
        data_folders=tuple(data_folders or ()),
        revoke_grant_ids=tuple(revoke_grant_ids or ()),
    )


def _link_states(store: AccessRepository | None, request: AccessRequestV1) -> list[dict[str, Any]]:
    """For each account link the proposal asked for: whether a link exists now.

    The approver's forms ended before the task was told of the decision, so
    this says what was linked; ``person`` links belong to the approver.
    """

    if store is None or request.change is None:
        return []
    stored = store.get_plan(request.change.change_id)
    if stored is None:
        return []
    proposal = AccessProposalV1.model_validate(stored[3])
    approver = request.decided_by.id if request.decided_by is not None else ""
    states = []
    for item in proposal.account_links:
        owner_kind = LinkOwnerKind(item.owner)
        owner_id = request.team_id if owner_kind is LinkOwnerKind.TEAM else approver
        linked = bool(owner_id) and store.active_link(request.team_id, item.tool_id, owner_kind, owner_id) is not None
        states.append({"tool_id": item.tool_id, "owner": item.owner, "linked": linked})
    return states


def _result(request: AccessRequestV1, store: AccessRepository | None = None) -> dict[str, Any]:
    if request.status is AccessRequestStatus.GRANTED and request.applied_at_utc is not None:
        applied: dict[str, Any] = {
            "status": "applied",
            "proposal_id": request.change.change_id if request.change else None,
            "grant_ids": list(request.grant_ids),
            "kept_for_later_tasks": list(request.kept_item_ids),
            "note": request.decision_note or None,
            "message": (
                "Applied. New tools are in the catalog now; an installed MCP server's tools appear on the next "
                "model call of each agent that may use it, for the work of the person it serves."
            ),
        }
        links = _link_states(store, request)
        if links:
            applied["account_links"] = links
            applied["message"] += (
                " account_links says which accounts were linked in the approver's forms. For one that is not "
                "linked, call set_up_tools for it (it opens the person's account link form), or tell them an "
                "administrator can link the team's account with /link <tool_id> team."
            )
        return applied
    if request.status is AccessRequestStatus.DENIED:
        message = "The proposal was denied. Do not propose it again unchanged; tell the person what will not happen."
    elif request.status is AccessRequestStatus.WITHDRAWN:
        message = "The proposal was withdrawn because its task ended."
    elif request.status is AccessRequestStatus.GRANTED:
        message = f"Approved but not applied: {request.apply_error or 'the gateway could not apply it'}."
    else:
        message = "Still waiting for an administrator to decide. Do not propose it again."
    return {"status": request.status.value, "request_id": request.request_id, "message": message}


def _mcp_store(agent: Any) -> Any:
    from custom_agents.providers.mcp_connections import resolve_agent_mcp_connection_paths
    from mcp_control.registry import MCPConnectionStore

    return MCPConnectionStore(resolve_agent_mcp_connection_paths(agent))


def _agreement_rows(agent: Any, proposal: AccessProposalV1) -> list[str]:
    rows = [f"Add {tool.display_name} ({tool.tool_id}) for {', '.join(tool.hosts)}" for tool in proposal.tools]
    if proposal.mcp_servers:
        store = _mcp_store(agent)
        for item in proposal.mcp_servers:
            record = store.get(item.connection_id)
            name = record.descriptor.connection_name if record else item.connection_id
            where = (record.descriptor.endpoint or record.descriptor.command or "") if record else ""
            rows.append(f"Install the MCP server {name}" + (f" ({where})" if where else ""))
    rows += [f"Register {folder.path} as data/{folder.name}" for folder in proposal.data_folders]
    return rows


def _install_by_agreement(agent: Any, proposal: AccessProposalV1) -> list[Any]:
    """Activate the proposed MCP servers with the person's agreement; the installed records."""

    from mcp_control.registry import MCPConnectionLifecycle

    if not proposal.mcp_servers:
        return []
    store = _mcp_store(agent)
    approval = ChangeApprovalRecordV1(
        mode=ChangeApprovalMode.AGREEMENT, approved_by=AGREEMENT_APPROVER, approved_at_utc=datetime.now(timezone.utc)
    )
    installed = []
    for item in proposal.mcp_servers:
        record = store.require(item.connection_id)
        if record.lifecycle is not MCPConnectionLifecycle.PENDING or record.record_digest != item.record_digest:
            raise ProposalRefused(
                f"The MCP server {record.descriptor.connection_name} is not the pending server you proposed; "
                "propose it again."
            )
        installed.append(store.update_lifecycle(record.connection_id, lifecycle=MCPConnectionLifecycle.ACTIVE, approval=approval))
    return installed


def _when_tools_appear(agent: Any, installed: list[Any]) -> dict[str, str]:
    """For each installed MCP server, when its tools appear (loaded before the next model call)."""

    agent_type = str(getattr(agent, "named_agent_type", "") or "")
    return {
        record.descriptor.capability_id: (
            "Installed. Its tools appear in your tool list from your next step, once the server answers."
            if record.permits_agent_type(agent_type)
            else f"Installed for {', '.join(record.target_agent_types)}; those agents see its tools from their next step."
        )
        for record in installed
    }


def apply_by_agreement(agent: Any, proposal: AccessProposalV1, confirmed: bool) -> dict[str, Any]:
    """Single-person mode (D8): add tools, MCP servers and data folders after the person's agreement."""

    from custom_tools.workspace_scope import _protected, runtime_layout

    rows = _agreement_rows(agent, proposal)
    if not confirmed:
        return {
            "status": "agreement_needed",
            "changes": rows,
            "message": "Authorization is off. Explain these changes to the person, get their agreement in the "
            "conversation, and call again with confirmed=true. Grants and restrictions are not needed.",
        }
    installed = _install_by_agreement(agent, proposal)
    layout = runtime_layout(agent)
    programs: list = []
    try:
        install_programs(layout.root, proposal.clis, fetch_https, programs)
    except ValueError:
        remove_programs(programs)
        raise
    for proposed in (*proposal.tools, *proposal.clis):
        write_instance_tool(layout.root, proposed)
    storage = agent.task_manager.storage_settings
    registry = DataFolderRegistry(AccessRepository(storage), str(agent.team_id))
    owner = PersonPrincipal(AGREEMENT_APPROVER, str(agent.team_id), ("owner",))
    for folder in proposal.data_folders:
        registry.register(
            owner,
            name=folder.name,
            path=folder.path,
            writable=folder.writable,
            reason=proposal.reason,
            protected=_protected(agent),
            now=datetime.now(timezone.utc),
        )
    logger.warning(
        "An access proposal was applied on the person's agreement (authorization off). "
        "event=proposal_applied_by_agreement tools=%s mcp_servers=%s folders=%s",
        ",".join(tool.tool_id for tool in (*proposal.tools, *proposal.clis)),
        ",".join(record.connection_id for record in installed),
        ",".join(folder.name for folder in proposal.data_folders),
    )
    applied: dict[str, Any] = {"status": "applied", "changes": rows, "message": "Applied with the person's agreement."}
    if installed:
        applied["mcp_servers"] = _when_tools_appear(agent, installed)
    return applied


def _existing_request(agent: Any, tool_context: ToolContext) -> AccessRequestV1 | None:
    """The request this tool call already made (a resumed call finds it by its tool-use id)."""

    from custom_agents.access_runtime import agent_principal, runtime_access, runtime_requests, task_person

    tool_use_id = str(tool_context.tool_use.get("toolUseId") or "") or None
    if not tool_use_id:
        return None
    requests = runtime_requests(agent)
    _person, task_id = task_person(agent, runtime_access(agent))
    return requests.service.store.request_for_tool_use(
        team_id=requests.service.team_id,
        agent_id=agent_principal(agent).agent_id,
        task_id=task_id,
        tool_use_id=tool_use_id,
    )


def _submit(agent: Any, proposal: AccessProposalV1, tool_context: ToolContext) -> AccessRequestV1:
    from custom_agents.access_runtime import runtime_access, runtime_requests, task_access
    from custom_tools.workspace_scope import _protected

    access = runtime_access(agent)
    requests = runtime_requests(agent)
    principal, person, task_id = task_access(agent, access)
    service = requests.service
    mcp_records = _mcp_store(agent).get if proposal.mcp_servers else (lambda _connection_id: None)
    # Default rows for MCP servers are added before the proposal is stored,
    # so the card and the approved digest show them (access_control.proposals).
    proposal = with_mcp_defaults(proposal, mcp_records, lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE)
    compiled = ProposalCompiler(
        catalog=access.catalog,
        team_id=service.team_id,
        holds=lambda someone, target, level: service.holds(someone, target, level, task_id=None),
        account_sharing=TeamAccountSharing(service.store, service.team_id),
        data_folders=DataFolderRegistry(service.store, service.team_id),
        protected=_protected(agent),
        grant_exists=service.store.get,
        mcp_records=mcp_records,
        lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
    ).compile(proposal, person=person, task_id=principal.work_id)
    change_id = new_id("proposal")
    service.store.save_plan(
        service.team_id, change_id, ACCESS_PROPOSAL_KIND, compiled.digest, proposal.model_dump(mode="json"), at=service.clock()
    )
    effect = tuple(row.text[:1000] for row in compiled.rows[:MAX_EFFECT_LINES])
    change = ChangeProposalV1(
        kind=ACCESS_PROPOSAL_KIND,
        change_id=change_id,
        plan_digest=compiled.digest,
        title=f"access proposal: {proposal.title}",
        summary=proposal_summary(compiled.rows),
        effect=effect,
        items=compiled.rows,
    )
    _task, meta = active_task_context(agent)
    outcome = requests.submit_change(
        requester=principal,
        person=person,
        proposal=change,
        reason=proposal.reason,
        task_id=task_id,
        conversation_id=getattr(meta, "conversation_id", None),
        tool_use_id=str(tool_context.tool_use.get("toolUseId") or "") or None,
    )
    if outcome.request is None:
        raise HandDownRefused("Proposals are always reviewed by an administrator; nothing was applied.")
    return outcome.request


def submit_and_wait(agent: Any, build: Callable[[], AccessProposalV1], tool_context: ToolContext) -> dict[str, Any]:
    """Submit the proposal ``build`` returns (once per tool call) and pause until an administrator decides.

    A resumed call finds its request by its tool-use id and does not build
    the proposal again (``build`` may record things, such as a new MCP
    connection).
    """

    from custom_agents.access_runtime import runtime_requests

    request = _existing_request(agent, tool_context) or _submit(agent, build(), tool_context)
    if request.status is AccessRequestStatus.WAITING:
        tool_context.interrupt(
            ACCESS_REQUEST_INTERRUPT_NAME,
            reason={
                "kind": ACCESS_REQUEST_INTERRUPT_KIND,
                "access_request_id": request.request_id,
                "summary": f"Waiting for an administrator to review the proposal: {request.change.title if request.change else ''}.",
            },
        )
        request = runtime_requests(agent).service.store.get_request(request.request_id) or request
    return _result(request, runtime_requests(agent).service.store)


def _propose(proposal: AccessProposalV1, tool_context: ToolContext, confirmed: bool) -> dict[str, Any]:
    from custom_agents.access_runtime import runtime_access

    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    if not runtime_access(agent).enabled:
        return apply_by_agreement(agent, proposal, confirmed)
    return submit_and_wait(agent, lambda: proposal, tool_context)


@tool(context=True)
async def propose_access_change(
    title: str,
    reason: str,
    tool_context: ToolContext,
    tools: list[dict] | None = None,
    mcp_servers: list[dict] | None = None,
    clis: list[dict] | None = None,
    grants: list[dict] | None = None,
    blocks: list[dict] | None = None,
    account_links: list[dict] | None = None,
    data_folders: list[dict] | None = None,
    revoke_grant_ids: list[str] | None = None,
    confirmed: bool = False,
) -> str:
    """Propose several access changes for one review by an administrator; your task waits.

    Use it when a service is not in the tool catalog (find_tools finds nothing,
    or a call is refused as an unlisted host), or when you need access nobody
    can hand on. Items:
    - tools: web API tools, each {"tool_id": "http:<name>", "display_name",
      "description", "hosts": [exact host names], "routes": [{"name",
      "method", "path", "level"}], "uses_credential": true,
      "credential_injections": [{"placement": "header", "name": "X-API-Key",
      "field": "api_key", "template": "{value}"}], "setup": {"external_steps":
      [{"step_id", "title", "instructions", "url" (https), "verify", "who":
      "person"|"administrator"}], "prerequisites": [tool ids]}}. Without a
      route, GET needs read, POST/PUT/PATCH write, DELETE full. setup is
      optional: steps the person does outside Universal Agents before the
      account can be linked (such as creating a token), in plain words, no
      secrets; set_up_tools shows them and waits until they are marked done.
    - clis: command-line tools, each {"tool_id": "cli:<name>", "display_name",
      "description", "installed_cli": {"binary_path": "team/tools/<name>/<file>",
      "sha256", "version", "source_url" (https, optional), "archive_member"
      (optional), "commands": {"repo.list": "read", "issue.create": "write",
      "repo.delete": "full"}, "blocked_commands": {"path": "why"},
      "blocked_flags": [...], "allowed_hosts": [...]}, "credential_injections":
      [{"placement": "env", "name": "TEA_TOKEN", "field": "token"} or
      {"placement": "file", "name": ".config/tea/token", "field": "token",
      "template": "{value}"}], "link_owners": ["person"] | ["person", "team"] |
      ["team"], "setup": {...}}. Put the program in team/tools/<name>/ first
      (or give its https download) and use its SHA-256; unlisted commands need
      full. Classify high-risk commands (deleting, trading, paying) as full.
      The broker runs a checked copy in the sandbox; agents never see the
      credential.
    - mcp_servers: pending MCP connections to install, each {"connection_id",
      "record_digest"} from mcp_connections(action="list") (its action
      "propose" does this for a new server in one step). Without grant rows for a server,
      its proposed agent types and you get full access to all its tools
      (mcp:<name>), and you may hand it on to workers.
    - grants: {"agent_type", "target": {"kind": "tool"|"operation"|"tool_set"|
      "folder", "id"}, "level": "read"|"write"|"full", "lifetime":
      "until_revoked"|"task", "delegation_depth": 0|1, "reason"}. They serve
      the work of the person your task works for.
    - blocks: {"agent_type", "target", "reason"} to restrict a worker.
    - account_links: {"tool_id", "owner": "team"|"person", "purpose"}: after
      approval the approver chooses the credential in a form, and your task
      waits until the forms end; the result says which links exist.
    - data_folders: {"name", "path" (absolute host path), "writable"}.
    - revoke_grant_ids: grants to remove.
    Say plainly why in `reason`; the card shows trusted code's own summary of
    each item. With authorization off, get the person's agreement in the
    conversation and call with confirmed=true.
    """

    from custom_agents.access_runtime import AuthorizationOff

    try:
        proposal = _proposal(
            title, reason, tools, mcp_servers, grants, blocks, account_links, data_folders, revoke_grant_ids, clis
        )
        outcome = await asyncio.to_thread(_propose, proposal, tool_context, confirmed)
    except InterruptException:
        raise
    except ValidationError as exc:
        return _json({"status": "refused", "reason": f"The proposal is not valid: {exc.errors(include_url=False)}"})
    except (AuthorizationOff, *PROPOSAL_REFUSALS) as exc:
        return _json({"status": "refused", "reason": str(exc)})
    return _json(outcome)


@tool
def get_proposal_status(proposal_id: str) -> str:
    """The state of a proposal you made (the proposal_id propose_access_change returned)."""

    from custom_agents.access_runtime import AuthorizationOff, runtime_requests

    agent = get_agent_ref()
    try:
        requests = runtime_requests(agent)
    except AuthorizationOff as exc:
        return _json({"status": "not_needed", "reason": str(exc)})
    for request in requests.service.store.list_requests(requests.service.team_id):
        if request.change is not None and request.change.change_id == str(proposal_id).strip():
            return _json(_result(request, requests.service.store))
    return _json({"status": "unknown", "reason": f"There is no proposal {proposal_id}."})


PROVIDER = ToolProvider("access_proposals", (propose_access_change, get_proposal_status))

__all__ = [
    "PROPOSAL_REFUSALS",
    "PROVIDER",
    "apply_by_agreement",
    "get_proposal_status",
    "propose_access_change",
    "submit_and_wait",
]
