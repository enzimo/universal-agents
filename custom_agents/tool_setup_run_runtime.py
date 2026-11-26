"""Driving ``set_up_tools`` runs in the team lead's process (owner's decision 2026-09-28).

``set_up_tools`` (``custom_tools.providers.tool_setup``) plans a run, publishes
its workflow, raises its access requests, stores the run and pauses
TeamLead's turn with a local Strands interrupt (``TOOL_SETUP_RUN_KIND``); it
never asks the person whether to start. This module does the rest, in trusted
code:

- **Begin** (``stage_run`` before ``ProgressState.WAITING`` and ``start_run``
  after it, from ``task_steering_runtime`` when the turn pauses): ``stage_run``
  persists the task's blocker record and updates ``run.interrupt_id`` before
  ``WAITING`` so a crash around pausing never leaves an unblocked waiting task;
  ``start_run`` then announces the workflow link and opens every piece that may
  start now. If a crash skips ``start_run``, recovery announces the workflow
  once before advancing or waiting.
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
  Recovery (below) may advance such a run first; the copy then still records
  the page on its workflow step and in the history, and its ``advance`` finds
  nothing left to do. Every save of the run (``advance``, applying a result,
  marking it handled) reads it again under the run's lock, and the resume
  paths change only the task's record, so recovery never overwrites the
  marks.
- **Resume** (``_resume``): once every piece is done, or the run stopped, the
  paused turn gets its interrupt answered and ``set_up_tools`` returns the
  compact per-tool result; with no live turn (after an agent restart) the
  task resumes from a note that states the same result.

  The task's record of the wait (its ``tool_setup_run`` blocker) is the only
  saved record of it, so it stays until the wait really ends. A live turn's
  answer waits in memory until the task's lane is free and the agent is not
  paused (``execution_lanes_runtime``), so the record is first marked as
  holding the answer (``mark_answer_held``), and the resumed turn
  removes it just before it runs (``task_steering_runtime``). Until
  2026-11-19 the record was deleted when the run ended, and a task whose
  answer waited for a busy lane was reported as stuck. A note is saved
  before the record is removed; a restart between the two saves the same
  note again, which is not added twice, and a failure after the removal puts
  the record back, so recovery tries again. Both decisions are made under
  the run's lock, after reading the record again, so a task is resumed once.
- **Answers a paused turn cannot take** (``resume_without_paused_turn``): the
  paused turn ended after its answer was held, or the answer could not be
  given to the SDK. The task then resumes from the note too, so the answer
  is not lost with the turn; a paused turn that can never take its answer is
  dropped, and the record loses its mark at once, so a resume that then
  fails leaves a task the stuck-task check can report. A run that is not
  stored resumes the task with a note saying the setup was not found
  (``_resume_missing_run``).
- **Recovery** (``resume_ended_setup_runs``, at agent start after restart
  recovery and on every scheduler pass): a task whose run ended without
  resuming it (a restart between the run's end and the resume, a lost
  wake-up, a run that is not stored, a result whose ``advance`` was lost) is
  found from its record and resumed. A restart leaves no paused turn, so
  start recovery first removes every held-answer mark and the task resumes
  from a note with the run's result. Only the agent's own tasks are read;
  another agent's are left to that agent, or to the takeover of a gone
  agent's work.

The run and the task blocker are stored, so both processes can restart
during the wait. Nothing here grants access or reads a credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import partial
from typing import Any

from custom_agents.access_workflow_setup_hooks import link_setup_request, record_setup_result
from custom_agents.paused_wait_resume import (
    WaitLogSpec,
    append_wait_history,
    drop_paused_turn,
    hand_answer_to_paused_turn,
    paused_turn_waits,
    resume_from_note,
)
from custom_agents.task_human_interaction_blockers import (
    SETUP_RUN_BLOCKER,
    answer_held,
    get_human_interaction_blocker,
    is_setup_run_blocker,
    record_human_interaction_blocker,
    remove_answer_mark,
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
    still_waits,
)
from custom_messages.contracts.access_v2 import AccessRequestV1
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import (
    TaskSteeringStatus,
    TaskSteeringType,
)
from id_generation import new_id
from storage.tool_setup_run_repository import ToolSetupRunRepository

logger = logging.getLogger(__name__)
# The UserNotification reason of the setup workflow's notice (audit and delivery routing).
WORKFLOW_NOTICE_REASON = "tool_setup_workflow"
_SETUP_WAIT_LOG = WaitLogSpec(
    label="setup-run",
    dropped_event="tool_setup_paused_turn_dropped",
)


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

    locks: dict[str, asyncio.Lock] | None = getattr(agent, "_tool_setup_run_locks", None)
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


# -- begin ------------------------------------------------------------------------------


async def stage_run(
    agent: Any, *, task_id: str, interrupt_id: str, reason: dict[str, Any], msg_meta: Any
) -> str:
    """Persist the setup-run blocker and interrupt id before ``ProgressState.WAITING`` is saved.

    Saving the record before ``WAITING`` (matching access requests and timed waits)
    prevents a crash right after ``WAITING`` from leaving an unblocked task that
    recovery immediately re-queues. Workflow announcement and opening pages stay
    in ``start_run`` after ``WAITING`` so an immediately settled run cannot wake
    the task before the state transition completes.
    """

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
            return run_id
        if run.interrupt_id != interrupt_id:
            await _save(agent, run.model_copy(update={"interrupt_id": interrupt_id}))
    return run_id


def _workflow_announce_attempted(agent: Any) -> set[str]:
    """Run ids whose workflow chat announcement was already attempted in this process."""

    attempted: set[str] | None = getattr(agent, "_setup_workflow_announce_attempted", None)
    if attempted is None:
        attempted = set()
        agent._setup_workflow_announce_attempted = attempted
    return attempted


async def start_run(agent: Any, *, task_id: str, run_id: str) -> None:
    """Post-``WAITING`` step for a staged setup run: announce its workflow, then open pieces.

    If a concurrent decision already settled the run during ``stage_run`` (before
    ``WAITING`` was saved), resume the task directly instead of leaving it for a
    later recovery pass.
    """

    settled_run: ToolSetupRunV1 | None = None
    async with _lock(agent, run_id):
        run = await _load(agent, run_id)
        if run is None:
            return
        if run.status != RUN_WAITING:
            settled_run = run
        else:
            # The link comes before any page, so the person knows what the pages are for.
            _workflow_announce_attempted(agent).add(run_id)
            run = await _announce_workflow(agent, run)
    if settled_run is not None:
        await _resume(agent, settled_run, None, None)
        return
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
    await append_wait_history(
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


async def advance(agent: Any, run_id: str, header: Any = None, meta: Any = None, *, wake: bool = True) -> bool:
    """Open every piece that may start now, then resume the task once the run ends; True when it resumed it.

    ``wake`` is False only for recovery at agent start (``resume_ended_setup_runs``).
    """

    manager = agent.task_manager
    async with _lock(agent, run_id):
        run = await _load(agent, run_id)
        if run is None or run.status != RUN_WAITING:
            return False
        task_info = await manager.operations_runtime.run(manager.get_task_info, run.task_id)
        if task_info is None or await manager.operations_runtime.run(manager.is_terminal_state, task_info.state):
            await _save(agent, run.model_copy(update={"status": RUN_STOPPED, "stop_reason": "task_ended"}))
            logger.info("A setup run ended with its task. event=tool_setup_run_task_ended run_id=%s", run_id)
            return False
        for piece in openable(run):
            run = await _open(agent, run, piece, task_info)
        settled = settle(run)
        if settled.status != run.status:
            run = await _save(agent, settled)
    await asyncio.to_thread(complete_access_steps, agent, run)
    if run.status == RUN_WAITING:
        return False
    return await _resume(agent, run, header, meta, wake=wake)


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
    await append_wait_history(
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


# The note a task resumes from when no paused turn can take the run's answer.
RUN_ENDED_NOTE = (
    "The tool setup this task paused for ended while the task was paused. Continue the task using this "
    "result; do the work if every tool is ready, otherwise tell the person plainly what is not set up: "
)


async def _resume(agent: Any, run: ToolSetupRunV1, header: Any, meta: Any, *, wake: bool = True) -> bool:
    """Answer the paused turn, or resume the task from a note (module docstring).

    True when this call resumed the task. Runs under the run's lock and reads
    the task's record again inside it, so callers that see the run ended at
    the same time resume the task once. ``wake=False`` (recovery at agent
    start) queues the task without waking the drain; a live turn, which
    cannot exist then, is always woken.
    """

    manager = agent.task_manager
    # "Once" rests on the record, read again here under the lock: a held
    # answer stops a second caller below, and each path's own record change
    # (the mark's compare-and-set, the note path's conditional removal) lets
    # only one caller through even without the lock.
    async with _lock(agent, run.run_id):
        current = await _load(agent, run.run_id) or run
        blocker = await manager.operations_runtime.run(
            partial(get_human_interaction_blocker, manager), current.task_id
        )
        # Only the task's own wait is answered.
        if not is_setup_run_blocker(blocker, current.run_id):
            logger.info("No task waits on this setup run. event=tool_setup_run_not_waited run_id=%s", current.run_id)
            return False
        if paused_turn_waits(agent, current.task_id, current.interrupt_id):
            if answer_held(blocker):
                return False  # answered already: the answer waits for the lane, or the lane is resuming it
            return await _answer_paused_turn(agent, current)
        task_info = await manager.operations_runtime.run(manager.get_task_info, current.task_id)
        if task_info is not None and await manager.operations_runtime.run(manager.is_executing_state, task_info.state):
            logger.warning(
                "Task is still in an executing state with a setup-run record and no matching paused turn; "
                "leaving for recovery pass. event=tool_setup_run_executing_with_blocker "
                "run_id=%s task_id=%s state=%s",
                current.run_id,
                current.task_id,
                task_info.state.value,
            )
            return False
        result = await asyncio.to_thread(outcome_for, agent, current)
        resumed = await resume_from_note(
            agent,
            task_id=current.task_id,
            kind=SETUP_RUN_BLOCKER,
            wait_id=current.run_id,
            note=RUN_ENDED_NOTE + json.dumps(result, sort_keys=True),
            history="Tool setup ended; the task resumes.",
            history_details={"event": "tool_setup_run_resume", "run_id": current.run_id},
            wake=wake,
        )
    if resumed:
        logger.info(
            "A setup run ended; the task resumes from a note. event=tool_setup_run_ended run_id=%s task_id=%s status=%s",
            current.run_id,
            current.task_id,
            current.status,
        )
    return resumed


async def _answer_paused_turn(agent: Any, run: ToolSetupRunV1) -> bool:
    """Mark the record, then hand the answer to the paused turn; it runs when the task's lane is free.

    The mark is saved before the answer exists in memory, so the answer is
    never held without its record; the resumed turn removes the record
    (``task_steering_runtime``). The turn reads the stored run.
    """

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
    held = await hand_answer_to_paused_turn(
        agent,
        task_id=run.task_id,
        kind=SETUP_RUN_BLOCKER,
        wait_id=run.run_id,
        steering=steering,
        interrupt_id=run.interrupt_id,
    )
    if not held:
        return False
    logger.info(
        "A setup run ended; its answer waits for the task's lane. event=tool_setup_answer_held run_id=%s task_id=%s "
        "status=%s",
        run.run_id,
        run.task_id,
        run.status,
    )
    return True


# The note a task resumes from when the run its record names is not stored.
RUN_NOT_FOUND_NOTE = (
    "The tool setup this task paused for could not be found, so its result is unknown. Continue the task: "
    "use a tool only if your context lists it as ready, otherwise call set_up_tools again or tell the person "
    "plainly what is not set up."
)


def _setup_answer(steering: TaskSteering) -> tuple[str, str] | None:
    """The (interrupt id, run id) of a ``set_up_tools`` answer (``_answer_paused_turn``), or None for any other."""

    for item in steering.interrupt_responses or []:
        response = item.get("response")
        if isinstance(response, dict) and response.get("kind") == TOOL_SETUP_RUN_RESPONSE_KIND and response.get("run_id"):
            return str(item.get("interrupt_id") or ""), str(response["run_id"])
    return None


async def resume_without_paused_turn(agent: Any, task_id: str, steering: TaskSteering, *, why: str) -> bool:
    """A ``set_up_tools`` answer its paused turn could not take: resume the task from a note (module docstring).

    ``why`` is ``paused_turn_gone`` or ``answer_not_converted``
    (``task_steering_runtime._resume_interrupted_task_turn``). Returns False,
    doing nothing, for any other answer, which keeps its own handling.

    It runs in the drain, so a failure is logged and not raised: raising
    would hold back every other task of the agent, and repeating it would
    not help, because the answer is already taken. The task keeps its
    record, which recovery finishes (``resume_ended_setup_runs``).
    """

    answer = _setup_answer(steering)
    if answer is None:
        return False
    interrupt_id, run_id = answer
    logger.warning(
        "A paused turn could not take its setup answer; the task resumes from a note. "
        "event=tool_setup_answer_not_taken task_id=%s run_id=%s why=%s",
        task_id,
        run_id,
        why,
    )
    drop_paused_turn(agent, task_id, interrupt_id, why=why, log=_SETUP_WAIT_LOG)
    manager = agent.task_manager
    try:
        # No paused turn waits for the answer any more, so the record must not
        # claim one is held: if the resume below fails, the task can be reported.
        await manager.operations_runtime.run(
            partial(remove_answer_mark, manager), task_id, SETUP_RUN_BLOCKER, run_id
        )
        run = await _load(agent, run_id)
        if run is None or run.task_id != task_id:
            return await _resume_missing_run(agent, task_id, run_id)
        return await _resume(agent, run, None, None)
    except Exception as exc:  # noqa: BLE001 - recovery resumes the task from its record (docstring)
        logger.warning(
            "Could not resume a task whose paused turn could not take its setup answer; recovery will. "
            "event=tool_setup_answer_not_taken_failed task_id=%s run_id=%s why=%s error_type=%s",
            task_id,
            run_id,
            why,
            type(exc).__name__,
            exc_info=exc,
        )
        return False


async def _resume_missing_run(agent: Any, task_id: str, run_id: str, *, wake: bool = True) -> bool:
    """The run the task's record names is not stored: resume the task saying the setup was not found (logged).

    A paused turn still waiting on it is dropped: its answer can never come,
    and ``set_up_tools`` would quietly plan a new run if it were answered.
    """

    manager = agent.task_manager
    async with _lock(agent, run_id):
        blocker = await manager.operations_runtime.run(partial(get_human_interaction_blocker, manager), task_id)
        if blocker is None or not is_setup_run_blocker(blocker, run_id):
            return False
        drop_paused_turn(
            agent, task_id, str(blocker.get("interrupt_id") or ""), why="setup_run_not_found", log=_SETUP_WAIT_LOG
        )
        resumed = await resume_from_note(
            agent,
            task_id=task_id,
            kind=SETUP_RUN_BLOCKER,
            wait_id=run_id,
            note=RUN_NOT_FOUND_NOTE,
            history="The tool setup was not found; the task resumes.",
            history_details={"event": "tool_setup_run_not_found", "run_id": run_id},
            wake=wake,
        )
    if resumed:
        logger.warning(
            "The setup run a task waited for is not stored; the task resumes without its result. "
            "event=tool_setup_run_not_found run_id=%s task_id=%s",
            run_id,
            task_id,
        )
    return resumed


# -- recovery ------------------------------------------------------------------------------

# How often an agent looks for setup runs whose task was not resumed (the pace
# of ``access_request_runtime.resume_decided_access_waits``).
SETUP_RUN_RECOVERY_SECONDS = 60.0
# A run saved this recently may be between two steps of its own (a result
# saved before its ``advance``, an ended run before ``_resume``); a pass
# leaves it to the next pass rather than report a lost wake-up that is not
# one (review C07). Below the pace, so a lost run waits at most two passes.
RECENT_RUN_SECONDS = 30.0
# What recovery does for one task (``_recovery_step``).
RECOVER_ADVANCE = "advance"
RECOVER_RESUME = "resume"
RECOVER_NOT_FOUND = "not_found"


def _setup_run_waits(agent: Any) -> list[tuple[str, dict[str, Any]]]:
    """This agent's unfinished tasks that have a setup-run record, with the record (an operations-lane read)."""

    manager = agent.task_manager
    waits = []
    for task_id, blocker in list(manager.task_state.human_interaction_blockers.items()):
        if not is_setup_run_blocker(blocker):
            continue
        info = manager.get_task_info(task_id)
        if info is not None and not manager.is_terminal_state(info.state):
            waits.append((task_id, dict(blocker)))
    return waits


def _recovery_step(agent: Any, blocker: dict[str, Any], run: ToolSetupRunV1) -> str | None:
    """What recovery does for a task whose record names the stored ``run``; None when the task waits legitimately."""

    if run.status == RUN_WAITING:
        return None if still_waits(run) else RECOVER_ADVANCE
    if answer_held(blocker) and paused_turn_waits(agent, run.task_id, run.interrupt_id):
        return None  # the answer waits for the task's lane
    return RECOVER_RESUME


async def resume_ended_setup_runs(agent: Any, *, now: float | None = None, at_start: bool = False) -> int:
    """Resume this agent's tasks whose setup run ended without resuming them (module docstring).

    Runs at agent start, after restart recovery, and on every scheduler pass,
    at most every ``SETUP_RUN_RECOVERY_SECONDS``. Only the agent's own tasks
    are read; nothing here starts an agent. Each task is handled on its own,
    so one failure is logged and the others go on. Returns how many it
    resumed.

    A pass leaves a run alone while another step of this process works on
    it (its lock is held) or just saved it (``RECENT_RUN_SECONDS``), so its
    warnings name only real lost wake-ups. ``at_start``: the steps of the
    process that saved a recent run stopped with it, so the run is recovered
    at once (a result that arrives during startup is still kept apart by
    the run's lock, and at worst adds one needless warning); and startup
    wakes the drain itself once it is ready, so recovery only queues tasks.
    """

    moment = time.monotonic() if now is None else now
    if moment < getattr(agent, "_setup_run_check_after", 0.0):
        return 0
    agent._setup_run_check_after = moment + SETUP_RUN_RECOVERY_SECONDS
    resumed = 0
    for task_id, blocker in await agent.task_manager.operations_runtime.run(partial(_setup_run_waits, agent)):
        resumed += await _recover_task(agent, task_id, blocker, at_start=at_start)
    return resumed


def _changed_recently(run: ToolSetupRunV1) -> bool:
    return (datetime.now(timezone.utc) - run.updated_at_utc).total_seconds() < RECENT_RUN_SECONDS


async def _announce_workflow_if_needed(agent: Any, run: ToolSetupRunV1) -> ToolSetupRunV1:
    """Announce a recovered waiting run's workflow once if a crash skipped ``start_run``."""

    if run.status != RUN_WAITING or not run.workflow_id or run.workflow_announced:
        return run
    attempted = _workflow_announce_attempted(agent)
    if run.run_id in attempted:
        return run
    if any(piece.attempt > 0 or piece.setup_request_id for piece in run.pieces):
        # Pages were already opened, so start_run already ran for this run.
        attempted.add(run.run_id)
        return run
    async with _lock(agent, run.run_id):
        current = await _load(agent, run.run_id)
        if current is None or current.status != RUN_WAITING or current.workflow_announced:
            return current or run
        attempted.add(run.run_id)
        return await _announce_workflow(agent, current)


async def _recover_task(agent: Any, task_id: str, blocker: dict[str, Any], *, at_start: bool) -> int:
    """Recover one task's setup run; 1 when it resumed the task. A failure is logged, never raised."""

    run_id = str(blocker.get("run_id") or "")
    if _lock(agent, run_id).locked():
        return 0  # a step of this process works on the run now; the next pass looks again
    wake = not at_start
    stage = "read"
    try:
        run = await _load(agent, run_id)
        if run is None or run.task_id != task_id:
            stage = RECOVER_NOT_FOUND
            return int(await _resume_missing_run(agent, task_id, run_id, wake=wake))
        if not at_start and _changed_recently(run):
            return 0
        run = await _announce_workflow_if_needed(agent, run)
        step = _recovery_step(agent, blocker, run)
        if step is None:
            return 0
        stage = step
        if step == RECOVER_ADVANCE:
            logger.warning(
                "A setup run waits with work left for it; advancing it. event=tool_setup_run_advanced_on_recovery "
                "run_id=%s task_id=%s",
                run_id,
                task_id,
            )
            return int(await advance(agent, run_id, wake=wake))
        resumed = await _resume(agent, run, None, None, wake=wake)
    except Exception as exc:  # noqa: BLE001 - one task must not stop recovery for the others
        logger.warning(
            "Could not recover a task's setup run; it keeps its record and the next pass tries again. "
            "event=tool_setup_run_recovery_failed "
            "task_id=%s run_id=%s stage=%s error_type=%s",
            task_id,
            run_id,
            stage,
            type(exc).__name__,
            exc_info=exc,
        )
        return 0
    if resumed:
        logger.warning(
            "Found a setup run that ended without resuming its task; resumed it. event=tool_setup_run_recovered "
            "run_id=%s task_id=%s status=%s why=%s",
            run_id,
            task_id,
            run.status,
            "answer_mark_stale" if answer_held(blocker) else "not_resumed",
        )
    return int(resumed)


def outcome_for(agent: Any, run: ToolSetupRunV1) -> dict[str, Any]:
    """The compact per-tool result of a run, with the services' own names."""

    return outcome(run, _names(agent, run))


__all__ = [
    "AppliedPageResult",
    "PageResultOutcome",
    "advance",
    "apply_page_result",
    "complete_access_steps",
    "continue_after_page_result",
    "mark_page_result_handled",
    "on_access_decided",
    "outcome_for",
    "resume_ended_setup_runs",
    "resume_without_paused_turn",
    "run_repository",
    "send_setup_request",
    "stage_run",
    "start_run",
]
