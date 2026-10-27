"""The model turns an agent runs now, each with its own work record.

Why: an agent may run several model turns at once (threads started with Launch
parallel thread). Each turn records itself in its task's work record
(``custom_tools.runtime_context.TaskWork``), which the runner bound around the
task's handling, and in the agent's ``_running_turns``, keyed by the asyncio
task that runs the turn. A stop, a custody release or a lease-loss handback
finds the turns of its task graph there; status reports read the running set.
Nothing reads an agent-wide "current task" any more.

Kept outside ``UniversalAgent``, whose size is capped
(``tests/test_runtime_architecture_boundaries.py``); the agent keeps thin
``_set_active_execution_context`` and ``_clear_active_execution_context``
methods for the turn executor and the resumed-turn path.
"""

from __future__ import annotations

import asyncio
from typing import Any

from custom_tools.runtime_context import TaskWork, current_task_work
from custom_tools.tool_stop import ToolStop


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
    """The tasks executing now: the one holding the execution slot and those whose turns run."""

    current = agent.task_manager.current_task_id
    return set(running_turn_task_ids(agent)) | ({current} if current else set())


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
        work.strands = agent._strands_parts
    return work.strands


__all__ = [
    "end_turn",
    "executing_task_ids",
    "pin_turn_strands",
    "running_turn_task_ids",
    "running_turn_works",
    "start_turn",
]
