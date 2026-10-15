"""Runtime operation coordination for TaskManager."""

from __future__ import annotations

from custom_agents.task_manager_ports import RuntimeOperationsCoordinatorOperations
from custom_agents.task_runtime_state import ExecutionRuntimeState
from custom_agents.task_runtime_state import PlanRuntimeState
from custom_agents.task_runtime_state import TaskRuntimeState
import logging

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from custom_messages.contracts.runtime_snapshot_v3 import RuntimeMailboxEventRecord, TaskRuntimeLeaseRecord, TaskStateProjectionRecord, TurnActionBatchRecord, TurnActionOutboxRecord
from custom_messages.enums import ProgressState, TaskHistoryImportance, TaskHistoryType
from id_generation import new_id


class RuntimeOperationsCoordinator:
    """Coordinate durable runtime actions, leases, mailbox events, and state projection."""

    def __init__(
        self,
        *,
        operations: RuntimeOperationsCoordinatorOperations,
        execution_state: ExecutionRuntimeState,
        plan_state: PlanRuntimeState,
        task_state: TaskRuntimeState,
        logger: logging.Logger,
    ) -> None:
        self.operations = operations
        self.execution_state = execution_state
        self.plan_state = plan_state
        self.task_state = task_state
        self._logger = logger


    def create_turn_action_batch(
        self,
        source_task_id: str,
        actions: List[Dict[str, Any]],
        *,
        plan_id: Optional[str] = None,
        plan_step_id: Optional[str] = None,
    ) -> Optional[TurnActionBatchRecord]:
        """Create durable outbox records for one ordered runtime-action batch."""
        if source_task_id not in self.task_state.tasks:
            self._logger.error("Cannot create turn-action batch for unknown task %s.", source_task_id)
            return None

        now_ts = datetime.now(timezone.utc).isoformat()
        batch_id = new_id("turn_batch")
        batch = TurnActionBatchRecord(
            batch_id=batch_id,
            source_task_id=source_task_id,
            plan_id=plan_id or self.operations.get_task_plan_id(source_task_id),
            plan_step_id=plan_step_id,
            status="prepared",
            current_action_index=0,
            created_at=now_ts,
            updated_at=now_ts,
        )
        self.execution_state.turn_action_batches[batch_id] = batch

        for index, action_payload in enumerate(actions):
            action_type = str(action_payload.get("action_type") or "unknown")
            action_id = f"{batch_id}_action_{index}"
            idempotency_key = (
                str(action_payload.get("idempotency_key") or "")
                or str(action_payload.get("task_id") or "")
                or f"{batch_id}:{index}"
            )
            record = TurnActionOutboxRecord(
                action_id=action_id,
                batch_id=batch_id,
                source_task_id=source_task_id,
                action_index=index,
                action_type=action_type,
                payload_json=dict(action_payload),
                phase="validate",
                recoverability=str(action_payload.get("recoverability") or "automatic_retry"),
                status="pending",
                idempotency_key=idempotency_key,
                created_at=now_ts,
                updated_at=now_ts,
            )
            self.execution_state.turn_action_outbox[action_id] = record

        self.operations.append_history_entry(
            source_task_id,
            f"Prepared runtime action batch {batch_id} with {len(actions)} ordered action(s).",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            timestamp=now_ts,
            details={
                "event": "turn_action_batch_prepared",
                "batch_id": batch_id,
                "action_count": len(actions),
                "action_ids": [f"{batch_id}_action_{index}" for index in range(len(actions))],
            },
        )
        self.operations.persist_runtime_state()
        return batch

    def get_turn_action_batch(self, batch_id: str) -> Optional[TurnActionBatchRecord]:
        return self.execution_state.turn_action_batches.get(batch_id)

    def get_turn_action_record(self, action_id: str) -> Optional[TurnActionOutboxRecord]:
        return self.execution_state.turn_action_outbox.get(action_id)

    def list_turn_action_records(self, batch_id: str) -> List[TurnActionOutboxRecord]:
        return sorted(
            [record for record in self.execution_state.turn_action_outbox.values() if record.batch_id == batch_id],
            key=lambda record: record.action_index,
        )

    def mark_turn_action_batch_status(
        self,
        batch_id: str,
        status: str,
        *,
        current_action_index: Optional[int] = None,
        increment_inline_recovery: bool = False,
    ) -> None:
        batch = self.execution_state.turn_action_batches.get(batch_id)
        if batch is None:
            return
        batch.status = status
        if current_action_index is not None:
            batch.current_action_index = int(current_action_index)
        if increment_inline_recovery:
            batch.inline_recovery_attempt_count += 1
        batch.updated_at = datetime.now(timezone.utc).isoformat()
        self.operations.persist_runtime_state()

    def mark_turn_action_record(
        self,
        action_id: str,
        *,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        recoverability: Optional[str] = None,
        target_route: Optional[str] = None,
        message_id: Optional[str] = None,
        side_effects: Optional[Dict[str, Any]] = None,
        next_retry_at: Optional[str] = None,
        last_error: Optional[str] = None,
        failure_report: Optional[Dict[str, Any]] = None,
        increment_attempt: bool = False,
    ) -> Optional[TurnActionOutboxRecord]:
        record = self.execution_state.turn_action_outbox.get(action_id)
        if record is None:
            return None
        if status is not None:
            record.status = status
        if phase is not None:
            record.phase = phase
        if recoverability is not None:
            record.recoverability = recoverability
        if target_route is not None:
            record.target_route = target_route
        if message_id is not None:
            record.message_id = message_id
        if side_effects is not None:
            record.side_effects.update(side_effects)
        if next_retry_at is not None:
            record.next_retry_at = next_retry_at
        if last_error is not None:
            record.last_error = last_error
        if failure_report is not None:
            record.failure_report = failure_report
        if increment_attempt:
            record.attempt_count += 1
        record.updated_at = datetime.now(timezone.utc).isoformat()
        self.operations.persist_runtime_state()
        return record

    def block_turn_actions_after(self, batch_id: str, failed_action_index: int, *, reason: str) -> None:
        now_ts = datetime.now(timezone.utc).isoformat()
        for record in self.list_turn_action_records(batch_id):
            if record.action_index <= failed_action_index:
                continue
            if record.status in {"completed", "dispatched"}:
                continue
            record.status = "blocked"
            record.phase = "validate"
            record.last_error = reason
            record.updated_at = now_ts
        self.operations.persist_runtime_state()

    def _linked_execution_parent_id(self, task_id: str) -> Optional[str]:
        plan_step_link = self.plan_state.plan_step_task_links.get(task_id)
        if plan_step_link is not None and plan_step_link.root_task_id != task_id:
            return plan_step_link.root_task_id
        recovery_link = self.plan_state.recovery_links.get(task_id)
        if recovery_link is not None and recovery_link.original_task_id != task_id:
            return recovery_link.original_task_id
        return None

    def resolve_root_task_id(self, task_id: Optional[str]) -> Optional[str]:
        """Resolve the execution root across delegation, plan-step, and recovery links."""
        if not task_id:
            return None
        current = str(task_id)
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            linked_parent_id = self._linked_execution_parent_id(current)
            if linked_parent_id is not None:
                current = linked_parent_id
                continue
            task_info = self.task_state.tasks.get(current)
            if task_info is None:
                return current
            parent_id = self.task_state.subtask_to_parent_map.get(current) or task_info.task.parent_task_id
            if not parent_id or parent_id not in self.task_state.tasks:
                return current
            current = parent_id
        return current or task_id

    def get_active_runtime_lease(self, root_task_id: Optional[str]) -> Optional[TaskRuntimeLeaseRecord]:
        if not root_task_id:
            return None
        for lease in self.execution_state.runtime_leases.values():
            if lease.root_task_id == root_task_id and lease.status == "active":
                return lease
        return None

    def begin_runtime_batch_lease(
        self,
        *,
        root_task_id: str,
        source_task_id: str,
        batch_id: Optional[str],
        owner_agent_id: str,
        current_action_index: int = 0,
        ttl_seconds: Optional[float] = None,
        timestamp: Optional[str] = None,
    ) -> TaskRuntimeLeaseRecord:
        root_task_id = self.resolve_root_task_id(root_task_id) or root_task_id
        existing = self.get_active_runtime_lease(root_task_id)
        if existing is not None:
            if batch_id and existing.batch_id == batch_id:
                return existing
            raise RuntimeError(
                f"Task tree {root_task_id} already has active runtime lease {existing.lease_id}."
            )
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        prior_epochs = [
            lease.epoch
            for lease in self.execution_state.runtime_leases.values()
            if lease.root_task_id == root_task_id
        ]
        expires_at = None
        if ttl_seconds is not None and ttl_seconds > 0:
            expires_at = (datetime.fromisoformat(now_ts) + timedelta(seconds=float(ttl_seconds))).isoformat()
        lease = TaskRuntimeLeaseRecord(
            lease_id=new_id("runtime_lease"),
            root_task_id=root_task_id,
            source_task_id=source_task_id,
            owner_agent_id=owner_agent_id,
            epoch=(max(prior_epochs) + 1) if prior_epochs else 1,
            batch_id=batch_id,
            status="active",
            current_action_index=max(0, int(current_action_index or 0)),
            started_at=now_ts,
            updated_at=now_ts,
            expires_at=expires_at,
        )
        self.execution_state.runtime_leases[lease.lease_id] = lease
        self.operations.append_history_entry(
            root_task_id,
            f"Runtime lease {lease.lease_id} started for action batch {batch_id or 'n/a'}.",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            timestamp=now_ts,
            details={
                "event": "runtime_lease_started",
                "lease_id": lease.lease_id,
                "batch_id": batch_id,
                "source_task_id": source_task_id,
                "epoch": lease.epoch,
            },
        )
        self.operations.persist_runtime_state()
        return lease

    def update_runtime_batch_lease(
        self,
        lease_id: str,
        *,
        current_action_index: Optional[int] = None,
        status: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskRuntimeLeaseRecord]:
        lease = self.execution_state.runtime_leases.get(lease_id)
        if lease is None:
            return None
        if current_action_index is not None:
            lease.current_action_index = max(0, int(current_action_index))
        if status is not None:
            lease.status = status
        lease.updated_at = timestamp or datetime.now(timezone.utc).isoformat()
        self.operations.persist_runtime_state()
        return lease

    def interrupt_runtime_batch_lease(
        self,
        *,
        root_task_id: str,
        event_id: Optional[str],
        reason: str,
        timestamp: Optional[str] = None,
    ) -> Optional[TaskRuntimeLeaseRecord]:
        lease = self.get_active_runtime_lease(self.resolve_root_task_id(root_task_id) or root_task_id)
        if lease is None:
            return None
        lease.interrupt_requested = True
        lease.interrupt_reason = reason
        lease.interrupt_event_id = event_id
        lease.updated_at = timestamp or datetime.now(timezone.utc).isoformat()
        self.operations.persist_runtime_state()
        return lease

    def complete_runtime_batch_lease(
        self,
        lease_id: str,
        *,
        status: str = "completed",
        timestamp: Optional[str] = None,
    ) -> Optional[TaskRuntimeLeaseRecord]:
        lease = self.execution_state.runtime_leases.get(lease_id)
        if lease is None:
            return None
        lease.status = status
        lease.updated_at = timestamp or datetime.now(timezone.utc).isoformat()
        if lease.root_task_id in self.task_state.tasks:
            self.operations.append_history_entry(
                lease.root_task_id,
                f"Runtime lease {lease.lease_id} ended with status {status}.",
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                timestamp=lease.updated_at,
                details={
                    "event": "runtime_lease_finished",
                    "lease_id": lease.lease_id,
                    "batch_id": lease.batch_id,
                    "status": status,
                },
            )
        self.operations.persist_runtime_state()
        return lease

    def mark_stale_runtime_leases(
        self,
        *,
        stale_after_seconds: float,
        reason: str = "Runtime lease became stale.",
    ) -> List[TaskRuntimeLeaseRecord]:
        now_dt = datetime.now(timezone.utc)
        now_ts = now_dt.isoformat()
        stale: List[TaskRuntimeLeaseRecord] = []
        for lease in self.execution_state.runtime_leases.values():
            if lease.status != "active":
                continue
            try:
                updated_at = datetime.fromisoformat(lease.updated_at or lease.started_at)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
            except Exception:
                updated_at = now_dt
            if (now_dt - updated_at).total_seconds() < max(0.0, float(stale_after_seconds)):
                continue
            lease.status = "stale"
            lease.updated_at = now_ts
            stale.append(lease)
            if lease.root_task_id in self.task_state.tasks:
                self.operations.append_history_entry(
                    lease.root_task_id,
                    f"Runtime lease {lease.lease_id} marked stale. {reason}",
                    entry_type=TaskHistoryType.PLAN_EVENT,
                    importance=TaskHistoryImportance.HIGH,
                    timestamp=now_ts,
                    details={
                        "event": "runtime_lease_marked_stale",
                        "lease_id": lease.lease_id,
                        "batch_id": lease.batch_id,
                        "source_task_id": lease.source_task_id,
                        "current_action_index": lease.current_action_index,
                        "reason": reason,
                    },
                )
        if stale:
            self.operations.persist_runtime_state()
        return stale

    def record_runtime_mailbox_event(
        self,
        *,
        root_task_id: str,
        event_type: str,
        event_class: str,
        priority: int,
        idempotency_key: str,
        payload_json: Dict[str, Any],
        header_json: Optional[Dict[str, Any]] = None,
        meta_json: Optional[Dict[str, Any]] = None,
        local_task_id: Optional[str] = None,
        related_task_id: Optional[str] = None,
        message_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        blocked_by_lease_id: Optional[str] = None,
        status: str = "received",
        timestamp: Optional[str] = None,
    ) -> RuntimeMailboxEventRecord:
        root_task_id = self.resolve_root_task_id(root_task_id) or root_task_id
        existing = self.find_runtime_mailbox_event_by_idempotency(idempotency_key)
        if existing is not None:
            return existing
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        event = RuntimeMailboxEventRecord(
            event_id=new_id("runtime_event"),
            root_task_id=root_task_id,
            local_task_id=local_task_id,
            related_task_id=related_task_id,
            event_type=event_type,
            event_class=event_class,
            status=status,
            priority=int(priority),
            idempotency_key=idempotency_key,
            message_id=message_id,
            correlation_id=correlation_id,
            schedule_id=schedule_id,
            payload_json=dict(payload_json or {}),
            header_json=dict(header_json or {}),
            meta_json=dict(meta_json or {}),
            blocked_by_lease_id=blocked_by_lease_id,
            received_at=now_ts,
            updated_at=now_ts,
        )
        self.execution_state.runtime_mailbox_events[event.event_id] = event
        self.operations.append_history_entry(
            root_task_id,
            f"Runtime mailbox recorded {event_type} event {event.event_id} as {status}.",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            timestamp=now_ts,
            related_task_id=related_task_id,
            details={
                "event": "runtime_mailbox_event_recorded",
                "event_id": event.event_id,
                "event_type": event_type,
                "event_class": event_class,
                "status": status,
                "blocked_by_lease_id": blocked_by_lease_id,
                "idempotency_key": idempotency_key,
            },
        )
        self.operations.persist_runtime_state()
        return event

    def find_runtime_mailbox_event_by_idempotency(self, idempotency_key: Optional[str]) -> Optional[RuntimeMailboxEventRecord]:
        if not idempotency_key:
            return None
        for event in self.execution_state.runtime_mailbox_events.values():
            if event.idempotency_key == idempotency_key:
                return event
        return None

    def list_deferred_runtime_events(
        self,
        root_task_id: str,
        *,
        limit: Optional[int] = None,
    ) -> List[RuntimeMailboxEventRecord]:
        root_task_id = self.resolve_root_task_id(root_task_id) or root_task_id
        events = [
            event
            for event in self.execution_state.runtime_mailbox_events.values()
            if event.root_task_id == root_task_id and event.status == "deferred"
        ]
        events.sort(key=lambda event: (event.priority, event.received_at, event.event_id))
        if limit is not None:
            return events[: max(0, int(limit))]
        return events

    def list_deferred_runtime_event_root_ids(self) -> List[str]:
        roots = {
            event.root_task_id
            for event in self.execution_state.runtime_mailbox_events.values()
            if event.status == "deferred"
        }
        return sorted(roots)

    def mark_runtime_event_status(
        self,
        event_id: str,
        status: str,
        *,
        last_error: Optional[str] = None,
        processed: bool = False,
    ) -> Optional[RuntimeMailboxEventRecord]:
        event = self.execution_state.runtime_mailbox_events.get(event_id)
        if event is None:
            return None
        event.status = status
        event.updated_at = datetime.now(timezone.utc).isoformat()
        if last_error is not None:
            event.last_error = last_error
        if processed:
            event.processed_at = event.updated_at
        self.operations.persist_runtime_state()
        return event

    def prune_runtime_mailbox_events(self, *, retention_seconds: float) -> int:
        """Delete old mailbox ledger events that can no longer affect runtime ordering."""
        retention = max(0.0, float(retention_seconds))
        now_dt = datetime.now(timezone.utc)
        eligible_statuses = {"processed", "stale", "rejected", "received"}
        pruned = 0
        for event_id, event in list(self.execution_state.runtime_mailbox_events.items()):
            if event.status not in eligible_statuses:
                continue
            raw_timestamp = event.processed_at or event.updated_at or event.received_at
            try:
                event_dt = datetime.fromisoformat(raw_timestamp)
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
            except Exception:
                event_dt = now_dt
            if (now_dt - event_dt).total_seconds() < retention:
                continue
            self.execution_state.runtime_mailbox_events.pop(event_id, None)
            pruned += 1
        if pruned:
            self._logger.info(
                "Pruned %s runtime mailbox event(s) older than %.1fs.",
                pruned,
                retention,
            )
            self.operations.persist_runtime_state()
        return pruned

    def try_transition_task_state(
        self,
        task_id: str,
        *,
        allowed_from: set[ProgressState],
        to_state: ProgressState,
        timestamp: str,
        reason: str,
        event_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        source: str = "coordinator",
    ) -> bool:
        task_info = self.operations.get_task_info(task_id)
        if task_info is None:
            return False
        previous_state = task_info.state
        if previous_state not in allowed_from:
            self.operations.append_history_entry(
                task_id,
                f"Skipped stale state transition from {previous_state.value} to {to_state.value}: {reason}",
                entry_type=TaskHistoryType.PLAN_EVENT,
                importance=TaskHistoryImportance.NORMAL,
                timestamp=timestamp,
                details={
                    "event": "runtime_state_transition_rejected",
                    "from_state": previous_state.value,
                    "to_state": to_state.value,
                    "reason": reason,
                    "event_id": event_id,
                    "batch_id": batch_id,
                },
            )
            return False
        if previous_state == to_state:
            return True
        if not self.operations.update_main_task_state(task_id, to_state, timestamp):
            return False
        projection = TaskStateProjectionRecord(
            projection_id=new_id("task_projection"),
            task_id=task_id,
            from_state=previous_state.value,
            to_state=to_state.value,
            reason=reason,
            source=source,
            batch_id=batch_id,
            event_id=event_id,
            created_at=timestamp,
        )
        self.execution_state.task_state_projection_records[projection.projection_id] = projection
        self.operations.persist_runtime_state()
        return True

    def project_task_state_after_batch(
        self,
        root_task_id: str,
        *,
        batch_id: Optional[str],
        reason: str,
        timestamp: Optional[str] = None,
    ) -> Optional[ProgressState]:
        root_task_id = self.resolve_root_task_id(root_task_id) or root_task_id
        task_info = self.operations.get_task_info(root_task_id)
        if task_info is None or self.operations.is_terminal_state(task_info.state):
            return None
        if self.get_active_runtime_lease(root_task_id):
            return task_info.state
        now_ts = timestamp or datetime.now(timezone.utc).isoformat()
        if self.operations.get_pending_recoverable_pause(root_task_id) is not None:
            target = ProgressState.PAUSED
        elif root_task_id in self.task_state.human_interaction_blockers:
            # A completed turn or empty local plan cannot clear a durable review
            # wait. Its authoritative resolution handler owns continuation.
            target = ProgressState.WAITING
        elif self.operations.is_plan_execution_root_active(root_task_id):
            target = ProgressState.WAITING
        elif self.operations.is_final_review_pending(root_task_id):
            target = ProgressState.PARTIAL
        elif self.operations.is_waiting_on_task_query(root_task_id) or self.operations.has_pending_subtasks(root_task_id):
            target = ProgressState.WAITING
        else:
            # State projection records readiness; it does not start an execution
            # coroutine. Claiming IN_PROGRESS here reserves the single execution
            # slot and can strand a root continuation that was queued while the
            # final internal plan-step action was being committed.
            target = ProgressState.PARTIAL
        self.try_transition_task_state(
            root_task_id,
            allowed_from={
                ProgressState.ACCEPTED,
                ProgressState.IN_PROGRESS,
                ProgressState.WAITING,
                ProgressState.PAUSED,
                ProgressState.PARTIAL,
                ProgressState.RETRYING,
                ProgressState.UNDEFINED,
            },
            to_state=target,
            timestamp=now_ts,
            reason=reason,
            batch_id=batch_id,
            source="coordinator",
        )
        return target

    def due_turn_action_retry_records(self, *, now: Optional[datetime] = None) -> List[TurnActionOutboxRecord]:
        current = now or datetime.now(timezone.utc)
        due: List[TurnActionOutboxRecord] = []
        for record in self.execution_state.turn_action_outbox.values():
            if record.status not in {"retry_scheduled", "failed_recoverable"}:
                continue
            if record.recoverability != "automatic_retry":
                continue
            if not record.next_retry_at:
                due.append(record)
                continue
            try:
                retry_at = datetime.fromisoformat(record.next_retry_at)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
            except Exception:
                due.append(record)
                continue
            if retry_at <= current:
                due.append(record)
        return sorted(due, key=lambda record: (record.next_retry_at or "", record.created_at, record.action_id))

    def mark_stale_dispatching_turn_actions(
        self,
        *,
        stale_after_seconds: float,
        now: Optional[datetime] = None,
    ) -> List[TurnActionOutboxRecord]:
        """Move live dispatching records that exceeded the stale window back to retry."""
        current = now or datetime.now(timezone.utc)
        stale_after = max(0.0, float(stale_after_seconds))
        marked: List[TurnActionOutboxRecord] = []
        for record in self.execution_state.turn_action_outbox.values():
            if record.status != "dispatching":
                continue
            raw_updated_at = record.updated_at or record.created_at
            try:
                updated_at = datetime.fromisoformat(raw_updated_at)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
            except Exception:
                updated_at = current
            if (current - updated_at).total_seconds() < stale_after:
                continue
            record.status = "retry_scheduled"
            record.recoverability = "automatic_retry"
            record.next_retry_at = current.isoformat()
            record.last_error = (
                record.last_error
                or f"Dispatching action exceeded stale window of {stale_after_seconds} seconds."
            )
            record.updated_at = current.isoformat()
            marked.append(record)
        if marked:
            self.operations.persist_runtime_state()
        return sorted(marked, key=lambda record: (record.action_index, record.action_id))

    def build_turn_action_recovery_brief(self, batch_id: str) -> str:
        batch = self.execution_state.turn_action_batches.get(batch_id)
        if batch is None:
            return "Runtime Action Recovery Brief: no durable action batch was found."

        records = self.list_turn_action_records(batch_id)
        completed = [record for record in records if record.status in {"completed", "dispatched"}]
        failed = [
            record
            for record in records
            if record.status in {"failed_recoverable", "failed_terminal", "retry_scheduled"}
        ]
        blocked = [record for record in records if record.status == "blocked"]

        lines = [
            "Runtime Action Recovery Brief",
            "",
            "The runtime executed an ordered action batch and stopped before all actions completed.",
            "Treat completed actions as facts. Do not repeat them unless you intentionally issue a follow-up or compensation action.",
            "",
            f"Batch: {batch.batch_id}",
            f"Task: {batch.source_task_id}",
            f"Batch status: {batch.status}",
            f"Current action index: {batch.current_action_index}",
            f"Inline recovery attempts: {batch.inline_recovery_attempt_count}",
        ]
        if batch.plan_id:
            lines.append(f"Plan: {batch.plan_id}")
        if batch.plan_step_id:
            lines.append(f"Plan step: {batch.plan_step_id}")

        lines.extend(["", "Completed actions:"])
        if completed:
            for record in completed:
                lines.append(
                    f"- #{record.action_index} {record.action_type} action_id={record.action_id} "
                    f"status={record.status} phase={record.phase} route={record.target_route or 'n/a'} "
                    f"message_id={record.message_id or 'n/a'} side_effects={record.side_effects or {}}"
                )
        else:
            lines.append("- None.")

        lines.extend(["", "Failed or retrying actions:"])
        if failed:
            for record in failed:
                lines.append(
                    f"- #{record.action_index} {record.action_type} action_id={record.action_id} "
                    f"status={record.status} phase={record.phase} recoverability={record.recoverability} "
                    f"attempts={record.attempt_count} route={record.target_route or 'n/a'} "
                    f"message_id={record.message_id or 'n/a'} side_effects={record.side_effects or {}} "
                    f"error={record.last_error or 'n/a'}"
                )
        else:
            lines.append("- None.")

        lines.extend(["", "Blocked actions not attempted:"])
        if blocked:
            for record in blocked:
                lines.append(f"- #{record.action_index} {record.action_type} action_id={record.action_id}")
        else:
            lines.append("- None.")

        return "\n".join(lines)
