"""Runtime support for the trusted Strands tool surface."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal, Optional

from access_control.entities import PersonPrincipal
from access_control.identity import IdentityRejected
from access_control.ownership import PRIVATE_UPDATE, named_task_access, objective_access
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.task_manager_objectives import ObjectiveSaveFailed
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
from storage.objective_repository import ObjectiveView

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


class _UnverifiedTask(Exception):
    """The active task has no valid identity, so it reaches no Objective."""


def _task_person(agent: "UniversalAgent") -> Optional[PersonPrincipal]:
    """Whom the active task works for, with roles, from its verified identity only.

    None is work for no person. A task without a valid signed context or
    schedule run identity raises ``_UnverifiedTask`` instead of counting as
    work for no person, which may change the team's Objectives.
    """

    from custom_agents.access_runtime import runtime_access, task_person

    try:
        person, _task_id = task_person(agent, runtime_access(agent))
    except (IdentityRejected, RuntimeError) as exc:
        logger.warning(
            "Objective tools refused a task without a verified identity. "
            "event=objective_task_identity_unverified agent_id=%s task_id=%s reason=%s",
            getattr(agent, "id", None),
            agent.task_manager.current_task_id,
            exc,
        )
        raise _UnverifiedTask(str(exc)) from exc
    return person


def _objective_view(agent: "UniversalAgent") -> Optional[ObjectiveView]:
    """The Objectives the active task may list, or None when it may list none."""

    try:
        person = _task_person(agent)
    except _UnverifiedTask:
        return None
    return ObjectiveView(person_id=person.person_id if person is not None else None)


def _objective_in_view(agent: "UniversalAgent", objective_id: str, *, action: str) -> bool:
    """Whether the active task may read or change (``action``) one Objective.

    Missing and inaccessible Objectives both fail here, so callers cannot
    distinguish another person's record from an unknown id.
    """

    objective = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_objective, objective_id
    )
    if objective is None:
        return False
    try:
        person = _task_person(agent)
    except _UnverifiedTask:
        return False
    return objective_access(
        person_id=person.person_id if person is not None else None,
        owner_id=objective.owner_canonical_user_id,
        objective_id=objective.objective_id,
        action=action,
        roles=person.roles if person is not None else (),
    ).allowed


def _resolve_objective_id_for_tool(
    agent: "UniversalAgent",
    objective_id: str = "",
    task_id: str = "",
    plan_id: str = "",
    *,
    action: str,
) -> Optional[str]:
    """The Objective a tool call means, if the active task may ``action`` it.

    Found by ``objective_id``, ``plan_id`` or ``task_id``, else from the
    current task. An Objective out of the task's view (another person's, or a
    team Objective it may only read) resolves to None, like one that does not
    exist, so the reply does not reveal it (``ownership.objective_access``).
    """

    resolved: Optional[str]
    if objective_id:
        resolved = objective_id
    elif plan_id:
        resolved = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_plan_objective_id, plan_id
        )
    else:
        resolved_task_id = task_id or agent.task_manager.current_task_id
        resolved = (
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.get_task_objective_id, resolved_task_id
            )
            if resolved_task_id
            else None
        )
    if not resolved:
        return None
    return resolved if _objective_in_view(agent, resolved, action=action) else None


def _objective_to_link(
    agent: "UniversalAgent", objective_id: str, task_id: str
) -> tuple[Optional[str], Optional[str]]:
    """The Objective a new schedule or heartbeat item links to, or why it cannot.

    Only the team lead links Objectives. Linking changes the Objective, so the
    task must be allowed to change it. A named Objective it may not change is
    refused as unknown; with none named, the current task's Objective is
    linked when the task may change it, and otherwise nothing is.
    """

    if getattr(agent, "named_agent_type", None) != TEAM_ARCHITECT_AGENT_TYPE:
        return None, None
    resolved = _resolve_objective_id_for_tool(agent, objective_id, task_id, "", action=PRIVATE_UPDATE)
    named = str(objective_id or "").strip()
    if named and not resolved:
        return None, f"Unknown Objective {named!r} for this manager."
    return resolved, None


def _named_task_refusal(agent: "UniversalAgent", task_id: str) -> Optional[str]:
    """Why the active task may not name ``task_id`` as a source of work, or None.

    Even the current task must have a verified identity. Another task must
    work for the same person (``ownership.named_task_access``); its person
    is recorded at admission, read from the archive once it has finished.
    """

    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    try:
        person = _task_person(agent)
    except _UnverifiedTask:
        return "This task has no verified identity, so it cannot name other tasks."
    found, owner = _task_owner(agent, task_id)
    if found and named_task_access(
        person_id=person.person_id if person is not None else None,
        owner_id=owner,
        task_id=task_id,
    ).allowed:
        return None
    return f"Task {task_id} was not found among this person's tasks."


def _task_owner(agent: "UniversalAgent", task_id: str) -> tuple[bool, Optional[str]]:
    """Whether the task exists (live here, or archived by any agent of the team), and its person."""

    manager = agent.task_manager
    info = manager.operations_runtime.run_sync(manager.get_task_info, task_id)
    if info is not None:
        return True, info.owner_person_id
    # Task ids are unique, and a finished task may have been archived by an
    # earlier process of this agent type under another agent id.
    archived = manager.task_archive_repository.get_archived_task(
        task_id, owner_agent_id=None, include_all_agents=True
    )
    if archived is None:
        return False, None
    return True, archived.owner_canonical_user_id


def _link_to_objective(
    agent: "UniversalAgent",
    kind: Literal["schedule", "heartbeat"],
    objective_id: Optional[str],
    item_id: str,
    **link_options: Any,
) -> dict[str, str]:
    """Record a schedule or heartbeat item that now exists on its Objective, if it has one.

    Returns fields for the tool's reply:
    none, or ``objective_link_warning`` when the Objective could not record
    the item. The item stays, since it was made as asked, but an Objective
    that does not list it does not pause or stop it, so the reply says so
    instead of reporting a complete link.
    """

    if not objective_id:
        return {}
    manager = agent.task_manager
    link = manager.link_schedule_to_objective if kind == "schedule" else manager.link_heartbeat_to_objective
    try:
        manager.operations_runtime.run_sync(link, objective_id, item_id, **link_options)
    except ObjectiveSaveFailed:
        return {
            "objective_link_warning": (
                f"{item_id} was saved, but Objective {objective_id} could not record it, so pausing "
                "or closing the Objective will not pause or stop it. Tell the person."
            )
        }
    return {}


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
