"""Control-message stop/cancel helpers for UniversalAgent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from custom_agents.message_routing import (
    build_outgoing_meta,
    construct_header,
    resolve_outgoing_routing,
    resolve_requester_routing_key,
)
from custom_agents.sdk.strands.turn_tool_stop import TASK_STOPPED_REASON
from custom_agents.task_manager import TaskInfo
from custom_agents.task_steering_runtime import discard_task_steering_state
from custom_messages.contracts.control_v1 import (
    ControlInstruction,
    SystemStatusMessage,
)
from custom_messages.contracts.envelope_v1 import CommsMessage, MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    TaskHistoryImportance,
    TaskHistoryType,
)
from custom_tools.tool_stop import (
    BACKGROUND_KILL_GRACE_SECONDS,
    BACKGROUND_TERM_GRACE_SECONDS,
    TOOL_STOP_GRACE_SECONDS,
)

# How long a stop waits for the stopped turn to finish unwinding. The turn
# itself first waits up to TOOL_STOP_GRACE_SECONDS for its running commands to
# end, then gives leftover groups BACKGROUND_TERM_GRACE_SECONDS to stop and
# BACKGROUND_KILL_GRACE_SECONDS to exit after a forced kill
# (custom_agents.sdk.strands.turn_tool_stop); the rest covers its cleanup.
ACTIVE_STOP_GRACE_SECONDS = (
    TOOL_STOP_GRACE_SECONDS + BACKGROUND_TERM_GRACE_SECONDS + BACKGROUND_KILL_GRACE_SECONDS + 2.0
)


async def _record_execution_graph_cancellation(
    agent: Any,
    *,
    root_task_id: str,
    task_ids: list[str],
    canonical_user_id: Optional[str],
    command_id: Optional[str],
    reason: str,
    cancelled_at: str,
) -> bool:
    """Attempt the restart barrier without making local operator control brittle."""

    if not getattr(agent, "team_id", None):
        return False
    try:
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.record_execution_graph_cancelled,
            root_task_id=root_task_id,
            task_ids=task_ids,
            canonical_user_id=canonical_user_id,
            command_id=(command_id or f"runtime_stop:{root_task_id}:{cancelled_at}"),
            reason=reason,
            cancelled_at=cancelled_at,
        )
        return True
    except Exception:
        agent._logger.exception(
            "Local cancellation will continue, but its restart barrier could not "
            "be persisted. event=runtime_stop_tombstone_failed "
            "root_task_id=%s task_ids=%s",
            root_task_id,
            task_ids,
        )
        return False


async def resolve_control_target_task_info(
    agent: Any,
    *,
    control_instruction: ControlInstruction,
    msg_meta: MessageMeta,
) -> Optional[TaskInfo]:
    """Resolve the task a control instruction targets on this agent.

    A named target never falls back to another task. Stops for delegated work
    travel on the shared named-type queue, so any worker of that type may
    receive one; falling back to the receiver's current task would stop
    unrelated work. Only an instruction that names no task or correlation (a
    direct "stop" to this agent) applies to its current task.
    """
    explicit_task_id = control_instruction.target_task_id or getattr(
        msg_meta, "followup_to_task_id", None
    )
    if explicit_task_id:
        return await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, explicit_task_id
        )

    explicit_correlation_id = control_instruction.target_correlation_id
    if explicit_correlation_id:
        return await agent.task_manager.operations_runtime.run(
            agent.task_manager.find_task_info_by_correlation_id, explicit_correlation_id
        )

    current_task_id = agent.task_manager.current_task_id
    if current_task_id:
        return await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, current_task_id
        )
    return None


async def send_control_status_response(
    agent: Any,
    *,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    status_text: str,
) -> None:
    if not getattr(msg_meta, "is_response_requested", False):
        return

    response_key = await resolve_requester_routing_key(
        incoming_header=msg_header,
        incoming_meta=msg_meta,
        agent_id_by_name=agent._agent_id_by_name,
        named_agent_type_queue_map=agent.named_agent_type_queue_map,
        team_id=agent.team_id,
        logger=agent._logger,
        agent_id=agent.id,
        repair_recorder=getattr(agent, "_record_runtime_repair_anomaly", None),
    )
    if not response_key:
        agent._logger.warning(
            f"[{agent.id}] Stop control requested a response but no routing key was available."
        )
        return

    response_envelope = MessageEnvelope(
        msg_content=SystemStatusMessage(system_status=status_text),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=response_key,
            agent_signature="",
        ),
        msg_meta=MessageMeta(
            msg_correlation_id=msg_meta.msg_correlation_id,
            conversation_id=getattr(msg_meta, "conversation_id", None),
        ),
    )
    await agent.team_comms.send_message(
        message=response_envelope, routing_key=response_key
    )


async def send_task_cancel_response(
    agent: Any,
    task_request: Task,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
    cancel_reason: str,
) -> None:
    cancelled_at = datetime.now(timezone.utc).isoformat()
    error_list = list(task_request.errors or [])
    if cancel_reason not in error_list:
        error_list.append(cancel_reason)
    task_state = list(task_request.task_state or [])
    if not task_state or task_state[-1][0] != ProgressState.CANCELLED:
        task_state.append((ProgressState.CANCELLED, cancelled_at))
    cancelled_task = Task(
        task_id=task_request.task_id,
        parent_task_id=task_request.parent_task_id,
        task_state=task_state,
        task_type=MessageMetaType.RESPONSE,
        problem_statement=task_request.problem_statement,
        errors=error_list,
        task_plan=task_request.task_plan,
        execution_history=list(task_request.execution_history or []),
        answer=f"Conversation cancelled: {cancel_reason}",
    )
    cancel_meta = await build_outgoing_meta(
        llm_meta=MessageMeta(conversation_id=msg_meta.conversation_id),
        incoming_meta=msg_meta,
        content=cancelled_task,
        from_agent_name=agent.name,
        repair_recorder=getattr(agent, "_record_runtime_repair_anomaly", None),
    )
    header = await construct_header(
        comms_message=CommsMessage(msg_content=cancelled_task, msg_meta=cancel_meta),
        explicit_to_path=None,
        route_response_to_self=False,
        agent_id=agent.id,
        agent_name=agent.name,
        team_id=agent.team_id,
        named_agent_type_queue_map=agent.named_agent_type_queue_map,
        agent_id_by_name=agent._agent_id_by_name,
        logger=agent._logger,
        repair_recorder=getattr(agent, "_record_runtime_repair_anomaly", None),
    )
    routing_key = await resolve_outgoing_routing(
        content=cancelled_task,
        header=header,
        incoming_header=msg_header,
        incoming_meta=msg_meta,
        agent_id_by_name=agent._agent_id_by_name,
        named_agent_type_queue_map=agent.named_agent_type_queue_map,
        team_id=agent.team_id,
        logger=agent._logger,
        agent_id=agent.id,
        repair_recorder=getattr(agent, "_record_runtime_repair_anomaly", None),
    )
    await agent.team_comms.send_message(
        message=MessageEnvelope(
            msg_content=cancelled_task,
            msg_meta=cancel_meta,
            msg_header=header,
        ),
        routing_key=routing_key,
    )


async def request_subtask_stop(
    agent: Any,
    *,
    parent_task_id: str,
    subtask_info: TaskInfo,
    reason: str,
) -> bool:
    """Stop one delegated subtask where it is (``delegated_work_runtime.stop_delegated_task``).

    The stop goes to the worker holding the task, or the task is withdrawn from
    its worker type's queue. Until 2026-10-04 a stop for a task delegated to a
    worker type was published into that type's shared queue, where whichever
    worker took the message was usually not the one holding the task; a parent
    that had already ended (no parent link left) still took that path.
    """

    from custom_agents.delegated_work_runtime import stop_delegated_task

    return await stop_delegated_task(
        agent,
        parent_task_id=parent_task_id,
        subtask_info=subtask_info,
        reason=reason,
    )


async def _execution_graph_for_task(
    agent: Any,
    task_info: TaskInfo,
) -> tuple[str, list[TaskInfo]]:
    requested_task_id = task_info.task.task_id
    root_task_id = (
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.resolve_root_task_id, requested_task_id
        )
        or requested_task_id
    )
    graph = list(
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_execution_graph_task_infos, root_task_id
        )
    )
    return root_task_id, graph or [task_info]


async def _pending_graph_subtasks(agent: Any, graph: list[TaskInfo]) -> list[TaskInfo]:
    return [
        item
        for item in graph
        if await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_parent_task_id, item.task.task_id
        )
        and not await agent.task_manager.operations_runtime.run(
            agent.task_manager.is_terminal_state, item.state
        )
    ]


async def _signal_graph_subtask_stops(
    agent: Any,
    *,
    root_task_id: str,
    pending_subtasks: list[TaskInfo],
    reason: str,
) -> int:
    sent = 0
    for subtask_info in pending_subtasks:
        parent_task_id = (
            await agent.task_manager.operations_runtime.run(
                agent.task_manager.get_parent_task_id, subtask_info.task.task_id
            )
            or root_task_id
        )
        if await request_subtask_stop(
            agent,
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            reason=reason,
        ):
            sent += 1
    return sent


async def _record_graph_stop_history(
    agent: Any,
    *,
    graph: list[TaskInfo],
    reason: str,
    timestamp: str,
    cascade_to_subtasks: bool,
) -> None:
    for item in graph:
        await discard_task_steering_state(
            agent,
            task_id=item.task.task_id,
            reason="stop_control",
        )
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            item.task.task_id,
            reason,
            entry_type=TaskHistoryType.TASK_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=timestamp,
            details={
                "reason": "stop_control",
                "requested_by": agent.id,
                "cascade_to_subtasks": cascade_to_subtasks,
            },
        )


async def _terminalize_cancelled_graph(
    agent: Any,
    *,
    root_task_id: str,
    graph: list[TaskInfo],
    reason: str,
    timestamp: str,
) -> int:
    cancelled = 0
    for item in sorted(
        graph,
        key=lambda candidate: candidate.task.task_id == root_task_id,
    ):
        if await agent.task_manager.operations_runtime.run(
            agent.task_manager.cancel_task,
            item.task.task_id,
            reason,
            timestamp,
            review_objective=False,
        ):
            cancelled += 1
    return cancelled


async def cancel_task_tree_locally(
    agent: Any,
    *,
    task_info: TaskInfo,
    reason: str,
    signal_pending_subtasks: bool,
    canonical_user_id: Optional[str] = None,
    command_id: Optional[str] = None,
    send_response: bool = True,
    durable_cancellation_recorded: Optional[bool] = None,
) -> Dict[str, Any]:
    task_id, cancel_tree = await _execution_graph_for_task(agent, task_info)
    pending_subtasks = await _pending_graph_subtasks(agent, cancel_tree)
    now_ts = datetime.now(timezone.utc).isoformat()
    if durable_cancellation_recorded is None:
        durable_cancellation_recorded = await _record_execution_graph_cancellation(
            agent,
            root_task_id=task_id,
            task_ids=[item.task.task_id for item in cancel_tree],
            canonical_user_id=canonical_user_id,
            command_id=command_id,
            reason=reason,
            cancelled_at=now_ts,
        )
    stop_signals_sent = (
        await _signal_graph_subtask_stops(
            agent,
            root_task_id=task_id,
            pending_subtasks=pending_subtasks,
            reason=reason,
        )
        if signal_pending_subtasks
        else 0
    )

    await _record_graph_stop_history(
        agent,
        graph=cancel_tree,
        reason=reason,
        timestamp=now_ts,
        cascade_to_subtasks=signal_pending_subtasks,
    )

    root_task_info = (
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, task_id
        )
        or task_info
    )
    await agent._finalize_conversation_turn(
        root_task_info,
        task_status=ProgressState.CANCELLED.value,
    )
    cancelled_task_count = await _terminalize_cancelled_graph(
        agent,
        root_task_id=task_id,
        graph=cancel_tree,
        reason=reason,
        timestamp=now_ts,
    )

    if send_response:
        await send_task_cancel_response(
            agent,
            root_task_info.task,
            root_task_info.header,
            root_task_info.meta,
            reason,
        )
    return {
        "task_id": task_id,
        "cancelled_task_count": cancelled_task_count,
        "pending_subtask_count": len(pending_subtasks),
        "stop_signals_sent": stop_signals_sent,
        "unconfirmed_worker_count": len(pending_subtasks),
        "durable_cancellation_recorded": durable_cancellation_recorded,
    }


def consume_handback_interrupt(agent: Any, task_id: str) -> bool:
    """Return True once if this task's turn was interrupted to move the task elsewhere.

    The lease-loss wind-down (custom_agents.lease_loss_wind_down) and the
    release of work another agent took over (custom_agents.custody_loss) record
    the task ids they interrupt on their own state; no other path sets them.
    """
    for owner in ("_lease_loss_wind_down", "_custody_release"):
        interrupting = getattr(getattr(agent, owner, None), "interrupting_task_ids", None)
        if interrupting is not None and task_id in interrupting:
            interrupting.discard(task_id)
            return True
    return False


async def handle_interrupted_turn(
    agent: Any,
    *,
    task_id: str,
    task_info: TaskInfo,
) -> None:
    """Finish a turn whose child execution was cancelled.

    A user or system stop cancels the whole task tree. A lease-loss handback,
    or the release of work another agent took over, also cancels the turn, but
    the task is moving to another worker, so it must not be cancelled or given
    a cancellation record.
    Callers re-raise first when the owning task itself is being cancelled
    (runtime shutdown).
    """
    if consume_handback_interrupt(agent, task_id):
        agent._logger.info(
            "Turn interrupted because its task is moving to another worker. "
            "event=turn_interrupted_for_handback agent_id=%s task_id=%s",
            agent.id,
            task_id,
        )
        return
    cancel_reason = agent._consume_stop_reason(task_id) or "Task cancelled by stop command."
    if (
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, task_id
        )
        is not None
    ):
        await agent._cancel_task_tree_locally(
            task_info=task_info,
            reason=cancel_reason,
            signal_pending_subtasks=False,
        )


async def _interrupt_active_execution(
    agent: Any,
    *,
    root_task_id: str,
    graph_task_ids: set[str],
    reason: str,
) -> Optional[asyncio.Task[Any]]:
    """Stop the running turn if it belongs to the graph; wait, bounded, until it ended.

    The turn's commands are asked to stop before the turn is cancelled, with
    no await in between, so the turn cannot act on a result first and a tool
    call that starts afterwards sees the stop. The cancelled turn waits for its
    running commands before it ends (turn_tool_stop), so when this returns
    they have ended or the wait was logged. ``reason`` is a short code
    (turn_tool_stop) for the logs.
    """

    active_task = agent._active_execution_task
    if (
        active_task is None
        or active_task.done()
        or agent._active_execution_task_id not in graph_task_ids
    ):
        return None

    tool_stop = getattr(agent, "_active_tool_stop", None)
    if tool_stop is not None:
        tool_stop.request(reason)
    else:
        agent._logger.warning(
            "The running turn has no ToolStop, so its commands cannot be stopped with it. "
            "event=active_turn_without_tool_stop agent_id=%s task_id=%s reason=%s",
            agent.id,
            root_task_id,
            reason,
        )
    active_task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.shield(active_task),
            timeout=ACTIVE_STOP_GRACE_SECONDS,
        )
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            # This stop was itself cancelled (shutdown); the turn still ends.
            raise
    except asyncio.TimeoutError:
        agent._logger.warning(
            "The stopped turn did not finish within the grace period; continuing without it. "
            "event=active_turn_stop_timeout agent_id=%s task_id=%s reason=%s grace_seconds=%s "
            "running_tool_calls=%s",
            agent.id,
            root_task_id,
            reason,
            ACTIVE_STOP_GRACE_SECONDS,
            len(tool_stop.running()) if tool_stop is not None else "unknown",
        )
    return active_task


async def stop_task_execution(
    agent: Any,
    *,
    task_info: TaskInfo,
    reason: str,
    cascade_to_subtasks: bool,
    canonical_user_id: Optional[str] = None,
    command_id: Optional[str] = None,
    send_response: bool = True,
) -> Dict[str, Any]:
    requested_task_id = task_info.task.task_id
    task_id = (
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.resolve_root_task_id, requested_task_id
        )
        or requested_task_id
    )
    graph_task_ids = {
        item.task.task_id
        for item in await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_execution_graph_task_infos, task_id
        )
    }
    graph_task_ids.add(task_id)
    cancelled_at = datetime.now(timezone.utc).isoformat()
    durable_cancellation_recorded = await _record_execution_graph_cancellation(
        agent,
        root_task_id=task_id,
        task_ids=sorted(graph_task_ids),
        canonical_user_id=canonical_user_id,
        command_id=command_id,
        reason=reason,
        cancelled_at=cancelled_at,
    )
    for graph_task_id in graph_task_ids:
        agent._stop_reasons_by_task_id[graph_task_id] = reason
    active_task = await _interrupt_active_execution(
        agent,
        root_task_id=task_id,
        graph_task_ids=graph_task_ids,
        reason=TASK_STOPPED_REASON,
    )
    if active_task is not None:
        current_task_info = await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, task_id
        ) or await agent.task_manager.operations_runtime.run(
            agent.task_manager.get_task_info, agent._active_execution_task_id
        )
        if current_task_info is None:
            return {
                "task_id": task_id,
                "cancelled_task_count": 1,
                "pending_subtask_count": 0,
                "stop_signals_sent": 0,
                "active_execution_cancelled": True,
                "durable_cancellation_recorded": (durable_cancellation_recorded),
            }

        result = await cancel_task_tree_locally(
            agent,
            task_info=current_task_info,
            reason=reason,
            signal_pending_subtasks=cascade_to_subtasks,
            canonical_user_id=canonical_user_id,
            command_id=command_id,
            send_response=send_response,
            durable_cancellation_recorded=durable_cancellation_recorded,
        )
        result["active_execution_cancelled"] = (
            active_task.cancelled() or not active_task.done()
        )
        return result
    result = await cancel_task_tree_locally(
        agent,
        task_info=task_info,
        reason=reason,
        signal_pending_subtasks=cascade_to_subtasks,
        canonical_user_id=canonical_user_id,
        command_id=command_id,
        send_response=send_response,
        durable_cancellation_recorded=durable_cancellation_recorded,
    )
    result["active_execution_cancelled"] = False
    return result
