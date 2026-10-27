"""Process-local execution context shared by trusted tool providers.

The context identifies the agent executing the current Strands tool call and
the task that call works for. It is deliberately process-local and carries no
authorization authority: providers still call their domain service and final
policy-enforcement point. A future remote runtime installs its own local
context and ACP-backed service adapters.

Why the task comes from a context variable (``TaskWork``): an agent can run
several model turns at once (threads started with Launch parallel thread,
``custom_agents.execution_lanes``). Each runner of a turn binds the task's work
record for the whole handling of that task, the model turn and the saving of
its result (``task_work``). Strands runs hooks in the turn's asyncio task and
starts tool calls with a copy of its context (``asyncio.create_task``,
``asyncio.to_thread``), so every hook, tool call and thread a call starts reads
its own task. An agent-wide "current task" would give a turn the task, person,
grants and model keys of whichever turn started last.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent
    from custom_agents.strands_parts import StrandsParts
    from custom_messages.contracts.routing_v1 import MessageMeta
    from custom_tools.tool_stop import ToolStop


_agent_ctx: ContextVar[UniversalAgent | None] = ContextVar("agent_ctx", default=None)


@dataclass
class TaskWork:
    """One task's handling by this agent: its model turn and the saving of its result.

    The runner creates it (``task_work``); the model turn fills in the rest
    when it starts (``custom_agents.turn_registry.start_turn``), and the
    runner reads them back afterwards, such as the turn's Strands messages.

    - ``turn_owner``: the asyncio task running the model call; a stop cancels it.
    - ``tool_stop``: the turn's stop for its tool commands (``custom_tools.tool_stop``).
    - ``strands``: the Strands agent the turn calls, with its own tool state
      (``custom_agents.strands_parts.StrandsParts``).
    - ``person_model``: the person's own model while the turn uses it
      (``custom_agents.person_model_runtime``).
    - ``context_assembly``: the turn's latest prompt assembly, for
      ``get_context_status``.
    - ``handling_finished``: the runner left its scope. Inherited timer
      callbacks may keep the record, but its SDK agent can have been reused
      by another task, so its transcript is no longer safe to archive.
    """

    task_id: str | None
    turn_owner: asyncio.Task[Any] | None = None
    tool_stop: ToolStop | None = None
    strands: StrandsParts | None = None
    person_model: Any = None
    context_assembly: Any = None
    handling_finished: bool = False


_task_work: ContextVar[TaskWork | None] = ContextVar("task_work", default=None)


def set_agent_ref(agent: UniversalAgent | None) -> None:
    """Set or clear the agent for the current asynchronous or thread context."""
    _agent_ctx.set(agent)


def get_agent_ref() -> UniversalAgent | None:
    """Return the agent executing the current tool invocation, if configured."""
    return _agent_ctx.get()


@contextmanager
def task_work(task_id: str | None) -> Iterator[TaskWork]:
    """Make ``task_id`` this context's task until the block ends; yields its work record.

    Asynchronous tasks and threads started inside the block, such as a model
    turn's child task and its tool calls, keep the same record.
    """

    work = TaskWork(task_id=task_id)
    token = _task_work.set(work)
    try:
        yield work
    finally:
        work.handling_finished = True
        _task_work.reset(token)


def current_task_work() -> TaskWork | None:
    """The work record bound in this context, or None outside a task's handling."""

    return _task_work.get()


def bound_task_id() -> str | None:
    """The task this context works for, or None outside a task's handling."""

    work = _task_work.get()
    return work.task_id if work is not None else None


def active_task_context(
    agent: UniversalAgent,
) -> tuple[str | None, MessageMeta | None]:
    """Resolve the bound task id and its structured request metadata (module docstring).

    Outside a task's handling there is no active task: such work is the
    agent's own (system work), never another turn's.
    """
    task_id = bound_task_id()
    if not task_id:
        return None, None
    task_manager: Any = getattr(agent, "task_manager", None)
    task_info = (
        task_manager.get_task_info(task_id)
        if task_manager is not None and hasattr(task_manager, "get_task_info")
        else None
    )
    return str(task_id), getattr(task_info, "meta", None)


def active_signed_task_context(agent: UniversalAgent) -> dict[str, Any] | None:
    """The active task's signed context, as JSON for another process.

    Enforcement points outside this process (the broker) trust who a task works
    for only from this signed value (``access_control.identity``).
    """

    _task_id, meta = active_task_context(agent)
    signed = getattr(getattr(meta, "gateway_context", None), "task_context", None)
    return signed.model_dump(mode="json") if signed is not None else None


def active_schedule_run(agent: UniversalAgent) -> dict[str, Any] | None:
    """The active task's schedule run identity (a schedule permit and the run's fields), as JSON.

    Set instead of a signed task context on a scheduled run started by an
    agent without the scheduler key; enforcement points check it with
    ``access_control.task_identity.resolve_task_identity``.
    """

    _task_id, meta = active_task_context(agent)
    run = getattr(getattr(meta, "gateway_context", None), "schedule_run", None)
    return run.model_dump(mode="json") if run is not None else None


def active_task_identity(agent: UniversalAgent) -> dict[str, Any]:
    """What proves whom the active task works for, for a broker request: ``task_context`` or ``schedule_run``."""

    identity = {"task_context": active_signed_task_context(agent), "schedule_run": active_schedule_run(agent)}
    return {key: value for key, value in identity.items() if value is not None}
