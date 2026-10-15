"""Runtime support for the trusted Strands tool surface."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.schedule_store import (
    ScheduleStore,
)
from custom_messages.contracts.control_v1 import ControlInstruction, Instruction
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, RoutingType
from id_generation import (
    new_id,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


logger = logging.getLogger(__name__)


async def _send_control_instruction_payload(
    agent: "UniversalAgent", target_agent: str, instruction: str
) -> str:
    routing_key = agent._agent_id_by_name.get(target_agent, target_agent)
    target_is_known_name = target_agent in agent._agent_id_by_name
    envelope = MessageEnvelope(
        msg_content=ControlInstruction(
            type=ControlType.CONTROL_MSG,
            instruction=Instruction(instruction=instruction),
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            to_agent_name=target_agent if target_is_known_name else None,
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=RoutingType.ALL_AGENTS_ROUTE
            if target_is_known_name
            else routing_key,
            agent_signature="",
        ),
    )
    if hasattr(agent, "_send_operational_message"):
        await agent._send_operational_message(envelope)
    else:
        await agent.team_comms.send_message(message=envelope, routing_key=routing_key)
    return routing_key


def _run_agent_coroutine(
    agent: "UniversalAgent", coroutine: Any, *, timeout: float = 15.0
) -> None:
    """Schedule an agent coroutine from either the agent loop or a sync tool worker thread."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        runtime_loop = getattr(agent, "_runtime_loop", None)
        if runtime_loop is not None and runtime_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coroutine, runtime_loop)
            future.result(timeout=timeout)
            return
        asyncio.run(coroutine)
        return

    if loop.is_running():
        asyncio.create_task(coroutine)
    else:
        loop.run_until_complete(coroutine)


def _require_manager_tool(agent: "UniversalAgent", tool_name: str) -> Optional[str]:
    if getattr(agent, "named_agent_type", None) == TEAM_ARCHITECT_AGENT_TYPE:
        return None
    return f"{tool_name} is manager-only; specialists should use task-local task history and digest tools."


def _resolve_objective_id_for_tool(
    agent: "UniversalAgent",
    objective_id: str = "",
    task_id: str = "",
    plan_id: str = "",
) -> Optional[str]:
    if objective_id:
        return objective_id
    if plan_id:
        return agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_plan_objective_id, plan_id
        )
    resolved_task_id = task_id or agent.task_manager.current_task_id
    if resolved_task_id:
        return agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_objective_id, resolved_task_id
        )
    return None


def _resolve_heartbeat_source_id(
    agent: "UniversalAgent", explicit_source_id: str = ""
) -> str:
    if explicit_source_id.strip():
        return explicit_source_id.strip()
    current_task_id = agent.task_manager.current_task_id
    task_info = (
        agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_info, current_task_id
        )
        if current_task_id
        else None
    )
    task_meta = getattr(task_info, "meta", None)
    gateway_context = getattr(task_meta, "gateway_context", None)
    source_id = getattr(gateway_context, "heartbeat_source_id", None)
    if source_id:
        return source_id
    raise ValueError(
        "No heartbeat_source_id was provided and the current task has no gateway heartbeat context."
    )


def _new_conversation_id() -> str:
    return new_id("conversation_id")


def _get_current_task_info(agent: "UniversalAgent"):
    current_task_id = agent.task_manager.current_task_id
    if not current_task_id:
        raise ValueError("No task currently in progress.")
    task_info = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_info, current_task_id
    )
    if task_info is None:
        raise ValueError(f"Current task info missing for task_id={current_task_id}.")
    return task_info


def _schedule_store(agent: "UniversalAgent") -> ScheduleStore:
    store = agent._schedule_store
    if store is None:
        raise ValueError("ScheduleStore not initialized on this agent.")
    return store
