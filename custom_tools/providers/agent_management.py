"""Agent management support for the trusted Strands tool surface."""

import json
import logging
from typing import TYPE_CHECKING, Optional

from strands import tool

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import AgentCreationResultV1
from custom_agents.team_templates import load_team_templates, summarize_team_templates
from custom_messages.contracts.control_v1 import AGENT_STATUS_RUNTIME_ONLY_FIELDS, AgentCreationRequest, AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, MessageMetaType, RoutingType
from custom_tools.agent_team_port import current_agent_type_map
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import (
    bound_task_id,
    get_agent_ref,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


from custom_tools.providers._runtime import (
    _run_agent_coroutine,
    _send_control_instruction_payload,
)
from custom_tools.providers.diagnostics import (
    build_runtime_pressure_snapshot,
)
from custom_tools.providers.runtime_settings import (
    _resolve_ephemeral_idle_ttl,
)

logger = logging.getLogger(__name__)


def _instantiate_agent_request(
    agent: "UniversalAgent",
    agent_type: str,
    team_id: str,
    mcp_profile_names: Optional[list[str]] = None,
    idle_ttl_seconds: Optional[float] = None,
) -> AgentCreationResultV1:
    resolved_idle_ttl_seconds = (
        _resolve_ephemeral_idle_ttl(agent, agent_type)
        if idle_ttl_seconds is None
        else idle_ttl_seconds
    )
    request = AgentCreationRequest(
        named_agent_type=agent_type,
        agent_quantity=1,
        team_id=team_id or agent.team_id,
        mcp_profile_names=list(mcp_profile_names or []) or None,
        idle_ttl_seconds=resolved_idle_ttl_seconds,
    )
    header = MessageHeader(
        from_agent_id=agent.id,
        to_routing_path=agent.id,
    )
    meta = MessageMeta(from_agent_name=agent.name)
    return agent.handle_agent_creation_request(request, header, meta)


@tool
def get_known_agents() -> str:
    """
    Get a map of all known agents and their current status.

    Returns information about other agents that this agent has received
    status updates from. Useful for understanding what agents are available
    for task delegation.

    Returns:
        JSON string containing a map of agent IDs to their AgentStatus objects.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    return agent.known_agents.model_dump_json(
        exclude={"agent_map": {"__all__": set(AGENT_STATUS_RUNTIME_ONLY_FIELDS)}}
    )


@tool
def query_agent_state(
    agent_name: str = "",
    agent_type: str = "",
    is_executing: str = "",
    has_execution_capacity: str = "",
    is_busy: str = "",
    team_id: str = "",
    live: str = "",
    target_agent: str = "",
    info_level: str = "basic",
) -> str:
    """
    Query known agent states with optional filters.

    Searches the persistent agent state store for agents matching the
    given criteria. All filters are optional — omit or pass empty string
    to skip a filter. Multiple filters are AND-combined.

    Args:
        agent_name: Filter by agent name (partial, case-insensitive match).
        agent_type: Filter by named_agent_type (exact match).
        is_executing: Filter by whether the agent is actively executing work — "true" or "false".
        has_execution_capacity: Filter by whether the agent can immediately start executing another top-level task — "true" or "false".
        is_busy: Deprecated alias for is_executing — "true" or "false".
        team_id: Filter by team ID (exact match).

    Args:
        live: When "true", "1", or "yes", also send live AgentStatus request messages
            before returning. This does not wait for replies; it triggers refresh and
            returns the current persisted snapshot.
        target_agent: Optional explicit agent ID/name to refresh when `live=true`.
        info_level: Live refresh detail level when `live=true` - "basic" or "full".

    Returns:
        JSON object containing matching AgentStatus rows and optional live-refresh metadata.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    agent_state_store = getattr(agent, "_agent_state_store", None)
    filters = {}
    if agent_name:
        filters["agent_name"] = agent_name
    if agent_type:
        filters["named_agent_type"] = agent_type
    if team_id:
        filters["team_id"] = team_id
    if is_executing.lower() in ("true", "1", "yes"):
        filters["is_executing"] = True
    elif is_executing.lower() in ("false", "0", "no"):
        filters["is_executing"] = False
    if has_execution_capacity.lower() in ("true", "1", "yes"):
        filters["has_execution_capacity"] = True
    elif has_execution_capacity.lower() in ("false", "0", "no"):
        filters["has_execution_capacity"] = False
    if is_busy.lower() in ("true", "1", "yes"):
        filters["is_executing"] = True
    elif is_busy.lower() in ("false", "0", "no"):
        filters["is_executing"] = False

    try:
        live_requested = live.lower() in ("true", "1", "yes")
        live_targets: list[str] = []
        if live_requested:
            current_task_id = bound_task_id()
            current_task_info = (
                agent.task_manager.operations_runtime.run_sync(
                    agent.task_manager.get_task_info, current_task_id
                )
                if current_task_id
                else None
            )
            conversation_id = getattr(
                getattr(current_task_info, "meta", None), "conversation_id", None
            )
            request_timezone = getattr(
                getattr(current_task_info, "meta", None), "request_timezone", None
            )
            info_type = (
                ControlType.STATUS_REQUEST_FULL
                if info_level == "full"
                else ControlType.STATUS_REQUEST_BASIC
            )
            status_request = AgentStatus(
                self_id=agent.id,
                team_id=agent.team_id,
                named_agent_type=agent.named_agent_type,
                is_paused=bool(getattr(agent, "paused", False)),
                is_executing=bool(getattr(agent, "is_executing", False)),
                has_execution_capacity=bool(
                    getattr(agent, "_has_execution_capacity", lambda: False)()
                ),
                is_busy=bool(getattr(agent, "is_executing", False)),
                name=agent.name,
                agent_role=agent.role,
                status_type=MessageMetaType.REQUEST,
                info_type=info_type,
            )

            if target_agent:
                live_targets = [target_agent]
            else:
                if agent_state_store is None:
                    return json.dumps(
                        {"error": "AgentStateStore not initialized on this agent."}
                    )
                matched_statuses = agent_state_store.query(filters)
                live_targets = [
                    status.self_id
                    for status in matched_statuses
                    if status.self_id
                ]

            for raw_target in live_targets:
                routing_key = agent._agent_id_by_name.get(raw_target, raw_target)
                target_is_known_name = raw_target in agent._agent_id_by_name
                envelope = MessageEnvelope(
                    msg_content=status_request,
                    msg_header=MessageHeader(
                        from_agent_id=agent.id,
                        to_routing_path=RoutingType.ALL_AGENTS_ROUTE
                        if target_is_known_name
                        else routing_key,
                        response_routing_key=getattr(
                            agent, "_operational_queue_name", None
                        )
                        or agent.id,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name=agent.name,
                        to_agent_name=raw_target if target_is_known_name else None,
                        conversation_id=conversation_id,
                        request_timezone=request_timezone,
                    ),
                )
                _run_agent_coroutine(agent, agent._send_operational_message(envelope))

        if agent_state_store is None:
            if live_requested and target_agent:
                results = []
            else:
                return json.dumps(
                    {"error": "AgentStateStore not initialized on this agent."}
                )
        else:
            results = agent_state_store.query(filters)
        return json.dumps(
            {
                "returned": len(results),
                "live_refresh_requested": live_requested,
                "live_refresh_targets": live_targets,
                "agents": [r.model_dump(exclude=set(AGENT_STATUS_RUNTIME_ONLY_FIELDS)) for r in results],
            },
            default=str,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to query AgentStateStore: {e}"})


@tool
def get_available_agent_types() -> str:
    """
    Get a list of all available agent types that can be instantiated.

    Returns a map of named agent types (like "CodeWritingAgent",
    "WebResearchAgent") to their descriptions: role and default tools (tool
    catalog ids with their names). With authorization on, default tools say
    what a type should be given; what an agent may use follows its grants.
    Use this to understand what specialist agents are available for delegation.

    Returns:
        JSON string containing a map of agent type names to their descriptions.
    """
    try:
        from custom_agents.access_runtime import authorization_enabled
        from custom_agents.team_config_helpers import get_all_named_agents_summary

        agent = get_agent_ref()
        # Describe every agent type's default tools for this team's authorization mode.
        summary = get_all_named_agents_summary(
            dict(current_agent_type_map()),
            authorization_enabled=authorization_enabled(getattr(agent, "authorization_config", None)),
        )
        return json.dumps(summary, indent=2)

    except ImportError as e:
        return json.dumps({"error": f"Could not import agent type configuration: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Error getting agent types: {e}"})


@tool
def instantiate_agent(
    agent_type: str,
    team_id: str,
    mcp_profile_names: Optional[list[str]] = None,
    idle_ttl_seconds: Optional[float] = None,
) -> str:
    """
    Create a new agent of the specified type.

    Instantiates a new specialist agent that will connect to the NATS
    message bus and be available for task delegation. The new agent
    will broadcast its status when it becomes operational.

    Args:
        agent_type: The named agent type to instantiate (e.g., "CodeWritingAgent",
                   "WebResearchAgent"). Use get_available_agent_types() to see options.
        team_id: The team ID this agent should belong to. Should match
                your own team_id for proper message routing.
        mcp_profile_names: Not accepted from agents; a non-empty value is
                refused. An agent uses an installed MCP server when it holds
                access to its tool (mcp:<name>); mcp_connections with
                action "propose" proposes installing one.
        idle_ttl_seconds: Optional idle TTL for this dynamically spawned agent.
                When omitted, TEAM_AGENTS.yaml ephemeral policy is used.

    Returns:
        Success message or error description.

    Example:
        instantiate_agent("CodeWritingAgent", "team_303")
    """
    agent = get_agent_ref()
    if agent is None:
        return "Error: Agent reference not set"
    if mcp_profile_names:
        return json.dumps(
            {
                "error": (
                    "Attaching MCP profiles changes the new agent's tool authority and requires "
                    "authenticated, deterministically recorded user authorization."
                ),
                "reason_code": "authenticated_user_authorization_required",
                "protected_action": "tool.attach",
                "requested_mcp_profile_names": sorted(
                    {
                        str(item).strip()
                        for item in mcp_profile_names
                        if str(item).strip()
                    }
                ),
            }
        )

    try:
        return _instantiate_agent_request(
            agent,
            agent_type,
            team_id,
            mcp_profile_names,
            idle_ttl_seconds,
        ).model_dump_json()
    except Exception as e:
        return f"Error instantiating agent: {e}"


@tool
def list_team_templates() -> str:
    """
    List workspace-backed team templates available to this assistant.

    Team templates live at the configured workspace team-templates path and
    capture operator- or onboarding-authored reusable starting teams. The
    manager can inspect and instantiate them, but generic agent file/shell tools
    cannot mutate the protected template catalog.

    Returns:
        JSON summary of the configured team templates.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    try:
        return json.dumps(
            summarize_team_templates(
                getattr(agent, "instance_root", None),
                getattr(agent, "team_templates_path", None),
            ),
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to load team templates: {e}"})


@tool
def instantiate_team_template(
    template_name: str,
    team_id: str = "",
    instantiate_missing_only: str = "true",
) -> str:
    """
    Instantiate all specialists defined by a named team template.

    Templates live in the team's TEAM_TEMPLATES.yaml control file; list them with
    list_team_templates. Each template contains a
    list of agent types plus counts and optional MCP profiles. By default this
    tool only creates the missing agents relative to currently known live agents
    on the same team so repeated calls do not blindly duplicate the full team.

    Args:
        template_name: Name of the template under `templates:` in the configured team templates file.
        team_id: Optional team to instantiate into. Defaults to the current team.
        instantiate_missing_only: "true" by default. When true, only create the
            missing delta between requested count and currently known non-exiting
            agents of that type on the target team.

    Returns:
        JSON object describing created and skipped members, or an error.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    try:
        loaded = load_team_templates(
            getattr(agent, "instance_root", None),
            getattr(agent, "team_templates_path", None),
        )
        template = loaded["templates"].get(template_name)
        if template is None:
            return json.dumps(
                {
                    "error": f"Unknown team template `{template_name}`.",
                    "available_templates": sorted(loaded["templates"].keys()),
                    "path": loaded["path"],
                }
            )

        target_team_id = team_id or agent.team_id
        missing_only = str(instantiate_missing_only).lower() in ("true", "1", "yes")
        state_store = getattr(agent, "_agent_state_store", None)
        created: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        errors: list[str] = []

        for member in template["members"]:
            member_agent_type = member["agent_type"]
            requested_count = int(member["count"])
            mcp_profile_names = list(member["mcp_profile_names"])
            existing_count = 0
            if missing_only and state_store is not None:
                statuses = state_store.query(
                    {"team_id": target_team_id, "named_agent_type": member_agent_type}
                )
                existing_count = sum(
                    1
                    for status in statuses
                    if not bool(getattr(status, "is_exiting", False))
                )
            create_count = (
                max(0, requested_count - existing_count)
                if missing_only
                else requested_count
            )

            if create_count == 0:
                skipped.append(
                    {
                        "agent_type": member_agent_type,
                        "requested_count": requested_count,
                        "existing_count": existing_count,
                        "created_count": 0,
                        "reason": "already_satisfied",
                    }
                )
                continue

            for _ in range(create_count):
                result = _instantiate_agent_request(
                    agent, member_agent_type, target_team_id, mcp_profile_names
                )
                if not result.accepted:
                    errors.append(f"{member_agent_type}: {result}")
                    continue
                created.append(
                    {
                        "agent_type": member_agent_type,
                        "mcp_profile_names": mcp_profile_names,
                        "launches": result.model_dump(mode="json")["results"],
                    }
                )

            if missing_only and existing_count:
                skipped.append(
                    {
                        "agent_type": member_agent_type,
                        "requested_count": requested_count,
                        "existing_count": existing_count,
                        "created_count": create_count,
                        "reason": "created_missing_delta",
                    }
                )

        return json.dumps(
            {
                "template_name": template_name,
                "team_id": target_team_id,
                "instantiate_missing_only": missing_only,
                "path": loaded["path"],
                "created": created,
                "skipped": skipped,
                "errors": errors,
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to instantiate team template: {e}"})


@tool
def send_control_instruction(target_agent: str, instruction: str) -> str:
    """
    Send a control instruction to another agent.

    Use this to manage agent lifecycle - pause, resume, or exit agents.

    Args:
        target_agent: The agent ID or name to send the instruction to.
        instruction: The control instruction - one of: "pause", "resume", "exit"

    Returns:
        Confirmation that the instruction was sent, or error message.
    """
    valid_instructions = ["pause", "resume", "exit"]
    if instruction.lower() not in valid_instructions:
        return f"Error: Invalid instruction '{instruction}'. Must be one of: {valid_instructions}"

    agent = get_agent_ref()
    if agent is None:
        return "Error: Agent reference not set"

    try:
        normalized_instruction = instruction.lower()
        _run_agent_coroutine(
            agent,
            _send_control_instruction_payload(
                agent, target_agent, normalized_instruction
            ),
        )

        return f"Control instruction '{normalized_instruction}' sent to {target_agent}"

    except Exception as e:
        return f"Error sending control instruction: {e}"


@tool
def get_current_task_status() -> str:
    """
    Get the status of the current task being processed.

    Returns information about the task currently in progress, including
    its ID, state, any subtasks, and associated queries.

    Returns:
        JSON string with current task status, or message if no task in progress.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    current_task_id = bound_task_id()

    if not current_task_id:
        return json.dumps({"status": "No task currently in progress"})

    task_info = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_info, current_task_id
    )
    if not task_info:
        return json.dumps({"error": f"Task {current_task_id} not found in manager"})

    # Get markdown summary
    summary = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.summarize_task_history_markdown, current_task_id
    )

    return json.dumps(
        {
            "task_id": current_task_id,
            "state": task_info.state.value
            if hasattr(task_info.state, "value")
            else str(task_info.state),
            "has_pending_subtasks": agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.has_pending_subtasks, current_task_id
            ),
            "summary": summary,
        }
    )


@tool
def get_runtime_pressure() -> str:
    """
    Return compact runtime-pressure telemetry for the manager's current task.

    Token counts are heuristic estimates, not provider-exact accounting. Use
    this to decide whether to continue inline, summarize and finish the current
    turn, or start isolated recovery with a compact brief.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return json.dumps(
            {
                "error": "get_runtime_pressure is manager-only; specialists should use task history/digest tools for their local task.",
                "manager_only": True,
            }
        )
    try:
        return json.dumps(build_runtime_pressure_snapshot(agent), default=str)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


PROVIDER = ToolProvider(
    name="agent_management",
    tools=(
        get_known_agents,
        query_agent_state,
        get_available_agent_types,
        instantiate_agent,
        list_team_templates,
        instantiate_team_template,
        send_control_instruction,
        get_current_task_status,
        get_runtime_pressure,
    ),
)
