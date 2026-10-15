"""Side-effect-free checks of a prospective scheduled worker's known needs.

A requirement names a tool catalog id and one of its catalog operations
(``ScheduleCapabilityRequirementV1``). Each check confirms, in order:

- the tool and the operation are in the catalog. A removed tool group or
  capability id such as ``builtin:web_research`` is not, so a saved schedule
  that still names one is blocked (``requirement_not_a_catalog_tool``) and
  TeamLead re-saves it (``custom_agents.schedule_repair_guidance``). A blocked
  requirement carries ``detail`` and ``suggestions`` naming what to use
  instead, so the model can correct it in one step (``RequirementProblem``);
- the worker type can reach the tool. With authorization on the tool list
  follows grants (``custom_agents.tool_sync``), so holding access is what
  counts; for an MCP server that is access to ``mcp:<name>``, and the server
  is in the catalog only while it is installed (active). With authorization
  off the worker's own tools (its role's standard tools and its default
  tools) must include the operation's function, or the broker or procedure
  dispatcher for command-line, web API and procedure tools; an MCP server
  must be installed and its record must name the worker type (or every type,
  ``*``), because those are the servers an agent loads then;
- with authorization on, the worker type and the schedule's owner hold access
  to the operation (``custom_agents.schedule_access``).

Tools that run through the credential broker (command-line and web API tools,
and tools that act with a linked account) are ``deferred`` when otherwise
ready: the broker checks the linked account when the tool runs.
"""

from __future__ import annotations

from typing import Any

from access_catalog.catalog import ToolCatalog
from access_catalog.tool_functions import functions_of_tools
from custom_agents.access_runtime import authorization_enabled, runtime_access
from custom_agents.agent_instantiation_service import AgentInstantiationService
from custom_agents.schedule_access import (
    ACCESS_MISSING,
    NOT_IN_TOOL_CATALOG,
    RequirementProblem,
    ScheduleAccessCheck,
    requirement_resolution,
)
from custom_agents.schedule_definition_identity import schedule_definition_digest
from custom_agents.schedule_execution_identity import execution_digest
from custom_messages.contracts.access_v2 import AccountMode, ToolKind
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleCapabilityRequirementV1,
    ScheduleCapabilityRequirementsV1,
    ScheduleWorkerBindingV1,
    ScheduleReadinessCheckV1,
    ScheduleReadinessV1,
)
from custom_tools.provider_contract import tool_name

TOOL_NOT_AVAILABLE_TO_WORKER_TYPE = "tool_not_available_to_worker_type"
# Authorization off: the installed server's record does not name the worker type.
MCP_SERVER_NOT_FOR_WORKER_TYPE = "mcp_server_not_for_worker_type"
RESOURCE_CONSTRAINTS_UNSUPPORTED = "resource_constraints_require_a_registered_enforcement_adapter"
BROKER_CHECKS_AT_USE = "linked_account_checked_when_the_tool_runs"
READY = "known_requirements_satisfied"


def effective_requirements(
    binding: ScheduleWorkerBindingV1,
) -> ScheduleCapabilityRequirementsV1:
    merged = {
        (item.capability_id, item.operation): item
        for item in binding.observed_capabilities
    }
    merged.update(
        {
            (item.capability_id, item.operation): item
            for item in binding.capabilities.requirements
        }
    )
    return binding.capabilities.model_copy(
        update={"requirements": tuple(merged.values())}
    )


def prospective_worker_config(
    agent: Any, agent_type: str, definition: dict[str, Any]
) -> dict[str, Any]:
    """The worker type's definition with this instance's settings and tool filters applied."""

    from config_loader import resolve_agent_type_settings
    from custom_agents.saved_runtime_config import current_launch_config
    from custom_agents.team_config_helpers import filter_agent_tools
    from custom_agents.skill_installation import SkillInstallationSettings

    config = {
        **definition,
        **resolve_agent_type_settings(current_launch_config(agent), agent_type),
    }
    config.update(team_id=agent.team_id, workspace_dir=agent.workspace_dir)
    skills = SkillInstallationSettings.from_mapping(
        config.get("skill_installation_config")
    )
    config["tools"] = filter_agent_tools(
        config.get("tools", ()),
        authorization_enabled=authorization_enabled(config.get("authorization_config")),
        skill_installation_enabled=bool(config.get("skills_enabled", False))
        and skills.enabled,
        tool_allowlist=config.get("tool_allowlist"),
        tool_blocklist=config.get("tool_blocklist"),
    )
    return config


def _functions_for(catalog: ToolCatalog, tool_id: str, operation: str) -> set[str]:
    """The functions that perform one operation: its own function, or the dispatcher."""

    function = operation.split(".", 1)[0]
    binding = catalog.functions.get(function)
    if binding is not None and binding.tool_id == tool_id:
        return {function}
    return functions_of_tools(catalog, {tool_id})


def _mcp_server_for_worker_type(agent: Any, tool_id: str, worker_type: str) -> bool:
    """Authorization off: whether the server behind ``tool_id`` is installed and its record names the worker type."""

    from custom_agents.providers.mcp_connections import resolve_agent_mcp_connection_paths
    from mcp_control.registry import MCPConnectionStore

    try:
        store = MCPConnectionStore(resolve_agent_mcp_connection_paths(agent))
    except RuntimeError:
        return False
    record = store.active_for_tool(tool_id)
    return record is not None and record.permits_agent_type(worker_type)


def _reach_problem(
    agent: Any,
    requirement: ScheduleCapabilityRequirementV1,
    *,
    catalog: ToolCatalog,
    config: dict[str, Any],
    worker_type: str,
    authorization_on: bool,
) -> str | None:
    """Why the worker type cannot reach the tool, or None (module docstring)."""

    tool = catalog.tools[requirement.capability_id]
    if authorization_on:
        # Held access decides (checked below); an MCP tool is in the catalog
        # only while its server is installed.
        return None
    if tool.kind is ToolKind.MCP:
        if not _mcp_server_for_worker_type(agent, tool.tool_id, worker_type):
            return MCP_SERVER_NOT_FOR_WORKER_TYPE
        return None
    functions = _functions_for(catalog, tool.tool_id, requirement.operation)
    names = {tool_name(item) for item in config.get("tools", ())}
    if functions and not functions & names:
        return TOOL_NOT_AVAILABLE_TO_WORKER_TYPE
    return None


def _runs_through_broker(tool: Any) -> bool:
    return tool.kind in {ToolKind.CLI, ToolKind.HTTP} or tool.account is AccountMode.LINKED_CREDENTIAL


def _check_requirement(
    agent: Any,
    requirement: ScheduleCapabilityRequirementV1,
    *,
    catalog: ToolCatalog,
    config: dict[str, Any],
    worker_type: str,
    authorization_on: bool,
    access: ScheduleAccessCheck | None,
) -> ScheduleReadinessCheckV1:
    fields: dict[str, Any] = {}
    status, reason = "ready", READY
    try:
        if requirement.resource_constraints:
            raise ValueError(RESOURCE_CONSTRAINTS_UNSUPPORTED)
        resolution = requirement_resolution(catalog, requirement.capability_id, requirement.operation)
        fields = {
            "tool_id": resolution.tool_id,
            "access_operation": resolution.operation,
            "access_level": resolution.level.value,
        }
        problem = _reach_problem(
            agent,
            requirement,
            catalog=catalog,
            config=config,
            worker_type=worker_type,
            authorization_on=authorization_on,
        )
        if problem:
            raise ValueError(problem)
        if access is not None:
            need = access.need(resolution)
            fields["missing_access_for"] = need.missing_for
            if need.missing_for:
                status, reason = "blocked", ACCESS_MISSING
        if status == "ready" and _runs_through_broker(catalog.tools[resolution.tool_id]):
            status, reason = "deferred", BROKER_CHECKS_AT_USE
    except RequirementProblem as exc:
        status, reason = "blocked", exc.code
        fields.update(detail=exc.detail, suggestions=exc.suggestions)
    except (ValueError, PermissionError) as exc:
        status, reason = "blocked", str(exc)
    return ScheduleReadinessCheckV1(
        capability_id=requirement.capability_id,
        operation=requirement.operation,
        status=status,
        reason=reason,
        required=requirement.necessity == "required",
        **fields,
    )


def prepare_schedule_access(
    agent: Any,
    schedule: Any,
    requirements: ScheduleCapabilityRequirementsV1,
    *,
    existing_worker: bool = False,
    binding: ScheduleWorkerBindingV1 | None = None,
    access: ScheduleAccessCheck | None = None,
) -> ScheduleReadinessV1:
    """Check known needs without effects.

    ``binding`` is the saved policy, if any. While its definition is current the
    schedule keeps its saved execution identity. ``access`` also checks that the
    worker type and the owner hold each requirement's tool (when TeamLead
    prepares the schedule for its owner); runs are decided per call instead.
    """
    worker_type = schedule.claim_named_agent_type
    definition = AgentInstantiationService(agent).resolve_definition(worker_type)
    digest = execution_digest(
        schedule, schedule_definition_digest(binding, definition)
    )
    config = prospective_worker_config(agent, worker_type, definition)
    if existing_worker:
        if authorization_enabled(config.get("authorization_config")) != authorization_enabled(
            agent.authorization_config
        ):
            raise ValueError("worker_authorization_mode_changed_restart_required")
        config["tools"] = agent.tools
    catalog = access.service.catalog if access is not None else runtime_access(agent).catalog
    # Every agent of an instance runs in the same authorization mode as the lead.
    authorization_on = authorization_enabled(getattr(agent, "authorization_config", None))
    checks = [
        _check_requirement(
            agent,
            requirement,
            catalog=catalog,
            config=config,
            worker_type=worker_type,
            authorization_on=authorization_on,
            access=access,
        )
        for requirement in requirements.requirements
    ]
    blocked = any(check.required and check.status == "blocked" for check in checks)
    provisional = requirements.runtime_discovery or any(
        check.status != "ready" for check in checks
    )
    return ScheduleReadinessV1(
        status="blocked" if blocked else "provisional" if provisional else "ready",
        checks=tuple(checks),
        execution_digest=digest,
    )


__all__ = [
    "MCP_SERVER_NOT_FOR_WORKER_TYPE",
    "NOT_IN_TOOL_CATALOG",
    "TOOL_NOT_AVAILABLE_TO_WORKER_TYPE",
    "effective_requirements",
    "prepare_schedule_access",
    "prospective_worker_config",
]
