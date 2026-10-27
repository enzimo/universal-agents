"""Each turn reads its task, person model and Strands agent from its own work record.

An agent may run several model turns at once (threads started with Launch
parallel thread). Nothing a turn reads may come from agent-wide "current"
state, or it gets the task, person, grants and model of whichever turn
started last (custom_tools.runtime_context.TaskWork).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from custom_agents import control_runtime, delegation_runtime, person_model_runtime
from custom_agents.person_model_runtime import PersonModelHook, active_bundle, select_turn_model, turn_context_window
from custom_agents.strands_agent import UniversalAgent
from custom_agents.strands_parts import StrandsParts, current_parts, turn_strands_agent
from custom_agents.tool_loading import LoadedTools, loaded_tools
from custom_agents.turn_registry import pin_turn_strands, running_turn_task_ids, running_turn_works
from custom_tools.runtime_context import active_task_context, bound_task_id, current_task_work, task_work


class TurnHost(SimpleNamespace):
    """The agent's own turn bookkeeping, without a model or transport."""

    _set_active_execution_context = UniversalAgent._set_active_execution_context
    _clear_active_execution_context = UniversalAgent._clear_active_execution_context

    def running_turn_works(self):
        return running_turn_works(self)

    def running_turn_task_ids(self):
        return running_turn_task_ids(self)


def _parts(generation: int = 0) -> StrandsParts:
    return StrandsParts(
        agent=SimpleNamespace(messages=[], model="team-model"),
        loaded=LoadedTools(),
        sync=None,
        generation=generation,
    )


def _host(*, current_task_id: str | None = None) -> TurnHost:
    main = _parts()
    tasks = {
        task_id: SimpleNamespace(meta=SimpleNamespace(conversation_id=f"conversation-{task_id}"))
        for task_id in ("task-a", "task-b", "task-main")
    }
    return TurnHost(
        id="agent-1",
        _logger=logging.getLogger("test_turn_work_isolation"),
        _running_turns={},
        _strands_parts=main,
        _strands_agent=main.agent,
        _strands_generation=0,
        _strands_model="team-model",
        task_manager=SimpleNamespace(get_task_info=tasks.get, current_task_id=current_task_id),
    )


@pytest.mark.asyncio
async def test_overlapping_handlings_each_read_their_own_task_in_hooks_and_threads():
    host = _host(current_task_id="task-main")
    both_bound = asyncio.Barrier(2)
    seen: dict[str, tuple] = {}

    async def handle(task_id: str) -> None:
        with task_work(task_id):
            await both_bound.wait()
            in_thread = await asyncio.to_thread(bound_task_id)
            in_child = await asyncio.create_task(asyncio.sleep(0, result=bound_task_id()))
            task, meta = active_task_context(host)
            seen[task_id] = (bound_task_id(), in_thread, in_child, task, meta.conversation_id)

    await asyncio.gather(handle("task-a"), handle("task-b"))

    assert seen == {
        "task-a": ("task-a", "task-a", "task-a", "task-a", "conversation-task-a"),
        "task-b": ("task-b", "task-b", "task-b", "task-b", "conversation-task-b"),
    }


def test_outside_a_handling_there_is_no_active_task_even_with_a_slot_holder():
    # The old fallback to the agent's single slot would name another turn's task.
    host = _host(current_task_id="task-main")

    assert bound_task_id() is None
    assert active_task_context(host) == (None, None)


@pytest.mark.asyncio
async def test_a_turn_needs_its_tasks_work_record():
    host = _host()

    with pytest.raises(RuntimeError, match="work record"):
        host._set_active_execution_context("task-a")
    with task_work("task-b"), pytest.raises(RuntimeError, match="work record"):
        host._set_active_execution_context("task-a")
    assert host._running_turns == {}


@pytest.mark.asyncio
async def test_a_stop_reaches_only_the_turns_of_its_task_graph():
    host = _host()
    started = {task_id: asyncio.Event() for task_id in ("task-a", "task-b")}
    release_b = asyncio.Event()

    async def turn(task_id: str, release: asyncio.Event) -> str:
        with task_work(task_id):
            host._set_active_execution_context(task_id)
            try:
                started[task_id].set()
                await release.wait()
                return "finished"
            finally:
                host._clear_active_execution_context()

    turn_a = asyncio.create_task(turn("task-a", asyncio.Event()))
    turn_b = asyncio.create_task(turn("task-b", release_b))
    await asyncio.gather(*(event.wait() for event in started.values()))
    assert sorted(host.running_turn_task_ids()) == ["task-a", "task-b"]

    stopped = await control_runtime._interrupt_active_execution(
        host, root_task_id="task-a", graph_task_ids={"task-a"}, reason="task_stopped"
    )

    assert stopped is not None and stopped.task_id == "task-a"
    assert stopped.tool_stop.is_requested()
    assert turn_a.cancelled()
    assert not turn_b.done()
    assert host.running_turn_task_ids() == ["task-b"]
    release_b.set()
    assert await turn_b == "finished"
    assert host._running_turns == {}


@pytest.mark.asyncio
async def test_a_stop_with_no_turn_of_its_graph_running_stops_nothing():
    host = _host()

    assert (
        await control_runtime._interrupt_active_execution(
            host, root_task_id="task-a", graph_task_ids={"task-a"}, reason="task_stopped"
        )
        is None
    )


@pytest.mark.asyncio
async def test_each_turn_uses_its_own_persons_model_and_window(monkeypatch):
    host = _host()
    bundles = {
        "alice": SimpleNamespace(person_id="alice", model="alice-model", context_window_tokens=1000, describe="a"),
        "bob": SimpleNamespace(person_id="bob", model="bob-model", context_window_tokens=2000, describe="b"),
    }
    person_of = {"task-a": "alice", "task-b": "bob"}
    monkeypatch.setattr(person_model_runtime, "person_for_task", lambda _agent, task_id: person_of.get(task_id))
    monkeypatch.setattr(person_model_runtime, "bundle_for_person", lambda _agent, person: bundles.get(person))
    both_chosen = asyncio.Barrier(2)
    hook = PersonModelHook(host)
    seen = {}

    async def turn(task_id: str) -> None:
        strands = SimpleNamespace(model="team-model")
        with task_work(task_id) as work:
            work.strands = StrandsParts(agent=strands, loaded=LoadedTools(), sync=None)
            select_turn_model(host, task_id)
            await both_chosen.wait()
            hook._before_model_call(SimpleNamespace(agent=strands))
            seen[task_id] = (active_bundle(host).person_id, turn_context_window(host), strands.model)
            host._set_active_execution_context(task_id)
            host._clear_active_execution_context()
            seen[f"{task_id}-after"] = strands.model

    await asyncio.gather(turn("task-a"), turn("task-b"))

    assert seen["task-a"] == ("alice", 1000, "alice-model")
    assert seen["task-b"] == ("bob", 2000, "bob-model")
    # The team's model goes back on each turn's own Strands agent when it ends.
    assert seen["task-a-after"] == seen["task-b-after"] == "team-model"
    assert active_bundle(host) is None


def test_choosing_a_model_outside_the_tasks_work_record_is_refused():
    with pytest.raises(RuntimeError, match="work record"):
        select_turn_model(_host(), "task-a")


def test_a_turn_keeps_its_strands_agent_until_the_model_is_rebuilt():
    host = _host()
    first = host._strands_parts
    with task_work("task-a") as work:
        assert pin_turn_strands(host) is first
        # Rotated for an interrupted session (same model): the running turn keeps its agent.
        host._strands_parts = _parts()
        assert pin_turn_strands(host) is first
        assert turn_strands_agent(host) is first.agent
        # The model was rebuilt: the turn takes the new agent where its conversation starts afresh.
        rebuilt = _parts(generation=1)
        host._strands_parts, host._strands_generation = rebuilt, 1
        assert pin_turn_strands(host) is rebuilt
        assert work.strands is rebuilt
    assert pin_turn_strands(host) is rebuilt


def test_loaded_tool_groups_and_strands_agent_follow_the_turns_parts():
    host = _host()
    own = _parts()
    with task_work("task-a") as work:
        work.strands = own
        assert current_parts(host) is own
        assert loaded_tools(host) is own.loaded
        assert turn_strands_agent(host) is own.agent
    assert current_parts(host) is host._strands_parts
    assert loaded_tools(host) is host._strands_parts.loaded


@pytest.mark.asyncio
async def test_a_delegation_takes_its_parent_from_the_turns_own_task():
    aborts = []

    async def run(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def record_abort(**details):
        aborts.append(details)

    host = SimpleNamespace(
        id="agent-1",
        _logger=logging.getLogger("test_turn_work_isolation"),
        # The agent's single slot holds another task; the delegation must not use it.
        task_manager=SimpleNamespace(
            current_task_id="task-main",
            get_task_info=lambda _task_id: None,
            operations_runtime=SimpleNamespace(run=run),
        ),
        _record_delegation_subtask_abort=record_abort,
    )

    with task_work("task-b"):
        assert await delegation_runtime._create_subtask_request(host, SimpleNamespace()) is None

    assert aborts[0]["failure_phase"] == "parent_task_missing"
    assert aborts[0]["parent_task_id"] == "task-b"
    assert current_task_work() is None
