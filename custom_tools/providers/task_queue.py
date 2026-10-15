"""Own-queue tools using the runtime's state and cancellation operations."""

from access_control.ownership import (
    PRIVATE_DELETE,
    PRIVATE_UPDATE,
    RESOURCE_TASK,
    private_access,
)
import json
from datetime import datetime, timezone
from functools import partial

from strands import tool

from custom_agents.control_runtime import stop_task_execution
from custom_agents.runtime_task_controls import QUEUE_PAUSE_EVENT
from custom_messages.enums import ProgressState
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._user_scope import _private_user_scope
from custom_tools.runtime_context import get_agent_ref
from id_generation import new_id


def _owned_root(agent, task_id, owner, action):
    manager = agent.task_manager
    info = manager.get_task_info(task_id)
    if info is None or manager.resolve_root_task_id(task_id) != task_id:
        raise ValueError("Choose an existing execution root from list_task_queue.")
    decision = private_access(
        person_id=owner,
        owner_id=info.owner_person_id,  # recorded at admission (TaskInfo)
        resource_type=RESOURCE_TASK,
        resource_id=task_id,
        action=PRIVATE_DELETE if action == "stop" else PRIVATE_UPDATE,
        team_id=agent.team_id,
    )
    if not decision.allowed:
        raise PermissionError("Only tasks owned by the current user are available.")
    return info


def _prepare_control(agent, task_id, owner, action, reason):
    """Validate and transition under the manager's serialized storage boundary."""
    manager = agent.task_manager
    info = _owned_root(agent, task_id, owner, action)
    graph = manager.get_execution_graph_task_infos(task_id)
    if manager.is_terminal_state(info.state):
        raise ValueError("This task has already finished.")
    if (
        any(item.task.task_id == manager.current_task_id for item in graph)
        or task_id == manager.current_task_id
    ):
        raise ValueError(
            "A queue tool cannot change its own executing task. Return a task response or ask the user to stop it."
        )
    if action == "stop":
        return info
    if not manager.is_locally_executable_task(task_id):
        raise ValueError(
            "This task is executed by another agent; its execution owner must control it."
        )
    if (
        task_id in manager.task_state.human_interaction_blockers
        or manager.get_pending_recoverable_pause(task_id)
        or manager.is_waiting_on_task_query(task_id)
        or manager.has_pending_subtasks(task_id)
        or manager.is_plan_execution_root_active(task_id)
        or manager.get_active_runtime_lease(task_id)
        or manager.is_final_review_pending(task_id)
    ):
        raise ValueError(
            "This task has unresolved input, approval, delegated work or runtime review. Resolve its blocker before changing its queue state."
        )
    if action == "pause" and not manager.is_runnable_state(info.state):
        raise ValueError("Only queued runnable work can be paused with this tool.")
    if action == "resume" and info.state != ProgressState.PAUSED:
        raise ValueError(
            "Only paused work can be resumed. Waiting work must receive its required result or input through the runtime."
        )
    state = ProgressState.PAUSED if action == "pause" else ProgressState.ACCEPTED
    changed_at = datetime.now(timezone.utc).isoformat()
    if action == "pause":
        # Write the marker first. A crash before the transition leaves runnable
        # work; a completed pause always has its durable recovery explanation.
        manager.log_agent_note(
            task_id,
            note=f"Queue pause: {reason}",
            agent_name=agent.name,
            agent_id=agent.id,
            details={"event": QUEUE_PAUSE_EVENT, "paused_at": changed_at},
        )
    if not manager.update_main_task_state(task_id, state, changed_at):
        raise ValueError(
            "The runtime rejected this state change; inspect the task again."
        )
    if action == "resume" and not manager.enqueue_task(task_id):
        raise ValueError("The runtime did not admit this task to the queue.")
    if action == "resume":
        manager.log_agent_note(
            task_id,
            note=f"Queue resume: {reason}",
            agent_name=agent.name,
            agent_id=agent.id,
        )
    return info


@tool
async def list_task_queue(offset: int = 0, limit: int = 20) -> str:
    """Inspect this agent's unfinished tasks for the current user, including blockers and delegated children.

    Use task IDs with get_task_history_window before stopping duplicates or
    resuming work. Paging is bounded to 20 tasks per call.
    """
    from custom_agents.manager_control_runtime import (
        _runtime_work_roots,
        _runtime_work_root_summary,
    )
    from custom_agents.runtime_task_controls import _queue_snapshot

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    owner, denial = _private_user_scope(agent, resource_name="Task queue")
    if denial:
        return json.dumps(denial)
    roots = await _runtime_work_roots(agent, owner)
    rows = await agent.task_manager.operations_runtime.run(
        partial(_queue_snapshot, agent.task_manager), roots
    )
    by_id = {root.task.task_id: root for root in roots}
    tasks = []
    for row in rows[max(0, offset) : max(0, offset) + max(1, min(limit, 20))]:
        root = by_id[row["root_task_id"]]
        summary = await _runtime_work_root_summary(agent, root)
        tasks.append(
            {
                **summary.model_dump(mode="json"),
                **row,
                "problem_statement": str(root.task.problem_statement or "")[:2000],
            }
        )
    return json.dumps({"agent_id": agent.id, "total": len(rows), "tasks": tasks})


@tool
async def control_task_queue(task_id: str, action: str, reason: str) -> str:
    """Pause, resume or stop another task in this agent's own queue for the current user.

    Inspect list_task_queue and task history first. Stop cancels attached work
    and preserves history. To consolidate confirmed duplicates, stop redundant
    tasks and resume the retained task if it has no blocker. Resume never grants
    access, answers a pending question or bypasses a runtime budget pause.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    owner, denial = _private_user_scope(agent, resource_name="Task queue")
    if denial:
        return json.dumps(denial)
    if action not in {"pause", "resume", "stop"} or not reason.strip():
        return json.dumps(
            {"error": "Choose pause, resume or stop and provide a reason."}
        )
    try:
        info = await agent.task_manager.operations_runtime.run(
            partial(_prepare_control, agent), task_id, owner, action, reason
        )
        if action == "stop":
            result = await stop_task_execution(
                agent,
                task_info=info,
                reason=reason,
                cascade_to_subtasks=True,
                canonical_user_id=owner,
                command_id=new_id("queue_control"),
                send_response=False,
            )
            return json.dumps(result, default=str)
        return json.dumps({"task_id": task_id, "state": info.state.value})
    except (ValueError, PermissionError) as exc:
        return json.dumps({"error": str(exc), "task_id": task_id})


PROVIDER = ToolProvider(name="task_queue", tools=(list_task_queue, control_task_queue))
