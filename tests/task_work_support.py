"""Bind the task a test's direct tool calls work for (``custom_tools.runtime_context.task_work``).

Production code binds a task's work record around the task's handling (its
model turn and the saving of its result); tools and hooks read their task
from it, never from agent-wide state. Tests that call tools or hooks directly
bind it here instead:

- ``bind_task(task_id)`` binds a task explicitly;
- ``set_agent_ref(agent)`` makes ``agent`` the agent of the following calls
  and binds the task the test gave it: its ``test_task_id``, else the task in
  its task manager's main slot (``current_task_id``). A test agent with
  neither leaves the binding as it is.

``conftest.isolate_task_work_between_tests`` clears the binding after every
test, so it never leaks into the next one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from custom_tools.runtime_context import TaskWork, _task_work, task_work
from custom_tools.runtime_context import set_agent_ref as _set_agent_ref


def bind_task(task_id: str | None) -> TaskWork:
    """Make ``task_id`` the task of this context's tool calls until the test ends or binds another."""

    work = TaskWork(task_id=task_id)
    _task_work.set(work)
    return work


def declared_task_id(agent: Any) -> str | None:
    """The task a test agent works on: its ``test_task_id``, else its task manager's main slot."""

    task_id = getattr(agent, "test_task_id", None)
    if task_id:
        return str(task_id)
    manager = getattr(agent, "task_manager", None)
    current = getattr(manager, "current_task_id", None) if manager is not None else None
    return str(current) if isinstance(current, str) and current else None


def set_agent_ref(agent: Any) -> None:
    """``runtime_context.set_agent_ref``, also binding the task the test agent works on (module docstring)."""

    _set_agent_ref(agent)
    if agent is None:
        return
    task_id = declared_task_id(agent)
    if task_id is not None:
        bind_task(task_id)


def use_agent(agent: Any, task_id: str | None) -> TaskWork:
    """Run the following tool calls as ``agent`` working on ``task_id``."""

    _set_agent_ref(agent)
    return bind_task(task_id)


def strands_parts(strands_agent: Any = None) -> Any:
    """Strands parts for a test's Strands agent (custom_agents.strands_parts)."""

    from custom_agents.strands_parts import StrandsParts
    from custom_agents.tool_loading import LoadedTools

    return StrandsParts(agent=strands_agent if strands_agent is not None else object(), loaded=LoadedTools(), sync=None)


def bind_turn(task_id: str | None, strands_agent: Any = None) -> TaskWork:
    """Bind ``task_id`` as a running turn's work, calling ``strands_agent`` (a placeholder by default)."""

    work = bind_task(task_id)
    work.strands = strands_parts(strands_agent)
    return work


class TurnHost(SimpleNamespace):
    """A test agent with the real turn bookkeeping of ``UniversalAgent`` and no model or transport.

    It keeps its running turns in ``_running_turns`` (by the asyncio task that
    runs each one), as the agent does.
    """

    def __init__(self, **attributes: Any) -> None:
        attributes.setdefault("_running_turns", {})
        super().__init__(**attributes)

    from custom_agents.strands_agent import UniversalAgent as _Agent

    _set_active_execution_context = _Agent._set_active_execution_context
    _clear_active_execution_context = _Agent._clear_active_execution_context
    _history_task_id_for_content = _Agent._history_task_id_for_content
    del _Agent

    def running_turn_works(self) -> list[TaskWork]:
        from custom_agents.turn_registry import running_turn_works

        return running_turn_works(self)

    def running_turn_task_ids(self) -> list[str]:
        from custom_agents.turn_registry import running_turn_task_ids

        return running_turn_task_ids(self)


def register_turn(host: Any, task_id: str, owner: Any, tool_stop: Any = None) -> TaskWork:
    """Record ``owner`` (an asyncio task) as the running turn of ``task_id`` on a test host."""

    work = TaskWork(task_id=task_id, turn_owner=owner, tool_stop=tool_stop)
    host._running_turns[owner] = work
    return work


@contextmanager
def acting_as(agent: Any) -> Iterator[TaskWork]:
    """Bind the task ``agent`` works on (``declared_task_id``) for the block only."""

    with task_work(declared_task_id(agent)) as work:
        yield work


def install_test_tool_sync(agent: Any, tools: list[Any]) -> Any:
    """Install the tool-list sync of a test agent's main Strands agent and keep its parts on the agent.

    Production builds the parts with the Strands agent
    (``custom_agents.strands_agent_builder.build_strands_parts``); a test without a Strands agent
    keeps them as its main parts, so ``tool_loading.loaded_tools(agent)`` and
    the hooks share them.
    """

    from custom_agents.tool_sync import install_tool_sync

    parts = getattr(agent, "_strands_parts", None)
    if parts is None:
        parts = strands_parts(None)
        parts.agent = None
        agent._strands_parts = parts
    parts.sync = install_tool_sync(agent, tools, parts)
    return parts.sync


def main_parts(agent: Any) -> Any:
    """A test agent's main Strands parts, made on first use (``install_test_tool_sync``)."""

    parts = getattr(agent, "_strands_parts", None)
    if parts is None:
        parts = strands_parts(None)
        parts.agent = None
        agent._strands_parts = parts
    return parts
