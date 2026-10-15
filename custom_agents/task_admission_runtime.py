"""Report admission failures without waiting for the agent's model execution slot."""

from functools import partial
from typing import Any

from custom_agents.deferred_message_runtime import DeferredInboxFull
from custom_messages.contracts.control_v1 import (
    AgentStatus,
    ControlInstruction,
)
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from custom_messages.contracts.task_v1 import (
    Task,
    TaskProgressStatus,
    TaskStatus,
    TaskSteering,
)
from custom_messages.enums import MessageMetaType, ProgressState


QUEUE_RECOVERY_MESSAGE = (
    "Open Settings → Tasks to review and remove unwanted tasks or purge waiting and "
    "queued work. Then send your message again."
)


def _admission_status(manager: Any, task_id: str) -> tuple[bool, bool, int]:
    """Read capacity and prior custody on the serialized task operations lane."""
    known = (
        manager.get_task_info(task_id) is not None
        or manager.get_task_from_db(
            task_id,
            owner_agent_id=manager.identity.owner_agent_id,
        )
        is not None
    )
    return known, manager.is_buffer_full(), manager.get_max_queued_top_level_tasks()


async def report_failed_admission(
    agent: Any,
    task: Task,
    header: Any,
    meta: Any,
    *,
    conversation_admitted: bool = False,
) -> None:
    known, full, limit = await agent.task_manager.operations_runtime.run(
        partial(_admission_status, agent.task_manager),
        task.task_id,
    )
    if known:
        return  # A competing delivery already admitted or completed this task.
    message = (
        f"Your message was not queued because the agent's task queue is full ({limit} tasks). "
        if full
        else "Your message could not be queued. "
    )
    agent._logger.warning(
        "Task admission rejected. event=task_admission_rejected task_id=%s capacity_full=%s limit=%s",
        task.task_id,
        full,
        limit,
    )
    message += QUEUE_RECOVERY_MESSAGE
    if conversation_admitted and agent._conversation_store is not None:
        # Context validation created this turn before the final admission check.
        # Do not leave its history claiming acceptance after a capacity race.
        await agent.task_manager.operations_runtime.run(
            agent._conversation_store.update_turn_completion,
            task_id=task.task_id,
            task_status=ProgressState.FAILED.value,
            final_answer_text=message,
            turn_summary_text=message,
        )
    await agent._send_task_error_response(task, header, meta, message)


async def reject_full_queue_request(agent: Any, envelope: Any) -> bool:
    task = envelope.msg_content
    if not isinstance(task, Task) or task.task_type != MessageMetaType.REQUEST:
        return False
    if not await agent.task_manager.operations_runtime.run(
        agent.task_manager.is_buffer_full
    ):
        return False
    known, full, _ = await agent.task_manager.operations_runtime.run(
        partial(_admission_status, agent.task_manager),
        task.task_id,
    )
    if known or not full:
        return False
    if any(
        getattr(item.msg_content, "task_id", None) == task.task_id
        for item in agent.deferred_inbox.messages
    ):
        return False  # Already in durable custody; do not contradict acceptance.
    await report_failed_admission(agent, task, envelope.msg_header, envelope.msg_meta)
    return True


# A message that found the deferred inbox full goes back to the broker for this
# long. The mailbox also stops asking while the inbox is full
# (pool_intake.mailbox_has_room), so this covers only a message that was already
# on its way; an immediate return made the broker hand it straight back.
DEFERRED_FULL_RETRY_SECONDS = 5.0


async def handle_busy_delivery(
    agent: Any, delivery: Any, requires_execution_slot: bool
) -> bool:
    """ACK only after durable admission or a published rejection; failures redeliver."""
    try:
        return await _handle_busy_delivery(agent, delivery, requires_execution_slot)
    except DeferredInboxFull:
        agent._logger.warning(
            "Incoming message queue full; retaining broker custody. "
            "event=deferred_admission_full agent_id=%s message_id=%s",
            agent.id,
            delivery.envelope.msg_header.message_id,
        )
        await delivery.nak(DEFERRED_FULL_RETRY_SECONDS)
        return True
    except Exception:
        agent._logger.exception(
            "Admission failed; requesting redelivery. event=task_admission_delivery_failed "
            "agent_id=%s message_id=%s",
            agent.id,
            delivery.envelope.msg_header.message_id,
        )
    await delivery.nak()
    return True


async def _handle_busy_delivery(
    agent: Any, delivery: Any, requires_execution_slot: bool
) -> bool:
    envelope = delivery.envelope
    if await reject_full_queue_request(agent, envelope):
        await delivery.ack()
        return True
    content = envelope.msg_content
    paused_work = agent.paused and not isinstance(
        content,
        (
            AgentStatus,
            ControlInstruction,
            *GATEWAY_MANAGER_CONTROL_TYPES,
            TaskProgressStatus,
            TaskStatus,
            TaskSteering,
            WorkerTaskHandbackV1,
        ),
    )
    if not (paused_work or (agent.is_executing and requires_execution_slot)):
        return False
    try:
        await agent.deferred_inbox.admit(envelope)
    except DeferredInboxFull:
        if (
            not isinstance(content, Task)
            or content.task_type != MessageMetaType.REQUEST
        ):
            raise  # Results and control traffic must retain broker custody.
        known, _, _ = await agent.task_manager.operations_runtime.run(
            partial(_admission_status, agent.task_manager),
            content.task_id,
        )
        deferred = any(
            getattr(item.msg_content, "task_id", None) == content.task_id
            for item in agent.deferred_inbox.messages
        )
        if not known and not deferred:
            await agent._send_task_error_response(
                content,
                envelope.msg_header,
                envelope.msg_meta,
                "Your message was not queued because the agent's incoming message queue is full. "
                + QUEUE_RECOVERY_MESSAGE,
            )
    await delivery.ack()
    return True
