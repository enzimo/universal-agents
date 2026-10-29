"""A stop owns its cleanup through error handling and further cancellation."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_tools.runtime_context import task_work
from tests.task_work_support import TurnHost
from custom_agents.control_runtime import _interrupt_active_execution
from custom_agents.pending_work_runtime import close_pending_work
from custom_agents.sdk.strands.turn_tool_stop import TASK_STOPPED_REASON
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.tool_stop import TOOL_STOP_STATE_KEY
from tests.test_runtime_owner_boundaries import build_turn_owner
from tests.turn_tool_stop_support import process_alive

LOGGER = logging.getLogger(__name__)


@pytest.fixture
def background_command():
    """A completed command's remaining process, including SIGTERM resistance."""

    children = []

    def start(stop):
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "print('ready', flush=True); time.sleep(60)"],
            start_new_session=True,
            stdout=subprocess.PIPE,
        )
        children.append(child)
        assert child.stdout.readline() == b"ready\n"
        stop.process_group_recorder("shell")(child.pid)
        return child

    yield start
    for child in children:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        child.stdout.close()


def prepared_turn(background_command):
    """Use the production turn executor and active-execution bookkeeping."""

    host = TurnHost(
        id="independent", _logger=LOGGER, _should_exit=asyncio.Event(),
        _pending_work_task=None, _pending_work_timers=set(), _owned_tasks=set(),
    )
    children = []
    from custom_agents.strands_agent import UniversalAgent

    async def invoke(prompt, **kwargs):
        children.append(background_command(kwargs["invocation_state"][TOOL_STOP_STATE_KEY]))
        return SimpleNamespace()

    executor, _ = build_turn_owner(invoke=invoke, guidance=deque(["", ""]), committed=[], cleared=[])
    executor.effects = replace(
        executor.effects,
        set_active_execution_context=lambda task_id: UniversalAgent._set_active_execution_context(host, task_id),
        clear_active_execution_context=lambda: UniversalAgent._clear_active_execution_context(host),
    )
    return host, executor, children


async def stop(host):
    await _interrupt_active_execution(
        host, root_task_id="task", graph_task_ids={"task"}, reason=TASK_STOPPED_REASON
    )


def start_turn(executor):
    with task_work("task"):  # the turn's task copies this binding when it is created
        return asyncio.create_task(executor.process_llm_interaction(
            Task(task_id="task", task_type=MessageMetaType.REQUEST, problem_statement="run"),
            MessageHeader(), MessageMeta(),
        ))


def running_stop(host):
    [work] = host.running_turn_works()
    return work.tool_stop


@pytest.mark.asyncio
async def test_stop_during_error_handling_ends_the_turns_background_command(background_command):
    host, executor, children = prepared_turn(background_command)
    in_error_handler = asyncio.Event()

    async def complete(turn):
        raise ValueError("Invalid model result after the shell command returned")

    async def write_context(**kwargs):
        # The real handler awaits the context engine's post-invocation write.
        in_error_handler.set()
        await asyncio.Event().wait()

    executor._complete_turn = complete
    executor._write_context_debug_post_turn = write_context
    running = start_turn(executor)
    try:
        await asyncio.wait_for(in_error_handler.wait(), 5)
        assert running_stop(host).running() == ()

        await stop(host)

        assert not process_alive(children[0].pid)
        assert host.running_turn_works() == []
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_cancellation", ["stop", "shutdown"])
async def test_repeated_cancellation_waits_for_forced_termination(background_command, second_cancellation):
    host, executor, children = prepared_turn(background_command)
    in_post_processing, term_sent = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = []

    async def complete(turn):
        in_post_processing.set()
        await asyncio.Event().wait()

    executor._complete_turn = complete
    running = start_turn(executor)
    stopping = None
    try:
        await asyncio.wait_for(in_post_processing.wait(), 5)
        tool_stop = running_stop(host)
        signal_groups = tool_stop.signal_process_groups

        def record_signal(sig):
            result = signal_groups(sig)
            signals.append(sig)
            if sig == signal.SIGTERM:
                loop.call_soon_threadsafe(term_sent.set)
            return result

        tool_stop.signal_process_groups = record_signal
        stopping = asyncio.create_task(stop(host))
        await asyncio.wait_for(term_sent.wait(), 5)
        assert process_alive(children[0].pid)
        if second_cancellation == "shutdown":
            host._pending_work_task = running
            await close_pending_work(host)
        else:
            await stop(host)
        await stopping

        assert not process_alive(children[0].pid)
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert tool_stop.reason == TASK_STOPPED_REASON
        assert host.running_turn_works() == []
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
