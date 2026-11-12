"""The model turns an agent runs now, each with its own work record.

Why: an agent may run several model turns at once (threads started with Launch
parallel thread). Each turn records itself in its task's work record
(``custom_tools.runtime_context.TaskWork``), which the runner bound around the
task's handling, and in the agent's ``_running_turns``, keyed by the asyncio
task that runs the turn. A stop, a custody release or a lease-loss handback
finds the turns of its task graph there; status reports read the running set.
A turn's own code (tools, hooks, post-turn steps) never reads an agent-wide
"current task"; it reads its bound work record. The task manager's main slot
(``TaskManager.current_task_id``) remains, but it names only the main lane's
task: ``executing_task_ids`` reads it beside the parallel lanes and running
turns to report everything executing now.

A sync tool that needs an asynchronous call for its turn, such as a model
call, runs it on the turn's own event loop with ``run_on_turn_loop``. Strands
runs sync tools in a worker thread, and a sync Strands call made there runs on
a fresh event loop in yet another thread, where model clients that keep
connections bound to the loop that opened them (the native Anthropic client)
fail or retry. On the turn's loop the call uses the turn's own model object
and its per-call key binding, and it stops when the turn stops. While it runs,
the waiting tool thread counts as one of the turn's running tool calls
(``custom_tools.tool_stop``), so a stopped turn finishes only after the call
was cancelled and the thread returned. A sync tool that is not otherwise
tracked, such as skill staging, gets that wait for this call only.

Kept outside ``UniversalAgent``, whose size is capped
(``tests/test_runtime_architecture_boundaries.py``); the agent keeps thin
``_set_active_execution_context`` and ``_clear_active_execution_context``
methods for the turn executor and the resumed-turn path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any, Generic, TypeVar, cast

from custom_agents.pending_work_runtime import AgentStopping, run_as_owned_task
from custom_tools.runtime_context import TaskWork, current_task_work
from custom_tools.tool_stop import ToolStop

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How often a tool thread waiting for work on its turn's loop checks that the
# turn still runs. A stop therefore reaches the work within this time.
TURN_POLL_SECONDS = 0.25

# How long a tool thread waits for work it cancelled to end on the turn's loop.
# The thread counts as a running tool call of its turn while it waits
# (run_on_turn_loop), and a stopped turn waits for its tool calls up to
# tool_stop.TOOL_STOP_GRACE_SECONDS (10 s). One poll plus this grace stays below
# that, so the stopped turn sees the thread return even when the work ignores
# its cancellation; that case is logged here (turn_work_cancel_unconfirmed).
TURN_WORK_CANCEL_GRACE_SECONDS = 5.0


class TurnEnded(RuntimeError):
    """The bound task's model turn ended or was asked to stop (or none runs), so its work did not run to the end."""


def start_turn(agent: Any, task_id: str | None) -> ToolStop:
    """Start the model turn of the bound task's work; returns the turn's new ToolStop.

    Runs in the asyncio task that runs the model call. The runner bound the
    task's work record (``runtime_context.task_work``) for the task's whole
    handling; the turn records itself there and in ``agent._running_turns``.
    """

    work = current_task_work()
    if work is None or work.task_id != task_id:
        raise RuntimeError(
            f"The turn of task {task_id} started without its task's work record "
            f"(bound: {getattr(work, 'task_id', None)}); wrap its handling in runtime_context.task_work."
        )
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("A model turn needs an owning asyncio task.")
    work.turn_owner = owner
    work.tool_stop = ToolStop(agent_id=agent.id, task_id=task_id)
    agent._running_turns[owner] = work
    return work.tool_stop


def end_turn(agent: Any) -> None:
    """End the model turn running in this asyncio task; its work record stays with the runner."""

    from custom_agents.person_model_runtime import restore_team_model

    work = agent._running_turns.pop(asyncio.current_task(), None)
    if work is not None:
        # The turn's person model goes with it (custom_agents.person_model_runtime).
        restore_team_model(agent, work)


def running_turn_works(agent: Any) -> list[TaskWork]:
    """The work records of the agent's model turns running now."""

    return list(agent._running_turns.values())


def running_turn_task_ids(agent: Any) -> list[str]:
    """The tasks whose model turns run now."""

    return sorted(work.task_id for work in agent._running_turns.values() if work.task_id)


def executing_task_ids(agent: Any) -> set[str]:
    """The tasks executing now: the main slot's, parallel requests whose lanes are held, and running turns."""

    from custom_agents.execution_lanes import root_of_lane

    current = agent.task_manager.current_task_id
    parallel_roots = {
        root for lane in agent._execution_lanes.held_lanes() if (root := root_of_lane(lane)) is not None
    }
    return set(running_turn_task_ids(agent)) | parallel_roots | ({current} if current else set())


def turn_has_ended(agent: Any, work: TaskWork | None) -> bool:
    """Whether ``work`` has no running model turn, or its turn was asked to stop.

    True with no work record, before its turn started, after the runner left
    its scope, once the turn's asyncio task is done, once ``end_turn`` removed
    the turn (it puts the team model back before the runner finishes, so the
    turn's person model is gone from then on), and once the turn's
    ``ToolStop`` was requested. It only reads, so a tool's worker thread may
    call it.
    """

    if work is None or work.handling_finished:
        return True
    owner = work.turn_owner
    if owner is None or owner.done() or agent._running_turns.get(owner) is not work:
        return True
    return work.tool_stop is not None and work.tool_stop.is_requested()


class _TurnLoopCall(Generic[T]):
    """One coroutine a tool thread runs on its turn's loop (``run_on_turn_loop``).

    ``start`` and ``cancel`` run on the loop; ``call_soon_threadsafe`` keeps
    their order, so ``cancel`` always finds the task. The done callback runs
    even for a task cancelled before its first step, so the tool thread always
    learns when the work ended and an unstarted coroutine is closed. (With
    ``asyncio.run_coroutine_threadsafe`` an early cancel stops the task before
    its first step, and a wrapper's own cleanup never runs.)
    """

    def __init__(self, agent: Any, coroutine: Coroutine[Any, Any, T], name: str) -> None:
        self._agent = agent
        self._coroutine = coroutine
        self._name = name
        self._task: asyncio.Task[T] | None = None
        self.ended = threading.Event()
        self.result: T | None = None
        self.error: BaseException | None = None
        self.cancelled = False

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(
            run_as_owned_task(self._agent, self._coroutine, name=self._name), name=self._name
        )
        self._task.add_done_callback(self._finish)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _finish(self, task: asyncio.Task[T]) -> None:
        # No-op once the coroutine ran; closes it when its task never started.
        self._coroutine.close()
        if task.cancelled():
            self.cancelled = True
        elif (error := task.exception()) is not None:
            self.error = error
        else:
            self.result = task.result()
        self.ended.set()


def _refuse_turn_work(coroutine: Coroutine[Any, Any, Any], error: Exception) -> Exception:
    coroutine.close()
    return error


def run_on_turn_loop(agent: Any, coroutine: Coroutine[Any, Any, T], *, name: str) -> T:
    """Run ``coroutine`` on the bound turn's event loop from a sync tool's worker thread; return its result.

    Why: see the module docstring. The coroutine keeps this thread's context
    (``call_soon_threadsafe`` copies it): the task's work record, the agent
    and the signed task context the model key lookup reads.

    It runs as owned work (``pending_work_runtime.run_as_owned_task``): refused
    once the agent is stopping, cancelled and joined at shutdown. While it runs
    this thread counts as one of the turn's running tool calls, named ``name``
    (``ToolStop.tool_call``), so a stopped turn waits for it. It checks every
    ``TURN_POLL_SECONDS`` that the turn still runs (``turn_has_ended``). Once
    the turn ended or was asked to stop, the coroutine is cancelled on its
    loop, this thread waits up to ``TURN_WORK_CANCEL_GRACE_SECONDS`` for it to
    end, and ``TurnEnded`` is raised. The coroutine's own exceptions, timeouts
    included, reach the caller unchanged; it bounds its own running time.

    Raises ``TurnEnded``, closing the coroutine unrun, when no turn runs for
    the bound work, its stop was requested or its loop is not running, and
    ``RuntimeError`` on an event loop thread, where waiting would block the
    loop the coroutine needs.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise _refuse_turn_work(
            coroutine, RuntimeError(f"{name} must start from a tool's worker thread, not an event loop thread.")
        )
    work = current_task_work()
    # start_turn sets the owner and the ToolStop together; a record without both has no turn.
    if work is None or work.turn_owner is None or work.tool_stop is None or turn_has_ended(agent, work):
        raise _refuse_turn_work(coroutine, TurnEnded(f"The model turn ended, so {name} was not started."))
    # tool_call and ToolStop.request take the same lock: this call is counted
    # before a stop (and the stopped turn waits for it) or sees the stop here.
    with work.tool_stop.tool_call(name, ""):
        if work.tool_stop.is_requested():
            raise _refuse_turn_work(coroutine, TurnEnded(f"The model turn was stopped, so {name} was not started."))
        return _run_counted(agent, work, work.turn_owner.get_loop(), coroutine, name)


def _run_counted(
    agent: Any, work: TaskWork, loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, T], name: str
) -> T:
    """Start ``coroutine`` on the turn's ``loop`` and wait for it (``run_on_turn_loop``, inside its tool call)."""

    call = _TurnLoopCall(agent, coroutine, name)
    try:
        if not loop.is_running():
            raise RuntimeError("the turn's event loop is not running")
        loop.call_soon_threadsafe(call.start)
    except RuntimeError as exc:  # also a loop closed after the check
        raise _refuse_turn_work(coroutine, TurnEnded(f"The model turn ended, so {name} was not started.")) from exc
    while not call.ended.wait(TURN_POLL_SECONDS):
        if turn_has_ended(agent, work) or not loop.is_running():
            _cancel_turn_work(agent, work, loop, call, name)
            raise TurnEnded(f"The model turn ended or was stopped, so {name} was cancelled.")
    if call.cancelled:
        raise TurnEnded(f"{name} was cancelled, as its turn or the agent is stopping.")
    if isinstance(call.error, AgentStopping):
        raise TurnEnded(f"The agent is stopping, so {name} was not started.") from call.error
    if call.error is not None:
        raise call.error
    return cast(T, call.result)


def _cancel_turn_work(
    agent: Any, work: TaskWork, loop: asyncio.AbstractEventLoop, call: _TurnLoopCall[Any], name: str
) -> None:
    """Cancel ``call`` on its loop and wait a bounded time for it to end (``run_on_turn_loop``)."""

    try:
        loop.call_soon_threadsafe(call.cancel)
    except RuntimeError:  # the loop closed; nothing runs on it any more
        return
    if loop.is_running() and call.ended.wait(TURN_WORK_CANCEL_GRACE_SECONDS):
        return
    logger.warning(
        "Work for a stopped model turn did not end within its grace period after it was cancelled. "
        "event=turn_work_cancel_unconfirmed agent_id=%s task_id=%s work_name=%s grace_seconds=%s loop_running=%s",
        getattr(agent, "id", None),
        work.task_id,
        name,
        TURN_WORK_CANCEL_GRACE_SECONDS,
        loop.is_running(),
    )


def pin_turn_strands(agent: Any) -> Any:
    """The Strands parts the bound turn calls, taking new ones after a model rebuild.

    A turn keeps its Strands agent for its whole conversation. It takes a new
    one only here, where its conversation starts afresh
    (``UniversalAgent._reset_llm_conversation``), when the agent's model was
    rebuilt since its own was made (``StrandsParts.generation``). A rebuild made
    by another turn therefore never swaps the agent under a running call.
    Outside a turn the main parts serve.
    """

    work = current_task_work()
    if work is None:
        return agent._strands_parts
    parts = work.strands
    if parts is None or parts.generation != agent._strands_generation:
        from custom_agents.execution_lanes_runtime import strands_parts_for_lane
        from runtime_hosting.agent_execution import current_lane

        work.strands = strands_parts_for_lane(agent, current_lane())
    return work.strands


__all__ = [
    "TURN_POLL_SECONDS",
    "TURN_WORK_CANCEL_GRACE_SECONDS",
    "TurnEnded",
    "end_turn",
    "executing_task_ids",
    "pin_turn_strands",
    "run_on_turn_loop",
    "running_turn_task_ids",
    "running_turn_works",
    "start_turn",
    "turn_has_ended",
]
