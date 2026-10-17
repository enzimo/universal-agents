"""Deferred-message persistence and draining helpers for UniversalAgent."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from custom_messages.contracts.access_v2 import AccessRequestResolvedV1
from custom_messages.contracts.control_v1 import (
    AgentStatus,
    ControlInstruction,
)
from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1
from custom_messages.contracts.delivery_v1 import DeliveryReceipt
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES, MessageEnvelope
from custom_messages.contracts.task_v1 import (
    AfterActionReport,
    Task,
    TaskProgressStatus,
    TaskQuery,
    TaskStatus,
    TaskSteering,
)
from custom_messages.enums import MessageMetaType
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from storage.runtime_state_repository import (
    DeferredMessageSnapshotRow,
    RuntimeStateRepository,
)
from default_values import AGENT_DEFAULTS
from runtime_hosting.blocking_io import BlockingIOLane


class DeferredInboxFull(RuntimeError):
    """Admission has not taken custody; the broker must retain the delivery."""


@dataclass(frozen=True)
class DeferredInboxOwner:
    agent_id: str
    agent_name: str | None
    team_id: str | None
    named_agent_type: str | None


@dataclass
class DeferredMessageInbox:
    """Own deferred admission, custody transfer, and dispatch disposition.

    The repository must commit or raise. Local queue changes become visible only
    after persistence succeeds; the inbox never receives an agent host object.
    """

    owner: DeferredInboxOwner
    repository: RuntimeStateRepository
    is_execution_cancelled: Callable[[str], Awaitable[bool]]
    logger: logging.Logger
    warning_threshold: int = 0
    capacity: int = AGENT_DEFAULTS.max_deferred_messages.value
    _messages: deque[MessageEnvelope] = field(default_factory=deque, init=False)
    _warning_active: bool = field(default=False, init=False)
    _mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _io: BlockingIOLane = field(default_factory=BlockingIOLane, init=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("Deferred inbox capacity must be positive.")

    @property
    def messages(self) -> tuple[MessageEnvelope, ...]:
        return tuple(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def _snapshot(
        self, messages: Sequence[MessageEnvelope]
    ) -> DeferredMessageSnapshotRow:
        return DeferredMessageSnapshotRow(
            owner_agent_id=self.owner.agent_id,
            owner_agent_name=self.owner.agent_name,
            team_id=self.owner.team_id,
            owner_named_agent_type=self.owner.named_agent_type,
            messages_json=json.dumps(
                [envelope.model_dump(mode="json") for envelope in messages]
            ),
            updated_at=None,
        )

    def _write_snapshot(self, row: DeferredMessageSnapshotRow) -> None:
        if row.messages_json != "[]":
            self.repository.save_deferred_messages(
                owner_agent_id=row.owner_agent_id,
                owner_agent_name=row.owner_agent_name,
                team_id=row.team_id,
                owner_named_agent_type=row.owner_named_agent_type,
                messages_json=row.messages_json,
            )
        else:
            self.repository.clear_deferred_messages(row.owner_agent_id)

    def _adopt(self, messages: Sequence[MessageEnvelope]) -> None:
        self._messages = deque(messages)
        self._check_backlog()

    async def _commit(self, messages: Sequence[MessageEnvelope]) -> None:
        await self._io.run(
            self._write_snapshot,
            self._snapshot(messages),
            committed=lambda _: self._adopt(messages),
        )

    async def admit(self, envelope: MessageEnvelope) -> None:
        """Return only after this message is durable, including ACK redelivery."""
        async with self._mutation_lock:
            await self._admit(envelope)

    async def _admit(self, envelope: MessageEnvelope) -> None:
        message_id = envelope.msg_header.message_id
        if any(item.msg_header.message_id == message_id for item in self._messages):
            return
        if len(self) >= self.capacity:
            raise DeferredInboxFull(
                f"Agent {self.owner.agent_id} deferred inbox is full ({self.capacity})."
            )
        candidate = list(self._messages)
        if is_priority_deferred_message(envelope):
            candidate.insert(0, envelope)
        else:
            candidate.append(envelope)
        await self._commit(candidate)

    async def clear(self) -> None:
        async with self._mutation_lock:
            await self._commit([])

    async def remove_messages(self, message_ids: set[str]) -> None:
        """Durably drop messages whose custody moved elsewhere."""
        async with self._mutation_lock:
            await self._commit(
                [
                    item
                    for item in self._messages
                    if item.msg_header.message_id not in message_ids
                ]
            )

    async def stored_team_id(self) -> str | None:
        return await self._io.run(
            self.repository.get_deferred_messages_team_id, self.owner.agent_id
        )

    async def _cancelled(self, envelope: MessageEnvelope) -> bool:
        content = envelope.msg_content
        task_ids = {
            str(value)
            for value in (
                getattr(content, "task_id", None),
                getattr(content, "parent_task_id", None),
                getattr(content, "target_task_id", None),
                envelope.msg_meta.followup_to_task_id
                if envelope.msg_meta is not None
                else None,
            )
            if value
        }
        cancelled = False
        for task_id in sorted(task_ids):
            if await self.is_execution_cancelled(task_id):
                cancelled = True
                break
        if cancelled:
            self.logger.warning(
                "Rejected deferred restoration for a cancelled execution",
                extra={
                    "event": "cancelled_deferred_message_restore_rejected",
                    "message_id": envelope.msg_header.message_id,
                    "task_ids": sorted(task_ids),
                },
            )
        return cancelled

    async def restore_rows(self, rows: Sequence[DeferredMessageSnapshotRow]) -> bool:
        async with self._mutation_lock:
            return await self._restore_rows(rows)

    async def _restore_rows(self, rows: Sequence[DeferredMessageSnapshotRow]) -> bool:
        if not rows:
            return False
        sources = list(rows)
        if self.owner.agent_id not in {row.owner_agent_id for row in sources}:
            sources.extend(
                await self._io.run(
                    self.repository.load_deferred_messages_by_owner, self.owner.agent_id
                )
            )
        restored: dict[str, MessageEnvelope] = {}
        for row in sources:
            payload = json.loads(row.messages_json)
            if not isinstance(payload, list):
                raise ValueError("Deferred snapshot must contain a message list.")
            for item in payload:
                envelope = MessageEnvelope.model_validate(item)
                if not await self._cancelled(envelope):
                    restored.setdefault(envelope.msg_header.message_id, envelope)
        messages = sorted(
            restored.values(), key=lambda item: not is_priority_deferred_message(item)
        )
        if len(messages) > self.capacity:
            if any(row.owner_agent_id != self.owner.agent_id for row in sources):
                raise DeferredInboxFull(
                    "Recovery exceeds deferred capacity; source custody retained."
                )
            self.logger.warning(
                "Restoring existing custody above the configured admission limit",
                extra={
                    "event": "deferred_restore_above_capacity",
                    "agent_id": self.owner.agent_id,
                    "count": len(messages),
                    "capacity": self.capacity,
                },
            )
        await self._io.run(
            self.repository.commit_deferred_messages_recovery,
            sources=sources,
            replacement=self._snapshot(messages),
            committed=lambda _: self._adopt(messages),
        )
        self.logger.info(
            "Restored durable deferred messages",
            extra={
                "event": "deferred_messages_restored",
                "agent_id": self.owner.agent_id,
                "deferred_count": len(messages),
            },
        )
        return bool(messages)

    async def restore(self) -> bool:
        return await self.restore_rows(
            await self._io.run(
                self.repository.load_deferred_messages_by_owner, self.owner.agent_id
            )
        )

    async def restore_owners(self, owner_ids: Sequence[str]) -> bool:
        rows = [
            row
            for owner_id in dict.fromkeys(owner_ids)
            for row in await self._io.run(
                self.repository.load_deferred_messages_by_owner, owner_id
            )
        ]
        return await self.restore_rows(rows)

    async def restore_role(
        self,
        stale_owner_filter: Callable[[list[str]], Awaitable[list[str]]],
    ) -> bool:
        if not self.owner.team_id or not self.owner.named_agent_type:
            return False
        candidates = await self._io.run(
            self.repository.peek_deferred_messages_by_role,
            self.owner.team_id,
            self.owner.named_agent_type,
        )
        owners = await stale_owner_filter([row.owner_agent_id for row in candidates])
        return await self.restore_owners(owners)

    async def drain(
        self,
        *,
        dispatch: Callable[[MessageEnvelope], Awaitable[None]],
        available: Callable[[], bool],
    ) -> bool:
        """Keep failed/cancelled work and end the pass without a tight retry loop."""
        while self._messages and available():
            envelope = self._messages[0]
            try:
                await dispatch(envelope)
                # Dispatch can await while another admission changes the queue.
                # Remove only this message from the latest committed contents.
                async with self._mutation_lock:
                    remaining = [
                        item
                        for item in self._messages
                        if item.msg_header.message_id != envelope.msg_header.message_id
                    ]
                    await self._commit(remaining)
            except Exception:
                # The ids are in the text: the agent log format prints no ``extra``.
                self.logger.exception(
                    "A waiting message could not be handled and is kept. "
                    "event=deferred_dispatch_retained agent_id=%s message_id=%s task_id=%s",
                    self.owner.agent_id,
                    envelope.msg_header.message_id,
                    getattr(envelope.msg_content, "task_id", None),
                )
                return False
        return True

    def _check_backlog(self) -> None:
        threshold = max(0, self.warning_threshold)
        if not threshold:
            return
        exceeded = len(self) >= threshold
        if exceeded and not self._warning_active:
            self.logger.warning(
                "Deferred message backlog threshold reached: deferred_count=%s threshold=%s",
                len(self),
                threshold,
                extra={"agent_id": self.owner.agent_id},
            )
        elif self._warning_active and not exceeded:
            self.logger.info(
                "Deferred message backlog recovered: deferred_count=%s threshold=%s",
                len(self),
                threshold,
                extra={"agent_id": self.owner.agent_id},
            )
        self._warning_active = exceeded


def is_priority_deferred_message(envelope: MessageEnvelope) -> bool:
    content = envelope.msg_content
    return (
        (isinstance(content, Task) and content.task_type == MessageMetaType.RESPONSE)
        or (
            isinstance(content, TaskQuery)
            and content.query_type == MessageMetaType.RESPONSE
        )
        or isinstance(content, AfterActionReport)
    )


def requires_execution_slot(envelope: MessageEnvelope) -> bool:
    return not isinstance(
        envelope.msg_content,
        (
            ControlInstruction,
            AgentStatus,
            *GATEWAY_MANAGER_CONTROL_TYPES,
            ManagedSkillCatalogChangedV1,
            TaskProgressStatus,
            TaskStatus,
            TaskSteering,
            DeliveryReceipt,
            AfterActionReport,
            WorkerTaskHandbackV1,
            AccessRequestResolvedV1,
            # Bookkeeping only: records which worker holds a delegated task.
            DelegatedTaskClaimedV1,
        ),
    )
