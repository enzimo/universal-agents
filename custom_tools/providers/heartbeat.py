"""Heartbeat support for the trusted Strands tool surface."""

import json
import logging
from typing import TYPE_CHECKING, Any

from strands import tool

from access_control.ownership import PRIVATE_READ, PRIVATE_UPDATE
from custom_agents.heartbeat_store import (
    HeartbeatItemRecord,
    HeartbeatStore,
)
from custom_messages.enums import TaskHistorySource
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import (
    get_agent_ref,
)
from id_generation import (
    HEARTBEAT_SCHEDULE_ID_PREFIX,
    HEARTBEAT_SOURCE_ID_PREFIX,
    operational_id,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


from custom_tools.providers._configuration import (
    _resolve_notify_user_instance_config_path,
)
from custom_tools.providers._runtime import (
    _new_conversation_id,
    _objective_in_view,
    _resolve_heartbeat_source_id,
    _objective_to_link,
)
from custom_tools.providers._user_scope import _private_user_scope

logger = logging.getLogger(__name__)


def _heartbeat_item_in_view(agent: "UniversalAgent", item: HeartbeatItemRecord, *, action: str) -> bool:
    """An existing link decides whose work this item may read or change."""

    return not item.objective_id or _objective_in_view(agent, item.objective_id, action=action)


def _heartbeat_item_payload(item: HeartbeatItemRecord) -> dict:
    return {
        "heartbeat_item_id": item.heartbeat_item_id,
        "source_id": item.source_id,
        "title": item.title,
        "instruction": item.instruction,
        "conversation_id": item.conversation_id,
        "target_agent_type": item.target_agent_type,
        "objective_id": item.objective_id,
        "enabled": item.enabled,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_run_at": item.last_run_at,
        "last_success_at": item.last_success_at,
        "last_failure_at": item.last_failure_at,
    }


def _heartbeat_store(agent: "UniversalAgent") -> HeartbeatStore:
    return HeartbeatStore(
        db_path=agent.task_manager._db_path,
        storage_settings=agent.task_manager.storage_settings,
    )


def _heartbeat_runtime_state(agent: "UniversalAgent", source_id: str) -> dict[str, Any]:
    state: dict[str, Any] = {
        "source_id": source_id,
        "config_available": False,
        "heartbeat_enabled": None,
        "configured_source_id": None,
        "configured_schedule_id": None,
        "launcher_schedule_installed": None,
        "notes": [],
    }
    config_path = _resolve_notify_user_instance_config_path(agent)
    if not config_path:
        state["notes"].append(
            "No instance_config.yaml path was available to check heartbeat enablement."
        )
        return state

    try:
        from assistant_gateway.config import GatewayInstanceConfig

        config = GatewayInstanceConfig.load_for_process(config_path)
    except Exception as exc:
        state["notes"].append(
            f"Could not load instance_config.yaml to check heartbeat enablement: {exc}"
        )
        return state

    configured_source_id = config.heartbeat.source_id or operational_id(
        HEARTBEAT_SOURCE_ID_PREFIX, config.instance_name
    )
    configured_schedule_id = config.heartbeat.schedule_id or operational_id(
        HEARTBEAT_SCHEDULE_ID_PREFIX, config.instance_name
    )
    state.update(
        {
            "config_available": True,
            "heartbeat_enabled": bool(config.heartbeat.enabled),
            "configured_source_id": configured_source_id,
            "configured_schedule_id": configured_schedule_id,
        }
    )
    if configured_source_id != source_id:
        state["notes"].append(
            f"Requested source_id {source_id!r} does not match configured heartbeat source {configured_source_id!r}."
        )

    schedule_store = getattr(agent, "_schedule_store", None)
    if schedule_store is not None:
        try:
            state["launcher_schedule_installed"] = (
                schedule_store.get_scheduled_message(configured_schedule_id) is not None
            )
        except Exception as exc:
            state["notes"].append(
                f"Could not inspect heartbeat launcher schedule: {exc}"
            )

    if state["heartbeat_enabled"] is False:
        state["notes"].append(
            "Heartbeat is disabled in instance_config.yaml; heartbeat items will not fire until it is enabled "
            "and the gateway installs the heartbeat launcher schedule."
        )
    elif (
        state["heartbeat_enabled"] is True
        and state["launcher_schedule_installed"] is False
    ):
        state["notes"].append(
            "Heartbeat is enabled in instance_config.yaml, but the manager-owned heartbeat launcher schedule "
            "is not installed in this agent's schedule store."
        )
    return state


def _objective_heartbeat_store_for_cascade(
    agent: "UniversalAgent",
    objective: Any,
    heartbeat_links: list[Any],
) -> tuple[HeartbeatStore | None, tuple[str, ...]]:
    if not heartbeat_links:
        return None, ()
    try:
        return _heartbeat_store(agent), ()
    except Exception as exc:
        warning = (
            "Objective status changed, but linked heartbeat items could not be "
            "reconciled; operator review is required."
        )
        getattr(agent, "_logger", logger).warning(
            "Objective heartbeat lifecycle cascade could not resolve its store. "
            "event=objective_heartbeat_cascade_store_failed objective_id=%s "
            "status=%s linked_heartbeat_count=%s error_type=%s",
            objective.objective_id,
            objective.status.value,
            len(heartbeat_links),
            type(exc).__name__,
            exc_info=True,
        )
        return None, (warning,)


@tool
def list_heartbeat_items(source_id: str = "", include_disabled: bool = True) -> str:
    """List canonical heartbeat items for the current source or an explicit source id."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        resolved_source_id = _resolve_heartbeat_source_id(agent, source_id)
        store = _heartbeat_store(agent)
        runtime_state = _heartbeat_runtime_state(agent, resolved_source_id)
        source = store.get_source(resolved_source_id)
        if source is None:
            status = (
                "disabled"
                if runtime_state.get("heartbeat_enabled") is False
                else "missing_source"
            )
            payload = {
                **runtime_state,
                "status": status,
                "source_exists": False,
                "returned": 0,
                "items": [],
            }
            if status == "missing_source":
                payload["warning"] = (
                    f"Heartbeat source not found: {resolved_source_id}. "
                    "If heartbeat is enabled, the gateway may not have installed the heartbeat source/launcher "
                    "or the gateway and manager may be using different manager storage."
                )
            return json.dumps(payload)
        items = store.list_items(resolved_source_id, enabled_only=not include_disabled)
        items = [item for item in items if _heartbeat_item_in_view(agent, item, action=PRIVATE_READ)]
        return json.dumps(
            {
                **runtime_state,
                "status": "ok",
                "source_exists": True,
                "source_id": source.source_id,
                "title": source.title,
                "enabled": source.enabled,
                "updated_at": source.updated_at,
                "returned": len(items),
                "items": [_heartbeat_item_payload(item) for item in items],
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def add_heartbeat_item(
    instruction: str,
    title: str = "",
    source_id: str = "",
    target_agent_type: str = "",
    objective_id: str = "",
) -> str:
    """Add a canonical heartbeat item with its own durable conversation thread."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    if not instruction.strip():
        return json.dumps({"error": "instruction is required"})
    try:
        resolved_source_id = _resolve_heartbeat_source_id(agent, source_id)
        resolved_objective_id, objective_refusal = _objective_to_link(
            agent, objective_id, agent.task_manager.current_task_id or ""
        )
        if objective_refusal:
            return json.dumps({"error": objective_refusal})
        store = _heartbeat_store(agent)
        store.ensure_source(
            source_id=resolved_source_id, title=f"Heartbeat:{resolved_source_id}"
        )
        item = store.add_item(
            source_id=resolved_source_id,
            title=title.strip() or instruction.strip()[:80],
            instruction=instruction.strip(),
            conversation_id=_new_conversation_id(),
            target_agent_type=target_agent_type.strip() or None,
            objective_id=resolved_objective_id,
        )
        if resolved_objective_id:
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.link_heartbeat_to_objective,
                resolved_objective_id,
                item.heartbeat_item_id,
                managed_by_objective=True,
                user_visible=True,
                reason="Heartbeat item created from objective-linked task context.",
            )
        payload = _heartbeat_item_payload(item)
        runtime_state = _heartbeat_runtime_state(agent, resolved_source_id)
        payload.update(
            {
                "heartbeat_runtime": runtime_state,
            }
        )
        if runtime_state.get("heartbeat_enabled") is False:
            payload["warning"] = (
                "Heartbeat item was saved, but heartbeat.enabled is false in instance_config.yaml. "
                "This item will not fire until heartbeat is enabled and the gateway installs the heartbeat launcher schedule."
            )
        elif (
            runtime_state.get("heartbeat_enabled") is True
            and runtime_state.get("launcher_schedule_installed") is False
        ):
            payload["warning"] = (
                "Heartbeat item was saved, but the heartbeat launcher schedule is not installed in this agent's "
                "schedule store. The gateway may need to be restarted or storage wiring checked."
            )
        return json.dumps(payload)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def update_heartbeat_item(
    heartbeat_item_id: str,
    *,
    title: str = "",
    instruction: str = "",
    target_agent_type: str = "",
    objective_id: str = "",
) -> str:
    """Update title, instruction, or target agent type for a heartbeat item."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        store = _heartbeat_store(agent)
        existing = store.get_item(heartbeat_item_id)
        if existing is None or not _heartbeat_item_in_view(agent, existing, action=PRIVATE_UPDATE):
            return json.dumps({"error": f"Heartbeat item not found: {heartbeat_item_id}"})
        resolved_objective_id, objective_refusal = (
            _objective_to_link(agent, objective_id, "") if objective_id.strip() else (None, None)
        )
        if objective_refusal:
            return json.dumps({"error": objective_refusal})
        item = store.update_item(
            heartbeat_item_id,
            title=title if title else None,
            instruction=instruction if instruction else None,
            target_agent_type=target_agent_type if target_agent_type else None,
            objective_id=resolved_objective_id,
        )
        if item is None:
            return json.dumps(
                {"error": f"Heartbeat item not found: {heartbeat_item_id}"}
            )
        if resolved_objective_id:
            agent.task_manager.operations_runtime.run_sync(
                agent.task_manager.link_heartbeat_to_objective,
                resolved_objective_id,
                item.heartbeat_item_id,
                managed_by_objective=True,
                user_visible=True,
                reason="Heartbeat item updated with objective link.",
            )
        return json.dumps(_heartbeat_item_payload(item))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def remove_heartbeat_item(heartbeat_item_id: str) -> str:
    """Remove a heartbeat item permanently."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        store = _heartbeat_store(agent)
        existing = store.get_item(heartbeat_item_id)
        if existing is None or not _heartbeat_item_in_view(agent, existing, action=PRIVATE_UPDATE):
            return json.dumps({"heartbeat_item_id": heartbeat_item_id, "removed": False})
        removed = store.remove_item(heartbeat_item_id)
        return json.dumps({"heartbeat_item_id": heartbeat_item_id, "removed": removed})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def enable_heartbeat_item(heartbeat_item_id: str) -> str:
    """Enable a heartbeat item."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        store = _heartbeat_store(agent)
        existing = store.get_item(heartbeat_item_id)
        if existing is None or not _heartbeat_item_in_view(agent, existing, action=PRIVATE_UPDATE):
            return json.dumps({"error": f"Heartbeat item not found: {heartbeat_item_id}"})
        item = store.set_item_enabled(heartbeat_item_id, True)
        if item is None:
            return json.dumps(
                {"error": f"Heartbeat item not found: {heartbeat_item_id}"}
            )
        return json.dumps(_heartbeat_item_payload(item))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def disable_heartbeat_item(heartbeat_item_id: str) -> str:
    """Disable a heartbeat item."""
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        store = _heartbeat_store(agent)
        existing = store.get_item(heartbeat_item_id)
        if existing is None or not _heartbeat_item_in_view(agent, existing, action=PRIVATE_UPDATE):
            return json.dumps({"error": f"Heartbeat item not found: {heartbeat_item_id}"})
        item = store.set_item_enabled(heartbeat_item_id, False)
        if item is None:
            return json.dumps(
                {"error": f"Heartbeat item not found: {heartbeat_item_id}"}
            )
        return json.dumps(_heartbeat_item_payload(item))
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
def get_task_history_window(
    task_id: str = "",
    start_index: int = 0,
    limit: int = 20,
    include_system: bool = True,
    include_agent: bool = True,
) -> str:
    """
    Retrieve a window of execution history for a task.

    Pass an explicit task_id to inspect any known or archived task. If omitted,
    the current in-progress task is used.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})

    resolved_task_id = task_id or agent.task_manager.current_task_id
    if not resolved_task_id:
        return json.dumps(
            {
                "error": "No task_id provided and no task currently in progress",
                "hint": "Call list_archived_tasks() first to discover archived task IDs.",
            }
        )

    current_owner, denial = _private_user_scope(agent, resource_name="Task history")
    if denial is not None:
        return json.dumps(denial)

    task_info = agent.task_manager.operations_runtime.run_sync(
        agent.task_manager.get_task_info, resolved_task_id
    )
    task = getattr(task_info, "task", None) if task_info is not None else None
    if task_info is not None:
        # The owner recorded at admission, as on the archived row below.
        if task_info.owner_person_id != current_owner:
            return json.dumps({"error": f"Task {resolved_task_id} not found"})
    else:
        archived = agent.task_manager.operations_runtime.run_sync(
            agent.task_manager.get_task_from_db,
            resolved_task_id,
            owner_canonical_user_id=current_owner,
            include_all_agents=True,
        )
        if not archived:
            return json.dumps({"error": f"Task {resolved_task_id} not found"})
        task = archived["main_task"]

    raw_entries = list(getattr(task, "execution_history", []) or [])
    entries = []
    for entry in raw_entries:
        if entry.source == TaskHistorySource.SYSTEM and not include_system:
            continue
        if entry.source == TaskHistorySource.AGENT and not include_agent:
            continue
        entries.append(entry)
    if start_index < 0:
        start_index = 0
    entries = (
        entries[start_index : start_index + limit]
        if limit is not None and limit >= 0
        else entries[start_index:]
    )

    return json.dumps(
        {
            "task_id": resolved_task_id,
            "start_index": start_index,
            "limit": limit,
            "returned": len(entries),
            "entries": [entry.model_dump() for entry in entries],
        },
        default=str,
    )


PROVIDER = ToolProvider(
    name="heartbeat",
    tools=(
        list_heartbeat_items,
        add_heartbeat_item,
        update_heartbeat_item,
        remove_heartbeat_item,
        enable_heartbeat_item,
        disable_heartbeat_item,
    ),
)
