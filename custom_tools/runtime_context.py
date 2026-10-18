"""Process-local execution context shared by trusted tool providers.

The context identifies the agent executing the current Strands tool call. It is
deliberately process-local and carries no authorization authority: providers
still call their domain service and final policy-enforcement point. A future
remote runtime installs its own local context and ACP-backed service adapters.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent
    from custom_messages.contracts.routing_v1 import MessageMeta


_agent_ctx: ContextVar[UniversalAgent | None] = ContextVar("agent_ctx", default=None)
# The task a running tool call works on, fixed when the call started
# (bind_tool_call_task). Without it, a call still running when the agent moved
# on to its next task would read that task, and its person, for every later
# step, such as each further command of a shell command list.
_tool_call_task: ContextVar[str | None] = ContextVar("tool_call_task", default=None)


def set_agent_ref(agent: UniversalAgent | None) -> None:
    """Set or clear the agent for the current asynchronous or thread context."""
    _agent_ctx.set(agent)


def get_agent_ref() -> UniversalAgent | None:
    """Return the agent executing the current tool invocation, if configured."""
    return _agent_ctx.get()


@contextmanager
def bind_tool_call_task(task_id: str | None) -> Iterator[None]:
    """Make ``task_id`` the active task for the rest of this tool call (this context).

    Threads the call starts with a copy of this context, such as the parallel
    shell's pool threads, keep the same task.
    """

    token = _tool_call_task.set(task_id)
    try:
        yield
    finally:
        _tool_call_task.reset(token)


def caller_invocation_state(invocation_state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The invocation_state a turn passed to Strands, as Strands hands it to tools and hooks.

    Strands passes the caller's dict on as it is, unless the invocation also
    got extra keyword arguments. Then (its deprecated ``**kwargs`` path in
    ``Agent.stream_async``) tools and hooks get
    ``{**kwargs, "invocation_state": <the caller's dict>}``, with the caller's
    values one level down. Every turn passes ``callback_handler`` that way
    (custom_agents.sdk.strands.turn), so read the caller's values through
    this, never from the top level alone.
    """

    state = invocation_state or {}
    nested = state.get("invocation_state")
    return nested if isinstance(nested, Mapping) else state


def active_task_context(
    agent: UniversalAgent,
) -> tuple[str | None, MessageMeta | None]:
    """Resolve the active task id and its structured request metadata.

    Inside a tool call the task bound when the call started wins over the
    agent's current one (bind_tool_call_task).
    """
    task_id = (
        _tool_call_task.get()
        or getattr(agent, "_active_execution_task_id", None)
        or getattr(getattr(agent, "task_manager", None), "current_task_id", None)
    )
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
