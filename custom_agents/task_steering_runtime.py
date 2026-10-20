"""Mid-turn steering helpers for UniversalAgent."""

from __future__ import annotations

import logging
import asyncio
from functools import partial
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from strands.hooks import BeforeToolCallEvent, HookRegistry

from custom_agents.message_routing import resolve_requester_routing_key
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.prompt_context_sanitization import render_replied_message_lines
from custom_agents.provider_error_runtime import ProviderTimeoutError, is_provider_timeout_error
from custom_agents.sdk.strands.turn_tool_stop import (
    TURN_CANCELLED_REASON,
    invoke_with_tool_stop,
    stop_tool_calls,
)
from custom_agents.task_runtime_state import TurnStartNotes
from custom_tools.tool_stop import ToolStop
from custom_agents.turn_failure_runtime import UnroutableTaskInterruptError, settle_failed_turn
from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_KIND
from custom_agents.task_human_interaction_blockers import (
    ACCESS_REQUEST_BLOCKER,
    clear_answered_access_blocker,
    clear_human_interaction_blocker,
    record_human_interaction_blocker,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import (
    ACCESS_REQUEST_ID_KEY,
    ACCESS_REQUEST_IDS_KEY,
    ACCESS_REQUEST_INTERRUPT_KIND,
    Task,
    TaskSteering,
    TaskSteeringInterruptRecord,
    access_interrupt_request_ids,
)
from custom_messages.enums import (
    ProgressState,
    RequesterType,
    RoutingType,
    TaskHistoryImportance,
    TaskHistoryType,
    TaskSteeringStatus,
    TaskSteeringStatusCode,
    TaskSteeringType,
)
from id_generation import new_id
from storage.runtime_custody import CustodyStore, RuntimeCustodyLost

logger = logging.getLogger(__name__)
GUIDANCE_ACCEPTED_EVENT = "task_steering_proactive_guidance_accepted"
GUIDANCE_APPLIED_EVENT = "task_steering_proactive_guidance_applied"


@dataclass
class StrandsInterruptedTurnSession:
    """In-memory state needed to resume a live Strands interrupt."""

    task_id: str
    strands_agent: Any
    invoke_kwargs: Dict[str, Any]
    full_prompt: str
    msg_content: Any
    msg_header: MessageHeader
    msg_meta: MessageMeta
    created_at: str
    interrupts: List[TaskSteeringInterruptRecord]
    steering_id: Optional[str] = None
    # What the paused turn read at its start; the resumed turn's decision
    # releases it (TurnStartNotes). Lost with the session at a restart, when
    # the notes are still saved and the task's fresh turn reads them again.
    start_notes: Optional[TurnStartNotes] = None


def ensure_fresh_strands_agent_for_new_turn(
    agent: Any,
    *,
    task_id: Optional[str],
) -> bool:
    """Keep a live SDK interrupt session isolated from unrelated model turns."""

    current = getattr(agent, "_strands_agent", None)
    session_owners = sorted(
        session_task_id
        for session_task_id, session in dict(
            getattr(agent, "_interrupted_turn_sessions", {}) or {}
        ).items()
        if session.strands_agent is current
    )
    replacement_required = bool(
        getattr(agent, "_strands_agent_replacement_required", False) or session_owners
    )
    if not replacement_required:
        return False

    previous_id = id(current) if current is not None else None
    agent._create_strands_agent()
    replacement = getattr(agent, "_strands_agent", None)
    if replacement is None or replacement is current:
        raise RuntimeError(
            "Could not isolate a live Strands interrupt from a new task turn."
        )
    agent._strands_agent_replacement_required = False
    agent._logger.warning(
        "[%s] Rotated the default Strands agent before a new turn because the "
        "prior instance belongs to an interrupted session. "
        "event=strands_agent_rotated_after_interrupt task_id=%s "
        "interrupted_task_ids=%s previous_strands_agent_id=%s "
        "replacement_strands_agent_id=%s",
        agent.id,
        task_id or "-",
        session_owners,
        previous_id,
        id(replacement),
    )
    return True


async def discard_task_steering_state(
    agent: Any,
    *,
    task_id: str,
    reason: str,
) -> bool:
    task_manager = getattr(agent, "task_manager", None)
    if task_manager is not None:
        await task_manager.operations_runtime.run(
            partial(clear_human_interaction_blocker, task_manager), task_id
        )
    return _discard_task_steering_memory(agent, task_id=task_id, reason=reason)


def _discard_task_steering_memory(
    agent: Any,
    *,
    task_id: str,
    reason: str,
) -> bool:
    """Discard all in-memory steering state when a task becomes terminal."""

    pending_response = getattr(agent, "_pending_interrupt_responses", {}).pop(
        task_id, None
    )
    session = getattr(agent, "_interrupted_turn_sessions", {}).pop(task_id, None)
    proactive = getattr(agent, "_pending_proactive_task_steering", {}).pop(
        task_id, None
    )
    getattr(agent, "_consumed_task_steering_ids", {}).pop(task_id, None)
    if session is not None and session.strands_agent is getattr(
        agent, "_strands_agent", None
    ):
        agent._strands_agent_replacement_required = True
    discarded = bool(session is not None or pending_response is not None or proactive)
    if discarded:
        agent._logger.info(
            "[%s] Discarded task steering state for terminal task. "
            "event=task_steering_state_discarded task_id=%s reason=%s "
            "had_interrupt_session=%s had_pending_interrupt_response=%s "
            "proactive_guidance_count=%s",
            agent.id,
            task_id,
            reason,
            session is not None,
            pending_response is not None,
            len(proactive or ()),
        )
    return discarded


def _mark_default_strands_agent_resumable(
    agent: Any,
    *,
    resumed_strands_agent: Any,
) -> None:
    if agent._strands_agent is not resumed_strands_agent:
        return
    if any(
        candidate.strands_agent is resumed_strands_agent
        for candidate in agent._interrupted_turn_sessions.values()
    ):
        return
    agent._strands_agent_replacement_required = False


def _register_terminal_task_steering_cleanup(agent: Any) -> None:
    callback = getattr(agent, "_task_steering_terminal_callback", None)
    if callback is None:

        def callback(task_id: str, state: ProgressState, _timestamp: str) -> None:
            # This callback is inside the synchronous task transaction owner.
            # Persist the blocker there, then hand SDK session state to its loop.
            clear_human_interaction_blocker(agent.task_manager, task_id)
            cleanup = partial(
                _discard_task_steering_memory,
                agent,
                task_id=task_id,
                reason=f"terminal_{state.value}",
            )
            owner_loop = agent.task_manager.operations_runtime.loop
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if (
                owner_loop is not None
                and owner_loop.is_running()
                and current_loop is not owner_loop
            ):
                owner_loop.call_soon_threadsafe(cleanup)
            else:
                cleanup()

        agent._task_steering_terminal_callback = callback
    agent.task_manager.add_task_terminal_callback(callback)


def mid_turn_steering_config(agent: Any) -> Dict[str, Any]:
    configured = getattr(agent, "mid_turn_steering", None)
    return dict(configured or {})


def mid_turn_steering_enabled(agent: Any) -> bool:
    return bool(mid_turn_steering_config(agent).get("enabled", True))


def _ttl_timestamp(seconds: float | int | None) -> Optional[str]:
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=value)).isoformat()


def _steering_response(
    incoming: TaskSteering,
    *,
    status: TaskSteeringStatus,
    reason: str,
    status_code: TaskSteeringStatusCode | None = None,
) -> TaskSteering:
    return TaskSteering(
        steering_id=incoming.steering_id,
        steering_type=TaskSteeringType.STATUS,
        target_task_id=incoming.target_task_id,
        target_correlation_id=incoming.target_correlation_id,
        conversation_id=incoming.conversation_id,
        target_agent_id=incoming.target_agent_id,
        target_named_agent_type=incoming.target_named_agent_type,
        status=status,
        status_code=status_code,
        status_reason=reason,
        idempotency_key=incoming.idempotency_key,
    )


def _record_interrupted_turn_resumed(
    task_manager: Any,
    *,
    task_id: str,
    steering_id: str,
) -> None:
    """Persist resume completion whether terminal dispatch archived the task or not.

    Runs in the task operations lane, like every other task-state write.
    """

    history_writer = (
        task_manager.append_history_entry
        if task_manager.get_task_info(task_id) is not None
        else task_manager.append_history_entry_to_archived_task
    )
    history_writer(
        task_id,
        "Strands interrupted turn resumed from TaskSteering response.",
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": "task_steering_interrupt_resumed",
            "steering_id": steering_id,
        },
    )


async def _find_target_task_info(agent: Any, steering: TaskSteering) -> Any:
    if steering.target_task_id:
        task_info = await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, steering.target_task_id
        )
        # An explicit subtask id is authoritative. Siblings share correlation
        # ids, so falling back would apply guidance to the wrong task.
        return task_info
    return await agent.task_manager.operations_runtime.run(
        agent.task_manager.find_task_info_by_correlation_id,
        steering.target_correlation_id,
    )


async def _send_steering_status_response(
    agent: Any,
    incoming: TaskSteering,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta,
    *,
    status: TaskSteeringStatus,
    reason: str,
    status_code: TaskSteeringStatusCode | None = None,
) -> None:
    if not getattr(incoming_meta, "is_response_requested", False):
        return
    routing_key = await resolve_requester_routing_key(
        incoming_header=incoming_header,
        incoming_meta=incoming_meta,
        agent_id_by_name=agent._agent_id_by_name,
        named_agent_type_queue_map=agent.named_agent_type_queue_map,
        team_id=agent.team_id,
        logger=agent._logger,
        agent_id=agent.id,
        repair_recorder=getattr(agent, "_record_runtime_repair_anomaly", None),
    )
    if not routing_key:
        return
    response = _steering_response(
        incoming,
        status=status,
        reason=reason,
        status_code=status_code,
    )
    envelope = MessageEnvelope(
        msg_content=response,
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=routing_key,
            response_routing_key=None,
            agent_signature="",
            idempotency_key=response.idempotency_key,
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            msg_correlation_id=getattr(incoming_meta, "msg_correlation_id", None),
            conversation_id=incoming.conversation_id
            or getattr(incoming_meta, "conversation_id", None),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
        ),
    )
    await agent.team_comms.send_message(message=envelope, routing_key=routing_key)


async def _record_guidance_history(
    agent: Any, task_id: str, steering: TaskSteering, *, applied: bool = False
) -> bool:
    text = steering.guidance_text or ""
    summary = (
        "Mid-turn proactive steering applied: "
        if applied
        else "Mid-turn proactive steering accepted: "
    )
    entry = await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        summary + text,
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": GUIDANCE_APPLIED_EVENT
            if applied
            else GUIDANCE_ACCEPTED_EVENT,
            "steering_id": steering.steering_id,
            "conversation_id": steering.conversation_id,
            "dedupe_key": steering.idempotency_key or steering.steering_id,
            **({"steering": steering.model_dump(mode="json")} if not applied else {}),
        },
    )
    return entry is not None


async def _forward_proactive_guidance_to_pending_subtasks(
    agent: Any,
    task_id: str,
    steering: TaskSteering,
    msg_meta: MessageMeta,
) -> int:
    get_pending_subtasks = getattr(agent.task_manager, "get_pending_subtasks", None)
    send_operational_message = getattr(agent, "_send_operational_message", None)
    if not callable(get_pending_subtasks) or not callable(send_operational_message):
        return 0

    forwarded_count = 0
    for subtask_info in (
        await agent.task_manager.operations_runtime.run(get_pending_subtasks, task_id)
        or []
    ):
        subtask = getattr(subtask_info, "task", None)
        subtask_meta = getattr(subtask_info, "meta", None)
        if subtask is None or not getattr(subtask, "task_id", None):
            continue
        target_named_agent_type = getattr(subtask_meta, "to_named_agent_type", None)
        target_agent_name = getattr(subtask_meta, "to_agent_name", None)
        if not target_named_agent_type and not target_agent_name:
            continue

        forwarded = TaskSteering(
            steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
            target_task_id=subtask.task_id,
            target_correlation_id=getattr(subtask_meta, "msg_correlation_id", None)
            or steering.target_correlation_id,
            conversation_id=steering.conversation_id
            or getattr(msg_meta, "conversation_id", None),
            target_named_agent_type=target_named_agent_type,
            guidance_text=steering.guidance_text,
            attachments=[
                attachment.model_copy(deep=True)
                for attachment in list(steering.attachments or [])
            ],
            replied_message=(
                steering.replied_message.model_copy(deep=True)
                if steering.replied_message is not None
                else None
            ),
            status=TaskSteeringStatus.PENDING,
            expires_at=steering.expires_at,
            idempotency_key=f"{steering.idempotency_key or steering.steering_id}:{subtask.task_id}",
        )
        envelope = MessageEnvelope(
            msg_content=forwarded,
            msg_header=MessageHeader(
                from_agent_id=agent.id,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                response_routing_key=None,
                agent_signature="",
                idempotency_key=forwarded.idempotency_key or forwarded.steering_id,
            ),
            msg_meta=MessageMeta(
                from_agent_name=agent.name,
                to_agent_name=target_agent_name,
                to_named_agent_type=target_named_agent_type,
                msg_correlation_id=new_id("corr_id"),
                conversation_id=forwarded.conversation_id,
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
                request_timezone=getattr(msg_meta, "request_timezone", None),
            ),
        )
        await send_operational_message(envelope)
        forwarded_count += 1

    if forwarded_count:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            f"Forwarded mid-turn proactive steering to {forwarded_count} active delegated subtask(s).",
            entry_type=TaskHistoryType.SUBTASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "event": "task_steering_forwarded_to_subtasks",
                "steering_id": steering.steering_id,
                "forwarded_count": forwarded_count,
            },
        )
    return forwarded_count


WORKER_INTERRUPT_REFUSED = (
    "Agents do not pass a worker's input requests on. A worker waits for access "
    "decisions in its own process, so this request cannot be answered here."
)


async def handle_task_steering_message(
    agent: Any,
    steering: TaskSteering,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    """Apply steering to one of this agent's tasks, or answer why it cannot."""

    if steering.steering_type == TaskSteeringType.INTERRUPT_REQUEST:
        # Only access requests pause a turn, and they wait in the worker's own
        # process for a person's decision (send_interrupt_request_for_result
        # never sends them). Anything else a worker sends up is refused, so the
        # worker fails its task with this reason instead of waiting forever.
        agent._logger.warning(
            "Refused a worker's input request; agents do not pass them on. "
            "event=worker_interrupt_refused task_id=%s sender=%s",
            steering.target_task_id,
            msg_header.from_agent_id,
        )
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.REJECTED,
            reason=WORKER_INTERRUPT_REFUSED,
        )
        return
    if not mid_turn_steering_enabled(agent) and steering.steering_type not in {
        TaskSteeringType.INTERRUPT_RESPONSE,
        TaskSteeringType.STATUS,
    }:
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.REJECTED,
            reason="Mid-turn steering is disabled for this agent.",
        )
        return

    task_info = await _find_target_task_info(agent, steering)
    if task_info is None:
        if steering.steering_type == TaskSteeringType.STATUS:
            return
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.STALE,
            reason="Target task was not found.",
            status_code=TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND,
        )
        return

    task_id = task_info.task.task_id
    if steering.steering_type == TaskSteeringType.STATUS:
        session = agent._interrupted_turn_sessions.get(task_id)
        if session is None or (
            session.steering_id != steering.steering_id
            or msg_header.from_agent_id != session.msg_header.from_agent_id
        ):
            return
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            "Requester interruption delivery status: "
            + str(steering.status_reason or steering.status.value),
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "event": "task_steering_delivery_status",
                "steering_id": steering.steering_id,
                "status": steering.status.value,
                "sender": msg_header.from_agent_id,
            },
        )
        if steering.status in {TaskSteeringStatus.REJECTED, TaskSteeringStatus.STALE}:
            agent._logger.error(
                "event=task_steering_delivery_failed task_id=%s sender=%s reason=%s",
                task_id,
                msg_header.from_agent_id,
                steering.status_reason,
            )
            error = "Requester could not handle the worker interruption: " + str(
                steering.status_reason or steering.status.value
            )
            # Keep the task and interrupt session available until the parent
            # owns the failure response. If publication fails, JetStream must
            # be able to redeliver this status and retry the same handoff.
            await agent._send_task_error_response(
                task_info.task, task_info.header, task_info.meta, error
            )
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.fail_task,
                task_id,
                error,
                datetime.now(timezone.utc).isoformat(),
            )
            await discard_task_steering_state(
                agent, task_id=task_id, reason="interrupt_delivery_failed"
            )
        return
    if await agent.task_manager.operations_runtime.run(
        agent.task_manager.is_terminal_state, task_info.state
    ):
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.STALE,
            reason="Target task is already terminal.",
            status_code=TaskSteeringStatusCode.TARGET_TASK_TERMINAL,
        )
        return

    seen = agent._processed_task_steering_ids
    dedupe_key = steering.idempotency_key or steering.steering_id
    _saved_guidance, saved_keys = _saved_proactive_guidance(task_info)
    if dedupe_key in seen or dedupe_key in saved_keys:
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.ACCEPTED,
            reason="Duplicate steering event ignored.",
        )
        return
    if steering.steering_type == TaskSteeringType.PROACTIVE_GUIDANCE:
        if not (steering.guidance_text or "").strip():
            await _send_steering_status_response(
                agent,
                steering,
                msg_header,
                msg_meta,
                status=TaskSteeringStatus.REJECTED,
                reason="Proactive guidance must include guidance_text.",
            )
            return
        # Acknowledge only after the complete guidance is in the task's durable
        # history. Restart reconstructs pending guidance and deduplication there.
        if not await _record_guidance_history(agent, task_id, steering, applied=False):
            await _send_steering_status_response(
                agent, steering, msg_header, msg_meta,
                status=TaskSteeringStatus.STALE,
                reason="This agent no longer holds the target task.",
                status_code=TaskSteeringStatusCode.TARGET_TASK_NOT_FOUND,
            )
            return
        seen.add(dedupe_key)
        agent._pending_proactive_task_steering.setdefault(task_id, []).append(steering)
        forwarded_count = await _forward_proactive_guidance_to_pending_subtasks(
            agent,
            task_id,
            steering,
            msg_meta,
        )
        reason = (
            f"Proactive steering accepted and forwarded to {forwarded_count} active delegated subtask(s)."
            if forwarded_count
            else "Proactive steering accepted and will apply at the next safe checkpoint."
        )
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.ACCEPTED,
            reason=reason,
        )
        return

    if steering.steering_type == TaskSteeringType.INTERRUPT_RESPONSE:
        seen.add(dedupe_key)
        response_text = _format_interrupt_responses(steering)
        session = agent._interrupted_turn_sessions.get(task_id)
        if session is not None:
            agent._pending_interrupt_responses[task_id] = steering
            await _send_steering_status_response(
                agent,
                steering,
                msg_header,
                msg_meta,
                status=TaskSteeringStatus.ACCEPTED,
                reason="Interrupt response accepted for live Strands resume.",
            )
            # The Core NATS subscription must remain free to answer status and
            # stop requests while the resumed SDK turn runs.
            schedule_pending_work(agent)
            return

        if not bool(
            mid_turn_steering_config(agent).get("allow_restart_fallback", True)
        ):
            await _send_steering_status_response(
                agent,
                steering,
                msg_header,
                msg_meta,
                status=TaskSteeringStatus.STALE,
                reason="Live Strands interrupt session is no longer available.",
            )
            return

        # Distinct replies can contain the same paragraphs. Keep the event
        # identity in the note so text deduplication cannot erase a correction.
        directive = (
            "Runtime mid-turn interrupt response received "
            f"for steering event {dedupe_key} after the live Strands session was unavailable. "
            "Continue the original task using this requester input as authoritative steering:\n"
            f"{response_text}"
        )
        await agent.task_manager.operations_runtime.run(
            partial(clear_answered_access_blocker, agent.task_manager),
            task_id,
            _answered_access_ids(steering),
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_runtime_resume_directive, task_id, directive
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            "Mid-turn interrupt response recorded for restart-safe recovery.",
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "event": "task_steering_interrupt_response_recovery",
                "steering_id": steering.steering_id,
            },
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.update_main_task_state,
            task_id,
            ProgressState.ACCEPTED,
            datetime.now(timezone.utc).isoformat(),
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.enqueue_task, task_id, front=True
        )
        await _send_steering_status_response(
            agent,
            steering,
            msg_header,
            msg_meta,
            status=TaskSteeringStatus.ACCEPTED,
            reason="Interrupt response accepted as durable recovery guidance.",
        )
        schedule_pending_work(agent)
        return

    await _send_steering_status_response(
        agent,
        steering,
        msg_header,
        msg_meta,
        status=TaskSteeringStatus.REJECTED,
        reason=f"Unsupported steering type: {steering.steering_type}",
    )


def _format_interrupt_responses(steering: TaskSteering) -> str:
    if steering.guidance_text:
        text = steering.guidance_text.strip()
    else:
        fragments = []
        for item in steering.interrupt_responses:
            interrupt_id = item.get("interrupt_id") or item.get("interruptId")
            fragments.append(f"- {interrupt_id}: {item.get('response')}")
        text = "\n".join(fragments) if fragments else "(empty response)"
    return _interrupt_reply_text(text, steering)


def _interrupt_reply_text(text: str, steering: TaskSteering) -> str:
    """Keep the same background label in SDK resume and durable recovery notes."""
    if steering.replied_message is None:
        return text
    return "\n\n".join([
        text,
        "\n".join(render_replied_message_lines(steering.replied_message, subject="answer")),
    ])


def _build_interrupt_response_blocks(
    session: StrandsInterruptedTurnSession,
    steering: TaskSteering,
) -> List[Dict[str, Any]]:
    response_records = list(steering.interrupt_responses or [])
    if not response_records and steering.guidance_text:
        if session.interrupts:
            response_records = [
                {
                    "interrupt_id": session.interrupts[0].interrupt_id,
                    "response": steering.guidance_text,
                }
            ]
        else:
            response_records = [{"response": steering.guidance_text}]

    blocks: List[Dict[str, Any]] = []
    for index, record in enumerate(response_records):
        interrupt_id = record.get("interrupt_id") or record.get("interruptId")
        if not interrupt_id and index < len(session.interrupts):
            interrupt_id = session.interrupts[index].interrupt_id
        response = record.get("response", steering.guidance_text or "")
        if isinstance(response, str):
            response = _interrupt_reply_text(response, steering)
        payload: Dict[str, Any] = {"response": response}
        if interrupt_id:
            payload["interruptId"] = interrupt_id
        blocks.append({"interruptResponse": payload})
    return blocks


def _answered_access_ids(
    steering: TaskSteering,
) -> list[tuple[str, str]]:
    """The (interrupt id, access request id) pairs a structured access outcome answers."""
    answers = []
    for item in steering.interrupt_responses or []:
        response = item.get("response")
        if isinstance(response, dict):
            interrupt_id = item.get("interrupt_id") or item.get("interruptId")
            request_id = response.get(ACCESS_REQUEST_ID_KEY)
            if interrupt_id and request_id:
                answers.append((str(interrupt_id), str(request_id)))
    return answers


async def resume_interrupted_task_turn(agent: Any, task_id: str) -> Optional[Any]:
    """Resume an in-memory Strands interrupt and apply the resulting task output."""

    session = agent._interrupted_turn_sessions.get(task_id)
    steering = agent._pending_interrupt_responses.pop(task_id, None)
    if session is None or steering is None:
        return None

    task_info = await agent.task_manager.operations_runtime.run(
        agent.task_manager.get_task_info, task_id
    )
    if task_info is None:
        agent._interrupted_turn_sessions.pop(task_id, None)
        await agent.task_manager.operations_runtime.run(
            partial(clear_human_interaction_blocker, agent.task_manager), task_id
        )
        return None

    response_blocks = _build_interrupt_response_blocks(session, steering)
    if not response_blocks:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            task_id,
            "Interrupt response could not be converted into Strands interruptResponse blocks.",
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "event": "task_steering_interrupt_response_empty",
                "steering_id": steering.steering_id,
            },
        )
        return None

    agent._strands_agent = session.strands_agent
    # Clear the old durable wait before output projection or another interrupt.
    # The access-request handler already checked the stored decision; every
    # tool call is still decided again when the SDK resumes.
    await agent.task_manager.operations_runtime.run(
        partial(clear_answered_access_blocker, agent.task_manager),
        task_id,
        _answered_access_ids(steering),
    )
    # A resumed turn is a new turn: it gets its own stop for its commands.
    tool_stop = agent._set_active_execution_context(task_id)
    try:
        try:
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.update_main_task_state,
                task_id,
                ProgressState.IN_PROGRESS,
                datetime.now(timezone.utc).isoformat(),
            )
            return await _run_resumed_turn(agent, session, steering, task_info, response_blocks, tool_stop)
        except Exception as exc:
            # The task holds the execution slot (IN_PROGRESS above). Unhandled, it
            # kept it and no queued task ever started again (turn_failure_runtime).
            agent._interrupted_turn_sessions.pop(task_id, None)
            agent._logger.warning(
                "A resumed turn failed; ending its task. event=resumed_turn_failed "
                "task_id=%s steering_id=%s error_type=%s",
                task_id,
                steering.steering_id,
                type(exc).__name__,
            )
            await settle_failed_turn(agent, task_id, task_info, _as_provider_timeout(exc))
            return None
    except asyncio.CancelledError:
        # Like a fresh turn (sdk.strands.turn): end the turn's tool work
        # wherever the stop landed, including while settling a failure.
        await stop_tool_calls(tool_stop, agent._logger, reason=TURN_CANCELLED_REASON)
        raise
    finally:
        agent._clear_active_execution_context()
        await agent._refresh_self_agent_state()


def _as_provider_timeout(error: Exception) -> Exception:
    """A resumed turn skips the turn runtime's recovery, so name a timeout the same way."""

    if isinstance(error, ProviderTimeoutError) or not is_provider_timeout_error(error):
        return error
    timeout = ProviderTimeoutError()
    timeout.__cause__ = error
    return timeout


async def _run_resumed_turn(
    agent: Any,
    session: StrandsInterruptedTurnSession,
    steering: TaskSteering,
    task_info: Any,
    response_blocks: list[dict[str, Any]],
    tool_stop: ToolStop,
) -> Optional[Any]:
    """Answer the SDK interrupt, then apply the turn's decision to the task."""

    task_id = session.task_id
    result = await invoke_with_tool_stop(
        session.strands_agent,
        response_blocks,
        tool_stop=tool_stop,
        invoke_kwargs=session.invoke_kwargs,
        logger=agent._logger,
    )
    result = agent.turn_executor._apply_agent_result_transformer(
        session.msg_content, result
    )
    if getattr(result, "stop_reason", None) == "interrupt":
        return await send_interrupt_request_for_result(
            agent,
            result=result,
            task=session.msg_content,
            msg_header=session.msg_header,
            msg_meta=session.msg_meta,
            strands_agent=session.strands_agent,
            invoke_kwargs=session.invoke_kwargs,
            full_prompt=session.full_prompt,
            start_notes=session.start_notes,
        )

    outcome = agent.turn_executor.decode_result(
        result=result,
        msg_content=session.msg_content,
        msg_meta=session.msg_meta,
    )

    await agent.turn_executor.prepare_turn_decision(
        msg_content=session.msg_content, msg_header=session.msg_header, msg_meta=session.msg_meta,
        task_id=task_id, decision=outcome.decision, strands_agent=session.strands_agent,
        decision_tool_name=getattr(session.invoke_kwargs.get("structured_output_model"), "__name__", ""),
    )

    content = await agent._execute_turn_decision_outbox(
        decision=outcome.decision,
        incoming_content=session.msg_content,
        incoming_header=session.msg_header,
        incoming_meta=session.msg_meta,
        structured_output_details=outcome.structured_output_details,
        start_notes=session.start_notes,
    )
    await agent._apply_task_processing_result(task_id, task_info, content)
    agent._interrupted_turn_sessions.pop(task_id, None)
    _mark_default_strands_agent_resumable(
        agent,
        resumed_strands_agent=session.strands_agent,
    )
    await agent.task_manager.operations_runtime.run(
        partial(_record_interrupted_turn_resumed, agent.task_manager),
        task_id=task_id,
        steering_id=steering.steering_id,
    )
    return content


# Waits trusted code ends in this process (people decide in the Inbox or on
# setup pages); they are never sent to the requester as questions.
_LOCAL_WAIT_KINDS = frozenset({ACCESS_REQUEST_INTERRUPT_KIND, TOOL_SETUP_RUN_KIND})


def _is_local_wait(record: TaskSteeringInterruptRecord) -> bool:
    return isinstance(record.reason, dict) and record.reason.get("kind") in _LOCAL_WAIT_KINDS


def _is_access_request(record: TaskSteeringInterruptRecord) -> bool:
    return isinstance(record.reason, dict) and record.reason.get("kind") == ACCESS_REQUEST_INTERRUPT_KIND


def _is_setup_run(record: TaskSteeringInterruptRecord) -> bool:
    return isinstance(record.reason, dict) and record.reason.get("kind") == TOOL_SETUP_RUN_KIND


async def send_interrupt_request_for_result(
    agent: Any,
    *,
    result: Any,
    task: Task,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    strands_agent: Any,
    invoke_kwargs: Dict[str, Any],
    full_prompt: str,
    start_notes: Optional[TurnStartNotes] = None,
) -> TaskSteering:
    config = mid_turn_steering_config(agent)
    expires_at = _ttl_timestamp(
        config.get("interrupt_response_timeout_seconds", 86400.0)
    )
    interrupt_records = [
        TaskSteeringInterruptRecord(
            interrupt_id=getattr(interrupt, "id", ""),
            name=getattr(interrupt, "name", None),
            reason=getattr(interrupt, "reason", None),
            source="strands",
            expires_at=expires_at,
        )
        for interrupt in list(getattr(result, "interrupts", None) or [])
    ]
    # Access requests wait here for a decision people make in the Inbox; only
    # other interrupts ask the requester (the parent agent or the chat).
    access_interrupts = [record for record in interrupt_records if _is_access_request(record)]
    setup_runs = [record for record in interrupt_records if _is_setup_run(record)]
    upstream_interrupts = [record for record in interrupt_records if not _is_local_wait(record)]
    routing_key = None
    if upstream_interrupts:
        routing_key = await resolve_requester_routing_key(
            incoming_header=msg_header,
            incoming_meta=msg_meta,
            agent_id_by_name=agent._agent_id_by_name,
            named_agent_type_queue_map=agent.named_agent_type_queue_map,
            team_id=agent.team_id,
            logger=agent._logger,
            agent_id=agent.id,
            repair_recorder=getattr(agent, "_record_runtime_repair_anomaly", None),
        )
        # Scheduler supervision is self-authored. Sending its interrupt back to the
        # same agent is acknowledged and discarded by the control receiver, not a
        # human review. Reject before persisting a blocker or reserving SDK state.
        if not routing_key or routing_key in {agent.id, RoutingType.SELF_ROUTE.value}:
            agent._strands_agent_replacement_required = True
            raise UnroutableTaskInterruptError(
                "This task needs input but has no external requester reply route. "
                "No input request was delivered to Inbox. Start the repair from an "
                "interactive conversation so any required review can reach you."
            )
    steering = TaskSteering(
        steering_type=TaskSteeringType.INTERRUPT_REQUEST,
        target_task_id=task.task_id,
        target_correlation_id=getattr(msg_meta, "msg_correlation_id", None),
        conversation_id=getattr(msg_meta, "conversation_id", None),
        target_agent_id=agent.id,
        target_named_agent_type=agent.named_agent_type,
        interrupts=upstream_interrupts or interrupt_records,
        status=TaskSteeringStatus.PENDING,
        expires_at=expires_at,
    )
    agent._interrupted_turn_sessions[task.task_id] = StrandsInterruptedTurnSession(
        task_id=task.task_id,
        strands_agent=strands_agent,
        invoke_kwargs=dict(invoke_kwargs),
        full_prompt=full_prompt,
        msg_content=task,
        msg_header=msg_header.model_copy(deep=True),
        msg_meta=msg_meta.model_copy(deep=True),
        created_at=datetime.now(timezone.utc).isoformat(),
        interrupts=interrupt_records,
        steering_id=steering.steering_id,
        start_notes=start_notes,
    )
    if access_interrupts:
        access_interrupt = access_interrupts[0]
        assert isinstance(access_interrupt.reason, dict)
        waited_ids = access_interrupt_request_ids(access_interrupt.reason)
        await agent.task_manager.operations_runtime.run(
            partial(record_human_interaction_blocker, agent.task_manager),
            task.task_id,
            {
                "kind": ACCESS_REQUEST_BLOCKER,
                ACCESS_REQUEST_ID_KEY: waited_ids[0] if waited_ids else "",
                # One pause for a workflow's batch waits on every listed request.
                ACCESS_REQUEST_IDS_KEY: waited_ids,
                "interrupt_id": access_interrupt.interrupt_id,
                "summary": str(access_interrupt.reason.get("summary") or ""),
                "correlation_id": getattr(msg_meta, "msg_correlation_id", None),
                "conversation_id": getattr(msg_meta, "conversation_id", None),
            },
        )
    agent._strands_agent_replacement_required = True
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task.task_id,
        "Strands turn interrupted for requester input.",
        entry_type=TaskHistoryType.QUERY_EVENT,
        importance=TaskHistoryImportance.HIGH,
        details={
            "event": "task_steering_interrupt_request",
            "steering_id": steering.steering_id,
            "interrupt_ids": [record.interrupt_id for record in interrupt_records],
        },
    )
    await agent.task_manager.operations_runtime.run(
        agent.task_manager.update_main_task_state,
        task.task_id,
        ProgressState.WAITING,
        datetime.now(timezone.utc).isoformat(),
    )
    for setup_run in setup_runs:
        # set_up_tools paused the turn (now waiting); trusted code opens its pages and resumes it.
        from custom_agents.tool_setup_run_runtime import begin_run

        assert isinstance(setup_run.reason, dict)
        await begin_run(
            agent, task_id=task.task_id, interrupt_id=setup_run.interrupt_id, reason=setup_run.reason, msg_meta=msg_meta
        )
    if routing_key is None:
        return steering

    envelope = MessageEnvelope(
        msg_content=steering,
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=routing_key,
            response_routing_key=agent.id,
            agent_signature="",
            idempotency_key=steering.steering_id,
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            msg_correlation_id=getattr(msg_meta, "msg_correlation_id", None),
            conversation_id=getattr(msg_meta, "conversation_id", None),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=True,
            request_timezone=getattr(msg_meta, "request_timezone", None),
            gateway_context=getattr(msg_meta, "gateway_context", None),
        ),
    )
    await agent.team_comms.send_message(message=envelope, routing_key=routing_key)
    return steering


def _saved_proactive_guidance(
    task_info: Any, batch_exists: Callable[[str], bool] = lambda _batch_id: False,
) -> tuple[dict[str, TaskSteering], set[str]]:
    """Keep guidance until the turn that read it records a durable decision.

    A checkpoint records that a live model turn read the guidance. It cannot
    discard it across restart because that turn's model input is in memory.
    A prepared action batch is the existing durable decision boundary.
    """
    pending: dict[str, TaskSteering] = {}
    seen: set[str] = set()
    read_by_turn: set[str] = set()
    for entry in getattr(getattr(task_info, "task", None), "execution_history", []) or []:
        details = entry.details or {}
        event = details.get("event")
        if event == "turn_action_batch_prepared" and batch_exists(details.get("batch_id", "")):
            for key in read_by_turn:
                pending.pop(key, None)
            read_by_turn.clear()
            continue
        if event not in {GUIDANCE_ACCEPTED_EVENT, GUIDANCE_APPLIED_EVENT}:
            continue
        key = details.get("dedupe_key") or details.get("steering_id")
        if not key:
            continue
        seen.add(key)
        if event == GUIDANCE_APPLIED_EVENT:
            read_by_turn.add(key)
        elif "steering" in details:
            pending[key] = TaskSteering.model_validate(details["steering"])
    return pending, seen


def _pending_guidance_state(manager: Any, task_id: str) -> dict[str, TaskSteering]:
    pending, _seen = _saved_proactive_guidance(
        manager.get_task_info(task_id),
        lambda batch_id: manager.get_turn_action_batch(batch_id) is not None,
    )
    return pending


async def consume_pending_proactive_guidance_message(
    agent: Any, task_id: Optional[str]
) -> str:
    """Return queued proactive guidance as a prompt-ready block and mark it applied."""
    if not task_id:
        return ""
    saved = await agent.task_manager.operations_runtime.run(
        partial(_pending_guidance_state, agent.task_manager), task_id
    )
    if not hasattr(agent, "_consumed_task_steering_ids"):
        agent._consumed_task_steering_ids = {}
    consumed = agent._consumed_task_steering_ids.setdefault(task_id, set())
    guidance_by_key = {
        item.idempotency_key or item.steering_id: item
        for item in agent._pending_proactive_task_steering.get(task_id, [])
    }
    guidance_by_key.update(saved)
    guidance_by_key = {key: item for key, item in guidance_by_key.items() if key not in consumed}
    guidance = list(guidance_by_key.values())
    if not guidance:
        return ""
    text = await _guidance_block(agent, task_id, guidance)
    consumed.update(guidance_by_key)
    remaining = [
        item for item in agent._pending_proactive_task_steering.get(task_id, [])
        if (item.idempotency_key or item.steering_id) not in guidance_by_key
    ]
    if remaining:
        agent._pending_proactive_task_steering[task_id] = remaining
    else:
        agent._pending_proactive_task_steering.pop(task_id, None)
    return text


class RuntimeSteeringPlugin:
    """Apply queued proactive task guidance at Strands safe checkpoints."""

    name = "runtime_steering"

    def __init__(self, agent: Any) -> None:
        self.runtime_agent = agent
        _register_terminal_task_steering_cleanup(agent)

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    async def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        agent = self.runtime_agent
        if not mid_turn_steering_enabled(agent):
            return
        config = mid_turn_steering_config(agent)
        if not bool(config.get("apply_proactive_guidance_before_tools", True)):
            return
        task_id = getattr(agent, "_active_execution_task_id", None)
        text = await consume_pending_proactive_guidance_message(agent, task_id)
        if not text:
            return
        event.cancel_tool = (
            "Tool call cancelled because new requester steering arrived while this task was in flight. "
            "You MUST incorporate this guidance immediately before deciding whether to retry the tool or choose a different path.\n"
            f"{text}"
        )


async def _guidance_block(
    agent: Any, task_id: Optional[str], guidance: List[TaskSteering]
) -> str:
    lines = []
    for item in guidance:
        if task_id:
            if not await _record_guidance_history(agent, task_id, item, applied=True):
                raise RuntimeCustodyLost(agent.id, CustodyStore.TASK_RUNTIME)
        lines.append(f"- {item.guidance_text}")
        if item.replied_message is not None:
            lines.extend(
                f"  {line}"
                for line in render_replied_message_lines(
                    item.replied_message, subject="guidance"
                )
            )
    return "\n".join(lines)
