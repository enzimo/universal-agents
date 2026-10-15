"""Typed dispatch operations for the task mailbox owner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from custom_agents.task_records import TaskInfo
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskQuery


class EnqueueFiredScheduledTaskOperation(Protocol):
    async def __call__(
        self,
        schedule_id: str,
        scheduled_message: ScheduledMessage,
        *,
        from_runtime_mailbox: bool = ...,
    ) -> bool: ...


class FailSubtaskAndReconcileParentOperation(Protocol):
    async def __call__(
        self,
        *,
        parent_task_id: str,
        subtask_info: TaskInfo,
        error_message: str,
        resume_parent: bool = ...,
        synthetic_context: Optional[str] = ...,
        from_runtime_mailbox: bool = ...,
        runtime_event_id: Optional[str] = ...,
    ) -> None: ...


class HandleTaskResponseOperation(Protocol):
    async def __call__(
        self,
        sub_task_response: Task,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        *,
        from_runtime_mailbox: bool = ...,
    ) -> None: ...


class HandleTaskQueryMessageOperation(Protocol):
    async def __call__(
        self,
        task_query_msg: TaskQuery,
        incoming_msg_header: MessageHeader,
        incoming_msg_meta: MessageMeta,
        *,
        from_runtime_mailbox: bool = ...,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeMailboxDispatch:
    enqueue_fired_scheduled_task: EnqueueFiredScheduledTaskOperation

    fail_subtask_and_reconcile_parent: FailSubtaskAndReconcileParentOperation

    handle_task_response: HandleTaskResponseOperation

    handle_task_query_message: HandleTaskQueryMessageOperation

