"""A Strands agent together with the tool state that belongs to it.

Why: every model turn calls a Strands agent that no other running turn calls.
Strands refuses a second call on one agent (``ConcurrencyException``) because
overlapping calls would interleave the conversation, and its documentation
recommends a separate agent per parallel task. What an agent's tool registry
shows also depends on state kept beside it, which therefore belongs to that
one agent:

- ``loaded``: the on-request groups loaded into its registry for the task it
  serves (``custom_agents.tool_loading.LoadedTools``);
- ``sync``: its tool-list sync, the one writer of the registry's catalog
  functions and MCP servers (``custom_agents.tool_sync``).

Two turns sharing them would unload each other's groups and write each
other's registries. A turn finds its parts through its work record
(``custom_tools.runtime_context.TaskWork.strands``); outside a turn the
agent's main parts serve (``UniversalAgent._strands_parts``).

``replace_before_next_turn`` marks parts whose Strands agent still holds an
interrupt that no session will resume (or that a session holds until its
resume); a new turn never starts on them
(``task_steering_runtime.ensure_fresh_strands_agent_for_new_turn``).

``generation`` is the agent's model generation the parts were built for
(``UniversalAgent._strands_generation``): a turn whose parts are older than the
agent's model rebuilds them before its next model call
(``custom_agents.turn_registry.pin_turn_strands``).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from custom_tools.runtime_context import current_task_work

if TYPE_CHECKING:
    from custom_agents.tool_loading import LoadedTools


@dataclass(eq=False)
class StrandsParts:
    """One Strands agent with its loaded groups and tool-list sync (module docstring)."""

    agent: Any
    loaded: LoadedTools
    sync: Any
    generation: int = 0
    replace_before_next_turn: bool = False


def current_parts(agent: Any) -> StrandsParts | None:
    """The parts of the turn bound in this context, else the agent's main parts (None without either)."""

    work = current_task_work()
    if work is not None and work.strands is not None:
        return work.strands
    main: StrandsParts | None = getattr(agent, "_strands_parts", None)
    return main


def turn_strands_agent(agent: Any) -> Any:
    """The Strands agent the turn bound in this context calls, else the agent's main one."""

    work = current_task_work()
    if work is not None and work.strands is not None:
        return work.strands.agent
    return getattr(agent, "_strands_agent", None)


def task_strands_messages(task_id: str) -> list[dict[str, Any]]:
    """Snapshot only the transcript this handling produced for ``task_id``.

    Background credential results, stops and watchdogs can finish a task
    without running its model. They have no fresh transcript to archive.
    Reading the main agent in that case would file another task's tool
    results in this task's conversation. A background callback may also
    inherit another task's work record, so both the id and parts must match.
    An expired record cannot supply messages: a timer may retain it after
    its SDK agent has been reused for the next main-lane task.
    """

    work = current_task_work()
    if work is None or work.handling_finished or work.task_id != task_id or work.strands is None:
        return []
    return deepcopy(list(getattr(work.strands.agent, "messages", []) or []))


__all__ = ["StrandsParts", "current_parts", "task_strands_messages", "turn_strands_agent"]
