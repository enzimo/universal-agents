"""A stopped turn ends only after the commands its tools started have ended.

Real pieces: a Strands agent with its default ConcurrentToolExecutor, the real
shell tool in Strands' worker thread, the sandbox and process supervisor, the
production execution scope (``UniversalAgent.process_llm_interaction``) and
the production stop (``control_runtime._interrupt_active_execution``). Only
the model is scripted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strands import Agent, tool
from strands.models.model import Model
from strands.types.tools import ToolContext

import custom_agents.sdk.strands.turn_tool_stop as turn_tool_stop
from custom_agents.control_runtime import _interrupt_active_execution
from custom_agents.sdk.strands.turn import LLMTurnExecutor
from custom_agents.sdk.strands.turn_tool_stop import (
    TASK_STOPPED_REASON,
    attach_tool_stop,
    invoke_with_tool_stop,
)
from custom_agents.strands_agent import UniversalAgent
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.runtime_context import set_agent_ref
from custom_tools.tool_stop import TOOL_STOP_STATE_KEY, ToolStop, tracked_tool_call
from runtime_hosting.agent_execution import AgentExecutionGate
from tests.turn_tool_stop_support import (
    agents_folder,
    kill_if_alive,
    long_command,
    process_alive,
    shell_agent,
    wait_for_pid,
)

LOGGER = logging.getLogger("tests.turn_tool_stop")


class Host(SimpleNamespace):
    """The production execution scope and turn bookkeeping, without NATS or storage."""

    is_executing = UniversalAgent.is_executing
    _execution_scope = UniversalAgent._execution_scope
    _announce_execution_state = UniversalAgent._announce_execution_state
    process_llm_interaction = UniversalAgent.process_llm_interaction


def make_host(sdk: Agent) -> Host:
    host = Host(
        id="agent",
        _execution_gate=AgentExecutionGate(),
        _active_execution_task=None,
        _active_execution_task_id=None,
        _active_tool_stop=None,
        _logger=LOGGER,
        _refresh_self_agent_state=AsyncMock(),
        _publish_presence_status=AsyncMock(),
        _strands_agent=sdk,
    )

    async def turn(content, header, meta):
        # What LLMTurnExecutor does around the model call.
        tool_stop = UniversalAgent._set_active_execution_context(host, content.task_id)
        try:
            await invoke_with_tool_stop(
                host._strands_agent, "go", tool_stop=tool_stop, invoke_kwargs={}, logger=LOGGER
            )
            return Task(task_id=content.task_id, task_type=MessageMetaType.RESPONSE, answer="done")
        finally:
            UniversalAgent._clear_active_execution_context(host)

    host.turn_executor = SimpleNamespace(process_llm_interaction=turn)
    return host


def task(task_id: str = "task-1") -> Task:
    return Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="run it")


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    set_agent_ref(None)
    folder = agents_folder(tmp_path, monkeypatch)
    yield folder
    for pid_file in folder.glob("*.pid"):
        kill_if_alive(pid_file)
    set_agent_ref(None)


async def start_owner(host: Host, observed: dict) -> asyncio.Task:
    """The code that runs a turn and handles its cancellation (task_message_runtime)."""

    async def owner():
        try:
            await host.process_llm_interaction(task(), MessageHeader(), MessageMeta())
        except asyncio.CancelledError:
            # Here the runtime archives the task, replies and frees the slot.
            observed["pids_alive_when_owner_resumed"] = [process_alive(pid) for pid in observed["pids"]]
            observed["slot_busy_when_owner_resumed"] = host.is_executing
            raise

    return asyncio.create_task(owner())


@pytest.mark.asyncio
async def test_a_stop_returns_only_after_the_running_command_ended(work_dir: Path) -> None:
    pid_file = work_dir / "cmd.pid"
    host = make_host(shell_agent([long_command(pid_file)], work_dir))
    observed: dict = {"pids": []}
    running = await start_owner(host, observed)
    observed["pids"].append(await wait_for_pid(pid_file))
    turn_stop = host._active_tool_stop

    started = time.monotonic()
    await _interrupt_active_execution(host, root_task_id="task-1", graph_task_ids={"task-1"}, reason=TASK_STOPPED_REASON)
    stop_seconds = time.monotonic() - started

    assert not process_alive(observed["pids"][0])
    assert stop_seconds < 6
    assert turn_stop.reason == TASK_STOPPED_REASON and turn_stop.running() == ()
    with pytest.raises(asyncio.CancelledError):
        await running
    # The owner learned of the stop only after the command had ended.
    assert observed["pids_alive_when_owner_resumed"] == [False]
    assert not host.is_executing
    assert host._active_tool_stop is None


@pytest.mark.asyncio
async def test_side_by_side_tool_calls_all_stop(work_dir: Path) -> None:
    pid_files = [work_dir / "first.pid", work_dir / "second.pid"]
    host = make_host(shell_agent([long_command(path) for path in pid_files], work_dir))
    observed: dict = {"pids": []}
    running = await start_owner(host, observed)
    observed["pids"] = [await wait_for_pid(path) for path in pid_files]
    assert len(host._active_tool_stop.running()) == 2

    await _interrupt_active_execution(host, root_task_id="task-1", graph_task_ids={"task-1"}, reason=TASK_STOPPED_REASON)

    assert not any(process_alive(pid) for pid in observed["pids"])
    with pytest.raises(asyncio.CancelledError):
        await running
    assert observed["pids_alive_when_owner_resumed"] == [False, False]


@pytest.mark.asyncio
async def test_cancelling_the_owner_at_shutdown_ends_the_running_command(work_dir: Path) -> None:
    """Shutdown cancels the turn's owner (close_pending_work, transport close)."""

    pid_file = work_dir / "cmd.pid"
    host = make_host(shell_agent([long_command(pid_file)], work_dir))
    observed: dict = {"pids": []}
    running = await start_owner(host, observed)
    observed["pids"].append(await wait_for_pid(pid_file))

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 8)

    assert not process_alive(observed["pids"][0])
    assert not host.is_executing


class HeldModel(Model):
    """A model call that waits until released, so a stop lands between tool calls."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "held"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("No structured output expected")
        yield

    async def stream(self, *args, **kwargs):
        self.entered.set()
        await asyncio.sleep(3600)
        yield {"messageStart": {"role": "assistant"}}


@pytest.mark.asyncio
async def test_a_stop_during_the_model_call_does_not_wait() -> None:
    model = HeldModel()
    host = make_host(Agent(model=model, callback_handler=None))
    running = asyncio.create_task(host.process_llm_interaction(task(), MessageHeader(), MessageMeta()))
    await asyncio.wait_for(model.entered.wait(), 5)

    started = time.monotonic()
    await _interrupt_active_execution(host, root_task_id="task-1", graph_task_ids={"task-1"}, reason=TASK_STOPPED_REASON)

    assert time.monotonic() - started < 1
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
async def test_a_tool_call_that_outlives_the_grace_period_is_logged(monkeypatch, caplog) -> None:
    monkeypatch.setattr(turn_tool_stop, "TOOL_STOP_GRACE_SECONDS", 0.3)
    caplog.set_level(logging.INFO, logger=LOGGER.name)
    entered = threading.Event()

    @tool(context=True)
    def stubborn(tool_context: ToolContext) -> str:
        """A tool call that ignores its stop, like a slow HTTP request."""
        with tracked_tool_call(tool_context, "stubborn"):
            entered.set()
            time.sleep(1.5)
        return "late"

    class CallsStubborn(Model):
        calls = 0

        def update_config(self, **config):
            pass

        def get_config(self):
            return {"model_id": "stubborn"}

        async def structured_output(self, *args, **kwargs):
            raise AssertionError
            yield

        async def stream(self, *args, **kwargs):
            CallsStubborn.calls += 1
            yield {"messageStart": {"role": "assistant"}}
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "use-7", "name": "stubborn"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}

    host = make_host(Agent(model=CallsStubborn(), tools=[stubborn], callback_handler=None))
    running = asyncio.create_task(host.process_llm_interaction(task(), MessageHeader(), MessageMeta()))
    await asyncio.to_thread(entered.wait, 5)

    await _interrupt_active_execution(host, root_task_id="task-1", graph_task_ids={"task-1"}, reason=TASK_STOPPED_REASON)
    with pytest.raises(asyncio.CancelledError):
        await running

    assert "event=tool_work_outlived_turn agent_id=agent task_id=task-1 reason=task_stopped" in caplog.text
    assert "tool_calls=stubborn:use-7:" in caplog.text
    await asyncio.sleep(1.5)  # let the stubborn thread finish before the loop closes


@pytest.mark.asyncio
async def test_an_invocation_that_fails_while_a_tool_call_runs_stops_it_first() -> None:
    stop = ToolStop(agent_id="agent", task_id="task-1")
    saw_stop = threading.Event()

    def tool_thread(event: threading.Event) -> None:
        with stop.tool_call("shell", "use-1"):
            if event.wait(5):
                saw_stop.set()

    class BrokenModel:
        async def invoke_async(self, prompt, **kwargs):
            threading.Thread(target=tool_thread, args=(kwargs["cancel_signal"],)).start()
            while not stop.running():
                await asyncio.sleep(0.01)
            raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        await invoke_with_tool_stop(BrokenModel(), "go", tool_stop=stop, invoke_kwargs={}, logger=LOGGER)

    assert saw_stop.is_set()
    assert stop.running() == ()
    assert stop.reason == "invocation_failed:RuntimeError"


@pytest.mark.asyncio
async def test_a_turn_whose_commands_were_stopped_is_not_retried_inline() -> None:
    stop = ToolStop(agent_id="agent", task_id="task-1")
    stop.request("invocation_failed:RuntimeError")
    error = RuntimeError("provider failed")

    with pytest.raises(RuntimeError) as raised:
        await LLMTurnExecutor._recover_invocation(SimpleNamespace(), SimpleNamespace(tool_stop=stop), error)

    assert raised.value is error


def test_a_resumed_turn_replaces_the_earlier_turns_stop_in_place() -> None:
    earlier, resumed = ToolStop(agent_id="a", task_id="t"), ToolStop(agent_id="a", task_id="t")
    state: dict = {"request_state": {"kept": True}}
    kwargs: dict = {"invocation_state": state}
    attach_tool_stop(kwargs, earlier)

    attach_tool_stop(kwargs, resumed)

    assert kwargs["cancel_signal"] is resumed.event
    assert kwargs["invocation_state"] is state
    assert state[TOOL_STOP_STATE_KEY] is resumed and state["request_state"] == {"kept": True}
