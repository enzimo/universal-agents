"""Driving ``set_up_tools`` runs in the team lead's process (owner's decision 2026-09-28).

``set_up_tools`` (``custom_tools.providers.tool_setup``) plans a run, publishes
its workflow, raises its access requests, stores the run and pauses
TeamLead's turn with a local Strands interrupt (``TOOL_SETUP_RUN_KIND``); it
never asks the person whether to start. This module does the rest, in trusted
code:

- **Begin** (``begin_run``, from ``task_steering_runtime`` when the turn
  pauses): records the task's blocker, so recovery and status summaries know
  the task waits, and opens every piece that may start now.
- **Open pieces** (``advance``): each setup page or external step is one
  ``ToolSetupRequestV1`` sent to the gateway's route for the person's chat,
  the same route the removed ``tool_setup_request`` model action used
  (``message_routing.requester_bound_primary_route``); pages whose
  prerequisites are done open at once. The page is shown on its workflow step
  (``access_workflow_setup_hooks``). A worker's access is handed on with
  ``AccessRequests.give`` once TeamLead holds it; if TeamLead may not hand it
  on, the worker asks for it itself when it starts.
- **Results** (``apply_page_result`` then ``continue_after_page_result``,
  and ``on_access_decided``): the gateway's ``ToolSetupResultV1`` and the
  Inbox's decisions update the run (``tool_setup_runs``): an expired page, or
  one nobody watched after a gateway restart, is offered again automatically,
  up to three pages; a closed page or a denial stops the run. A page's result
  is applied once, under the run's lock, before anything else looks at it,
  and marked handled only after its follow-up steps finished (the workflow
  step, the history entry and the next pages, or the answer when the task had
  ended; ``mark_page_result_handled``). The gateway keeps a result it could
  not send and sends it when a team lead is back, so a second copy can
  arrive: a copy of a handled result is ignored and logged
  (``event=tool_setup_result_duplicate``), even after the task ended, and a
  copy of one whose handling failed partway runs the follow-up steps again
  (``event=tool_setup_result_handled_again``), so the task still continues.
- **Resume** (``_resume``): once every piece is done, or the run stopped, the
  paused turn gets its interrupt answered and ``set_up_tools`` returns the
  compact per-tool result; after an agent restart (no live turn) the task
  resumes from a directive that states the same result.

The run and the task blocker are stored, so both processes can restart
during the wait. Nothing here grants access or reads a credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import partial
from typing import Any

from custom_agents.access_workflow_setup_hooks import link_setup_request, record_setup_result
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_human_interaction_blockers import (
    clear_human_interaction_blocker,
    get_human_interaction_blocker,
    record_human_interaction_blocker,
)
from custom_agents.tool_setup_runs import (
    ACCESS_APPROVED_CODE,
    FAILED,
    PENDING,
    PIECE_ACCESS,
    RUN_STOPPED,
    RUN_WAITING,
    SETTLED,
    TOOL_SETUP_RUN_KIND,
    TOOL_SETUP_RUN_RESPONSE_KIND,
    WAITING,
    RunPieceV1,
    ToolSetupRunV1,
    openable,
    outcome,
    workflow_notice,
    piece_for_request,
    piece_for_setup,
    piece_title,
    record_access_decision,
    record_page_result,
    result_applied,
    result_handled,
    service_name,
    settle,
)
from custom_messages.contracts.access_v2 import AccessRequestV1
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import (
    ProgressState,
    TaskHistoryImportance,
    TaskHistoryType,
    TaskSteeringStatus,
    TaskSteeringType,
)
from id_generation import new_id
from storage.tool_setup_run_repository import ToolSetupRunRepository

logger = logging.getLogger(__name__)
# The UserNotification reason of the setup workflow's notice (audit and delivery routing).
WORKFLOW_NOTICE_REASON = "tool_setup_workflow"


# -- storage and locking ------------------------------------------------------------------


def run_repository(agent: Any) -> ToolSetupRunRepository:
    """The agent's run store over its task storage (cached on the agent)."""

    existing = getattr(agent, "_tool_setup_run_repository", None)
    if isinstance(existing, ToolSetupRunRepository):
        return existing
    repository = ToolSetupRunRepository(agent.task_manager.storage_settings)
    agent._tool_setup_run_repository = repository
    return repository


def _lock(agent: Any, run_id: str) -> asyncio.Lock:
    """One lock per run, so results arriving together update it one after another."""

    locks = getattr(agent, "_tool_setup_run_locks", None)
    if locks is None:
        locks = {}
        agent._tool_setup_run_locks = locks
    return locks.setdefault(run_id, asyncio.Lock())


async def _load(agent: Any, run_id: str) -> ToolSetupRunV1 | None:
    return await asyncio.to_thread(run_repository(agent).get, run_id)


async def _save(agent: Any, run: ToolSetupRunV1) -> ToolSetupRunV1:
    return await asyncio.to_thread(run_repository(agent).save, run)


def _names(agent: Any, run: ToolSetupRunV1) -> dict[str, str]:
    from custom_agents.access_runtime import runtime_access

    catalog = runtime_access(agent).catalog
    names = {}
    for tool_id in run.tool_ids:
        tool = catalog.tools.get(tool_id)
        names[tool_id] = service_name(tool_id, tool.display_name if tool else "")
    return names


async def _history(agent: Any, task_id: str, text: str, details: dict[str, Any]) -> None:
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        text,
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details=details,
    )


# -- begin ------------------------------------------------------------------------------


async def begin_run(agent: Any, *, task_id: str, interrupt_id: str, reason: dict[str, Any], msg_meta: Any) -> None:
    """The turn paused on a run: record the wait, then open what may start now."""

    run_id = str(reason.get("run_id") or "")
    await agent.task_manager.operations_runtime.run(
        partial(record_human_interaction_blocker, agent.task_manager),
        task_id,
        {
            "kind": TOOL_SETUP_RUN_KIND,
            "run_id": run_id,
            "interrupt_id": interrupt_id,
            "summary": str(reason.get("summary") or ""),
            "correlation_id": getattr(msg_meta, "msg_correlation_id", None),
            "conversation_id": getattr(msg_meta, "conversation_id", None),
        },
    )
    async with _lock(agent, run_id):
        run = await _load(agent, run_id)
        if run is None:
            logger.warning("A paused turn names no stored setup run. event=tool_setup_run_missing run_id=%s", run_id)
            return
        if run.interrupt_id != interrupt_id:
            run = await _save(agent, run.model_copy(update={"interrupt_id": interrupt_id}))
        # The link comes before any page, so the person knows what the pages are for.
        await _announce_workflow(agent, run)
    logger.info(
        "A task waits for its tools to be set up. event=tool_setup_run_waiting run_id=%s task_id=%s pieces=%s",
        run_id,
        task_id,
        len(run.pieces),
    )
    await advance(agent, run_id)


# -- telling the person --------------------------------------------------------------------


def workflow_link(workflow_id: str) -> str:
    """The workflow page's full address, so it opens from any channel (Telegram, Discord, Web Chat)."""

    from custom_tools.single_page_site_tools import website_base_url

    return f"{website_base_url()}/workflows/{workflow_id}"


def _chat_envelope(agent: Any, content: Any, task_info: Any, *, key: str, correlation_id: str | None = None) -> Any:
    """A message for the chat of the person the task works for (the gateway's route; see person_chat_route)."""

    from custom_agents.message_routing import person_chat_route

    meta = task_info.meta
    route = person_chat_route(
        task_info.header,
        instance_config_path=getattr(agent, "instance_config_path", None),
        team_id=agent.team_id,
        requester_type=getattr(meta, "requester_type", None),
    )
    if not route:
        raise RuntimeError("No route reaches the person's chat.")
    envelope = MessageEnvelope(
        msg_content=content,
        msg_header=MessageHeader(from_agent_id=agent.id, to_routing_path=route, agent_signature="", idempotency_key=key),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            msg_correlation_id=correlation_id or getattr(meta, "msg_correlation_id", None),
            conversation_id=getattr(meta, "conversation_id", None),
            followup_to_task_id=task_info.task.task_id if correlation_id else None,
            request_timezone=getattr(meta, "request_timezone", None),
            gateway_context=getattr(meta, "gateway_context", None),
        ),
    )
    return envelope, route


async def _announce_workflow(agent: Any, run: ToolSetupRunV1) -> ToolSetupRunV1:
    """Tell the person, once, that a workflow tracks the setup, with its link (a UserNotification)."""

    if not run.workflow_id or run.workflow_announced:
        return run
    manager = agent.task_manager
    task_info = await manager.operations_runtime.run(manager.get_task_info, run.task_id)
    if task_info is None:
        return run
    names = await asyncio.to_thread(_names, agent, run)
    notice = UserNotification(
        text=workflow_notice(run, names, workflow_link(run.workflow_id)),
        reason=WORKFLOW_NOTICE_REASON,
        source_task_id=run.task_id,
        source_agent_id=agent.id,
        source_agent_name=agent.name,
    )
    try:
        envelope, route = _chat_envelope(
            agent, notice, task_info, key=f"{run.run_id}:workflow", correlation_id=new_id("notify")
        )
        await agent.team_comms.send_message(message=envelope, routing_key=route)
    except Exception as exc:  # noqa: BLE001 - the pages still open; the result still names the workflow
        logger.warning(
            "The setup workflow could not be announced in chat. event=tool_setup_workflow_notice_failed "
            "run_id=%s workflow_id=%s error=%s",
            run.run_id,
            run.workflow_id,
            type(exc).__name__,
        )
        return run
    logger.info(
        "The person was told about the setup workflow. event=tool_setup_workflow_announced run_id=%s workflow_id=%s",
        run.run_id,
        run.workflow_id,
    )
    return await _save(agent, run.model_copy(update={"workflow_announced": True}))


# -- opening pieces ------------------------------------------------------------------------


async def send_setup_request(agent: Any, request: ToolSetupRequestV1, task_info: Any) -> None:
    """Send one setup page request to the gateway for the person's chat (module docstring)."""

    envelope, route = _chat_envelope(agent, request, task_info, key=request.setup_request_id)
    await agent.team_comms.send_message(message=envelope, routing_key=route)


def _page_reason(run: ToolSetupRunV1, piece: RunPieceV1, name: str) -> str:
    purpose = run.purpose.strip().rstrip(".")
    why = f" so I can {purpose[0].lower()}{purpose[1:]}" if purpose else ""
    return f"{piece_title(piece, name)}{why}."[:2000]


async def _open_page(agent: Any, run: ToolSetupRunV1, piece: RunPieceV1, task_info: Any) -> ToolSetupRunV1:
    names = await asyncio.to_thread(_names, agent, run)
    request = ToolSetupRequestV1(
        parent_task_id=run.task_id,
        tool_id=piece.tool_id,
        kind=piece.kind,
        owner=piece.owner,
        step_id=piece.step_id,
        reason=_page_reason(run, piece, names.get(piece.tool_id, piece.tool_id)),
    )
    opened = piece.model_copy(
        update={"status": WAITING, "setup_request_id": request.setup_request_id, "attempt": piece.attempt + 1}
    )
    run = await _save(agent, run.with_piece(opened))
    try:
        await send_setup_request(agent, request, task_info)
    except Exception as exc:  # noqa: BLE001 - the run stops with a plain reason instead of waiting forever
        logger.warning(
            "A setup page could not be sent. event=tool_setup_page_send_failed run_id=%s tool_id=%s kind=%s error=%s",
            run.run_id,
            piece.tool_id,
            piece.kind,
            type(exc).__name__,
            exc_info=exc,
        )
        failed = opened.model_copy(update={"status": FAILED, "note": "the page could not be sent to the chat"})
        return await _save(agent, run.with_piece(failed))
    await asyncio.to_thread(link_setup_request, agent, request, task_info.meta)
    await _history(
        agent,
        run.task_id,
        f"Setup page ({piece.kind}) opened for {piece.tool_id}.",
        {
            "event": "tool_setup_request",
            "run_id": run.run_id,
            "setup_request_id": request.setup_request_id,
            "tool_id": piece.tool_id,
            "kind": piece.kind,
            "attempt": opened.attempt,
        },
    )
    return run


async def _open(agent: Any, run: ToolSetupRunV1, piece: RunPieceV1, task_info: Any) -> ToolSetupRunV1:
    return await _open_page(agent, run, piece, task_info)


async def advance(agent: Any, run_id: str, header: Any = None, meta: Any = None) -> None:
    """Open every piece that may start now, then resume the task once the run ends."""

    manager = agent.task_manager
    async with _lock(agent, run_id):
        run = await _load(agent, run_id)
        if run is None or run.status != RUN_WAITING:
            return
        task_info = await manager.operations_runtime.run(manager.get_task_info, run.task_id)
        if task_info is None or await manager.operations_runtime.run(manager.is_terminal_state, task_info.state):
            await _save(agent, run.model_copy(update={"status": RUN_STOPPED, "stop_reason": "task_ended"}))
            logger.info("A setup run ended with its task. event=tool_setup_run_task_ended run_id=%s", run_id)
            return
        for piece in openable(run):
            run = await _open(agent, run, piece, task_info)
        settled = settle(run)
        if settled.status != run.status:
            run = await _save(agent, settled)
    await asyncio.to_thread(complete_access_steps, agent, run)
    if run.status != RUN_WAITING:
        await _resume(agent, run, header, meta)


# -- results ------------------------------------------------------------------------------


async def _run_for(agent: Any, task_id: str, match: Any) -> ToolSetupRunV1 | None:
    runs = await asyncio.to_thread(run_repository(agent).for_task, team_id=str(agent.team_id), task_id=task_id)
    return next((run for run in runs if match(run)), None)


class PageResultOutcome(StrEnum):
    """What applying a page's result to its run did (``apply_page_result``)."""

    # Applied now, or applied before but its follow-up steps never finished: run them.
    APPLIED = "applied"
    # Its follow-up steps already finished: a second copy, ignored.
    REPEAT = "repeat"
    # No run of the task knows this page.
    NO_RUN = "no_run"


@dataclass(frozen=True)
class AppliedPageResult:
    outcome: PageResultOutcome
    run: ToolSetupRunV1 | None = None
    # The piece gets a fresh page (an expired or unwatched page, within the limit).
    offer_again: bool = False


def _knows_page(run: ToolSetupRunV1, setup_request_id: str) -> bool:
    """The run opened this page: one of its pieces carries it, or it applied its result before a re-offer."""

    return setup_request_id in run.results_applied or piece_for_setup(run, setup_request_id) is not None


async def apply_page_result(agent: Any, result: ToolSetupResultV1) -> AppliedPageResult:
    """Apply a page's result to the run that opened it, once, under the run's lock (module docstring).

    It runs before the caller checks whether the task ended, so a copy that
    arrives after the task ended is recognised too. A result applied before
    whose follow-up steps never finished (``results_handled``) is returned as
    applied again, so the caller runs them now and the task continues.
    """

    run = await _run_for(agent, result.parent_task_id, lambda item: _knows_page(item, result.setup_request_id))
    if run is None:
        return AppliedPageResult(PageResultOutcome.NO_RUN)
    async with _lock(agent, run.run_id):
        current = await _load(agent, run.run_id)
        if current is None:
            return AppliedPageResult(PageResultOutcome.NO_RUN)
        if result_handled(current, result.setup_request_id):
            logger.info(
                "A setup page's result arrived again after its run handled it; the copy is ignored. "
                "event=tool_setup_result_duplicate run_id=%s task_id=%s setup_request_id=%s status=%s",
                current.run_id,
                result.parent_task_id,
                result.setup_request_id,
                result.status,
            )
            return AppliedPageResult(PageResultOutcome.REPEAT, current)
        if result_applied(current, result.setup_request_id):
            logger.warning(
                "A setup page's result arrived again before its handling finished; its follow-up steps run again. "
                "event=tool_setup_result_handled_again run_id=%s task_id=%s setup_request_id=%s status=%s",
                current.run_id,
                result.parent_task_id,
                result.setup_request_id,
                result.status,
            )
            return AppliedPageResult(PageResultOutcome.APPLIED, current, _offered_again(current, result))
        changed, again = record_page_result(current, result)
        changed = await _save(agent, changed)
    return AppliedPageResult(PageResultOutcome.APPLIED, changed, again)


def _offered_again(run: ToolSetupRunV1, result: ToolSetupResultV1) -> bool:
    """For a result handled again: its piece still waits for its fresh page (only the history line says so)."""

    piece = piece_for_setup(run, result.setup_request_id)
    return piece is not None and piece.status == PENDING


async def mark_page_result_handled(agent: Any, run_id: str, setup_request_id: str) -> None:
    """Record that a page result's follow-up steps finished, so a later copy of it is ignored."""

    async with _lock(agent, run_id):
        run = await _load(agent, run_id)
        if run is None or result_handled(run, setup_request_id):
            return
        await _save(agent, run.model_copy(update={"results_handled": (*run.results_handled, setup_request_id)}))


async def continue_after_page_result(
    agent: Any, result: ToolSetupResultV1, applied: AppliedPageResult, header: Any, meta: Any
) -> None:
    """After a result was applied to a running task's run: its workflow step, the history, then what comes next.

    The result is marked handled only after all of them, so a failure on the
    way (or the agent stopping) leaves it to run again when the message is
    dispatched again.
    """

    run = applied.run
    if run is None:
        return
    task_info = await agent.task_manager.operations_runtime.run(agent.task_manager.get_task_info, result.parent_task_id)
    if task_info is not None:
        await asyncio.to_thread(record_setup_result, agent, result, task_info.meta)
    await _history(
        agent,
        result.parent_task_id,
        f"Setup page ({result.kind}) for {result.tool_id}: {result.status}"
        + ("; a new page is offered." if applied.offer_again else "."),
        {
            "event": "tool_setup_result",
            "run_id": run.run_id,
            "setup_request_id": result.setup_request_id,
            "status": result.status,
            "source": result.source,
            "attempt": result.attempt,
            "reason_code": result.reason_code,
            "offered_again": applied.offer_again,
        },
    )
    await advance(agent, run.run_id, header, meta)
    await mark_page_result_handled(agent, run.run_id, result.setup_request_id)


def complete_access_steps(agent: Any, run: ToolSetupRunV1) -> None:
    """Complete each workflow access step whose access is all settled (logged on failure).

    Trusted code, not the model, closes these steps: the access is approved
    (or handed on) and there is nothing else to do on them, and an open step
    would keep the task from finishing (the workflow completion guard).
    """

    from custom_agents.access_workflows import workflow_service_for
    from custom_messages.contracts.access_workflow_v2 import CLOSED_STEP_STATES, WorkflowProgressV2

    if not run.workflow_id:
        return
    service = workflow_service_for(agent)
    for step_id in _settled_access_steps(run):
        try:
            record = service.repository.get(run.workflow_id)
            step = record.step(step_id) if record is not None else None
            if record is None or step is None or step.state in CLOSED_STEP_STATES or step.blocked_by_person:
                continue
            service.trusted_progress(
                record,
                WorkflowProgressV2(step_id=step_id, state="complete", summary="Access approved."),
                code=ACCESS_APPROVED_CODE,
            )
        except Exception as exc:  # noqa: BLE001 - the page shows the requests' own status meanwhile
            logger.warning(
                "Could not complete a workflow access step. event=tool_setup_access_step_failed run_id=%s "
                "step_id=%s error=%s",
                run.run_id,
                step_id,
                type(exc).__name__,
            )


def _settled_access_steps(run: ToolSetupRunV1) -> list[str]:
    """The workflow access steps whose pieces are all done or skipped."""

    steps = {piece.workflow_step_id for piece in run.pieces if piece.kind == PIECE_ACCESS}
    return [
        step_id
        for step_id in sorted(item for item in steps if item)
        if all(item.status in SETTLED for item in run.pieces if item.workflow_step_id == step_id)
    ]


def _record_denial(agent: Any, run: ToolSetupRunV1, request: AccessRequestV1) -> None:
    """A denied request closes its workflow step at once (logged on failure)."""

    from custom_agents.access_workflows import workflow_service_for

    if not run.workflow_id:
        return
    try:
        workflow_service_for(agent).record_denial(run.workflow_id, request.request_id)
    except Exception as exc:  # noqa: BLE001 - the next read of the workflow records it
        logger.warning(
            "Could not close a workflow step for a denial. event=tool_setup_denial_step_failed run_id=%s error=%s",
            run.run_id,
            type(exc).__name__,
        )


async def on_access_decided(agent: Any, request: AccessRequestV1, header: Any, meta: Any) -> bool:
    """Apply a decision on one of a run's access requests; False when no run waits for it."""

    if not request.task_id:
        return False
    run = await _run_for(agent, str(request.task_id), lambda item: piece_for_request(item, request.request_id))
    if run is None:
        return False
    async with _lock(agent, run.run_id):
        current = await _load(agent, run.run_id)
        if current is None:
            return True
        changed = record_access_decision(current, request.request_id, request.status.value)
        if changed != current:
            current = await _save(agent, changed)
        piece = piece_for_request(current, request.request_id)
    if piece is not None and request.status.value == "denied":
        await asyncio.to_thread(_record_denial, agent, current, request)
    await advance(agent, run.run_id, header, meta)
    return True


# -- resume --------------------------------------------------------------------------------


async def _resume(agent: Any, run: ToolSetupRunV1, header: Any, meta: Any) -> None:
    """Answer the paused turn, or resume the task from a directive after a restart."""

    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    blocker = await run_ops(partial(get_human_interaction_blocker, manager), run.task_id)
    # Only the task's own wait is answered, once: the blocker is cleared first.
    if not blocker or blocker.get("kind") != TOOL_SETUP_RUN_KIND or blocker.get("run_id") != run.run_id:
        logger.info("No task waits on this setup run. event=tool_setup_run_not_waited run_id=%s", run.run_id)
        return
    await run_ops(partial(clear_human_interaction_blocker, manager), run.task_id)
    logger.info(
        "A setup run ended; the task resumes. event=tool_setup_run_ended run_id=%s task_id=%s status=%s",
        run.run_id,
        run.task_id,
        run.status,
    )
    session = getattr(agent, "_interrupted_turn_sessions", {}).get(run.task_id)
    if session is not None and any(item.interrupt_id == run.interrupt_id for item in session.interrupts):
        # The live turn resumes with its interrupt answered, as the steering
        # runtime does for a live session (``task_steering_runtime``); the
        # tool reads the stored run.
        steering = TaskSteering(
            steering_id=f"tool_setup_resume_{run.run_id}",
            steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
            target_task_id=run.task_id,
            target_agent_id=agent.id,
            interrupt_responses=[
                {"interrupt_id": run.interrupt_id, "response": {"kind": TOOL_SETUP_RUN_RESPONSE_KIND, "run_id": run.run_id}}
            ],
            status=TaskSteeringStatus.PENDING,
            idempotency_key=f"tool_setup_resume:{run.interrupt_id}",
        )
        agent._pending_interrupt_responses[run.task_id] = steering
        schedule_pending_work(agent)
        return
    result = await asyncio.to_thread(outcome_for, agent, run)
    directive = (
        "The tool setup this task paused for ended while the task was paused. Continue the task using this "
        "result; do the work if every tool is ready, otherwise tell the person plainly what is not set up: "
        + json.dumps(result, sort_keys=True)
    )
    await run_ops(manager.append_runtime_resume_directive, run.task_id, directive)
    await _history(agent, run.task_id, "Tool setup ended; the task resumes.", {"event": "tool_setup_run_resume", "run_id": run.run_id})
    await run_ops(manager.update_main_task_state, run.task_id, ProgressState.ACCEPTED, datetime.now(timezone.utc).isoformat())
    await run_ops(manager.enqueue_task, run.task_id, front=True)
    schedule_pending_work(agent)


def outcome_for(agent: Any, run: ToolSetupRunV1) -> dict[str, Any]:
    """The compact per-tool result of a run, with the services' own names."""

    return outcome(run, _names(agent, run))


__all__ = [
    "AppliedPageResult",
    "PageResultOutcome",
    "advance",
    "apply_page_result",
    "begin_run",
    "complete_access_steps",
    "continue_after_page_result",
    "mark_page_result_handled",
    "on_access_decided",
    "outcome_for",
    "run_repository",
    "send_setup_request",
]
