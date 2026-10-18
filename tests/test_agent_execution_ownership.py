"""Race actual runtime entry points against an occupied Strands instance."""

import asyncio
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strands import Agent
from strands.models.model import Model

from custom_agents import subtask_watchdog_runtime, task_message_runtime
from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from runtime_hosting.agent_execution import AgentExecutionGate


class HeldModel(Model):
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "local-held-provider"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("No structured output expected")
        yield

    async def stream(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "done"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class RuntimeHost(SimpleNamespace):
    # Use the production admission and ownership methods, with local provider I/O.
    is_executing = UniversalAgent.is_executing
    _execution_scope = UniversalAgent._execution_scope
    _announce_execution_state = UniversalAgent._announce_execution_state
    process_llm_interaction = UniversalAgent.process_llm_interaction
    _resume_parent_task_if_ready = UniversalAgent._resume_parent_task_if_ready
    _check_and_process_next_task = UniversalAgent._check_and_process_next_task
    _can_continue_pending_work = UniversalAgent._can_continue_pending_work


@pytest.fixture
def host(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("agent", "agent", "team", "TeamArchitect")
    provider = HeldModel()
    sdk = Agent(model=provider, callback_handler=None)
    preparations = []

    async def invoke(content, header, meta):
        preparations.append(content.task_id)
        sdk.messages.clear()
        await sdk.invoke_async(content.problem_statement)
        return Task(
            task_id=content.task_id, task_type=MessageMetaType.RESPONSE, answer="done"
        )

    async def complete(task_id, timestamp):
        await manager.operations_runtime.run(manager.complete_task, task_id, timestamp)

    async def apply_result(agent, task_id, task_info, result):
        await complete(task_id, datetime.now(timezone.utc).isoformat())

    monkeypatch.setattr(
        subtask_watchdog_runtime, "finalize_conversation_turn", AsyncMock()
    )
    monkeypatch.setattr(
        task_message_runtime, "_apply_task_processing_result", apply_result
    )
    monkeypatch.setattr(task_message_runtime, "finalize_conversation_turn", AsyncMock())
    return RuntimeHost(
        id="agent",
        name="agent",
        team_id="team",
        named_agent_type="TeamArchitect",
        task_manager=manager,
        paused=False,
        _should_exit=asyncio.Event(),
        _execution_gate=AgentExecutionGate(),
        _active_execution_task_id=None,
        _logger=logging.getLogger(__name__),
        _refresh_self_agent_state=AsyncMock(),
        _publish_presence_status=AsyncMock(),
        _notify_context_after_turn=AsyncMock(),
        _send_task_error_response=AsyncMock(),
        _complete_task_and_schedule_aar_review=complete,
        _conversation_store=None,
        _conversation_compactor=None,
        _conversation_artifact_offloader=None,
        _conversation_summary_generator=None,
        _strands_agent=sdk,
        turn_executor=SimpleNamespace(process_llm_interaction=invoke),
        provider=provider,
        preparations=preparations,
    )


def add_task(host, task_id, *, waiting=False):
    task = Task(
        task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=task_id
    )
    host.task_manager.add_task(
        task, MessageHeader(from_agent_id="gateway"), MessageMeta(),
        owner_person_id=None,
    )
    if waiting:
        host.task_manager.update_main_task_state(
            task_id, ProgressState.WAITING, datetime.now(timezone.utc).isoformat()
        )
    return task


@pytest.mark.asyncio
async def test_parent_and_aar_wait_for_actual_model_turn_without_failing(host):
    original = Task(
        task_id="original",
        task_type=MessageMetaType.REQUEST,
        problem_statement="original",
    )
    add_task(host, "parent", waiting=True)
    # WAITING parents normally leave the runnable queue through their first turn.
    host.task_manager.task_state.task_queue.clear()
    add_task(host, "aar")
    active = asyncio.create_task(
        host.process_llm_interaction(original, MessageHeader(), MessageMeta())
    )
    await asyncio.wait_for(host.provider.entered.wait(), 2)
    parent = asyncio.create_task(host._resume_parent_task_if_ready("parent"))
    try:
        await asyncio.sleep(0)
        assert not parent.done()
        assert host.is_executing
        assert host.preparations == ["original"]
        assert host.task_manager.get_task_info("parent").state == ProgressState.WAITING
        assert await host._check_and_process_next_task() is False
        assert list(host.task_manager.task_state.task_queue) == ["aar"]
        host.provider.release.set()
        await asyncio.wait_for(asyncio.gather(active, parent), 3)
        assert not host.is_executing
        assert await host._check_and_process_next_task() is True
        assert host.preparations == ["original", "parent", "aar"]
        host._send_task_error_response.assert_not_awaited()
        assert host.task_manager.get_task_info("parent") is None
        assert host.task_manager.get_task_info("aar") is None
    finally:
        host.provider.release.set()
        await asyncio.gather(active, parent, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_claim_precedes_first_storage_await(host, monkeypatch):
    add_task(host, "first")
    add_task(host, "second")
    entered, release = asyncio.Event(), asyncio.Event()
    run = host.task_manager.operations_runtime.run

    async def delayed(operation, *args, **kwargs):
        if operation == host.task_manager.can_start_new_task:
            entered.set()
            await release.wait()
        return await run(operation, *args, **kwargs)

    monkeypatch.setattr(host.task_manager.operations_runtime, "run", delayed)
    first = asyncio.create_task(host._check_and_process_next_task())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert host.is_executing
        assert await host._check_and_process_next_task() is False
        assert list(host.task_manager.task_state.task_queue) == ["first", "second"]
        release.set()
        host.provider.release.set()
        assert await asyncio.wait_for(first, 3)
        assert host.preparations == ["first"]
    finally:
        release.set()
        host.provider.release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_release_or_reset_active_turn(host):
    task = Task(
        task_id="original",
        task_type=MessageMetaType.REQUEST,
        problem_statement="original",
    )
    active = asyncio.create_task(
        host.process_llm_interaction(task, MessageHeader(), MessageMeta())
    )
    await asyncio.wait_for(host.provider.entered.wait(), 2)
    waiter = asyncio.create_task(
        host.process_llm_interaction(
            task.model_copy(update={"task_id": "waiting"}),
            MessageHeader(),
            MessageMeta(),
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    try:
        assert host.is_executing
        assert host.preparations == ["original"]
        assert not active.done()
    finally:
        host.provider.release.set()
        await active
    assert not host.is_executing


@pytest.mark.asyncio
async def test_cancelled_owner_finishes_sdk_cleanup_before_waiter_starts(host):
    original = Task(
        task_id="original",
        task_type=MessageMetaType.REQUEST,
        problem_statement="original",
    )
    active = asyncio.create_task(
        host.process_llm_interaction(original, MessageHeader(), MessageMeta())
    )
    await asyncio.wait_for(host.provider.entered.wait(), 2)
    waiting = asyncio.create_task(
        host.process_llm_interaction(
            original.model_copy(update={"task_id": "waiting"}),
            MessageHeader(),
            MessageMeta(),
        )
    )
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    host.provider.release.set()
    result = await asyncio.wait_for(waiting, 3)
    assert result.task_id == "waiting"
    assert host.preparations == ["original", "waiting"]
    assert not host.is_executing


@pytest.mark.asyncio
async def test_inline_nested_scope_retains_owner_and_child_task_waits():
    gate = AgentExecutionGate()
    entered = asyncio.Event()

    async def child():
        async with gate.hold():
            entered.set()

    async with gate.hold():
        async with gate.hold():
            assert gate.owned_by_current_task
        assert gate.busy
        contender = asyncio.create_task(child())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(contender, 2)
    assert entered.is_set()
    assert not gate.busy


@pytest.mark.asyncio
async def test_empty_queue_probe_does_not_publish_busy_idle_presence(host):
    assert await host._check_and_process_next_task() is False
    assert not host.is_executing
    # Presence delivery itself drains pending work. Publishing here creates a
    # feedback loop even though no task was admitted or run.
    host._refresh_self_agent_state.assert_not_awaited()
    host._publish_presence_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_broadcasts_busy_when_it_starts_and_idle_when_it_ends(host):
    """Web Chat's activity mesh hears of work as it starts and ends.

    The heartbeat alone reports a change up to 20 seconds late.
    """

    seen = []

    async def publish(*, is_exiting):
        seen.append((host.is_executing, is_exiting))

    host._publish_presence_status = publish
    original = Task(task_id="original", task_type=MessageMetaType.REQUEST, problem_statement="original")
    host.provider.release.set()
    await asyncio.wait_for(host.process_llm_interaction(original, MessageHeader(), MessageMeta()), 3)
    assert seen == [(True, False), (False, False)]


@pytest.mark.asyncio
async def test_a_failed_presence_broadcast_never_affects_the_turn(host, caplog):
    host._publish_presence_status = AsyncMock(side_effect=RuntimeError("transport down"))
    original = Task(task_id="original", task_type=MessageMetaType.REQUEST, problem_statement="original")
    host.provider.release.set()
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(host.process_llm_interaction(original, MessageHeader(), MessageMeta()), 3)
    assert host.preparations == ["original"]
    assert "event=agent_execution_presence_failed" in caplog.text


@pytest.mark.asyncio
async def test_user_stop_cancels_sdk_child_without_cancelling_its_owner(host):
    from custom_agents.control_runtime import _interrupt_active_execution

    invoke = host.turn_executor.process_llm_interaction
    host._active_execution_task = None

    async def tracked_invoke(content, header, meta):
        UniversalAgent._set_active_execution_context(host, content.task_id)
        try:
            return await invoke(content, header, meta)
        finally:
            UniversalAgent._clear_active_execution_context(host)

    host.turn_executor.process_llm_interaction = tracked_invoke
    task = Task(task_id="stopped", task_type=MessageMetaType.REQUEST, problem_statement="wait")

    async def owner():
        with pytest.raises(asyncio.CancelledError):
            await host.process_llm_interaction(task, MessageHeader(), MessageMeta())
        assert asyncio.current_task().cancelling() == 0

    running = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(host.provider.entered.wait(), 2)
        assert host._active_execution_task is not running
        await _interrupt_active_execution(
            host, root_task_id="stopped", graph_task_ids={"stopped"}, reason="task_stopped"
        )
        await asyncio.wait_for(running, 2)
        assert not host.is_executing
        assert host._active_execution_task is None
        host.provider.release.set()
        result = await host.process_llm_interaction(
            task.model_copy(update={"task_id": "next"}), MessageHeader(), MessageMeta())
        assert result.task_id == "next"
    finally:
        host.provider.release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_gate_release_callback_runs_once_per_outer_hold_even_when_cancelled():
    released = []
    gate = AgentExecutionGate(on_release=released.append)

    async def holder():
        async with gate.hold():
            async with gate.hold():  # nested in the same task: no release yet
                pass
            assert released == []

    owner = asyncio.create_task(holder())
    await owner
    assert released == [owner]

    entered = asyncio.Event()

    async def cancelled_holder():
        async with gate.hold():
            entered.set()
            await asyncio.Event().wait()

    stopped = asyncio.create_task(cancelled_holder())
    await entered.wait()
    stopped.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopped
    assert released == [owner, stopped]
    assert not gate.busy


@pytest.mark.asyncio
async def test_a_failing_release_callback_is_logged_and_keeps_the_holders_outcome(caplog):
    def broken_callback(_owner):
        raise RuntimeError("callback bug")

    gate = AgentExecutionGate(on_release=broken_callback)

    async def holder_that_fails():
        async with gate.hold():
            raise ValueError("the holder's own error")

    async def holder_that_succeeds():
        async with gate.hold():
            return "done"

    with caplog.at_level(logging.ERROR, logger="runtime_hosting.agent_execution"):
        with pytest.raises(ValueError, match="the holder's own error"):
            await asyncio.create_task(holder_that_fails())
        assert await asyncio.create_task(holder_that_succeeds()) == "done"
    assert caplog.text.count("event=execution_gate_release_callback_failed") == 2
    assert not gate.busy
