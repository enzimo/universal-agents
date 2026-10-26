"""Owner-scoped task inspection and cancellation for Settings.

The manager owns the live queue. Browser controls use its normal cancellation
path and persist restart barriers, never delete runtime rows behind its back.
"""

from access_control.ownership import (
    PRIVATE_DELETE,
    RESOURCE_TASK,
    private_access,
)
from datetime import datetime, timezone
import json
from functools import partial
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.contracts.control_v1 import FinishedTaskSummary

QUEUE_PAUSE_EVENT = "task_queue_explicit_pause"


if TYPE_CHECKING:
    from custom_agents.manager_control_runtime import CommandPerson

def has_explicit_queue_pause(manager: Any, task_id: str) -> bool:
    """A trusted queue pause applies only to its exact state transition."""
    info = manager.get_task_info(task_id)
    if info is None or not info.task.task_state:
        return False
    state, timestamp = info.task.task_state[-1]
    if str(getattr(state, "value", state)) != "paused":
        return False
    return any(
        entry.details.get("event") == QUEUE_PAUSE_EVENT
        and entry.details.get("paused_at") == timestamp
        for entry in info.task.execution_history if entry.details
    )


def finished_task_inventory(agent: Any, command: ManagerRuntimeWorkCommand, *, person: "CommandPerson") -> tuple[list[FinishedTaskSummary], int]:
    rows, total = agent.task_manager.task_archive_repository.list_finished_tasks(
        team_id=agent.team_id, owner_canonical_user_id=person.canonical_user_id,
        offset=command.task_offset, limit=command.task_limit,
    )
    result = []
    for row in rows:
        payload = json.loads(row.task_json)
        answer = payload.get("answer") or ""
        if isinstance(answer, dict):
            answer = answer.get("answer") or ""
        answer = str(answer)
        if len(answer) > 12000:
            answer = answer[:12000] + "\n[Result shortened. Ask the agent to inspect this task's history for the full answer.]"
        result.append(FinishedTaskSummary(
            task_id=row.task_id, state=row.status,
            problem_statement=str(payload.get("problem_statement") or "")[:2000],
            owner_agent_id=row.owner_agent_id, owner_agent_name=row.owner_agent_name,
            finished_at=row.timestamp, answer=answer,
        ))
    return result, total


def _is_current(manager: Any, root: Any) -> bool:
    graph = manager.get_execution_graph_task_infos(root.task.task_id)
    return manager.is_executing_state(root.state) or any(
        item.task.task_id == manager.current_task_id for item in graph
    )


def _queue_snapshot(manager: Any, roots: list[Any]) -> list[dict[str, Any]]:
    positions = manager.get_root_task_queue_positions()
    rows = [
        {
            "root_task_id": root.task.task_id,
            "state": root.state.value,
            "is_current": _is_current(manager, root),
            "queue_position": positions.get(root.task.task_id),
        }
        for root in roots
    ]
    return sorted(
        rows,
        key=lambda row: (
            not row["is_current"],
            row["queue_position"] or float("inf"),
            row["root_task_id"],
        ),
    )


def _authorize_removal(root: Any, command: ManagerRuntimeWorkCommand, *, person: "CommandPerson") -> None:
    decision = private_access(
        person_id=person.canonical_user_id,
        owner_id=root.owner_person_id,  # recorded at admission (TaskInfo)
        resource_type=RESOURCE_TASK,
        resource_id=root.task.task_id,
        action=PRIVATE_DELETE,
        team_id=person.team_id,
        roles=person.roles,
    )
    if not decision.allowed:
        raise PermissionError("Task removal requires its owner's authorization.")


def _reserve_queued_cancellation(
    manager: Any, root_id: str, command: ManagerRuntimeWorkCommand, person: "CommandPerson"
) -> bool:
    """Check current execution and persist its barrier in one serialized operation.

    No task selection can interleave between this check and the restart barrier.
    Tasks that started since the browser snapshot stay active.
    """
    root = manager.get_task_info(root_id)
    if (
        root is None
        or manager.is_terminal_state(root.state)
        or _is_current(manager, root)
    ):
        return False
    _authorize_removal(root, command, person=person)
    graph = manager.get_execution_graph_task_infos(root_id)
    manager.record_execution_graph_cancelled(
        root_task_id=root_id,
        task_ids=sorted({root_id, *(item.task.task_id for item in graph)}),
        canonical_user_id=person.canonical_user_id,
        command_id=command.command_id,
        reason=command.reason,
        cancelled_at=datetime.now(timezone.utc).isoformat(),
    )
    return True


async def _inventory(
    agent: Any,
    command: ManagerRuntimeWorkCommand,
    response: ManagerRuntimeWorkCommand,
    *,
    person: "CommandPerson",
    list_roots: Callable[..., Awaitable[list[Any]]],
    summarize_root: Callable[..., Awaitable[Any]],
) -> list[Any]:
    roots = await list_roots(agent, person.canonical_user_id)
    rows = await agent.task_manager.operations_runtime.run(
        partial(_queue_snapshot, agent.task_manager), roots
    )
    response.inventory_digest = contract_sha256(
        {
            "owner": person.canonical_user_id,
            "team": person.team_id,
            "agent_id": agent.id,
            "tasks": rows,
        }
    )
    response.task_total = len(rows)
    response.current_task_count = sum(row["is_current"] for row in rows)
    response.queued_task_count = len(rows) - response.current_task_count
    # Parallel means independent conversations sharing this agent. Compute it
    # after owner filtering and before paging, so another person's work cannot
    # affect the label and a peer on another page still counts.
    conversations = {root.meta.conversation_id for root in roots if root.meta.conversation_id}
    by_id = {root.task.task_id: root for root in roots}
    response.roots = []
    for row in rows[command.task_offset : command.task_offset + command.task_limit]:
        root = by_id[row["root_task_id"]]
        summary = await summarize_root(agent, root)
        summary.problem_statement = str(root.task.problem_statement or "")[:2000]
        summary.is_current = row["is_current"]
        summary.is_parallel = bool(root.meta.conversation_id and len(conversations) > 1)
        summary.queue_position = row["queue_position"]
        response.roots.append(summary)
    return [by_id[row["root_task_id"]] for row in rows]


async def apply_task_control(
    agent: Any,
    command: ManagerRuntimeWorkCommand,
    response: ManagerRuntimeWorkCommand,
    *,
    person: "CommandPerson",
    list_roots: Callable[..., Awaitable[list[Any]]],
    summarize_root: Callable[..., Awaitable[Any]],
    cancel_roots: Callable[..., Awaitable[list[Any]]],
) -> None:
    inventory = partial(
        _inventory,
        agent,
        command,
        response,
        person=person,
        list_roots=list_roots,
        summarize_root=summarize_root,
    )
    roots = await inventory()
    if command.action == "list_tasks":
        response.success = True
        response.message = "Tasks loaded."
        return
    if (
        not command.inventory_digest
        or command.inventory_digest != response.inventory_digest
    ):
        response.success = False
        response.message = (
            "The task queue changed. Refresh and review it before removing tasks."
        )
        return
    selected = (
        roots
        if command.action == "purge_tasks"
        else [root for root in roots if root.task.task_id == command.target_task_id]
    )
    if command.action == "remove_task" and not selected:
        response.success = False
        response.message = "This task is no longer available. Refresh the task list."
        return
    for root in selected:
        _authorize_removal(root, command, person=person)
        if (
            command.action == "purge_tasks"
            and not await agent.task_manager.operations_runtime.run(
                partial(_reserve_queued_cancellation, agent.task_manager),
                root.task.task_id,
                command,
                person,
            )
        ):
            continue
        response.cancellation_receipts.extend(
            await cancel_roots(agent, roots=[root], command=command, person=person)
        )
    response.success = all(
        receipt.confirmed for receipt in response.cancellation_receipts
    )
    response.message = _cancellation_message(response)
    await inventory()


def _cancellation_message(response: ManagerRuntimeWorkCommand) -> str:
    if not response.success:
        return "Some cancellations could not be confirmed. Refresh and inspect the results."
    count = len(response.cancellation_receipts)
    if not count:
        return "No queued tasks were removed. Current work was preserved."
    noun = "task" if count == 1 else "tasks"
    return f"Removed {count} {noun}. Cancellation history is retained; recurring schedules are unchanged."
