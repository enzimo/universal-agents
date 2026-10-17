"""The team lead's capability tools (``fn:capabilities``), and team skill plans.

Four tools, each taking an ``action`` (owner's decision 2026-10-10: a model
request may carry only so many tool definitions, and these were eleven
functions). The catalog keeps a level per action (``argument: action``) and
each tool checks which inputs its action takes (``custom_tools.action_inputs``):

- ``mcp_connections``: list the MCP connections, or propose installing a
  remote MCP server. Installing one is an access proposal (access plan 7d):
  ``propose`` records the inert connection and proposes installing it, with
  who may use it, for one administrator review
  (``custom_tools.providers.access_proposals``).
- ``hosted_mcp_servers``: declarative hosted MCP servers: list, propose,
  start, stop, and stage installing or removing one (``plan_install``,
  ``plan_remove``, applied with ``apply_change``).
- ``installed_permissions``: list installed procedures, or propose one.
- ``specialist_definitions``: stage a generated specialist (``plan``), see its
  record (``status``), or stage removing it (``plan_removal``).

``invoke_installed_permission`` stays its own function: the catalog decides it
as the installed procedure it runs (``dispatchers``).
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Literal, Optional

from pydantic import ValidationError
from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from custom_agents.generated_agent_provisioning import GeneratedAgentProvisioningService
from custom_agents.installed_permissions import InstalledPermissionService
from custom_agents.skill_installation import SkillInstallationService
from custom_agents.skill_uninstallation import SkillUninstallationService
from custom_tools.action_inputs import actions, input_refusal
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import (
    get_agent_ref,
)

logger = logging.getLogger(__name__)

McpConnectionsAction = Literal["list", "propose"]
HostedMcpServersAction = Literal["list", "propose", "start", "stop", "plan_install", "plan_remove"]
InstalledPermissionsAction = Literal["list", "propose"]
SpecialistDefinitionsAction = Literal["plan", "status", "plan_removal"]

MCP_CONNECTIONS_ACTIONS = actions(
    list=((), ()),
    propose=(
        ("connection_name", "endpoint"),
        (
            "reason",
            "transport",
            "tool_name_prefix",
            "include_tools",
            "exclude_tools",
            "target_agent_types",
            "credential_injections",
            "confirmed",
        ),
    ),
)
HOSTED_MCP_SERVERS_ACTIONS = actions(
    list=((), ()),
    propose=(("server_name", "purpose", "tools"), ("target_agent_types",)),
    start=(("hosted_server_id",), ()),
    stop=(("hosted_server_id",), ()),
    plan_install=(("hosted_server_id", "reason"), ()),
    plan_remove=(("hosted_server_id", "reason"), ()),
)
INSTALLED_PERMISSIONS_ACTIONS = actions(
    list=((), ()),
    propose=(("handler_id", "display_name", "purpose", "justification"), ()),
)
SPECIALIST_DEFINITIONS_ACTIONS = actions(
    plan=(("agent_type", "display_name", "role", "description", "prompt", "default_tools", "reason"), ("visible",)),
    status=((), ("change_id", "agent_type")),
    plan_removal=(("change_id", "reason"), ()),
)
_DEFAULT_MCP_TRANSPORT = "streamable_http"



# -- MCP connections -------------------------------------------------------------------


def _list_connections(agent: Any) -> Dict[str, Any]:
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.providers.mcp_connections import MCPConnectionAdministration
    from custom_agents.mcp_server_tools import server_status

    config = GatewayInstanceConfig.load_for_process(str(agent.instance_config_path))
    connections = MCPConnectionAdministration.for_instance_config(config).list_public()
    for connection in connections:
        load_status = server_status(agent, connection["tool_id"])
        if load_status:
            connection["load_status"] = load_status
    return {"connections": connections}


def _record_connection(agent: Any, fields: Dict[str, Any]) -> Any:
    """Record the inert connection (or find the same pending one); never contacts the endpoint."""

    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.providers.mcp_connections import MCPConnectionAdministration
    from mcp_control.registry import MCPConnectionLifecycle

    config = GatewayInstanceConfig.load_for_process(str(agent.instance_config_path))
    targets = fields.pop("target_agent_types") or [str(agent.named_agent_type)]
    known_agent_types = set(config.agent_types) | {str(agent.named_agent_type)}
    unknown_targets = sorted(set(targets) - known_agent_types)
    if unknown_targets:
        raise ValueError(f"Unknown MCP target agent types: {', '.join(unknown_targets)}")
    record = MCPConnectionAdministration.for_instance_config(config).propose_by_agent(
        agent,
        target_agent_types=targets,
        allow_legacy_sse=bool(config.mcp.legacy_sse.enabled),
        **fields,
    )
    if record.lifecycle is MCPConnectionLifecycle.ACTIVE:
        tool_id = record.descriptor.capability_id
        raise ValueError(
            f"The MCP server {record.descriptor.connection_name} is already installed as {tool_id}. "
            f"If you cannot use its tools, call request_access with tool_id={tool_id!r} and level='full'."
        )
    return record


def _install_proposal(record: Any, reason: str) -> Any:
    from custom_messages.contracts.access_v2 import AccessProposalV1

    name = record.descriptor.connection_name
    return AccessProposalV1(
        title=f"Install the MCP server {name}",
        reason=str(reason or "").strip() or f"Use the MCP server {name} for the person's request.",
        mcp_servers=({"connection_id": record.connection_id, "record_digest": record.record_digest},),
    )


def _propose_connection(
    agent: Any, fields: Dict[str, Any], reason: str, tool_context: ToolContext, confirmed: bool
) -> Dict[str, Any]:
    from custom_agents.access_runtime import runtime_access
    from custom_tools.providers.access_proposals import apply_by_agreement, submit_and_wait

    if not runtime_access(agent).enabled:
        record = _record_connection(agent, dict(fields))
        outcome = apply_by_agreement(agent, _install_proposal(record, reason), confirmed)
        return {**outcome, "connection_id": record.connection_id, "tool_id": record.descriptor.capability_id}
    return submit_and_wait(agent, lambda: _install_proposal(_record_connection(agent, dict(fields)), reason), tool_context)


@tool(context=True)
async def mcp_connections(
    action: McpConnectionsAction,
    tool_context: ToolContext,
    connection_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    reason: Optional[str] = None,
    transport: Optional[str] = None,
    tool_name_prefix: Optional[str] = None,
    include_tools: Optional[List[str]] = None,
    exclude_tools: Optional[List[str]] = None,
    target_agent_types: Optional[List[str]] = None,
    credential_injections: Optional[List[Dict[str, str]]] = None,
    confirmed: bool = False,
) -> str:
    """See the team's MCP connections, or propose installing a remote MCP server.

    Actions:
    - `list`: installed, pending, drifted and disabled connections. For a server whose tools did not
      load for you, load_status says why (not answering, or refused) and when it is tried again; tell
      the person.
    - `propose`: propose installing the server at `endpoint` as `connection_name`; one administrator
      review installs it and says who may use it, and your task waits. Nothing connects to the
      endpoint before then. The review installs it as the team tool mcp:<name> and gives
      `target_agent_types` (by default you) and you full access to all its tools for the work of the
      person your task works for; you may hand it on to workers. Its tools appear on your next step
      once approved. For other access rows, use propose_access_change with mcp_servers. With
      authorization off, explain the server, get the person's agreement, and call again with
      confirmed=true.

    Agents cannot propose stdio commands, headers, environment values or credential material. Each
    credential injection names only a slot ({"target_kind": "header", "target_name": "Authorization",
    "field_name": "api_token", "template": "Bearer {value}"}); the broker fills it from the account
    linked to the server's tool, and the approver is asked to link one.

    Args:
        action: What to do; see Actions.
        connection_name: The name to install the server under.
        endpoint: The server's URL.
        reason: Why the person's work needs it.
        transport: streamable_http (the default) or sse where the operator allows it.
        tool_name_prefix: A prefix for the server's tool names.
        include_tools: Only these of the server's tools.
        exclude_tools: All of the server's tools except these.
        target_agent_types: The agent types that may use it (by default you).
        credential_injections: Credential slots the server needs, as described above.
        confirmed: With authorization off, true once the person agreed.
    """

    from custom_agents.access_runtime import AuthorizationOff
    from custom_tools.providers.access_proposals import PROPOSAL_REFUSALS

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    fields = {
        "connection_name": connection_name,
        "endpoint": endpoint,
        "transport": transport,
        "tool_name_prefix": tool_name_prefix,
        "include_tools": include_tools,
        "exclude_tools": exclude_tools,
        "target_agent_types": target_agent_types,
        "credential_injections": credential_injections,
    }
    refusal = input_refusal(
        "mcp_connections", action, MCP_CONNECTIONS_ACTIONS, {**fields, "reason": reason, "confirmed": confirmed}
    )
    if refusal:
        return refusal
    if action == "list":
        try:
            return json.dumps(await asyncio.to_thread(_list_connections, agent), default=str)
        except Exception as exc:  # noqa: BLE001 - the model gets the reason
            return json.dumps({"error": str(exc)}, default=str)
    fields.update(
        transport=transport or _DEFAULT_MCP_TRANSPORT,
        tool_name_prefix=tool_name_prefix or "",
        include_tools=include_tools or (),
        exclude_tools=exclude_tools or (),
        credential_injections=credential_injections or (),
    )
    try:
        outcome = await asyncio.to_thread(_propose_connection, agent, fields, reason or "", tool_context, confirmed)
    except InterruptException:
        raise
    except ValidationError as exc:
        return json.dumps({"status": "refused", "reason": f"The proposal is not valid: {exc.errors(include_url=False)}"})
    except (AuthorizationOff, PermissionError, *PROPOSAL_REFUSALS) as exc:
        return json.dumps({"status": "refused", "reason": str(exc)}, default=str)
    return json.dumps(outcome, default=str, sort_keys=True)


# -- hosted MCP servers ------------------------------------------------------------------


def _hosting_config(agent: Any) -> Any:
    from assistant_gateway.config import GatewayInstanceConfig

    return GatewayInstanceConfig.load_for_process(str(agent.instance_config_path))


def _propose_hosted(agent: Any, server_name: str, purpose: str, tools: List[Dict[str, Any]], target_agent_types) -> Dict[str, Any]:
    from custom_agents.providers.mcp_hosting import MCPHostingAdministration

    config = _hosting_config(agent)
    targets = target_agent_types or [str(agent.named_agent_type)]
    unknown_targets = sorted(set(targets) - (set(config.agent_types) | {str(agent.named_agent_type)}))
    if unknown_targets:
        raise ValueError(f"Unknown hosted MCP target agent types: {', '.join(unknown_targets)}")
    definition = MCPHostingAdministration(config).propose_by_agent(
        agent, server_name=server_name, purpose=purpose, tools=tools, target_agent_types=targets
    )
    return {
        "status": definition.lifecycle.value,
        "hosted_server_id": definition.hosted_server_id,
        "definition_digest": definition.definition_digest,
        "executable_code_accepted": False,
        "next_step": (
            "Stage it with hosted_mcp_servers(action='plan_install'), then apply_change; an administrator "
            "approves it, which also lets the agent types it names use its tools. Then start it with "
            "hosted_mcp_servers(action='start'); its tools appear on the next step of each agent that may use it."
        ),
    }


def _list_hosted(agent: Any) -> Dict[str, Any]:
    from custom_agents.providers.mcp_hosting import MCPHostingAdministration

    return {"hosted_servers": MCPHostingAdministration(_hosting_config(agent)).list_public()}


def _plan_hosted_change(agent: Any, hosted_server_id: str, operation: str, reason: str) -> Dict[str, Any]:
    from custom_agents.mcp_hosting_changes import HostedMCPServerChangeService

    return HostedMCPServerChangeService(agent).stage(hosted_server_id=hosted_server_id, operation=operation, reason=reason)


async def _change_running_state(agent: Any, hosted_server_id: str, operation: str) -> Dict[str, Any]:
    from custom_agents.access_runtime import acting_agent
    from custom_agents.providers.mcp_hosting import MCPHostingAdministration

    config = _hosting_config(agent)
    if not config.mcp.hosting.enabled:
        raise PermissionError("Declarative MCP hosting is disabled by operator policy.")
    return await MCPHostingAdministration(config).change_running_state(
        hosted_server_id, operation, by=acting_agent(agent).agent.agent_id
    )


@tool
async def hosted_mcp_servers(
    action: HostedMcpServersAction,
    hosted_server_id: Optional[str] = None,
    server_name: Optional[str] = None,
    purpose: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    target_agent_types: Optional[List[str]] = None,
    reason: Optional[str] = None,
) -> str:
    """Declarative MCP servers the runtime hosts: list, propose, start, stop, and stage installing or removing.

    Actions:
    - `list`: hosted server definitions and their lifecycle state.
    - `propose`: an inert definition `server_name` for `purpose`. Each of its `tools` selects a trusted
      behavior (`echo_arguments` or `static_response`) plus exact JSON schemas; executable code, imports,
      commands, packages, endpoints and credentials are not accepted.
    - `plan_install`, `plan_remove`: stage installing or removing server `hosted_server_id`, saying why
      in `reason`; apply it with apply_change. Installing also lets the agent types the definition
      names use all its tools (full access to mcp:<name>, for everyone's work) once it is started. With
      authorization on, an administrator approves the exact change in the Inbox; with it off, get the
      person's agreement and pass confirmed=true to apply_change.
    - `start`, `stop`: start or stop installed server `hosted_server_id`.

    Args:
        action: What to do; see Actions.
        hosted_server_id: The hosted server to start, stop, install or remove.
        server_name: The new server's name.
        purpose: What the new server is for.
        tools: The new server's tools, each with its behavior and JSON schemas.
        target_agent_types: The agent types that may use the new server (by default you).
        reason: Why it should be installed or removed.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    inputs = {
        "hosted_server_id": hosted_server_id,
        "server_name": server_name,
        "purpose": purpose,
        "tools": tools,
        "target_agent_types": target_agent_types,
        "reason": reason,
    }
    refusal = input_refusal("hosted_mcp_servers", action, HOSTED_MCP_SERVERS_ACTIONS, inputs)
    if refusal:
        return refusal
    try:
        if action == "list":
            result = await asyncio.to_thread(_list_hosted, agent)
        elif action == "propose":
            result = await asyncio.to_thread(_propose_hosted, agent, server_name, purpose, tools, target_agent_types)
        elif action in {"start", "stop"}:
            result = await _change_running_state(agent, str(hosted_server_id), action)
        else:
            operation = action.removeprefix("plan_")
            result = await asyncio.to_thread(_plan_hosted_change, agent, str(hosted_server_id), operation, str(reason))
        return json.dumps(result, default=str)
    except Exception as exc:  # noqa: BLE001 - the model gets the reason
        return json.dumps({"error": str(exc)}, default=str)


# -- team skill plans (fn:team_skill_plans) ------------------------------------------------


@tool
def plan_skill_installation(
    repository_url: str,
    skill_name: str = "",
    skill_path: str = "",
) -> str:
    """Download, quarantine, and SkillSpector-scan one GitHub-hosted skill.

    The public HTTPS repository may contain one or more directories named
    ``skills`` at any depth. If a name is ambiguous, call this again with the
    exact repository-relative ``skill_path`` returned in ``candidates``. This
    tool never installs or activates a skill and never accepts credentials;
    apply the returned change_id with apply_change.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            SkillInstallationService(agent).stage(
                repository_url=repository_url,
                skill_name=skill_name,
                skill_path=skill_path,
            ),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def plan_team_skill_change(draft_path: str, replace_existing: bool = False) -> str:
    """Propose a new or changed team skill from a draft folder you prepared.

    Team skills are loaded by every agent, so no agent can write them directly.
    Write the skill (a folder with SKILL.md and any resources) in this task's
    folders, then call this with its path. The draft is copied into quarantine
    and scanned with SkillSpector. Set replace_existing=true to replace a team
    skill that was installed through review; the result lists the added,
    removed and changed files. To change an installed skill, copy its folder
    (readable, not writable) into this task's folders and edit the copy. Then
    call apply_change with the returned change_id: it applies at once if you
    may manage team skills, and otherwise a person approves it (with
    authorization off, get their agreement and pass confirmed=true). Built-in
    and operator-provided skills cannot be replaced this way.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            SkillInstallationService(agent).stage_draft(
                draft_path=draft_path,
                replace_existing=replace_existing,
            ),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def get_skill_installation_status(change_id: str) -> str:
    """Verify plan, installation receipt, and temporary-grant activity state."""

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            SkillInstallationService(agent).status(change_id),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def plan_skill_uninstallation(skill_name: str) -> str:
    """Plan removal of one exact reviewed skill in managed storage; apply it with apply_change.

    Workspace-owned and startup-refreshed external skills are outside this
    lifecycle and cannot be removed by this tool.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            SkillUninstallationService(agent).stage(skill_name=skill_name),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def get_skill_uninstallation_status(change_id: str) -> str:
    """Inspect a skill-removal plan, receipt, and temporary-grant state."""

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            SkillUninstallationService(agent).status(change_id),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


# -- installed procedures ------------------------------------------------------------------


@tool
def installed_permissions(
    action: InstalledPermissionsAction,
    handler_id: Optional[str] = None,
    display_name: Optional[str] = None,
    purpose: Optional[str] = None,
    justification: Optional[str] = None,
) -> str:
    """See the team's installed procedures (installed permissions), or propose one for an administrator.

    Actions:
    - `list`: the installed definitions available to this team, and the proposals made.
    - `propose`: an inert, administrator-facing proposal for permission handler `handler_id`. Its text is
      untrusted and never registers code or grants authority: an administrator may install it only after
      trusted runtime code and registry metadata independently define the exact handler, JSON schema,
      impact, risk and access level. Run an installed one with invoke_installed_permission.

    Args:
        action: What to do; see Actions.
        handler_id: The trusted handler the proposal is for.
        display_name: A short name for people.
        purpose: What it would be used for.
        justification: Why the team needs it.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    inputs = {"handler_id": handler_id, "display_name": display_name, "purpose": purpose, "justification": justification}
    refusal = input_refusal("installed_permissions", action, INSTALLED_PERMISSIONS_ACTIONS, inputs)
    if refusal:
        return refusal
    try:
        service = InstalledPermissionService.for_agent(agent)
        if action == "propose":
            return json.dumps(service.propose(agent=agent, **inputs), default=str)
        team_id = str(getattr(agent, "team_id", "") or "")
        return json.dumps(
            {
                "installed_permissions": service.list_installed(team_id=team_id),
                "capability_proposals": service.list_proposals(team_id=team_id),
            },
            default=str,
        )
    except Exception as exc:  # noqa: BLE001 - the model gets the reason
        return json.dumps({"error": str(exc)}, default=str)


@tool
def invoke_installed_permission(
    definition_id: str,
    arguments_json: str = "{}",
) -> str:
    """Invoke one installed permission through its trusted final enforcement point.

    Arguments must be one JSON object accepted by the registered handler's
    runtime-owned schema. Each call is checked as the catalog tool
    procedure:<handler_id>; if it is refused with access_denied, call
    request_access with the tool_id and level it names. The agent
    cannot select or rewrite the action, resource, risk, or impact.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        arguments = json.loads(str(arguments_json or "{}"))
        if not isinstance(arguments, dict):
            raise ValueError("arguments_json must contain one JSON object")
        return json.dumps(
            InstalledPermissionService.for_agent(agent).invoke(
                agent=agent,
                definition_id=definition_id,
                arguments=arguments,
            ),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


# -- generated specialists ------------------------------------------------------------------


@tool
def specialist_definitions(
    action: SpecialistDefinitionsAction,
    agent_type: Optional[str] = None,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    description: Optional[str] = None,
    prompt: Optional[str] = None,
    default_tools: Optional[List[str]] = None,
    reason: Optional[str] = None,
    visible: bool = False,
    change_id: Optional[str] = None,
) -> str:
    """Stage a new generated specialist, see one's saved record, or stage removing one.

    Actions:
    - `plan`: stage a reviewed specialist definition. default_tools lists one to eight tool catalog ids
      or tool set ids (from find_tools, for example fn:publishing, code:python or google_workspace). With
      authorization on they are what the new type should be given, not access: the specialist starts
      with its role's standard tools and uses the rest once it holds access. With authorization off they
      are its tools. Platform controls and host-command tools cannot be defaults. The plan cannot contain
      imports, packages, commands, endpoints, executable paths, headers, environment values or
      credentials. Apply it with apply_change: a person approves the exact specialist (with
      authorization off, the person's agreement applies it).
    - `status`: a specialist's saved definition record and state, by `change_id` or `agent_type`. It lists
      the default tools the person approved; what the specialist may use comes from its grants.
    - `plan_removal`: stage removing the specialist from provisioning change `change_id` (from `status`);
      apply the returned change_id with apply_change. Its definition is kept in a recoverable folder,
      and removal takes effect after a manager restart.

    Args:
        action: What to do; see Actions.
        agent_type: The specialist's agent type name.
        display_name: Its name for people.
        role: What it does, in a few words.
        description: When TeamLead should use it.
        prompt: Its instructions.
        default_tools: One to eight catalog tool ids or tool set ids.
        reason: Why the team needs it, or why it should go.
        visible: Whether people see it listed.
        change_id: The specialist's provisioning change id.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    inputs = {
        "agent_type": agent_type,
        "display_name": display_name,
        "role": role,
        "description": description,
        "prompt": prompt,
        "default_tools": default_tools,
        "reason": reason,
        "visible": visible,
        "change_id": change_id,
    }
    refusal = input_refusal("specialist_definitions", action, SPECIALIST_DEFINITIONS_ACTIONS, inputs)
    if refusal:
        return refusal
    try:
        service = GeneratedAgentProvisioningService(agent)
        if action == "plan":
            del inputs["change_id"]
            result = service.stage(**inputs)
        elif action == "status":
            result = service.status(change_id=change_id or "", agent_type=agent_type or "")
        else:
            result = service.stage_rollback(str(change_id), reason=str(reason))
        return json.dumps(result, default=str)
    except Exception as exc:  # noqa: BLE001 - the model gets the reason
        return json.dumps({"error": str(exc)}, default=str)


PROVIDER = ToolProvider(
    name="capabilities",
    tools=(
        mcp_connections,
        hosted_mcp_servers,
        installed_permissions,
        invoke_installed_permission,
        specialist_definitions,
    ),
)
# Staging team skill changes is the team lead's own work (catalog
# fn:team_skill_plans); applying them needs a grant or a person
# (fn:team_skills, access_control.changes).
TEAM_SKILL_PLANS_PROVIDER = ToolProvider(
    name="team_skill_plans",
    tools=(
        plan_skill_installation,
        plan_team_skill_change,
        get_skill_installation_status,
        plan_skill_uninstallation,
        get_skill_uninstallation_status,
    ),
)
