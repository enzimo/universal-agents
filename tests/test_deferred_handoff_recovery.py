"""Deferred replies survive interrupted handling; transferred requests stay transferred."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import partial
from unittest.mock import AsyncMock

import pytest

from custom_agents.runtime_recovery import recover_runtime_after_restart
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import TaskQuery
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RecoverablePauseTrigger,
    TaskQueryDecision,
    TaskQueryReason,
)
from storage.runtime_state_repository import RuntimeCustodyLost
from tests.test_custody_loss import OLD, NEW, admit, holders, inbox_for, manager
from tests.test_deferred_message_inbox import _envelope
from tests.test_turn_action_outbox import _add_active_parent_task, _make_agent, _make_task_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [TaskQueryDecision.RESUME, TaskQueryDecision.CANCEL])
async def test_a_deferred_pause_decision_survives_shutdown_before_resolution(tmp_path, decision):
    task_manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(task_manager)
    task_manager.update_main_task_state(parent.task_id, ProgressState.PAUSED, datetime.now(timezone.utc).isoformat())
    query = TaskQuery(
        parent_task_id=parent.task_id,
        query_type=MessageMetaType.REQUEST,
        query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
        recoverable_pause_trigger=RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT,
        message="Choose how to continue.",
        allowed_decisions=[TaskQueryDecision.RESUME, TaskQueryDecision.CANCEL],
    )
    task_manager.add_task_query(parent.task_id, query)
    task_manager.create_recoverable_pause(
        parent.task_id,
        trigger=RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT,
        friendly_reason="A worker timed out.",
        task_query_id=query.task_query_id,
    )
    agent = _make_agent(tmp_path, task_manager=task_manager)
    object.__setattr__(agent, "_conversation_store", None)
    response = TaskQuery(
        task_query_id=query.task_query_id, parent_task_id=parent.task_id,
        query_type=MessageMetaType.RESPONSE, message=decision.value,
    )
    envelope = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="gateway"), msg_meta=MessageMeta(), msg_content=response,
    )
    await agent.deferred_inbox.admit(envelope)
    original_run = task_manager.operations_runtime.run

    async def stop_after_query_save(operation, *args, **kwargs):
        result = await original_run(operation, *args, **kwargs)
        if operation == task_manager.add_task_query:
            raise asyncio.CancelledError("Runtime shutdown after saving query history")
        return result

    task_manager.operations_runtime.run = stop_after_query_save
    with pytest.raises(asyncio.CancelledError):
        await agent.deferred_inbox.drain(dispatch=agent._dispatch_deferred_envelope, available=lambda: True)

    restored = _make_task_manager(tmp_path)
    restarted = _make_agent(tmp_path, task_manager=restored)
    object.__setattr__(restarted, "_conversation_store", None)
    object.__setattr__(restarted, "process_llm_interaction", AsyncMock(return_value=None))
    await recover_runtime_after_restart(restarted)
    assert restored.get_task_info(parent.task_id).state == ProgressState.PAUSED
    assert restored.get_pending_recoverable_pause(parent.task_id) is not None
    assert restored.get_task_queries(parent.task_id)[-1].message == decision.value
    assert len(restarted.deferred_inbox) == 1

    # The retained reply follows the real handler after restart and completes
    # the person's decision, rather than merely preserving a history entry.
    assert await restarted.deferred_inbox.drain(dispatch=restarted._dispatch_deferred_envelope, available=lambda: True)
    assert len(restarted.deferred_inbox) == 0
    assert restored.get_pending_recoverable_pause(parent.task_id) is None
    if decision is TaskQueryDecision.CANCEL:
        assert restored.get_task_from_db(parent.task_id)["status"] == ProgressState.CANCELLED.value
        restarted.process_llm_interaction.assert_not_awaited()
    else:
        assert restored.get_task_info(parent.task_id).state == ProgressState.IN_PROGRESS
        restarted.process_llm_interaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_recreated_deferred_row_cannot_authorize_an_old_dispatch(tmp_path):
    old = manager(tmp_path, OLD)
    old_inbox = inbox_for(OLD, old)
    await old_inbox.admit(_envelope("m1"))
    new = manager(tmp_path, NEW)
    new_inbox = inbox_for(NEW, new)
    assert await new_inbox.restore_owners([OLD])
    started, continue_dispatch = asyncio.Event(), asyncio.Event()
    old_runs = []

    async def old_dispatch(envelope):
        started.set()
        await continue_dispatch.wait()
        await old.operations_runtime.run(partial(admit, old, envelope.msg_content.task_id))
        old_runs.append(envelope.msg_content.task_id)

    drain = asyncio.create_task(old_inbox.drain(dispatch=old_dispatch, available=lambda: not old_runs))
    await started.wait()
    try:
        # The first new delivery discovers the takeover. A later delivery
        # creates fresh custody before the in-flight dispatch saves its task.
        with pytest.raises(RuntimeCustodyLost):
            await old_inbox.admit(_envelope("m2"))
        await old_inbox.admit(_envelope("m3"))
    finally:
        continue_dispatch.set()
    assert await drain is False
    assert old_runs == []
    assert [item.msg_content.task_id for item in old_inbox.messages] == ["m3"]

    async def dispatch_new(envelope):
        await new.operations_runtime.run(partial(admit, new, envelope.msg_content.task_id))

    async def dispatch_old(envelope):
        await old.operations_runtime.run(partial(admit, old, envelope.msg_content.task_id))

    assert await new_inbox.drain(dispatch=dispatch_new, available=lambda: True)
    assert await old_inbox.drain(dispatch=dispatch_old, available=lambda: True)
    assert holders(old) == {NEW: ["m1"], OLD: ["m3"]}


@pytest.mark.asyncio
async def test_settlement_waiting_for_the_lock_preserves_fresh_custody(tmp_path):
    old = manager(tmp_path, OLD)
    inbox = inbox_for(OLD, old)
    envelope, second = _envelope("m1"), _envelope("m2")
    await inbox.admit(envelope)
    await inbox_for(NEW, manager(tmp_path, NEW)).restore_owners([OLD])
    dispatch_waiting, finish_dispatch = asyncio.Event(), asyncio.Event()
    admission_started, finish_admission = asyncio.Event(), asyncio.Event()
    original_run = inbox._io.run

    async def held_admission(operation, *args, **kwargs):
        if operation == inbox.repository.add_deferred_message and kwargs.get("message_id") == second.msg_header.message_id:
            admission_started.set()
            await finish_admission.wait()
        return await original_run(operation, *args, **kwargs)

    inbox._io.run = held_admission

    async def dispatch(_):
        dispatch_waiting.set()
        await finish_dispatch.wait()

    drain = asyncio.create_task(inbox.drain(dispatch=dispatch, available=lambda: True))
    await dispatch_waiting.wait()
    losing_admission = asyncio.create_task(inbox.admit(second))
    await admission_started.wait()
    fresh_admission = asyncio.create_task(inbox.admit(envelope))
    await asyncio.sleep(0)  # fresh admission queues ahead of settlement
    finish_dispatch.set()
    await asyncio.sleep(0)  # settlement reads the old version, then waits
    finish_admission.set()
    with pytest.raises(RuntimeCustodyLost):
        await losing_admission
    await fresh_admission
    assert await drain is False
    assert inbox.messages == (envelope,)
    assert inbox.repository.load_deferred_messages_by_owner(OLD)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["before_schedule", "after_schedule"])
async def test_a_delayed_retry_decision_creates_one_schedule_across_restart(tmp_path, shutdown):
    tasks = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(tasks)
    tasks.update_main_task_state(parent.task_id, ProgressState.PAUSED, datetime.now(timezone.utc).isoformat())
    query = TaskQuery(
        parent_task_id=parent.task_id, query_type=MessageMetaType.REQUEST,
        query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
        recoverable_pause_trigger=RecoverablePauseTrigger.PROVIDER_OVERLOADED,
        message="Choose how to continue.", allowed_decisions=[TaskQueryDecision.RETRY_IN_5_MINUTES],
    )
    tasks.add_task_query(parent.task_id, query)
    tasks.create_recoverable_pause(
        parent.task_id, trigger=RecoverablePauseTrigger.PROVIDER_OVERLOADED,
        friendly_reason="A provider is overloaded.", task_query_id=query.task_query_id,
    )
    agent = _make_agent(tmp_path, task_manager=tasks)
    object.__setattr__(agent, "_conversation_store", None)
    response = TaskQuery(
        task_query_id=query.task_query_id, parent_task_id=parent.task_id,
        query_type=MessageMetaType.RESPONSE, message="retry in 5 minutes",
    )
    envelope = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="gateway"), msg_meta=MessageMeta(), msg_content=response,
    )
    await agent.deferred_inbox.admit(envelope)
    original = agent._schedule_provider_overload_retry

    async def schedule_then_shutdown(**kwargs):
        if shutdown == "after_schedule":
            await original(**kwargs)
        raise asyncio.CancelledError()

    object.__setattr__(agent, "_schedule_provider_overload_retry", schedule_then_shutdown)
    with pytest.raises(asyncio.CancelledError):
        await agent.deferred_inbox.drain(dispatch=agent._dispatch_deferred_envelope, available=lambda: True)
    restored = _make_task_manager(tmp_path)
    restarted = _make_agent(tmp_path, task_manager=restored)
    object.__setattr__(restarted, "_conversation_store", None)
    await recover_runtime_after_restart(restarted)
    assert len(restarted.deferred_inbox) == (1 if shutdown == "before_schedule" else 0)
    assert await restarted.deferred_inbox.drain(dispatch=restarted._dispatch_deferred_envelope, available=lambda: True)
    assert len(restarted._ensure_schedule_store().get_all()) == 1
    assert restored.get_task_info(parent.task_id).state == ProgressState.PAUSED
    assert not restored.has_queued_top_level_tasks()

    # The existing timer path is what makes this work runnable, after the
    # person's chosen delay. Recovery must not start another attempt first.
    from custom_agents.scheduled_runtime import enqueue_provider_overload_retry

    row = restarted._ensure_schedule_store().get_all()[0]
    scheduled = restarted._ensure_schedule_store().get_scheduled_message(row["schedule_id"])
    object.__setattr__(restarted, "paused", True)  # inspect admission without starting a model turn
    assert await enqueue_provider_overload_retry(restarted, scheduled.schedule_id, scheduled)
    assert restored.get_task_info(parent.task_id).state == ProgressState.ACCEPTED
