"""Process-local execution context shared by trusted tool providers.

The context identifies the agent executing the current Strands tool call. It is
deliberately process-local and carries no authorization authority: providers
still call their domain service and final policy-enforcement point. A future
remote runtime installs its own local context and ACP-backed service adapters.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent
    from custom_messages.contracts.routing_v1 import MessageMeta


_agent_ctx: ContextVar[UniversalAgent | None] = ContextVar("agent_ctx", default=None)


def set_agent_ref(agent: UniversalAgent | None) -> None:
    """Set or clear the agent for the current asynchronous or thread context."""
    _agent_ctx.set(agent)


def get_agent_ref() -> UniversalAgent | None:
    """Return the agent executing the current tool invocation, if configured."""
    return _agent_ctx.get()


def active_task_context(
    agent: UniversalAgent,
) -> tuple[str | None, MessageMeta | None]:
    """Resolve the active task id and its structured request metadata."""
    task_id = getattr(agent, "_active_execution_task_id", None) or getattr(
        getattr(agent, "task_manager", None),
        "current_task_id",
        None,
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
