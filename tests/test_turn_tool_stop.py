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
import os
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
    turn_invoke_kwargs,
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
                host._strands_agent,
                "go",
                tool_stop=tool_stop,
                invoke_kwargs=turn_invoke_kwargs(content.task_id),
                logger=LOGGER,
            )
            return Task(task_id=content.task_id, task_type=MessageMetaType.RESPONSE, answer="done")
        finally:
            UniversalAgent._clear_active_execution_context(host)

    host.turn_executor = SimpleNamespace(process_llm_interaction=turn)
    return host


def task(task_id: str = "task-1") -> Task:
    return Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="run it")


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands_without_agent):
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
        await invoke_with_tool_stop(BrokenModel(), "go", tool_stop=stop, invoke_kwargs=turn_invoke_kwargs(), logger=LOGGER)

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


@pytest.mark.asyncio
async def test_a_stop_ends_a_command_of_a_turn_run_by_the_production_turn_executor(work_dir: Path, caplog) -> None:
    """The invocation keywords come from the real LLMTurnExecutor._prepare_invocation.

    They include the per-turn callback_handler, which makes Strands hand the
    tool the turn's invocation_state one level down. The first version of the
    fix read only the top level, found no stop on every real turn, and left
    the command running.
    """

    from collections import deque
    from dataclasses import replace

    from tests.test_runtime_owner_boundaries import build_turn_owner

    caplog.set_level(logging.WARNING)
    pid_file = work_dir / "cmd.pid"
    sdk = shell_agent([long_command(pid_file)], work_dir)
    production_kwargs: dict = {}

    async def invoke(prompt, **kwargs):
        production_kwargs.update(kwargs)
        return await sdk.invoke_async(prompt, **kwargs)

    executor, _ = build_turn_owner(invoke=invoke, guidance=deque([""]), committed=[], cleared=[])
    stops: list[ToolStop] = []

    def set_active_execution_context(task_id):
        stops.append(ToolStop(agent_id="independent", task_id=task_id))
        return stops[-1]

    executor.effects = replace(executor.effects, set_active_execution_context=set_active_execution_context)
    turn_task = Task(task_id="production-turn", task_type=MessageMetaType.REQUEST, problem_statement="run it")
    running = asyncio.create_task(executor.process_llm_interaction(turn_task, MessageHeader(), MessageMeta()))
    pid = await wait_for_pid(pid_file)
    [stop] = stops

    assert {"callback_handler", "cancel_signal", "invocation_state", "structured_output_model", "structured_output_prompt"} <= set(production_kwargs)
    assert production_kwargs["cancel_signal"] is stop.event
    assert [call.tool_name for call in stop.running()] == ["shell"]
    host = SimpleNamespace(
        id="independent",
        _active_execution_task=running,
        _active_execution_task_id=turn_task.task_id,
        _active_tool_stop=stop,
        _logger=LOGGER,
    )
    await _interrupt_active_execution(
        host, root_task_id=turn_task.task_id, graph_task_ids={turn_task.task_id}, reason=TASK_STOPPED_REASON
    )

    assert not process_alive(pid)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert "event=tool_stop_missing" not in caplog.text


@pytest.mark.asyncio
async def test_a_turn_that_ends_normally_leaves_its_background_processes_running(work_dir: Path) -> None:
    """Only a stop ends what a command left running; normal completion is unchanged."""

    pid_file = work_dir / "bg.pid"
    command = f"sh -c 'echo $$ > {pid_file.name}; exec sleep 60' > /dev/null 2>&1 & sleep 0.3; echo started"
    stop = ToolStop(agent_id="agent", task_id="task-1")
    kwargs = turn_invoke_kwargs()
    # Without the structured-output keywords the scripted model's text reply ends the turn normally.
    kwargs.pop("structured_output_model")
    kwargs.pop("structured_output_prompt")

    await invoke_with_tool_stop(shell_agent([command], work_dir), "go", tool_stop=stop, invoke_kwargs=kwargs, logger=LOGGER)
    pid = await wait_for_pid(pid_file)
    await asyncio.sleep(0.5)

    assert process_alive(pid)
    assert not stop.is_requested()


BACKGROUND = "sh -c 'echo $$ > {pid}; exec sleep 60' > /dev/null 2>&1 & sleep 0.3; echo started"


@pytest.mark.asyncio
async def test_a_stop_after_the_model_call_still_ends_background_processes(work_dir: Path) -> None:
    """A stop can land after the invocation returned, while the turn still runs.

    The command left a background process and returned, the invocation ended
    normally, and the turn is in its late-guidance check when the stop comes.
    The turn's leftovers must still end; at first they were ended only when
    the cancellation hit inside the model call.
    """

    from collections import deque
    from dataclasses import replace

    from tests.test_runtime_owner_boundaries import build_turn_owner

    pid_file = work_dir / "bg.pid"
    sdk = shell_agent([BACKGROUND.format(pid=pid_file.name)], work_dir)

    async def invoke(prompt, **kwargs):
        # Without structured output the scripted text reply ends the invocation normally.
        kwargs.pop("structured_output_model", None)
        kwargs.pop("structured_output_prompt", None)
        return await sdk.invoke_async(prompt, **kwargs)

    in_post_processing, hold = asyncio.Event(), asyncio.Event()
    guidance_calls: list[str] = []

    async def late_guidance(task_id):
        # Asked once while preparing the turn and again right after the invocation returned.
        guidance_calls.append(task_id)
        if len(guidance_calls) >= 2:
            in_post_processing.set()
            await hold.wait()
        return ""

    executor, _ = build_turn_owner(invoke=invoke, guidance=deque([""]), committed=[], cleared=[])
    stops: list[ToolStop] = []

    def set_active_execution_context(task_id):
        stops.append(ToolStop(agent_id="independent", task_id=task_id))
        return stops[-1]

    executor.effects = replace(
        executor.effects,
        set_active_execution_context=set_active_execution_context,
        consume_pending_proactive_guidance_message=late_guidance,
    )
    turn_task = Task(task_id="post-turn", task_type=MessageMetaType.REQUEST, problem_statement="run it")
    running = asyncio.create_task(executor.process_llm_interaction(turn_task, MessageHeader(), MessageMeta()))
    await asyncio.wait_for(in_post_processing.wait(), 30)
    pid = await wait_for_pid(pid_file)
    [stop] = stops
    assert stop.running() == ()  # the model call is over
    host = SimpleNamespace(
        id="independent",
        _active_execution_task=running,
        _active_execution_task_id=turn_task.task_id,
        _active_tool_stop=stop,
        _logger=LOGGER,
    )

    await _interrupt_active_execution(
        host, root_task_id=turn_task.task_id, graph_task_ids={turn_task.task_id}, reason=TASK_STOPPED_REASON
    )

    assert not process_alive(pid)
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
@pytest.mark.parametrize("during_error_handler", [False, True], ids=["applying_result", "settling_failure"])
async def test_a_resumed_turn_stopped_after_its_model_call_ends_background_processes(
    tmp_path: Path, monkeypatch, during_error_handler: bool
) -> None:
    """Resumed turns end leftovers on a stop, including while settling a failure."""

    import subprocess
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from custom_agents.task_manager import TaskManager
    from custom_agents.task_steering_runtime import StrandsInterruptedTurnSession, resume_interrupted_task_turn
    from custom_messages.contracts.task_v1 import TaskSteering, TaskSteeringInterruptRecord
    from custom_messages.enums import TaskSteeringType
    from custom_tools.tool_stop import TOOL_STOP_STATE_KEY

    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("manager", "Manager", "team", "TeamArchitect")
    task = Task(task_id="resumed", task_type=MessageMetaType.REQUEST, problem_statement="resume")
    manager.add_task(task, MessageHeader(from_agent_id="gateway"), MessageMeta(), owner_person_id=None)
    background: list[int] = []
    in_post_processing, hold = asyncio.Event(), asyncio.Event()

    async def invoke(prompt, **kwargs):
        # Stands in for a resumed Strands call whose command left a server running.
        record = kwargs["invocation_state"][TOOL_STOP_STATE_KEY].process_group_recorder("shell")
        leader = subprocess.Popen(
            ["sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], stdout=subprocess.PIPE, start_new_session=True
        )
        record(leader.pid)
        background.append(int(leader.stdout.readline()))
        leader.wait()
        return SimpleNamespace(stop_reason="end_turn")

    async def dispatch(**kwargs):
        if during_error_handler:
            raise ValueError("Invalid result after the resumed command returned")
        await wait_for_stop()

    async def wait_for_stop(*args, **kwargs):
        in_post_processing.set()
        await hold.wait()

    monkeypatch.setattr("custom_agents.task_steering_runtime.settle_failed_turn", wait_for_stop)

    now = datetime.now(timezone.utc).isoformat()
    session = StrandsInterruptedTurnSession(
        task.task_id, SimpleNamespace(invoke_async=invoke), {}, "prompt", task,
        MessageHeader(from_agent_id="gateway"), MessageMeta(), now,
        [TaskSteeringInterruptRecord(interrupt_id="interrupt_1", reason={"access_request_id": "areq_1"})],
    )
    steering = TaskSteering(
        steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
        target_task_id=task.task_id,
        interrupt_responses=[{"interrupt_id": "interrupt_1", "response": {"access_request_id": "areq_1", "status": "granted"}}],
    )
    stops: list[ToolStop] = []
    agent = SimpleNamespace(
        id="manager",
        name="Manager",
        task_manager=manager,
        _interrupted_turn_sessions={task.task_id: session},
        _pending_interrupt_responses={task.task_id: steering},
        _strands_agent=None,
        _strands_agent_replacement_required=False,
        _set_active_execution_context=lambda task_id: stops.append(ToolStop(agent_id="manager", task_id=task_id))
        or stops[-1],
        _clear_active_execution_context=lambda: None,
        _refresh_self_agent_state=AsyncMock(),
        _apply_task_processing_result=AsyncMock(),
        _execute_turn_decision_outbox=dispatch,
        _logger=LOGGER,
        turn_executor=SimpleNamespace(
            _apply_agent_result_transformer=lambda _, result: result,
            decode_result=lambda **_: SimpleNamespace(decision=None, structured_output_details=None),
        ),
    )
    resumed = asyncio.create_task(resume_interrupted_task_turn(agent, task.task_id))
    try:
        await asyncio.wait_for(in_post_processing.wait(), 10)
        [pid] = background
        assert process_alive(pid)

        resumed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await resumed

        assert not process_alive(pid)
        assert stops[0].reason == "turn_cancelled"
    finally:
        for leftover in background:
            if process_alive(leftover):
                os.kill(leftover, 9)
