"""Pausing and resuming tasks around access requests, in the agent's process.

``request_access`` pauses its task with a Strands interrupt, and
``task_steering_runtime`` records an ``access_request`` blocker instead of
asking the requester (``ACCESS_REQUEST_INTERRUPT_KIND``). This module:

- **resumes** the task when the gateway reports a decision
  (``AccessRequestResolvedV1``). The message carries only ids: the decision
  is read from the access store and must belong to this agent and task. A
  live interrupted turn gets its interrupt answered, so the paused
  ``request_access`` call returns the decision; after a restart (no live
  turn) the task resumes from a directive that states the decision;
- waits for **every** request of a batch: a workflow's batch of approvals
  pauses once for several requests (``ACCESS_REQUEST_IDS_KEY``), and the task
  resumes only after the last of them is decided;
- **finds decisions whose notice never arrived** (``resume_decided_access_waits``,
  on the agent's scheduler pass): an agent's id is new after every restart, and
  a restarted agent of the same type takes over its predecessor's waiting tasks
  (``runtime_recovery``). The gateway addresses the notice to the request's
  ``requester_agent_id`` and does not resend it, so before 2026-10-04 such a
  task waited forever while the reviewer believed it was approved. The check
  points the task's requests at this agent (``AccessRepository.rebind_requests``)
  so later notices arrive, and resumes the task when its requests are already
  decided. A notice and the check may both see one decision; the second finds
  no blocker and does nothing.

A request belongs to an agent type and a task, not to one process: the
notice is accepted when its request was made by this agent's type for a task
that waits here (the blocker or live interrupt names the request).
Withdrawing the request of a task that ends first, and ending the task's
task-bound grants, is in ``access_runtime`` (``register_task_access_cleanup``).

Nothing here grants access: the grants were created when the person decided,
and every later call is decided again at the access decision point.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    clear_human_interaction_blocker,
    get_human_interaction_blocker,
)
from custom_messages.contracts.access_v2 import AccessRequestResolvedV1, AccessRequestStatus, AccessRequestV1
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import TaskSteering, access_interrupt_request_ids
from custom_messages.enums import (
    ProgressState,
    TaskHistoryImportance,
    TaskHistoryType,
    TaskSteeringStatus,
    TaskSteeringType,
)

logger = logging.getLogger(__name__)

ACCESS_DECISION_RESPONSE_KIND = "access_request_decision"


def decision_response(request: AccessRequestV1) -> dict[str, Any]:
    """The interrupt answer: a wake-up signal; the tool re-reads the store."""

    return {
        "kind": ACCESS_DECISION_RESPONSE_KIND,
        "access_request_id": request.request_id,
        "status": request.status.value,
    }


def _matching_interrupt(agent: Any, task_id: str, request_id: str) -> Any | None:
    session = getattr(agent, "_interrupted_turn_sessions", {}).get(task_id)
    if session is None:
        return None
    return next(
        (item for item in session.interrupts if request_id in access_interrupt_request_ids(item.reason)),
        None,
    )


def _still_waiting(agent: Any, request_ids: list[str]) -> list[str]:
    """Requests of one pause that nobody has decided yet (read from the access store)."""

    from custom_agents.access_runtime import runtime_requests

    store = runtime_requests(agent).service.store
    waiting = []
    for request_id in request_ids:
        request = store.get_request(request_id)
        if request is not None and request.status is AccessRequestStatus.WAITING:
            waiting.append(request_id)
    return waiting


async def _batch_still_waiting(agent: Any, request: AccessRequestV1, request_ids: list[str]) -> bool:
    """Whether other requests of the same pause still wait for a person (logged)."""

    others = [request_id for request_id in request_ids if request_id != request.request_id]
    if not others:
        return False
    waiting = await agent.task_manager.operations_runtime.run(partial(_still_waiting, agent, others))
    if waiting:
        logger.info(
            "One request of a batch was decided; the task waits for the rest. event=access_batch_partly_decided "
            "request_id=%s task_id=%s still_waiting=%s",
            request.request_id,
            request.task_id,
            ",".join(waiting),
        )
    return bool(waiting)


def _decided_request(agent: Any, message: AccessRequestResolvedV1) -> AccessRequestV1 | None:
    """The decided request the message names, when it is this agent's; None otherwise (logged)."""

    from custom_agents.access_runtime import AuthorizationOff, runtime_requests

    try:
        request = runtime_requests(agent).service.store.get_request(message.request_id)
    except AuthorizationOff:
        request = None
    problem = None
    if request is None or request.team_id != agent.team_id:
        problem = "unknown_request"
    elif request.status is AccessRequestStatus.WAITING:
        problem = "still_waiting"
    elif request.requester_agent_type != getattr(agent, "named_agent_type", None) or not request.task_id:
        # The task's own blocker or interrupt must also name the request
        # (checked when resuming), so another agent of the type cannot resume it.
        problem = "not_this_agents_task"
    if problem is not None:
        logger.warning(
            "Ignored an access decision notice. event=access_decision_notice_ignored why=%s request_id=%s agent_id=%s",
            problem,
            message.request_id,
            agent.id,
        )
        return None
    return request


async def handle_access_request_resolved(
    agent: Any, *, message: AccessRequestResolvedV1, msg_header: MessageHeader, msg_meta: MessageMeta
) -> None:
    """Resume the task that waits on the decided request (see module docstring)."""

    run = agent.task_manager.operations_runtime.run
    request = await run(partial(_decided_request, agent, message))
    if request is None:
        return
    from custom_agents.tool_setup_run_runtime import on_access_decided

    # A set_up_tools run waits for this request together with its setup pages.
    if await on_access_decided(agent, request, msg_header, msg_meta):
        return
    task_id = str(request.task_id)
    interrupt = _matching_interrupt(agent, task_id, request.request_id)
    if interrupt is not None:
        if await _batch_still_waiting(agent, request, access_interrupt_request_ids(interrupt.reason)):
            return
        from custom_agents.task_steering_runtime import handle_task_steering_message

        steering = TaskSteering(
            steering_id=f"access_resume_{request.request_id}",
            steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
            target_task_id=task_id,
            conversation_id=request.conversation_id,
            target_agent_id=agent.id,
            interrupt_responses=[{"interrupt_id": interrupt.interrupt_id, "response": decision_response(request)}],
            status=TaskSteeringStatus.PENDING,
            # One resume per pause: the last two decisions of a batch may both see the batch finished.
            idempotency_key=f"access_resume:{interrupt.interrupt_id}",
        )
        await handle_task_steering_message(agent, steering=steering, msg_header=msg_header, msg_meta=msg_meta)
        return
    await _resume_without_live_turn(agent, request)


async def _resume_without_live_turn(agent: Any, request: AccessRequestV1) -> None:
    """After a restart the paused turn is gone; resume the task from a directive.

    The work is queued for the retained drain, so a resumed turn never holds up
    the decision's message handler or the scheduler loop.
    """

    manager = agent.task_manager
    run = manager.operations_runtime.run
    task_id = str(request.task_id)
    blocker = await run(partial(get_human_interaction_blocker, manager), task_id)
    waited = access_interrupt_request_ids(blocker) if blocker else []
    if not blocker or blocker.get("kind") != ACCESS_REQUEST_BLOCKER or request.request_id not in waited:
        logger.info(
            "No task waits on this access decision. event=access_decision_no_waiting_task request_id=%s task_id=%s",
            request.request_id,
            task_id,
        )
        return
    task_info = await run(manager.get_task_info, task_id)
    if task_info is None or await run(manager.is_terminal_state, task_info.state):
        await run(partial(clear_human_interaction_blocker, manager), task_id)
        return
    if await _batch_still_waiting(agent, request, waited):
        return
    if len(waited) > 1:
        directive = (
            "The access requests this task paused for were decided while it was paused. Continue the task; "
            "for each staged change call apply_change with its change_id (it finds its approved request), and do "
            "not ask again for anything denied. Requests: " + json.dumps(waited)
        )
    else:
        directive = (
            "Your access request was decided while this task was paused. Continue the task using this "
            "decision; call the tool you needed again only if it was granted: "
            + json.dumps(decision_response(request), sort_keys=True)
        )
    # Recovery may already have saved a restart outcome the next turn must
    # read. Resolving this access wait must not replace that information.
    await run(manager.append_runtime_resume_directive, task_id, directive)
    await run(partial(clear_human_interaction_blocker, manager), task_id)
    await run(
        manager.append_history_entry,
        task_id,
        "Access request decided; the task resumes.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={"event": "access_request_task_resume", "request_id": request.request_id, "status": request.status.value},
    )
    await run(manager.update_main_task_state, task_id, ProgressState.ACCEPTED, datetime.now(timezone.utc).isoformat())
    await run(manager.enqueue_task, task_id, front=True)
    schedule_pending_work(agent)


# How often an agent looks for decided requests whose notice never arrived.
DECIDED_ACCESS_CHECK_SECONDS = 60.0


def _access_waits(agent: Any) -> list[tuple[str, list[str]]]:
    """This agent's unfinished tasks that wait on access requests, with their request ids."""

    manager = agent.task_manager
    waits = []
    for info in manager.list_task_infos():
        if manager.is_terminal_state(info.state):
            continue
        blocker = get_human_interaction_blocker(manager, info.task.task_id)
        if blocker and blocker.get("kind") == ACCESS_REQUEST_BLOCKER:
            waits.append((info.task.task_id, access_interrupt_request_ids(blocker)))
    return waits


def _settle_requests(agent: Any, request_ids: list[str]) -> tuple[list[AccessRequestV1], list[str]]:
    """The requests (pointed at this agent first) and the ids that moved."""

    from custom_agents.access_runtime import runtime_requests

    store = runtime_requests(agent).service.store
    moved = store.rebind_requests(
        agent.team_id, request_ids, agent_type=agent.named_agent_type, to_agent_id=agent.id
    )
    requests = [store.get_request(request_id) for request_id in request_ids]
    return [request for request in requests if request is not None], moved


async def resume_decided_access_waits(agent: Any, *, now: float | None = None) -> int:
    """Resume this agent's tasks whose access requests were decided without a notice reaching it.

    Runs on every scheduler pass, at most every ``DECIDED_ACCESS_CHECK_SECONDS``
    (see module docstring). Returns how many tasks it resumed.
    """

    import time

    from custom_agents.access_runtime import AuthorizationOff

    moment = time.monotonic() if now is None else now
    if moment < getattr(agent, "_decided_access_check_after", 0.0):
        return 0
    agent._decided_access_check_after = moment + DECIDED_ACCESS_CHECK_SECONDS
    run = agent.task_manager.operations_runtime.run
    resumed = 0
    for task_id, request_ids in await run(partial(_access_waits, agent)):
        if not request_ids:
            continue
        try:
            resumed += await _resume_if_decided(agent, task_id, request_ids)
        except AuthorizationOff:
            return resumed
    return resumed


async def _resume_if_decided(agent: Any, task_id: str, request_ids: list[str]) -> int:
    """Point one waiting task's requests here; resume it when all are decided (1 if resumed)."""

    run = agent.task_manager.operations_runtime.run
    requests, moved = await run(partial(_settle_requests, agent, request_ids))
    if moved:
        logger.warning(
            "Pointed a task's access requests at the agent that now holds it. "
            "event=access_requests_rebound task_id=%s agent_id=%s request_ids=%s",
            task_id,
            agent.id,
            ",".join(moved),
        )
    if len(requests) != len(request_ids) or any(
        request.status is AccessRequestStatus.WAITING for request in requests
    ):
        return 0
    if any(_matching_interrupt(agent, task_id, request.request_id) for request in requests):
        return 0  # a live turn waits; its notice answers the interrupt
    logger.warning(
        "Found an access decision whose notice never reached this agent; resuming the task. "
        "event=access_decision_found_without_notice task_id=%s agent_id=%s request_ids=%s",
        task_id,
        agent.id,
        ",".join(request_ids),
    )
    await _resume_without_live_turn(agent, requests[-1])
    return 1


__all__ = [
    "ACCESS_DECISION_RESPONSE_KIND",
    "decision_response",
    "handle_access_request_resolved",
]
