"""Task-tree runtime coordination for ordered action batches and inbound events."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional
from collections.abc import Callable
import logging
from custom_messages.contracts.runtime_snapshot_v3 import (
    TaskRuntimeLeaseRecord,
    RuntimeMailboxEventRecord,
)
from custom_agents.task_records import TaskInfo
from custom_messages.enums import ProgressState
from custom_agents.task_manager_runtime_operations import RuntimeOperationsCoordinator
from custom_agents.task_runtime_ports import RuntimeMailboxDispatch
from custom_agents.task_operation_runtime import TaskOperationRuntime

from default_values import AGENT_DEFAULTS
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskQuery


class RuntimeBatchInterrupted(RuntimeError):
    """Raised when a preemptive runtime event interrupts an ordered action batch."""


class TaskRuntimeCoordinator:
    """Coordinates task-tree leases, inbound mailbox deferral, and state projection."""

    def __init__(
        self,
        *,
        agent_id: str,
        logger: logging.Logger,
        configuration: Callable[[], dict[str, Any]],
        available: Callable[[], bool],
        runtime: RuntimeOperationsCoordinator,
        get_task_info: Callable[[str], TaskInfo | None],
        operations_runtime: TaskOperationRuntime,
        is_terminal_state: Callable[[ProgressState], bool],
        dispatch: RuntimeMailboxDispatch,
    ) -> None:
        self.agent_id = agent_id
        self.logger = logger
        self.configuration = configuration
        self.available = available
        self.runtime = runtime
        self.operations_runtime = operations_runtime
        self.get_task_info = get_task_info
        self.is_terminal_state = is_terminal_state
        self.dispatch = dispatch
        self.replay_event_id: str | None = None

    def config(self) -> dict[str, Any]:
        defaults = dict(AGENT_DEFAULTS.runtime_coordinator.value)
        configured = self.configuration()
        defaults.update(configured)
        mode = str(defaults.get("mode") or "enforced").strip().lower()
        if mode not in {"enforced", "observe", "disabled"}:
            mode = "enforced"
        defaults["mode"] = mode
        defaults["enabled"] = bool(defaults.get("enabled", True)) and mode != "disabled"
        defaults["max_mailbox_drain_events_per_pass"] = max(
            1,
            int(defaults.get("max_mailbox_drain_events_per_pass") or 100),
        )
        defaults["batch_lease_stale_after_seconds"] = max(
            0.0,
            float(defaults.get("batch_lease_stale_after_seconds") or 60.0),
        )
        return defaults

    def enabled(self) -> bool:
        return bool(self.config().get("enabled", True))

    def enforced(self) -> bool:
        config = self.config()
        return bool(config.get("enabled", True)) and config.get("mode") == "enforced"

    def resolve_root_task_id(self, task_id: Optional[str]) -> Optional[str]:
        return self.runtime.resolve_root_task_id(task_id)

    def event_priority(self, event_type: str, event_class: str) -> int:
        if event_class == "preemptive":
            return 0
        if event_type == "task_query_response":
            return 10
        if event_type == "task_response":
            return 20
        if event_type == "provider_retry":
            return 30
        if event_class == "recovery":
            return 40
        if event_type == "scheduled_message":
            return 50
        return 90

    def envelope_idempotency_key(
        self,
        *,
        event_type: str,
        content: Any,
        header: MessageHeader,
        meta: MessageMeta,
        schedule_id: Optional[str] = None,
    ) -> str:
        explicit = getattr(header, "idempotency_key", None)
        if explicit:
            return str(explicit)
        if schedule_id:
            return f"{event_type}:{schedule_id}"
        message_id = getattr(header, "message_id", None)
        if message_id:
            return f"{event_type}:{message_id}"
        content_id = content.get_id() if hasattr(content, "get_id") else None
        correlation_id = getattr(meta, "msg_correlation_id", None)
        return f"{event_type}:{content_id or 'unknown'}:{correlation_id or 'no_correlation'}"

    def should_defer_for_active_lease(
        self,
        *,
        root_task_id: Optional[str],
        event_type: str,
        event_class: str,
    ) -> tuple[bool, Optional[TaskRuntimeLeaseRecord]]:
        if not self.enabled() or not root_task_id:
            return False, None
        if event_class == "advisory":
            return False, None
        if self.replay_event_id:
            return False, None
        lease = self.runtime.get_active_runtime_lease(root_task_id)
        if lease is None:
            return False, None
        if event_class == "preemptive":
            return False, lease
        if event_type == "task_response" and not self.config().get(
            "defer_task_responses_during_active_batch", True
        ):
            return False, lease
        if event_type == "scheduled_message" and not self.config().get(
            "defer_scheduled_events_during_active_batch",
            True,
        ):
            return False, lease
        if event_class == "recovery" and not self.config().get(
            "defer_recovery_events_during_active_batch", True
        ):
            return False, lease
        return True, lease

    def record_envelope_event(
        self,
        *,
        root_task_id: str,
        event_type: str,
        event_class: str,
        content: Any,
        header: MessageHeader,
        meta: MessageMeta,
        status: str,
        related_task_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        blocked_by_lease_id: Optional[str] = None,
    ) -> RuntimeMailboxEventRecord:
        idempotency_key = self.envelope_idempotency_key(
            event_type=event_type,
            content=content,
            header=header,
            meta=meta,
            schedule_id=schedule_id,
        )
        return self.runtime.record_runtime_mailbox_event(
            root_task_id=root_task_id,
            local_task_id=getattr(content, "task_id", None)
            or getattr(content, "parent_task_id", None),
            related_task_id=related_task_id,
            event_type=event_type,
            event_class=event_class,
            status=status,
            priority=self.event_priority(event_type, event_class),
            idempotency_key=idempotency_key,
            message_id=getattr(header, "message_id", None),
            correlation_id=getattr(meta, "msg_correlation_id", None),
            schedule_id=schedule_id,
            payload_json=content.model_dump(mode="json")
            if hasattr(content, "model_dump")
            else {},
            header_json=header.model_dump(mode="json"),
            meta_json=meta.model_dump(mode="json"),
            blocked_by_lease_id=blocked_by_lease_id,
        )

    def synthetic_idempotency_key(
        self,
        *,
        event_type: str,
        root_task_id: str,
        related_task_id: Optional[str],
        payload_json: dict[str, Any],
    ) -> str:
        stable_payload = json.dumps(
            payload_json, sort_keys=True, default=str, separators=(",", ":")
        )
        digest = hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()[:16]
        return f"{event_type}:{root_task_id}:{related_task_id or 'none'}:{digest}"

    def record_synthetic_event(
        self,
        *,
        root_task_id: str,
        event_type: str,
        event_class: str,
        status: str,
        payload_json: dict[str, Any],
        local_task_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        blocked_by_lease_id: Optional[str] = None,
    ) -> RuntimeMailboxEventRecord:
        return self.runtime.record_runtime_mailbox_event(
            root_task_id=root_task_id,
            local_task_id=local_task_id,
            related_task_id=related_task_id,
            event_type=event_type,
            event_class=event_class,
            status=status,
            priority=self.event_priority(event_type, event_class),
            idempotency_key=(
                idempotency_key
                or self.synthetic_idempotency_key(
                    event_type=event_type,
                    root_task_id=root_task_id,
                    related_task_id=related_task_id,
                    payload_json=payload_json,
                )
            ),
            payload_json=payload_json,
            header_json={},
            meta_json={},
            blocked_by_lease_id=blocked_by_lease_id,
        )

    def defer_synthetic_event_if_blocked(
        self,
        *,
        root_task_id: Optional[str],
        event_type: str,
        event_class: str,
        payload_json: dict[str, Any],
        local_task_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> bool:
        should_defer, lease = self.should_defer_for_active_lease(
            root_task_id=root_task_id,
            event_type=event_type,
            event_class=event_class,
        )
        if not should_defer or not root_task_id:
            if (
                self.enabled()
                and root_task_id
                and lease is not None
                and not self.enforced()
            ):
                self.record_synthetic_event(
                    root_task_id=root_task_id,
                    event_type=event_type,
                    event_class=event_class,
                    status="received",
                    payload_json=payload_json,
                    local_task_id=local_task_id,
                    related_task_id=related_task_id,
                    idempotency_key=idempotency_key,
                    blocked_by_lease_id=getattr(lease, "lease_id", None),
                )
            return False
        max_deferred = max(
            1, int(self.config().get("max_deferred_events_per_task") or 500)
        )
        if len(self.runtime.list_deferred_runtime_events(root_task_id)) >= max_deferred:
            event = self.record_synthetic_event(
                root_task_id=root_task_id,
                event_type=event_type,
                event_class=event_class,
                status="rejected",
                payload_json=payload_json,
                local_task_id=local_task_id,
                related_task_id=related_task_id,
                idempotency_key=idempotency_key,
                blocked_by_lease_id=lease.lease_id if lease else None,
            )
            self.logger.error(
                "[%s] event=runtime_mailbox_capacity_exceeded event_id=%s event_type=%s root_task_id=%s "
                "max_deferred_events_per_task=%s",
                self.agent_id,
                event.event_id,
                event_type,
                root_task_id,
                max_deferred,
            )
            return self.enforced()
        event = self.record_synthetic_event(
            root_task_id=root_task_id,
            event_type=event_type,
            event_class=event_class,
            status="deferred",
            payload_json=payload_json,
            local_task_id=local_task_id,
            related_task_id=related_task_id,
            idempotency_key=idempotency_key,
            blocked_by_lease_id=lease.lease_id if lease else None,
        )
        self.logger.info(
            "[%s] event=runtime_mailbox_deferred event_id=%s event_type=%s root_task_id=%s "
            "blocked_by_lease_id=%s",
            self.agent_id,
            event.event_id,
            event_type,
            root_task_id,
            getattr(lease, "lease_id", None),
        )
        return self.enforced()

    def defer_subtask_failure_if_blocked(
        self,
        *,
        parent_task_id: str,
        subtask_id: str,
        error_message: str,
        resume_parent: bool,
        synthetic_context: Optional[str],
    ) -> bool:
        root_task_id = self.resolve_root_task_id(parent_task_id) or parent_task_id
        return self.defer_synthetic_event_if_blocked(
            root_task_id=root_task_id,
            event_type="subtask_failure_reconciliation",
            event_class="recovery",
            local_task_id=parent_task_id,
            related_task_id=subtask_id,
            idempotency_key=f"subtask_failure_reconciliation:{parent_task_id}:{subtask_id}",
            payload_json={
                "parent_task_id": parent_task_id,
                "subtask_id": subtask_id,
                "error_message": error_message,
                "resume_parent": bool(resume_parent),
                "synthetic_context": synthetic_context,
            },
        )

    def defer_envelope_if_blocked(
        self,
        *,
        root_task_id: Optional[str],
        event_type: str,
        event_class: str,
        content: Any,
        header: MessageHeader,
        meta: MessageMeta,
        related_task_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
    ) -> bool:
        should_defer, lease = self.should_defer_for_active_lease(
            root_task_id=root_task_id,
            event_type=event_type,
            event_class=event_class,
        )
        if not should_defer or not root_task_id:
            if (
                self.enabled()
                and root_task_id
                and lease is not None
                and not self.enforced()
            ):
                self.record_envelope_event(
                    root_task_id=root_task_id,
                    event_type=event_type,
                    event_class=event_class,
                    content=content,
                    header=header,
                    meta=meta,
                    status="received",
                    related_task_id=related_task_id,
                    schedule_id=schedule_id,
                    blocked_by_lease_id=getattr(lease, "lease_id", None),
                )
            return False
        max_deferred = max(
            1, int(self.config().get("max_deferred_events_per_task") or 500)
        )
        if len(self.runtime.list_deferred_runtime_events(root_task_id)) >= max_deferred:
            event = self.record_envelope_event(
                root_task_id=root_task_id,
                event_type=event_type,
                event_class=event_class,
                content=content,
                header=header,
                meta=meta,
                status="rejected",
                related_task_id=related_task_id,
                schedule_id=schedule_id,
                blocked_by_lease_id=lease.lease_id if lease else None,
            )
            self.logger.error(
                "[%s] event=runtime_mailbox_capacity_exceeded event_id=%s event_type=%s root_task_id=%s "
                "max_deferred_events_per_task=%s",
                self.agent_id,
                event.event_id,
                event_type,
                root_task_id,
                max_deferred,
            )
            return self.enforced()
        event = self.record_envelope_event(
            root_task_id=root_task_id,
            event_type=event_type,
            event_class=event_class,
            content=content,
            header=header,
            meta=meta,
            status="deferred",
            related_task_id=related_task_id,
            schedule_id=schedule_id,
            blocked_by_lease_id=lease.lease_id if lease else None,
        )
        self.logger.info(
            "[%s] event=runtime_mailbox_deferred event_id=%s event_type=%s root_task_id=%s "
            "blocked_by_lease_id=%s",
            self.agent_id,
            event.event_id,
            event_type,
            root_task_id,
            getattr(lease, "lease_id", None),
        )
        return self.enforced()

    def begin_batch(
        self, *, root_task_id: str, source_task_id: str, batch_id: str
    ) -> TaskRuntimeLeaseRecord | None:
        if not self.enabled():
            return None
        return self.runtime.begin_runtime_batch_lease(
            root_task_id=root_task_id,
            source_task_id=source_task_id,
            batch_id=batch_id,
            owner_agent_id=self.agent_id,
            ttl_seconds=self.config().get("batch_lease_stale_after_seconds"),
        )

    def update_batch_action_index(
        self, lease_id: Optional[str], action_index: int
    ) -> None:
        if lease_id:
            self.runtime.update_runtime_batch_lease(
                lease_id,
                current_action_index=action_index,
            )

    def interrupt_if_requested(
        self, lease_id: Optional[str], batch_id: str, action_index: int
    ) -> None:
        if not lease_id or not self.config().get(
            "check_interrupts_between_actions", True
        ):
            return
        lease = self.runtime.execution_state.runtime_leases.get(lease_id)
        if lease is None or not lease.interrupt_requested:
            return
        self.runtime.block_turn_actions_after(
            batch_id,
            action_index,
            reason=lease.interrupt_reason
            or "Runtime batch interrupted by a preemptive event.",
        )
        self.runtime.complete_runtime_batch_lease(
            lease_id,
            status="interrupted",
        )
        raise RuntimeBatchInterrupted(
            lease.interrupt_reason or "Runtime batch interrupted."
        )

    def complete_batch(
        self,
        *,
        lease_id: Optional[str],
        root_task_id: str,
        batch_id: str,
        status: str = "completed",
    ) -> None:
        if lease_id:
            self.runtime.complete_runtime_batch_lease(lease_id, status=status)
        if status == "completed":
            self.runtime.project_task_state_after_batch(
                root_task_id,
                batch_id=batch_id,
                reason=f"Runtime batch {batch_id} finished with status {status}.",
            )

    def request_interrupt(
        self,
        *,
        root_task_id: str,
        event_id: Optional[str],
        reason: str,
    ) -> None:
        self.runtime.interrupt_runtime_batch_lease(
            root_task_id=root_task_id,
            event_id=event_id,
            reason=reason,
        )

    async def drain_deferred_events(self, root_task_id: str) -> bool:
        if not self.enabled() or not self.available():
            return False
        if (
            await self.operations_runtime.run(
                self.runtime.get_active_runtime_lease, root_task_id
            )
            is not None
        ):
            return False
        drained_any = False
        limit = self.config().get("max_mailbox_drain_events_per_pass", 100)
        events = await self.operations_runtime.run(
            self.runtime.list_deferred_runtime_events, root_task_id, limit=limit
        )
        for event in events:
            if not self.available():
                break
            if (
                await self.operations_runtime.run(
                    self.runtime.get_active_runtime_lease, root_task_id
                )
                is not None
            ):
                break
            task_info = await self.operations_runtime.run(
                self.get_task_info, root_task_id
            )
            if task_info is None or self.is_terminal_state(task_info.state):
                await self.operations_runtime.run(
                    self.runtime.mark_runtime_event_status,
                    event.event_id,
                    "stale",
                    last_error="Root task is missing or terminal.",
                    processed=True,
                )
                continue
            await self.operations_runtime.run(
                self.runtime.mark_runtime_event_status, event.event_id, "processing"
            )
            self.replay_event_id = event.event_id
            try:
                await self._process_mailbox_event(event)
                await self.operations_runtime.run(
                    self.runtime.mark_runtime_event_status,
                    event.event_id,
                    "processed",
                    processed=True,
                )
                drained_any = True
            except Exception as exc:
                await self.operations_runtime.run(
                    self.runtime.mark_runtime_event_status,
                    event.event_id,
                    "deferred",
                    last_error=str(exc),
                )
                self.logger.exception(
                    "[%s] Failed to drain runtime mailbox event %s.",
                    self.agent_id,
                    event.event_id,
                )
                break
            finally:
                self.replay_event_id = None
        if drained_any:
            await self.operations_runtime.run(
                self.runtime.project_task_state_after_batch,
                root_task_id,
                batch_id=None,
                reason="Runtime mailbox drain completed.",
            )
            await self.operations_runtime.run(self.prune_mailbox_events)
        return drained_any

    async def drain_all_deferred_events(self) -> bool:
        for root_task_id in await self.operations_runtime.run(
            self.runtime.list_deferred_runtime_event_root_ids
        ):
            if await self.drain_deferred_events(root_task_id):
                return True
        await self.operations_runtime.run(self.prune_mailbox_events)
        return False

    async def _process_mailbox_event(self, event: RuntimeMailboxEventRecord) -> None:
        if event.event_type == "subtask_failure_reconciliation":
            payload = dict(event.payload_json or {})
            subtask_id = str(payload.get("subtask_id") or "")
            parent_task_id = str(
                payload.get("parent_task_id") or event.local_task_id or ""
            )
            subtask_info = await self.operations_runtime.run(
                self.get_task_info, subtask_id
            )
            if not parent_task_id or subtask_info is None:
                self.logger.warning(
                    "[%s] event=runtime_mailbox_subtask_failure_stale event_id=%s "
                    "parent_task_id=%s subtask_id=%s",
                    self.agent_id,
                    event.event_id,
                    parent_task_id,
                    subtask_id,
                )
                return
            await self.dispatch.fail_subtask_and_reconcile_parent(
                parent_task_id=parent_task_id,
                subtask_info=subtask_info,
                error_message=str(
                    payload.get("error_message") or "Delegated subtask failed."
                ),
                resume_parent=bool(payload.get("resume_parent", False)),
                synthetic_context=payload.get("synthetic_context") or None,
                from_runtime_mailbox=True,
                runtime_event_id=event.event_id,
            )
            return

        header = MessageHeader.model_validate(event.header_json)
        meta = MessageMeta.model_validate(event.meta_json)
        if event.event_type == "task_response":
            task = Task.model_validate(event.payload_json)
            await self.dispatch.handle_task_response(
                sub_task_response=task,
                msg_header=header,
                msg_meta=meta,
                from_runtime_mailbox=True,
            )
            return
        if event.event_type == "task_query_response":
            query = TaskQuery.model_validate(event.payload_json)
            await self.dispatch.handle_task_query_message(
                task_query_msg=query,
                incoming_msg_header=header,
                incoming_msg_meta=meta,
                from_runtime_mailbox=True,
            )
            return
        if event.event_type in {"scheduled_message", "provider_retry"}:
            scheduled = ScheduledMessage.model_validate(event.payload_json)
            await self.dispatch.enqueue_fired_scheduled_task(
                event.schedule_id or scheduled.schedule_id,
                scheduled,
                from_runtime_mailbox=True,
            )
            return
        raise RuntimeError(
            f"Unsupported runtime mailbox event type {event.event_type}."
        )

    def recover_after_restart(self) -> int:
        if not self.enabled():
            return 0
        stale = self.runtime.mark_stale_runtime_leases(
            stale_after_seconds=0.0,
            reason="Agent startup recovered a persisted active runtime lease from a prior process.",
        )
        for lease in stale:
            self.logger.warning(
                "[%s] event=runtime_lease_marked_stale_after_restart lease_id=%s root_task_id=%s "
                "batch_id=%s current_action_index=%s",
                self.agent_id,
                lease.lease_id,
                lease.root_task_id,
                lease.batch_id,
                lease.current_action_index,
            )
        self.prune_mailbox_events()
        return len(stale)

    def prune_mailbox_events(self) -> int:
        if not self.enabled():
            return 0
        retention_seconds = self.config().get(
            "mailbox_event_retention_seconds", 86400.0
        )
        try:
            retention = float(retention_seconds)
        except (TypeError, ValueError):
            retention = 86400.0
        return self.runtime.prune_runtime_mailbox_events(retention_seconds=retention)


def task_response_root_task_id(
    resolve_root: Callable[[Optional[str]], Optional[str]], task: Task
) -> Optional[str]:
    return resolve_root(task.parent_task_id or task.task_id)


def task_query_response_root_task_id(
    resolve_root: Callable[[Optional[str]], Optional[str]], query: TaskQuery
) -> Optional[str]:
    return resolve_root(query.parent_task_id)


def scheduled_message_root_task_id(
    resolve_root: Callable[[Optional[str]], Optional[str]],
    scheduled_message: ScheduledMessage,
) -> Optional[str]:
    payload = scheduled_message.payload
    if isinstance(payload, Task):
        return resolve_root(
            payload.parent_task_id
            or getattr(scheduled_message.payload_meta, "followup_to_task_id", None)
            or payload.task_id
        )
    return resolve_root(
        getattr(scheduled_message.payload_meta, "followup_to_task_id", None)
    )
