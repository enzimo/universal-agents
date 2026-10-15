"""Warn task owners about capacity without creating work in the queue being warned about."""

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from custom_agents.instance_delivery_context import configured_owner_route
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.enums import RequesterType
from custom_agents.task_queue_safety import QUEUE_NOTICE_EVENT, pending_safety_notices
from id_generation import new_id

QUEUE_WARNING_PERCENT = 90


@dataclass
class QueuePressureNotifications:
    """Coalesce checks and notify each owner once per observed pressure period."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    notified_owners: set[str] = field(default_factory=set)


def _queue_snapshot(manager: Any) -> tuple[int, int, int, list[Any]]:
    count = manager.get_tracked_top_level_task_count()
    limit = manager.get_max_queued_top_level_tasks()
    waiting = manager.get_waiting_top_level_task_count()
    # Task operations detach the returned records before crossing back to asyncio.
    infos = (
        manager.get_top_level_task_infos()
        if count * 100 >= limit * QUEUE_WARNING_PERCENT
        else []
    )
    return count, limit, waiting, infos


async def check_queue_pressure(agent: Any) -> None:
    """Also called by the scheduler so restored and internally created work is covered."""
    if not getattr(agent, "is_team_lead", False):
        return
    state = agent._queue_pressure_notifications
    async with state.lock:
        try:
            await _notify_safety_changes(agent)
            await _notify_owners(agent, state)
        except Exception:
            # Accepted work must not be retried because an advisory failed to publish.
            agent._logger.exception(
                "Could not publish task queue warning. event=task_queue_warning_failed agent_id=%s",
                agent.id,
            )


async def _notify_owners(agent: Any, state: QueuePressureNotifications) -> None:
    count, limit, waiting, infos = await agent.task_manager.operations_runtime.run(
        partial(_queue_snapshot, agent.task_manager),
    )
    if count * 100 < limit * QUEUE_WARNING_PERCENT:
        state.notified_owners.clear()
        return
    for owner, info in _owner_recipients(infos, agent.id).items():
        if owner in state.notified_owners:
            continue
        route, context = info.header.response_routing_key, info.meta.gateway_context
        if not route or route == agent.id:
            configured = await agent.task_manager.operations_runtime.run(
                configured_owner_route,
                getattr(agent, "instance_config_path", None),
                owner,
            )
            if configured is None:
                continue
            route, context = configured
        await _send_warning(
            agent, route, context, count=count, limit=limit, waiting=waiting
        )
        state.notified_owners.add(owner)


async def _notify_safety_changes(agent: Any) -> None:
    notices = await agent.task_manager.operations_runtime.run(
        partial(pending_safety_notices, agent.task_manager)
    )
    for info, entry in notices:
        context = info.meta.gateway_context
        if context is None or not context.canonical_user_id:
            continue
        route = info.header.response_routing_key
        if not route or route == agent.id:
            recipient = await agent.task_manager.operations_runtime.run(
                configured_owner_route,
                getattr(agent, "instance_config_path", None),
                context.canonical_user_id,
            )
            if recipient is None:
                continue
            route, context = recipient
        notice_id = f"queue_safety_{entry.entry_id}"
        envelope = MessageEnvelope(
            msg_content=UserNotification(
                text=f"Task {info.task.task_id}: {entry.summary} Open Settings → Tasks to review its status or stop it.",
                reason="task_queue_safety",
                source_task_id=info.task.task_id,
                source_agent_id=agent.id,
                source_agent_name=agent.name,
            ),
            msg_meta=MessageMeta(
                from_agent_name=agent.name,
                msg_correlation_id=notice_id,
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
                gateway_context=context.model_copy(
                    update={
                        "scheduled_job_id": None,
                        "schedule_owner_agent_id": None,
                        "heartbeat_source_id": None,
                        "delivery_mode": "autonomous",
                    }
                ),
            ),
            msg_header=MessageHeader(
                from_agent_id=agent.id,
                to_routing_path=route,
                response_routing_key=agent.id,
                idempotency_key=notice_id,
            ),
        )
        await agent.team_comms.send_message(message=envelope, routing_key=route)
        await agent.task_manager.operations_runtime.run(
            agent.task_manager.append_history_entry,
            info.task.task_id,
            "Sent a task queue safety notice to the requester.",
            details={"event": QUEUE_NOTICE_EVENT, "source_entry_id": entry.entry_id},
        )


def _owner_recipients(infos: list[Any], agent_id: str) -> dict[str, Any]:
    """Prefer a retained chat route over self-authored work for the same owner."""
    recipients = {}
    for info in infos:
        context = info.meta.gateway_context
        route = info.header.response_routing_key
        if context is not None and context.canonical_user_id:
            if context.canonical_user_id not in recipients or (
                route and route != agent_id
            ):
                recipients[context.canonical_user_id] = info
    return recipients


async def _send_warning(
    agent: Any,
    route: str,
    context: GatewayContext,
    *,
    count: int,
    limit: int,
    waiting: int,
) -> None:
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            text=(
                f"The agent's task queue is nearly full: {count} of {limit} slots are in use, "
                f"including {waiting} waiting tasks. New messages will be rejected if it fills up. "
                "Open Settings → Tasks to review and remove unwanted tasks or purge waiting and queued work."
            ),
            reason="task_queue_near_capacity",
            source_agent_id=agent.id,
            source_agent_name=agent.name,
        ),
        msg_meta=MessageMeta(
            from_agent_name=agent.name,
            # Never consume the active chat's terminal-response correlation or mark
            # a task's answer delivered when only this advisory was shown.
            msg_correlation_id=new_id("queue_warning"),
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
            gateway_context=context.model_copy(
                update={
                    "scheduled_job_id": None,
                    "schedule_owner_agent_id": None,
                    "heartbeat_source_id": None,
                    "delivery_mode": "autonomous",
                }
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=route,
            response_routing_key=agent.id,
        ),
    )
    await agent.team_comms.send_message(message=envelope, routing_key=route)
    agent._logger.warning(
        "Task queue capacity warning sent. event=task_queue_near_capacity agent_id=%s tracked=%s limit=%s waiting=%s",
        agent.id,
        count,
        limit,
        waiting,
    )
