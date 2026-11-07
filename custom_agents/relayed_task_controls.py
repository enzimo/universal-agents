"""What an agent answers when the team services relay a person's command to it (``ctl.agent.<agent>``).

The gateway sends ``tasks.list``, ``tasks.list_finished``, ``tasks.remove``, ``tasks.purge``
and ``debug.dump`` to the team services, which pass them to the host running the agent with
the gateway's command token unchanged (README "Relays to an agent's host"). The agent
answers with today's task controls (``custom_agents.runtime_task_controls``), so lists,
removals and their checks are unchanged:

- **Who asks** comes from the gateway's command token only: its person reference is read
  back from the gateway's copy of the team's people (``access_control.people_directory``),
  and a person who is unknown, disabled or removed gets ``not_allowed``. The relay must come
  from the team services with a token a gateway signed.
- **Lists and removals are the person's own** work, as before: the inventory is scoped to
  them, and a removal names the queue version the person saw (``seen_queue_digest``).
- **The context dump setting** is the team lead's (``UniversalAgent.apply_runtime_context_dump_settings``).

These handlers run on the agent's own event loop (``custom_agents.agent_host``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame

_LOGGER = logging.getLogger(__name__)
_MESSAGE_MAX = 500
TASKS_PAGE_MAX = 20


def _message(outcome: str, message: str) -> dict[str, Any]:
    text = (message or "").strip() or "No details."
    return {"outcome": outcome, "message": text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"}


def relayed_person(agent: Any, verified: VerifiedFrame) -> tuple[Optional[Any], str]:
    """The person a relayed command acts for, or None with why it is refused (logged)."""

    from access_control.people_directory import PeopleDirectory
    from custom_agents.manager_control_runtime import CommandPerson
    from storage.access_repository import AccessRepository

    authority, sender = verified.authority, verified.certificate
    reason = ""
    person = None
    if sender is None or "team_services" not in sender.roles:
        reason = "Only the team services relay a person's command."
    elif authority is None or "gateway" not in authority.certificate.roles or authority.token.bound_to.type != "command" \
            or not isinstance(authority.token.person, str):
        reason = "The command carries no gateway's token for a person's command."
    else:
        people = PeopleDirectory(AccessRepository(agent.task_manager.storage_settings), str(agent.team_id))
        now = people.person_by_ref(authority.token.person)
        if now is None:
            reason = "The command names a person this team does not know."
        elif not now.active or not now.roles:
            reason = f"The person is {now.describe()} in this team."
        else:
            person = CommandPerson(canonical_user_id=now.person_id, team_id=str(agent.team_id), roles=now.roles)
    if person is None:
        _LOGGER.warning("Refused a relayed command. event=relayed_command_refused agent=%s kind=%s reason=%s",
                        getattr(agent, "id", None), verified.frame.kind, reason)
    return person, reason


def _timestamp(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return format_timestamp(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))


async def _task_control(agent: Any, action: str, body: dict[str, Any], person: Any) -> ManagerRuntimeWorkCommand:
    from custom_agents.manager_control_runtime import (
        _cancel_runtime_work_roots,
        _runtime_work_root_summary,
        _runtime_work_roots,
    )
    from custom_agents.runtime_task_controls import apply_task_control

    command = ManagerRuntimeWorkCommand(
        action=action, scope="user", target_task_id=body.get("task_id"),
        inventory_digest=body.get("seen_queue_digest"), task_offset=int(body.get("offset", 0)),
        task_limit=min(TASKS_PAGE_MAX, int(body.get("limit", TASKS_PAGE_MAX))),
        **({"reason": body["reason"]} if body.get("reason") else {}),
    )
    response = command.model_copy(deep=True)
    response.roots, response.cancellation_receipts = [], []
    await apply_task_control(agent, command, response, person=person, list_roots=_runtime_work_roots,
                             summarize_root=_runtime_work_root_summary, cancel_roots=_cancel_runtime_work_roots)
    return response


async def tasks_list(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    person, reason = relayed_person(agent, verified)
    if person is None:
        return _message("not_allowed", reason)
    body = verified.raw["body"]
    response = await _task_control(agent, "list_tasks", body, person)
    tasks = []
    for root in response.roots:
        row: dict[str, Any] = {"task_id": root.root_task_id, "state": root.state.value if hasattr(root.state, "value")
                               else str(root.state), "is_current": bool(root.is_current)}
        if root.problem_statement:
            row["problem_statement"] = root.problem_statement
        if root.is_parallel:
            row["is_parallel"] = True
        if root.queue_position is not None:
            row["queue_position"] = int(root.queue_position)
        if root.latest_activity_at:
            row["last_updated_at"] = _timestamp(root.latest_activity_at)
        if root.blocker:
            row["blocker"] = {"type": "inline", "text": root.blocker}
        if root.latest_activity:
            row["latest_activity"] = root.latest_activity
        tasks.append(row)
    reply: dict[str, Any] = {"outcome": "ok", "tasks": tasks, "total": int(response.task_total or 0),
                             "queued_count": int(response.queued_task_count or 0),
                             "current_count": int(response.current_task_count or 0),
                             "queue_digest": str(response.inventory_digest)}
    following = int(body["offset"]) + len(tasks)
    if tasks and following < reply["total"]:
        reply["next_offset"] = following
    return reply


async def tasks_list_finished(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    from functools import partial

    from custom_agents.runtime_task_controls import finished_task_inventory

    person, reason = relayed_person(agent, verified)
    if person is None:
        return _message("not_allowed", reason)
    body = verified.raw["body"]
    command = ManagerRuntimeWorkCommand(action="list_finished_tasks", scope="user", task_offset=int(body["offset"]),
                                        task_limit=min(TASKS_PAGE_MAX, int(body["limit"])))
    finished, total = await agent.task_manager.operations_runtime.run(
        partial(finished_task_inventory, agent, person=person), command)
    tasks = []
    for item in finished:
        row: dict[str, Any] = {"task_id": item.task_id, "state": item.state,
                               "owner_agent_id": item.owner_agent_id or str(agent.id)}
        if item.problem_statement:
            row["problem_statement"] = item.problem_statement
        if item.finished_at:
            row["finished_at"] = _timestamp(item.finished_at)
        if item.answer:
            row["answer"] = item.answer
        if item.owner_agent_name:
            row["owner_agent_name"] = item.owner_agent_name
        tasks.append(row)
    reply: dict[str, Any] = {"outcome": "ok", "tasks": tasks, "total": int(total)}
    following = int(body["offset"]) + len(tasks)
    if tasks and following < reply["total"]:
        reply["next_offset"] = following
    return reply


async def _removal(agent: Any, verified: VerifiedFrame, action: str) -> dict[str, Any]:
    person, reason = relayed_person(agent, verified)
    if person is None:
        return _message("not_allowed", reason)
    body = verified.raw["body"]
    response = await _task_control(agent, action, body, person)
    if response.inventory_digest != body["seen_queue_digest"] and not response.cancellation_receipts:
        return {"outcome": "queue_changed", "current_digest": str(response.inventory_digest),
                "message": _message("queue_changed", response.message or "")["message"]}
    if action == "remove_task" and not response.cancellation_receipts and not response.success:
        return _message("not_found", response.message)
    receipts = [{"root_task_id": receipt.root_task_id, "cancelled_task_count": int(receipt.cancelled_task_count),
                 "stop_signals_sent": int(receipt.stop_signals_sent),
                 "unconfirmed_worker_count": int(receipt.unconfirmed_worker_count),
                 "state": "confirmed" if receipt.confirmed else "failed"}
                for receipt in response.cancellation_receipts]
    return {"outcome": "ok", "receipts": receipts, "removed": len(receipts),
            "queue_digest": str(response.inventory_digest), "all_confirmed": all(r["state"] == "confirmed" for r in receipts)}


async def tasks_remove(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    return await _removal(agent, verified, "remove_task")


async def tasks_purge(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    return await _removal(agent, verified, "purge_tasks")


async def debug_dump(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    person, reason = relayed_person(agent, verified)
    if person is None:
        return _message("not_allowed", reason)
    if not getattr(agent, "is_team_lead", False):
        return _message("not_allowed", "Context dumps are the team lead's setting.")
    body = verified.raw["body"]
    applied = agent.apply_runtime_context_dump_settings(enabled=bool(body["enabled"]), mode=str(body["mode"]),
                                                        source=f"person:{person.canonical_user_id}")
    return {"outcome": "ok", "enabled": bool(applied["enabled"]), "mode": str(applied["mode"])}


RELAYED_HANDLERS = {
    "tasks.list": tasks_list,
    "tasks.list_finished": tasks_list_finished,
    "tasks.remove": tasks_remove,
    "tasks.purge": tasks_purge,
    "debug.dump": debug_dump,
}
