"""Deferred-message persistence and draining helpers for UniversalAgent."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional, Sequence

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
from storage.runtime_custody import CustodyStore, RuntimeCustodyLost
from storage.runtime_state_repository import DeferredMessageSnapshotRow, RuntimeStateRepository
from default_values import AGENT_DEFAULTS
from runtime_hosting.blocking_io import BlockingIOLane
from custom_agents.task_operation_runtime import submitting_task


class DeferredInboxFull(RuntimeError):
    """Admission has not taken custody; the broker must retain the delivery."""


@dataclass
class DeferredHandoff:
    """One deferred message being dispatched, and whether its handling was saved.

    Every task save by the dispatching asyncio task checks that this exact
    message still belongs to the inbox. Admission of the request's own task
    consumes it atomically (``TaskManager.add_task``), as does insertion of a
    completed delayed-retry decision's schedule. Other handlers keep
    the message until dispatch completes: a saved query history entry, for
    example, does not yet apply a person's resume or cancel decision. These
    checks apply through the task lane (``submitting_task``) or directly on the
    event loop. Child tasks, other threads and saves after dispatch closes
    never carry this handoff.

    Why the task and not the context alone: ``create_task``, ``to_thread`` and
    ``run_coroutine_threadsafe`` copy the context, so with the context alone an
    unrelated save of work started during the dispatch took the message out of
    the deferred row while its handling had not been saved, and a failure then
    left it in no row (review, 2026-10-17). Passing the message id explicitly
    through every handler that can save was the alternative; it would touch
    every message handler, so the hand-off is tied to the dispatching task.
    """

    message_id: str
    dispatch_task: Optional["asyncio.Task[Any]"] = None
    admission_task_id: Optional[str] = None
    custody_version: int = 0
    committed: bool = False
    failure: Optional[BaseException] = None
    closed: bool = False

    @property
    def pending(self) -> bool:
        return not (self.committed or self.closed)


_CURRENT_HANDOFF: ContextVar[Optional[DeferredHandoff]] = ContextVar("deferred_handoff", default=None)


def pending_deferred_handoff() -> Optional[DeferredHandoff]:
    """The message whose custody this dispatching task's save must check (class docstring)."""
    handoff = _CURRENT_HANDOFF.get()
    if handoff is None or not handoff.pending or handoff.dispatch_task is None:
        return None
    return handoff if submitting_task() is handoff.dispatch_task else None


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

    Like the task snapshot, the inbox remembers whether it holds a saved row,
    and its writes name messages (add, remove) instead of rewriting the list.
    A request leaves the row atomically with admission of its own task. A
    delayed-retry reply leaves with its schedule's insertion. Other
    messages stay until their dispatch completes, with custody checked at
    each task save (``DeferredHandoff``). When
    a write finds the row gone (``RuntimeCustodyLost``: another agent took these
    messages over), the inbox forgets every message, never retries them here,
    and tells ``on_custody_lost`` (``custom_agents.custody_loss``).
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
    _holds_row: bool = field(default=False, init=False)
    # A dispatch of released work must never settle against fresh custody.
    _custody_version: int = field(default=0, init=False)
    # Called on the event loop with the number of forgotten messages.
    on_custody_lost: Callable[[int], None] | None = field(default=None, init=False)

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

    def _adopt(self, messages: Sequence[MessageEnvelope]) -> None:
        self._messages = deque(messages)
        self._check_backlog()

    def _adopt_recovered(self, messages: Sequence[MessageEnvelope]) -> None:
        # The recovery commit inserted this owner's row.
        self._holds_row = True
        self._adopt(messages)

    async def _write(self, operation: Callable[..., None], remaining: Sequence[MessageEnvelope], **kwargs: Any) -> None:
        """Run one checked write of this owner's row, then show its result in memory."""

        def adopt(_: object) -> None:
            self._holds_row = bool(remaining)
            self._adopt(remaining)

        try:
            await self._io.run(
                operation,
                owner_agent_id=self.owner.agent_id,
                expect_existing=self._holds_row,
                committed=adopt,
                **kwargs,
            )
        except RuntimeCustodyLost as lost:
            self._release_lost_custody(lost)
            raise

    async def _remove(self, message_ids: set[str]) -> None:
        remaining = [item for item in self._messages if item.msg_header.message_id not in message_ids]
        await self._write(self.repository.remove_deferred_messages, remaining, message_ids=sorted(message_ids))

    def _release_lost_custody(self, lost: RuntimeCustodyLost) -> None:
        """Forget every message: its saved row now belongs to another agent."""
        message_ids = [item.msg_header.message_id for item in self._messages]
        released = len(message_ids)
        self._messages = deque()
        self._holds_row = False
        self._custody_version += 1
        self._check_backlog()
        self.logger.warning(
            "Another agent took this agent's deferred messages over; dropped them from memory "
            "and will not run them here. event=runtime_custody_lost agent_id=%s "
            "owner_agent_id=%s store=%s message_ids=%s",
            self.owner.agent_id,
            lost.owner_agent_id,
            lost.store.value,
            message_ids,
        )
        if self.on_custody_lost is None:
            return
        try:
            self.on_custody_lost(released)
        except Exception:
            # The refusal itself must still reach the caller; the messages are already released.
            self.logger.exception(
                "Could not tell the agent about released messages. event=custody_listener_failed "
                "owner_agent_id=%s store=%s",
                lost.owner_agent_id,
                lost.store.value,
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
        first = is_priority_deferred_message(envelope)
        candidate = [envelope, *self._messages] if first else [*self._messages, envelope]
        await self._write(
            self.repository.add_deferred_message,
            candidate,
            owner_agent_name=self.owner.agent_name,
            team_id=self.owner.team_id,
            owner_named_agent_type=self.owner.named_agent_type,
            message_id=message_id,
            envelope_json=json.dumps(envelope.model_dump(mode="json")),
            first=first,
        )

    async def clear(self) -> None:
        async with self._mutation_lock:
            await self._remove({item.msg_header.message_id for item in self._messages})

    async def remove_messages(self, message_ids: set[str]) -> None:
        """Durably drop messages whose custody moved elsewhere."""
        async with self._mutation_lock:
            await self._remove(set(message_ids))

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
            committed=lambda _: self._adopt_recovered(messages),
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
        """Dispatch in order; consume requests at admission and other messages at completion.

        Keep failed or cancelled work and end the pass without a tight retry
        loop. If the row turns out to belong to another agent, every message is
        dropped here and the pass ends (``RuntimeCustodyLost``).
        """
        while self._messages and available():
            envelope = self._messages[0]
            message_id = envelope.msg_header.message_id
            content = envelope.msg_content
            handoff = DeferredHandoff(
                message_id,
                dispatch_task=asyncio.current_task(),
                admission_task_id=content.task_id
                if isinstance(content, Task) and content.task_type == MessageMetaType.REQUEST else None,
                custody_version=self._custody_version,
            )
            token = _CURRENT_HANDOFF.set(handoff)
            error: Optional[BaseException] = None
            try:
                await dispatch(envelope)
            except Exception as exc:
                error = exc
            except BaseException:
                # Cancelled (a stop or shutdown): settle without waiting.
                self._forget_handed_off(envelope, handoff)
                raise
            finally:
                # No later save may take this message, whatever ended the dispatch.
                handoff.closed = True
                _CURRENT_HANDOFF.reset(token)
            if not await self._settle_dispatched(envelope, handoff, error):
                return False
        return True

    def _forget_handed_off(self, envelope: MessageEnvelope, handoff: DeferredHandoff) -> None:
        """A cancelled dispatch whose handling was saved: the message is done here."""
        if not handoff.committed or handoff.custody_version != self._custody_version:
            return
        message_id = envelope.msg_header.message_id
        self._messages = deque(item for item in self._messages if item.msg_header.message_id != message_id)
        self._check_backlog()
        self.logger.info(
            "A cancelled dispatch had already saved its message's handling; dropped it from memory",
            extra={"event": "deferred_dispatch_cancelled_after_save", "agent_id": self.owner.agent_id,
                   "message_id": message_id},
        )

    async def _settle_dispatched(
        self, envelope: MessageEnvelope, handoff: DeferredHandoff, error: Optional[BaseException]
    ) -> bool:
        """Remove a dispatched message unless it must stay; True to continue the pass."""
        if handoff.custody_version != self._custody_version:
            # An admission already released this dispatch's inbox. A fresh
            # row may now hold new messages; this old dispatch cannot clear it.
            return False
        message_id = envelope.msg_header.message_id
        lost = handoff.failure if isinstance(handoff.failure, RuntimeCustodyLost) else error
        if isinstance(lost, RuntimeCustodyLost) and lost.store is CustodyStore.DEFERRED_MESSAGES:
            # The task save that would have taken this message refused: the row is gone.
            self._release_lost_custody(lost)
            return False
        if not handoff.committed and (error is not None or handoff.failure is not None):
            # Nothing of its handling was saved here; the message stays in the row.
            # The ids are also in the text: the agent log format prints no ``extra``.
            self.logger.warning(
                "A waiting message could not be handled and is kept. "
                "event=deferred_dispatch_retained agent_id=%s message_id=%s task_id=%s",
                self.owner.agent_id,
                message_id,
                getattr(envelope.msg_content, "task_id", None),
                extra={"event": "deferred_dispatch_retained", "agent_id": self.owner.agent_id,
                       "message_id": message_id},
                exc_info=error or handoff.failure,
            )
            return False
        # Saved with its handling, or its handling saved nothing: remove it now.
        try:
            async with self._mutation_lock:
                if handoff.custody_version != self._custody_version:
                    # Admission may have released this inbox while settlement
                    # waited for the lock. Leave the new row and queue alone.
                    return False
                await self._remove({message_id})
        except RuntimeCustodyLost:
            return False
        if error is not None:
            self.logger.warning(
                "Deferred dispatch failed after its handling was saved; the message is done here. "
                "event=deferred_dispatch_failed_after_save agent_id=%s message_id=%s task_id=%s",
                self.owner.agent_id,
                message_id,
                getattr(envelope.msg_content, "task_id", None),
                extra={"event": "deferred_dispatch_failed_after_save", "agent_id": self.owner.agent_id,
                       "message_id": message_id},
                exc_info=error,
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
    # Request handling only admits durable queued work and wakes the retained
    # drain. Requiring the model slot hid new threads in the deferred inbox
    # until the current turn finished. Results can resume a turn and still wait.
    if isinstance(envelope.msg_content, Task) and envelope.msg_content.task_type == MessageMetaType.REQUEST:
        return False
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
