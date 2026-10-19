"""A person's requests in the change ledger: opened when admitted, closed when they end.

The change ledger (``storage.change_ledger``) refuses a request's change to a
record that another request changed after it started, and any change from a
request that was stopped or has ended. It needs each request's start and end:

- **Start:** when the agent that receives a person's message (its first task,
  with no parent task) admits it, and the task's verified context is a
  person's request the gateway signed. Its start position is the ledger's
  position at that moment. Scheduled work and system work have no request row:
  rule (b) does not apply to them.
- **End:** when that first task ends, as access for one request ends
  (``access_runtime._ended_request_id``): cancelled is "stopped", completed,
  failed or abandoned is "ended". Delegated tasks carry the same request id,
  so a worker still running after its request ended is refused.

A failure here is logged and the task goes on: a request without a row gets
one at its first change (``change_ledger_request_row_created_late``), and a
request whose close failed stays open until the ledger prunes it.

The operator's switch ``agent.request_conflict_checks`` (default on) is kept
with the agent's store: with it off, writers' changes are recorded as
unchecked and no request is refused for another's change or for having
ended (``storage.change_ledger.ChangeWriter.checked``). Only the stores'
"changed since you read it" checks remain.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from access_control.identity import ISSUER_GATEWAY
from custom_messages.enums import ProgressState
from storage.change_ledger import ChangeRequestStore, RequestState

logger = logging.getLogger(__name__)


def register_change_requests(agent: Any, *, conflict_checks: bool) -> None:
    """Open the agent's request-row store and close each request when its first task ends."""

    agent._change_request_store = ChangeRequestStore(agent.task_manager.storage_settings)
    agent._request_conflict_checks = conflict_checks

    def closed(task_id: str, state: ProgressState, _timestamp: str) -> None:
        close_ended_request(agent, task_id, state)

    agent.task_manager.add_task_terminal_callback(closed)


def request_conflict_checks(agent: Any) -> bool:
    """Whether this agent's writes are checked against the person's other requests (the operator's switch)."""

    return bool(getattr(agent, "_request_conflict_checks", True))


async def open_admitted_request(agent: Any, task_request: Any, meta: Any) -> None:
    """When a person's request is admitted as its first task, record that it started."""

    if getattr(task_request, "parent_task_id", None):
        return
    store = getattr(agent, "_change_request_store", None)
    if store is None:
        return
    from custom_agents.access_runtime import resolve_meta_identity

    context = await asyncio.to_thread(resolve_meta_identity, agent, meta)
    if context is None or context.issuer != ISSUER_GATEWAY or not context.person_id:
        return
    try:
        start = await asyncio.to_thread(
            store.open,
            team_id=str(agent.team_id),
            request_id=context.context_id,
            person_id=context.person_id,
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - the task goes on; its first change opens the row late
        logger.exception(
            "Could not record the start of a request. event=change_ledger_request_open_failed task_id=%s request_id=%s",
            getattr(task_request, "task_id", ""),
            context.context_id,
        )
        return
    logger.info(
        "A request started. event=change_ledger_request_opened request_id=%s task_id=%s start_position=%s",
        context.context_id,
        getattr(task_request, "task_id", ""),
        start,
    )


def close_ended_request(agent: Any, task_id: str, state: ProgressState) -> None:
    """When a person's request's first task ends, mark the request stopped or ended."""

    request = _request_of_first_task(agent, task_id)
    store = getattr(agent, "_change_request_store", None)
    if request is None or store is None:
        return
    request_id, person_id = request
    closed_as = RequestState.STOPPED if state is ProgressState.CANCELLED else RequestState.ENDED
    try:
        store.close(
            team_id=str(agent.team_id),
            request_id=request_id,
            person_id=person_id,
            state=closed_as,
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - the task still ends; the row stays open until pruned
        logger.exception(
            "Could not record the end of a request. event=change_ledger_request_close_failed task_id=%s request_id=%s",
            task_id,
            request_id,
        )
        return
    logger.info(
        "A request %s. event=change_ledger_request_closed request_id=%s task_id=%s",
        closed_as.value,
        request_id,
        task_id,
    )


def _request_of_first_task(agent: Any, task_id: str) -> tuple[str, str] | None:
    """The person's request (context id, person) whose first task this is, or None.

    Read without checking the signature again, like ending a request's access:
    closing takes nothing away that the person gave, and a context that expired
    meanwhile must still close.
    """

    info = agent.task_manager.get_task_info(task_id)
    if info is None or getattr(info.task, "parent_task_id", None):
        return None
    gateway_context = getattr(getattr(info, "meta", None), "gateway_context", None)
    signed = getattr(gateway_context, "task_context", None)
    context = getattr(signed, "context", None)
    if context is None or context.issuer != ISSUER_GATEWAY or not context.person_id:
        return None
    return str(context.context_id), str(context.person_id)


__all__ = ["close_ended_request", "open_admitted_request", "register_change_requests", "request_conflict_checks"]
