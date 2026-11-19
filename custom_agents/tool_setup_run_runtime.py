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
- **Results** (``on_setup_result``, ``on_access_decided``): the gateway's
  ``ToolSetupResultV1`` and the Inbox's decisions update the run
  (``tool_setup_runs``): an expired page, or one nobody watched after a
  gateway restart, is offered again automatically, up to three pages; a
  closed page or a denial stops the run.
- **Resume** (``_resume``): once every piece is done, or the run stopped, the
  paused turn gets its interrupt answered and ``set_up_tools`` returns the
  compact per-tool result; with no live turn (after an agent restart) the
  task resumes from a note that states the same result.

  The task's record of the wait (its ``tool_setup_run`` blocker) is the only
  saved record of it, so it stays until the wait really ends. A live turn's
  answer waits in memory until the task's lane is free and the agent is not
  paused (``execution_lanes_runtime``), so the record is first marked as
  holding the answer (``mark_setup_answer_held``), and the resumed turn
  removes it just before it runs (``task_steering_runtime``). Until
  2026-11-19 the record was deleted when the run ended, and a task whose
  answer waited for a busy lane was reported as stuck. A note is saved
  before the record is removed; a restart between the two saves the same
  note again, which is not added twice. Both decisions are made under the
  run's lock, after reading the record again, so a task is resumed once.
- **Answers a paused turn cannot take** (``resume_without_paused_turn``): the
  paused turn ended after its answer was held, or the answer could not be
  given to the SDK. The task then resumes from the note too, so the answer
  is not lost with the turn; a paused turn that can never take its answer is
  dropped. A run that is not stored resumes the task with a note saying the
  setup was not found (``_resume_missing_run``).
- **Recovery** (``resume_ended_setup_runs``, at agent start after restart
  recovery and on every scheduler pass): a task whose run ended without
  resuming it (a restart between the run's end and the resume, a lost
  wake-up, a run that is not stored, a result whose ``advance`` was lost) is
  found from its record and resumed. A restart leaves no paused turn, so
  every held-answer mark is stale then and the task resumes from a note with
  the run's result. Only the agent's own tasks are read; another agent's are
  left to that agent, or to the takeover of a gone agent's work.

The run and the task blocker are stored, so both processes can restart
during the wait. Nothing here grants access or reads a credential.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.access_workflow_setup_hooks import link_setup_request, record_setup_result
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_human_interaction_blockers import (
    clear_setup_run_blocker,
    get_human_interaction_blocker,
    is_setup_run_blocker,
    mark_setup_answer_held,
    record_human_interaction_blocker,
    setup_answer_held,
)
from custom_agents.tool_setup_runs import (
    ACCESS_APPROVED_CODE,
    FAILED,
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


async def on_setup_result(agent: Any, result: ToolSetupResultV1, header: Any, meta: Any) -> bool:
    """Apply a page's result to its run; False when no run waits for it."""

    run = await _run_for(agent, result.parent_task_id, lambda item: piece_for_setup(item, result.setup_request_id))
    if run is None:
        return False
    async with _lock(agent, run.run_id):
        current = await _load(agent, run.run_id)
        if current is None:
            return True
        changed, again = record_page_result(current, result)
        if changed != current:
            await _save(agent, changed)
    task_info = await agent.task_manager.operations_runtime.run(agent.task_manager.get_task_info, result.parent_task_id)
    if task_info is not None:
        await asyncio.to_thread(record_setup_result, agent, result, task_info.meta)
    await _history(
        agent,
        result.parent_task_id,
        f"Setup page ({result.kind}) for {result.tool_id}: {result.status}"
        + ("; a new page is offered." if again else "."),
        {
            "event": "tool_setup_result",
            "run_id": run.run_id,
            "setup_request_id": result.setup_request_id,
            "status": result.status,
            "source": result.source,
            "attempt": result.attempt,
            "reason_code": result.reason_code,
            "offered_again": again,
        },
    )
    await advance(agent, run.run_id, header, meta)
    return True


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paused_turn_waits(agent: Any, run: ToolSetupRunV1) -> bool:
    """The task's paused turn is still in memory and waits on this run's interrupt."""

    session = getattr(agent, "_interrupted_turn_sessions", {}).get(run.task_id)
    return session is not None and any(item.interrupt_id == run.interrupt_id for item in session.interrupts)


async def _resume(agent: Any, run: ToolSetupRunV1, header: Any, meta: Any, *, wake: bool = True) -> bool:
    """Answer the paused turn, or resume the task from a note (module docstring).

    True when this call resumed the task. Runs under the run's lock and reads
    the task's record again inside it, so callers that see the run ended at
    the same time resume the task once. ``wake=False`` (recovery at agent
    start) queues the task without waking the drain; a live turn, which
    cannot exist then, is always woken.
    """

    manager = agent.task_manager
    async with _lock(agent, run.run_id):
        blocker = await manager.operations_runtime.run(partial(get_human_interaction_blocker, manager), run.task_id)
        # Only the task's own wait is answered.
        if not is_setup_run_blocker(blocker, run.run_id):
            logger.info("No task waits on this setup run. event=tool_setup_run_not_waited run_id=%s", run.run_id)
            return False
        if _paused_turn_waits(agent, run):
            if setup_answer_held(blocker):
                return False  # answered already: the answer waits for the lane, or the lane is resuming it
            return await _answer_paused_turn(agent, run)
        result = await asyncio.to_thread(outcome_for, agent, run)
        resumed = await _resume_from_note(
            agent, run.task_id, run.run_id, RUN_ENDED_NOTE + json.dumps(result, sort_keys=True),
            history="Tool setup ended; the task resumes.", event="tool_setup_run_resume", wake=wake,
        )
    if resumed:
        logger.info(
            "A setup run ended; the task resumes from a note. event=tool_setup_run_ended run_id=%s task_id=%s status=%s",
            run.run_id,
            run.task_id,
            run.status,
        )
    return resumed


async def _answer_paused_turn(agent: Any, run: ToolSetupRunV1) -> bool:
    """Mark the record, then hand the answer to the paused turn; it runs when the task's lane is free.

    The mark is saved before the answer exists in memory, so the answer is
    never held without its record; the resumed turn removes the record
    (``task_steering_runtime``). The turn reads the stored run.
    """

    manager = agent.task_manager
    marked = await manager.operations_runtime.run(
        partial(mark_setup_answer_held, manager), run.task_id, run.run_id, holder_id=str(agent.id), at=_now()
    )
    if not marked:
        return False
    # The live turn resumes with its interrupt answered, as the steering
    # runtime does for a live session (``task_steering_runtime``).
    agent._pending_interrupt_responses[run.task_id] = TaskSteering(
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
    logger.info(
        "A setup run ended; its answer waits for the task's lane. event=tool_setup_answer_held run_id=%s task_id=%s "
        "status=%s",
        run.run_id,
        run.task_id,
        run.status,
    )
    schedule_pending_work(agent)
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


def _drop_paused_turn(agent: Any, task_id: str, interrupt_id: str, *, why: str) -> None:
    """Forget the task's paused turn when it waits on ``interrupt_id`` but can never take its answer (logged).

    Its Strands agent is not reused (``replace_before_next_turn``); the task's
    next turn starts afresh from its note. Left in place, a later answer
    would be handed to it again.
    """

    sessions = getattr(agent, "_interrupted_turn_sessions", {})
    session = sessions.get(task_id)
    if session is None or not any(item.interrupt_id == interrupt_id for item in session.interrupts):
        return
    sessions.pop(task_id, None)
    session.strands.replace_before_next_turn = True
    logger.warning(
        "Dropped a paused turn that cannot take its setup answer. event=tool_setup_paused_turn_dropped "
        "task_id=%s interrupt_id=%s why=%s",
        task_id,
        interrupt_id,
        why,
    )


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
    _drop_paused_turn(agent, task_id, interrupt_id, why=why)
    try:
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
        _drop_paused_turn(agent, task_id, str(blocker.get("interrupt_id") or ""), why="setup_run_not_found")
        resumed = await _resume_from_note(
            agent, task_id, run_id, RUN_NOT_FOUND_NOTE,
            history="The tool setup was not found; the task resumes.", event="tool_setup_run_not_found", wake=wake,
        )
    if resumed:
        logger.warning(
            "The setup run a task waited for is not stored; the task resumes without its result. "
            "event=tool_setup_run_not_found run_id=%s task_id=%s",
            run_id,
            task_id,
        )
    return resumed


async def _resume_from_note(
    agent: Any, task_id: str, run_id: str, note: str, *, history: str, event: str, wake: bool = True
) -> bool:
    """Queue the task at the front with a note its next turn reads; True when this call removed the record.

    The caller holds the run's lock. The note is saved before the record is
    removed: a restart between the two finds the record and saves the same
    note again, which is not added twice (``append_runtime_resume_directive``).
    """

    manager = agent.task_manager
    run_ops = manager.operations_runtime.run
    await run_ops(manager.append_runtime_resume_directive, task_id, note)
    if not await run_ops(partial(clear_setup_run_blocker, manager), task_id, run_id):
        # The record was read under the lock; only the task ending removes it meanwhile.
        return False
    await _history(agent, task_id, history, {"event": event, "run_id": run_id})
    await run_ops(manager.update_main_task_state, task_id, ProgressState.ACCEPTED, _now())
    await run_ops(manager.enqueue_task, task_id, front=True)
    if wake:
        schedule_pending_work(agent)
    return True


# -- recovery ------------------------------------------------------------------------------

# How often an agent looks for setup runs whose task was not resumed (the pace
# of ``access_request_runtime.resume_decided_access_waits``).
SETUP_RUN_RECOVERY_SECONDS = 60.0
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
    if setup_answer_held(blocker) and _paused_turn_waits(agent, run):
        return None  # the answer waits for the task's lane
    return RECOVER_RESUME


async def resume_ended_setup_runs(agent: Any, *, now: float | None = None, wake: bool = True) -> int:
    """Resume this agent's tasks whose setup run ended without resuming them (module docstring).

    Runs at agent start, after restart recovery (``wake=False``: startup wakes
    the drain once it is ready), and on every scheduler pass, at most every
    ``SETUP_RUN_RECOVERY_SECONDS``. Only the agent's own tasks are read;
    nothing here starts an agent. Each task is handled on its own, so one
    failure is logged and the others go on. Returns how many it resumed.
    """

    import time

    moment = time.monotonic() if now is None else now
    if moment < getattr(agent, "_setup_run_check_after", 0.0):
        return 0
    agent._setup_run_check_after = moment + SETUP_RUN_RECOVERY_SECONDS
    resumed = 0
    for task_id, blocker in await agent.task_manager.operations_runtime.run(partial(_setup_run_waits, agent)):
        resumed += await _recover_task(agent, task_id, blocker, wake=wake)
    return resumed


async def _recover_task(agent: Any, task_id: str, blocker: dict[str, Any], *, wake: bool) -> int:
    """Recover one task's setup run; 1 when it resumed the task. A failure is logged, never raised."""

    run_id = str(blocker.get("run_id") or "")
    stage = "read"
    try:
        run = await _load(agent, run_id)
        if run is None or run.task_id != task_id:
            stage = RECOVER_NOT_FOUND
            return int(await _resume_missing_run(agent, task_id, run_id, wake=wake))
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
            "Could not recover a task's setup run; the next pass tries again. event=tool_setup_run_recovery_failed "
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
            "answer_mark_stale" if setup_answer_held(blocker) else "not_resumed",
        )
    return int(resumed)


def outcome_for(agent: Any, run: ToolSetupRunV1) -> dict[str, Any]:
    """The compact per-tool result of a run, with the services' own names."""

    return outcome(run, _names(agent, run))


__all__ = [
    "advance",
    "begin_run",
    "complete_access_steps",
    "on_access_decided",
    "on_setup_result",
    "outcome_for",
    "resume_ended_setup_runs",
    "resume_without_paused_turn",
    "run_repository",
    "send_setup_request",
]
