"""An agent stand-in that runs the real queue drain, execution slot and deferred inbox.

The drain pass (``UniversalAgent._continue_pending_work``), the queue check,
delivery handling and the deferred inbox are the production code. Only the
task manager is a small in-memory fake that runs on the real operation lane,
so tests can hold a storage call open and deliver a message at an exact point.
The fake never starts a model turn: marking a task in progress is recorded as
"the drain would start it" and refused.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from functools import partial
from types import SimpleNamespace
from typing import Any

from custom_agents import task_message_runtime
from custom_agents.deferred_message_runtime import DeferredInboxOwner, DeferredMessageInbox
from custom_agents.pending_work_runtime import wake_after_slot_release
from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from runtime_hosting.agent_execution import AgentExecutionGate


async def _false(*_args: Any, **_kwargs: Any) -> bool:
    return False


async def _true(*_args: Any, **_kwargs: Any) -> bool:
    return True


def use_simple_queue_checks(monkeypatch) -> None:
    """No orphaned-slot repair or wind-down rules in the queue check."""
    monkeypatch.setattr(task_message_runtime, "release_orphaned_execution_slot", _false)
    monkeypatch.setattr(task_message_runtime, "may_start_next_task", _true)


class _Repository:
    def save_deferred_messages(self, **_kwargs: Any) -> None:
        pass

    def clear_deferred_messages(self, *_args: Any) -> None:
        pass


class QueueHold:
    """Hold one call to ``get_next_task_to_process`` open until released."""

    def __init__(self) -> None:
        self.armed = False
        self.entered = threading.Event()
        self.released = threading.Event()

    def wait_if_armed(self) -> None:
        if self.armed:
            self.armed = False
            self.entered.set()
            self.released.wait(5)

    async def until_entered(self) -> None:
        await asyncio.to_thread(self.entered.wait, 5)
        assert self.entered.is_set(), "the queue check never started"


class FakeTaskManager:
    """The task-manager calls a drain pass and the scheduler enqueue make."""

    def __init__(self) -> None:
        self.operations_runtime = TaskOperationRuntime()
        self.queue: list[str] = []
        self.known: set[str] = set()
        self.would_start: list[str] = []
        self.queue_checks = 0
        self.hold = QueueHold()

    def can_start_new_task(self) -> bool:
        return True

    def get_next_task_to_process(self) -> str | None:
        self.hold.wait_if_armed()
        self.queue_checks += 1
        return self.queue[0] if self.queue else None

    def get_task_info(self, task_id: str) -> Any:
        return SimpleNamespace(state=ProgressState.ACCEPTED) if task_id in self.known else None

    def update_main_task_state(self, task_id: str, state: ProgressState, _timestamp: str) -> bool:
        if state == ProgressState.IN_PROGRESS:
            self.would_start.append(task_id)
            self.queue.remove(task_id)
            return False  # no model turn in these tests
        return True

    def is_terminal_state(self, _state: Any) -> bool:
        return False

    def append_runtime_resume_directive(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def append_history_entry(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def enqueue_task(self, task_id: str, front: bool = False) -> bool:
        self.known.add(task_id)
        self.queue.insert(0, task_id) if front else self.queue.append(task_id)
        return True

    def is_buffer_full(self) -> bool:
        return False


class DrainAgent:
    """Real drain, delivery and slot code around fake storage."""

    on_nats_delivery = UniversalAgent.on_nats_delivery
    _continue_pending_work = UniversalAgent._continue_pending_work
    _can_continue_pending_work = UniversalAgent._can_continue_pending_work
    _check_and_process_next_task = UniversalAgent._check_and_process_next_task
    _dispatch_deferred_envelope = UniversalAgent._dispatch_deferred_envelope
    _execution_scope = UniversalAgent._execution_scope
    _requires_execution_slot = UniversalAgent._requires_execution_slot
    _should_apply_stale_broadcast_filter = UniversalAgent._should_apply_stale_broadcast_filter

    def __init__(self, agent_id: str = "agent_worker") -> None:
        self.id = agent_id
        self.name = "Worker"
        self.team_id = "team_drain"
        self.is_team_lead = False
        self.named_agent_type = "WebResearchAgent"
        self.named_agent_type_queue_map: dict[str, str] = {}
        self.max_broadcast_age_ms = 60_000
        self.nats_max_deliver = 5
        self.paused = False
        self._should_exit = asyncio.Event()
        self._pending_work_task: asyncio.Task | None = None
        self._pending_work_requested = False
        self._pending_work_timers: set[asyncio.TimerHandle] = set()
        self._owned_tasks: set[asyncio.Task] = set()
        self._pending_interrupt_responses: dict[str, Any] = {}
        self._execution_gate = AgentExecutionGate(on_release=partial(wake_after_slot_release, self))
        self._active_execution_task_id = None
        self._lease_loss_wind_down = None
        self._logger = logging.getLogger("tests.pending_work_support")
        self.task_manager = FakeTaskManager()
        self.dispatched: list[str] = []
        self.deferred_inbox = DeferredMessageInbox(
            owner=DeferredInboxOwner(self.id, self.name, self.team_id, self.named_agent_type),
            repository=_Repository(),
            is_execution_cancelled=_false,
            logger=self._logger,
        )
        self._runtime_coordinator = lambda: SimpleNamespace(drain_all_deferred_events=_false)

    @property
    def is_executing(self) -> bool:
        return self._execution_gate.busy

    def _message_targets_other_team(self, _envelope: Any) -> bool:
        return False

    async def _announce_execution_state(self) -> None:
        return None

    async def _refresh_self_agent_state(self, **_kwargs: Any) -> None:
        return None

    async def _drain_due_turn_action_outbox(self) -> bool:
        return False

    async def _dispatch_message(self, *, msg_content: Any, **_kwargs: Any) -> None:
        self.dispatched.append(getattr(msg_content, "task_id", "?"))

    def _runtime_resume_directive(self, *_args: Any) -> str:
        return "retry"


class Delivery:
    """One broker delivery as ``on_nats_delivery`` receives it."""

    def __init__(self, envelope: MessageEnvelope, route: str) -> None:
        self.envelope = envelope
        self.logical_route = route
        self.source_queue_name = route
        self.transport_subject = route
        self.redelivery_count = 0
        self.max_deliver = 5
        self.acked = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, *_args: Any) -> None:
        raise AssertionError("the message was refused instead of being kept")


def subtask_result(agent: DrainAgent, task_id: str) -> Delivery:
    """A delegated task's result for ``agent``: it needs the execution slot."""
    envelope = MessageEnvelope(
        msg_content=Task(
            task_type=MessageMetaType.RESPONSE, task_id=task_id, parent_task_id="task_parent", answers="done"
        ),
        msg_header=MessageHeader(from_agent_id="agent_other", to_routing_path=agent.id),
        msg_meta=MessageMeta(),
    )
    return Delivery(envelope, agent.id)


async def settled(agent: DrainAgent) -> None:
    """Wait until no drain runs and loop callbacks have run."""
    for _ in range(3):
        task = agent._pending_work_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 5)
        await asyncio.sleep(0.05)
