"""Typed browser task controls: an agent's queue and finished tasks, through the team services.

The Tasks page lists one agent's queue or the person's finished tasks and removes or
purges queued tasks. The gateway sends ``tasks.list``, ``tasks.list_finished``,
``tasks.remove`` and ``tasks.purge`` to the team services, which pass them on to the host
running the agent with the gateway's command token unchanged (README "Relays to an agent's
host"); the agent answers with today's task controls (``custom_agents.relayed_task_controls``).
"""

from __future__ import annotations

from typing import Any

from assistant_gateway.application.team_commands import GatewayTeamCommandService
from assistant_gateway.application.website_application import (
    WebTaskControlRequestV1,
    WebTaskControlResultV1,
    WebTaskListRequestV1,
    WebTaskListV1,
    WebTaskV1,
)
from custom_messages.contracts.common_v1 import contract_sha256

REMOVAL_REASON = "Task removal requested in Settings by its authenticated owner."
QUEUE_CHANGED_TEXT = "The task queue changed. Refresh and review it before removing tasks."
_PAGE_LIMIT = 20


def _person(request: WebTaskListRequestV1 | WebTaskControlRequestV1) -> str:
    person = str(request.context.canonical_user_id or "").strip()
    if not person:
        raise PermissionError("Sign in to see your tasks.")
    return person


def _refusal(reply: dict[str, Any], fallback: str) -> RuntimeError:
    return RuntimeError(str(reply.get("message") or fallback))


async def list_tasks(team: GatewayTeamCommandService, request: WebTaskListRequestV1) -> WebTaskListV1:
    body = {"agent": request.agent_id, "offset": request.offset, "limit": min(request.limit, _PAGE_LIMIT)}
    if request.view == "finished":
        reply = await team.request("tasks.list_finished", body, person_id=_person(request))
        if reply.get("outcome") != "ok":
            raise _refusal(reply, "Tasks could not be loaded.")
        tasks = tuple(
            WebTaskV1(
                root_task_id=row["task_id"], state=row["state"], problem_statement=row.get("problem_statement", ""),
                is_current=False, latest_activity_at=row.get("finished_at"), answer=row.get("answer", ""),
                owner_agent_id=row.get("owner_agent_id"), owner_agent_name=row.get("owner_agent_name"),
            )
            for row in reply["tasks"]
        )
        return WebTaskListV1(tasks=tasks, total=reply["total"], queued_count=0, current_count=0,
                             inventory_digest=contract_sha256({"finished_tasks": reply["tasks"]}))
    reply = await team.request("tasks.list", body, person_id=_person(request))
    if reply.get("outcome") != "ok":
        raise _refusal(reply, "Tasks could not be loaded.")
    return WebTaskListV1(
        tasks=tuple(
            WebTaskV1(
                root_task_id=row["task_id"],
                state=row["state"],
                problem_statement=row.get("problem_statement", ""),
                is_current=row["is_current"],
                is_parallel=bool(row.get("is_parallel", False)),
                queue_position=row.get("queue_position"),
                latest_activity_at=row.get("last_updated_at"),
                latest_activity=row.get("latest_activity"),
                blocker=(row.get("blocker") or {}).get("text"),
            )
            for row in reply["tasks"]
        ),
        total=reply["total"],
        queued_count=reply["queued_count"],
        current_count=reply["current_count"],
        inventory_digest=reply["queue_digest"],
    )


async def control_tasks(team: GatewayTeamCommandService, request: WebTaskControlRequestV1) -> WebTaskControlResultV1:
    if request.action == "remove" and not request.task_id:
        raise ValueError("Choose a task to remove.")
    if not request.inventory_digest:
        return WebTaskControlResultV1(success=False, message=QUEUE_CHANGED_TEXT, cancellation_receipts=())
    body: dict[str, Any] = {"agent": request.agent_id, "seen_queue_digest": request.inventory_digest,
                            "reason": REMOVAL_REASON}
    if request.action == "remove":
        body["task_id"] = request.task_id
    kind = "tasks.remove" if request.action == "remove" else "tasks.purge"
    reply = await team.request(kind, body, person_id=_person(request))
    outcome = reply.get("outcome")
    if outcome == "queue_changed":
        return WebTaskControlResultV1(success=False, message=QUEUE_CHANGED_TEXT, cancellation_receipts=())
    if outcome != "ok":
        return WebTaskControlResultV1(success=False, message=str(reply.get("message") or "The tasks were not removed."),
                                      cancellation_receipts=())
    receipts = tuple(
        {"root_task_id": receipt["root_task_id"], "confirmed": receipt["state"] == "confirmed",
         "cancelled_task_count": receipt["cancelled_task_count"], "stop_signals_sent": receipt["stop_signals_sent"],
         "unconfirmed_worker_count": receipt["unconfirmed_worker_count"], "message": ""}
        for receipt in reply["receipts"]
    )
    confirmed = bool(reply["all_confirmed"])
    return WebTaskControlResultV1(success=confirmed, message=_removal_message(confirmed, int(reply["removed"])),
                                  cancellation_receipts=receipts)


def _removal_message(confirmed: bool, removed: int) -> str:
    """The agent's own words for a removal (``runtime_task_controls._cancellation_message``)."""

    if not confirmed:
        return "Some cancellations could not be confirmed. Refresh and inspect the results."
    if not removed:
        return "No queued tasks were removed. Current work was preserved."
    noun = "task" if removed == 1 else "tasks"
    return f"Removed {removed} {noun}. Cancellation history is retained; recurring schedules are unchanged."
