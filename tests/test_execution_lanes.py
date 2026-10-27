"""Requests sent in a parallel thread run in execution lanes of their own.

The team lead runs a request a person sent in a thread started with Web Chat's
Launch parallel thread beside its other work; everything else shares the main
lane, one turn at a time (custom_agents.execution_lanes,
runtime_hosting.agent_execution).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_agents.execution_lanes import (
    MAX_PARALLEL_LANES,
    executing_parallel_task_ids,
    is_parallel_request,
    lane_for_message,
    lane_for_task,
    parallel_lane,
)
from custom_agents import execution_lanes_runtime
from custom_agents.execution_lanes_runtime import ParallelLanes
from custom_agents.task_manager import TaskManager
from custom_agents.task_queue_safety import queued_parallel_lanes
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, ProgressState
from runtime_hosting.agent_execution import MAIN_LANE, AgentExecutionLanes, current_lane


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta(*, parallel: bool, delivery_mode: str = "interactive") -> MessageMeta:
    return MessageMeta(
        conversation_id="conversation",
        gateway_context=GatewayContext(
            canonical_user_id="owner", delivery_mode=delivery_mode, parallel_thread=parallel
        ),
    )


def _manager(tmp_path, *, lead: bool = True) -> TaskManager:
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.parallel_lanes_enabled = lead
    return manager


def _add(manager: TaskManager, task_id: str, *, parallel: bool, delivery_mode: str = "interactive") -> None:
    assert manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=task_id),
        MessageHeader(from_agent_id="gateway"),
        _meta(parallel=parallel, delivery_mode=delivery_mode),
        owner_person_id="owner",
    )


# -- the lanes ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_lanes_run_at_once_and_one_lane_runs_one_owner_at_a_time():
    lanes = AgentExecutionLanes()
    inside: list[str] = []
    release = asyncio.Event()

    async def hold(lane: str, name: str) -> None:
        async with lanes.hold(lane):
            inside.append(name)
            assert current_lane() == lane
            await release.wait()

    main = asyncio.create_task(hold(MAIN_LANE, "main"))
    parallel = asyncio.create_task(hold("parallel:a", "parallel"))
    waiting = asyncio.create_task(hold("parallel:a", "same request"))
    await asyncio.sleep(0.01)

    assert inside == ["main", "parallel"]
    assert lanes.busy and lanes.main_busy and lanes.parallel_lanes_held() == 1
    release.set()
    await asyncio.gather(main, parallel, waiting)
    assert inside == ["main", "parallel", "same request"]
    assert not lanes.busy
    # A parallel lane's lock goes with its last user; the main one stays.
    assert set(lanes._locks) == {MAIN_LANE}


@pytest.mark.asyncio
async def test_an_owner_shares_its_lane_inline_but_never_takes_a_second_one():
    lanes = AgentExecutionLanes()
    async with lanes.hold("parallel:a"):
        async with lanes.hold("parallel:a"):
            assert lanes.owned_by_current_task("parallel:a")
        with pytest.raises(RuntimeError, match="one lane at a time"):
            async with lanes.hold(MAIN_LANE):
                pass
    assert not lanes.busy


@pytest.mark.asyncio
async def test_the_release_callback_names_the_lane():
    released = []
    lanes = AgentExecutionLanes(on_release=lambda owner, lane: released.append(lane))
    async with lanes.hold("parallel:a"):
        pass
    async with lanes.hold(MAIN_LANE):
        pass
    assert released == ["parallel:a", MAIN_LANE]


# -- which lane --------------------------------------------------------------------


def test_only_a_team_leads_interactive_request_from_a_parallel_thread_gets_a_lane(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    _add(manager, "ordinary", parallel=False)
    _add(manager, "scheduled", parallel=True, delivery_mode="cron")

    assert lane_for_task(manager, "parallel") == parallel_lane("parallel")
    assert lane_for_task(manager, "ordinary") == MAIN_LANE
    assert lane_for_task(manager, "scheduled") == MAIN_LANE, "a run whose context was copied from a chat"
    assert lane_for_task(manager, "unknown") == MAIN_LANE
    assert lane_for_task(_manager(tmp_path / "worker", lead=False), "parallel") == MAIN_LANE
    assert not is_parallel_request(_meta(parallel=False))


def test_a_message_goes_to_the_lane_of_the_task_it_concerns(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    question = TaskQuery(parent_task_id="parallel", query_type=MessageMetaType.REQUEST, message="Which account?")
    result = Task(task_id="child", parent_task_id="parallel", task_type=MessageMetaType.RESPONSE, answer="done")

    assert lane_for_message(manager, question) == parallel_lane("parallel")
    assert lane_for_message(manager, result) == parallel_lane("parallel")
    assert lane_for_message(manager, Task(task_id="x", task_type=MessageMetaType.RESPONSE)) == MAIN_LANE


# -- the task manager --------------------------------------------------------------


def test_a_parallel_task_neither_takes_nor_preempts_the_main_slot(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "ordinary", parallel=False)
    _add(manager, "parallel", parallel=True)

    # The main lane takes ordinary work only; the parallel lane its own request.
    assert queued_parallel_lanes(manager) == [parallel_lane("parallel")]
    assert manager.get_next_task_to_process() == "ordinary"
    assert manager.update_main_task_state("ordinary", ProgressState.IN_PROGRESS, _now())
    assert manager.get_next_task_to_process(parallel_lane("parallel")) == "parallel"
    assert manager.update_main_task_state("parallel", ProgressState.IN_PROGRESS, _now())

    assert manager.get_task_info("ordinary").state == ProgressState.IN_PROGRESS, "not paused by the parallel task"
    assert manager.current_task_id == "ordinary"
    assert executing_parallel_task_ids(manager) == ["parallel"]
    assert manager.update_main_task_state("parallel", ProgressState.WAITING, _now())
    assert manager.current_task_id == "ordinary"
    assert executing_parallel_task_ids(manager) == []


def test_the_main_lane_still_waits_for_its_slot_while_a_parallel_task_runs(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "first", parallel=False)
    _add(manager, "second", parallel=False)
    assert manager.get_next_task_to_process() == "first"
    assert manager.update_main_task_state("first", ProgressState.IN_PROGRESS, _now())

    assert manager.get_next_task_to_process() is None


# -- the limit ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_most_the_limit_of_parallel_lanes_start_and_a_freed_one_lets_the_next_start(monkeypatch):
    release = asyncio.Event()
    started: list[str] = []
    waiting = [parallel_lane(f"request-{index}") for index in range(MAX_PARALLEL_LANES + 1)]

    async def run_lane(_agent, lane: str) -> None:
        started.append(lane)
        await release.wait()

    async def with_work(_agent):
        return [lane for lane in waiting if lane not in started]

    monkeypatch.setattr(execution_lanes_runtime, "run_parallel_lane", run_lane)
    monkeypatch.setattr(execution_lanes_runtime, "parallel_lanes_with_work", with_work)
    host = SimpleNamespace(
        id="lead",
        paused=False,
        _should_exit=asyncio.Event(),
        _owned_tasks=set(),
        _parallel_lanes=ParallelLanes(),
        _logger=logging.getLogger("test_execution_lanes"),
        task_manager=SimpleNamespace(parallel_lanes_enabled=True),
    )

    await execution_lanes_runtime.start_parallel_lanes(host)
    await asyncio.sleep(0)
    assert started == waiting[:MAX_PARALLEL_LANES]

    release.set()
    await asyncio.gather(*host._parallel_lanes.workers.values())
    await execution_lanes_runtime.start_parallel_lanes(host)
    await asyncio.sleep(0)
    assert started == waiting
    await asyncio.gather(*host._parallel_lanes.workers.values())


@pytest.mark.asyncio
async def test_the_agent_reads_a_tasks_lane_on_the_operations_lane(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    agent = SimpleNamespace(task_manager=manager)
    assert await execution_lanes_runtime.lane_of(agent, "parallel") == parallel_lane("parallel")
    assert await execution_lanes_runtime.lane_of(agent, None) == MAIN_LANE
