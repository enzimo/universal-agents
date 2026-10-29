"""Real commits under controlled latency must not stall unrelated loop work."""

import asyncio
import json
import logging
import threading
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from custom_agents.deferred_message_runtime import (
    DeferredInboxOwner,
    DeferredMessageInbox,
)
from storage.runtime_state_repository import SqliteRuntimeStateRepository
from tests.test_deferred_message_inbox import _envelope
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.records.task_results import TaskResultUpdate
from custom_messages.enums import MessageMetaType
from custom_agents.task_message_runtime import _check_and_process_next_task


def _task_manager(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("owner", "Owner", "team", "Worker")
    manager.add_task(
        Task(
            task_id="task", task_type=MessageMetaType.REQUEST, problem_statement="Work"
        ),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=None,
    )
    return manager


@pytest.mark.asyncio
async def test_runtime_shutdown_propagates_cancellation_without_cancelling_durable_work(
    tmp_path,
):
    manager = _task_manager(tmp_path)
    started = asyncio.Event()

    async def invocation(**kwargs):
        started.set()
        await asyncio.Event().wait()

    agent = SimpleNamespace(
        task_manager=manager,
        is_executing=False,
        main_lane_busy=False,  # the main lane (runtime_hosting.agent_execution)
        paused=False,
        _logger=logging.getLogger(__name__),
        _refresh_self_agent_state=AsyncMock(),
        _announce_execution_state=AsyncMock(),
        process_llm_interaction=invocation,
        _consume_stop_reason=lambda _: None,
        _cancel_task_tree_locally=AsyncMock(),
    )
    task = asyncio.create_task(_check_and_process_next_task(agent))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    agent._cancel_task_tree_locally.assert_not_awaited()
    assert not manager.is_execution_cancelled("task")
    assert manager.get_task_info("task") is not None
    assert not agent.is_executing


@pytest.mark.asyncio
async def test_cancelled_slow_admission_finishes_custody_on_loop_before_next_writer(
    tmp_path, monkeypatch
):
    repository = SqliteRuntimeStateRepository(str(tmp_path / "runtime.db"))
    inbox = DeferredMessageInbox(
        owner=DeferredInboxOwner("worker", "Worker", "team", "Worker"),
        repository=repository,
        is_execution_cancelled=AsyncMock(return_value=False),
        logger=logging.getLogger(__name__),
    )
    started, release = threading.Event(), threading.Event()
    original = repository.add_deferred_message
    threads = []

    def slow_commit(**kwargs):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(3), "test did not release the storage operation"
        original(**kwargs)

    monkeypatch.setattr(repository, "add_deferred_message", slow_commit)
    first = asyncio.create_task(inbox.admit(_envelope("first")))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        second = asyncio.create_task(inbox.admit(_envelope("second")))
        ticks = 0
        deadline = asyncio.get_running_loop().time() + 0.08
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
            ticks += 1
        assert ticks >= 8
        assert not first.done()
        assert len(threads) == 1  # The second admission cannot overtake the first.
        assert len(inbox) == 0
    finally:
        release.set()
        results = await asyncio.gather(
            first, *([second] if second else []), return_exceptions=True
        )
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    assert all(ident != threading.get_ident() for ident in threads)
    assert [item.msg_content.task_id for item in inbox.messages] == ["first", "second"]
    rows = repository.load_deferred_messages_by_owner("worker")
    assert len(json.loads(rows[0].messages_json)) == 2


@pytest.mark.asyncio
async def test_sdk_tool_and_cancelled_history_write_share_one_commit_lane(
    tmp_path, monkeypatch
):
    manager = _task_manager(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = manager.runtime_state_store.save_task_runtime_task
    writes = []

    def slow_write(**kwargs):
        writes.append(threading.get_ident())
        if len(writes) == 1:
            started.set()
            assert release.wait(3)
        original(**kwargs)

    monkeypatch.setattr(
        manager.runtime_state_store, "save_task_runtime_task", slow_write
    )
    first = asyncio.create_task(
        manager.operations_runtime.run(
            manager.append_history_entry, "task", "committed history"
        )
    )
    tool = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        tool = asyncio.create_task(
            asyncio.to_thread(
                manager.operations_runtime.run_sync,
                manager.update_task_result,
                "task",
                TaskResultUpdate(answer="committed answer"),
            )
        )
        # Keepalive/control callbacks continue while both callers wait for I/O.
        for _ in range(12):
            await asyncio.sleep(0.005)
        assert len(writes) == 1
        assert not first.done() and not tool.done()
    finally:
        release.set()
        results = await asyncio.gather(
            first, *([tool] if tool else []), return_exceptions=True
        )
    assert isinstance(results[0], asyncio.CancelledError)
    result = results[1]
    assert result.task.answer == "committed answer"
    assert result.task.execution_history[-1].summary == "committed history"
    result.task.answer = "local edit"
    owned = await manager.operations_runtime.run(manager.get_task_info, "task")
    assert owned.task.answer == "committed answer"
    assert all(ident != threading.get_ident() for ident in writes)
    snapshot = manager.runtime_state_store.load_task_runtime_by_owner("owner")[0]
    assert (
        json.loads(snapshot.runtime_state_json)["tasks"][0]["task"]["answer"]
        == "committed answer"
    )
