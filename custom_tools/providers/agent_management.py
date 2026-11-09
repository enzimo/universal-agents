"""Agent management support for the trusted Strands tool surface."""

import json
from typing import Any
import logging
from typing import TYPE_CHECKING, Optional

from strands import tool

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.contracts.schedule_worker_v1 import AgentCreationResultV1
from custom_agents.team_templates import load_team_templates, summarize_team_templates
from custom_messages.contracts.control_v1 import AGENT_STATUS_RUNTIME_ONLY_FIELDS, AgentCreationRequest
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_tools.agent_team_port import current_agent_type_map
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import (
    bound_task_id,
    get_agent_ref,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


from custom_tools.providers._runtime import (
    CONTROL_WAIT_MARGIN_SECONDS,
    _run_agent_coroutine,
    _send_control_instruction_payload,
    control_reply_seconds,
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


def _refused_for_another_contract(result: AgentCreationResultV1) -> Optional[str]:
    """The model's answer when the process runs another message contract and starts no agents; else None."""

    from custom_agents.agent_instantiation_service import LAUNCH_REFUSED_OTHER_CONTRACT, NO_NEW_WORK_MESSAGE

    if result.results and all(item.reason == LAUNCH_REFUSED_OTHER_CONTRACT for item in result.results):
        return json.dumps({"error": NO_NEW_WORK_MESSAGE, "reason_code": LAUNCH_REFUSED_OTHER_CONTRACT})
    return None


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


# How long query_agent_state(live=true) waits for the agents' answers, all asked at once.
LIVE_STATUS_SECONDS = 10.0


async def _live_statuses(agent: Any, targets: dict[str, str], level: str) -> dict[str, dict[str, Any]]:
    """Each target's own status answer (``agent.status``), asked at once under one deadline, or why it has none.

    ``targets`` maps what the caller asked for (a name or an id) to the agent id. One target's
    failure, whatever it is, is reported for that target only (``agent_requests.ask_each``).
    """

    from custom_agents.agent_requests import ask_each, cannot_ask

    blocked = cannot_ask(agent, need_view=False)
    if blocked is not None:
        return {asked: {"outcome": "unavailable", "message": blocked.reason} for asked in targets}
    answers = await ask_each(agent, "agent.status", {"level": level}, set(targets.values()), timeout=LIVE_STATUS_SECONDS)
    found: dict[str, dict[str, Any]] = {}
    for asked, target_id in targets.items():
        answer = answers[target_id]
        if isinstance(answer, Exception):
            logger.info("An agent's live status could not be read. event=agent_live_status_unavailable agent_id=%s "
                        "target_agent_id=%s error_type=%s error=%s", agent.id, target_id, type(answer).__name__, answer)
            answer = {"outcome": "unavailable", "message": f"{type(answer).__name__}: {answer}"}
        found[asked] = answer
    return found


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
        live: When "true", "1", or "yes", also ask each matching agent (or `target_agent`) for its
            status now, all at once, waiting at most 10 seconds in all, and include each answer under
            "live_status"; an agent that could not be asked or did not answer gets
            {"outcome": "unavailable", "message": <why>}.
        target_agent: Optional explicit agent ID/name to ask when `live=true`.
        info_level: Detail of the live answer when `live=true` - "basic" or "full" (full adds diagnostics).

    Returns:
        JSON object containing matching AgentStatus rows and, with `live`, each agent's own answer.
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
        live_answers: dict[str, Any] = {}
        if live_requested:
            if target_agent:
                live_targets = [target_agent]
            elif agent_state_store is None:
                return json.dumps({"error": "AgentStateStore not initialized on this agent."})
            else:
                live_targets = [status.self_id for status in agent_state_store.query(filters) if status.self_id]
            level = "full" if info_level == "full" else "basic"
            targets = {raw: agent._agent_id_by_name.get(raw, raw) for raw in live_targets}
            # All at once, within LIVE_STATUS_SECONDS, however many there are (A21).
            live_answers = _run_agent_coroutine(agent, _live_statuses(agent, targets, level),
                                                timeout=LIVE_STATUS_SECONDS + CONTROL_WAIT_MARGIN_SECONDS)

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
                "agents": [r.model_dump(exclude=set(AGENT_STATUS_RUNTIME_ONLY_FIELDS)) for r in results],
                **({"live_status": live_answers} if live_requested else {}),
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
        result = _instantiate_agent_request(
            agent,
            agent_type,
            team_id,
            mcp_profile_names,
            idle_ttl_seconds,
        )
    except Exception as e:
        return f"Error instantiating agent: {e}"
    return _refused_for_another_contract(result) or result.model_dump_json()


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
                refused = _refused_for_another_contract(result)
                if refused is not None:
                    return refused  # nothing else would start either
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
        What the agent answered (for example "paused" or "already paused"), or why it could not be reached.
    """
    valid_instructions = ["pause", "resume", "exit"]
    if instruction.lower() not in valid_instructions:
        return f"Error: Invalid instruction '{instruction}'. Must be one of: {valid_instructions}"

    agent = get_agent_ref()
    if agent is None:
        return "Error: Agent reference not set"

    normalized_instruction = instruction.lower()
    try:
        # Waits longer than the request may take, so the request's own deadline answers first.
        return _run_agent_coroutine(
            agent,
            _send_control_instruction_payload(
                agent, target_agent, normalized_instruction
            ),
            timeout=control_reply_seconds() + CONTROL_WAIT_MARGIN_SECONDS,
        )
    except TimeoutError as e:
        return (
            f"{target_agent} did not answer in time ({e}), so whether it will "
            f"{normalized_instruction} is unknown. Check its state with query_agent_state(live=true)."
        )
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
