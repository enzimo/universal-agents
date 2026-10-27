"""Run the team lead's parallel requests in execution lanes of their own.

Which lane a task belongs to is decided in ``custom_agents.execution_lanes``;
``runtime_hosting.agent_execution.AgentExecutionLanes`` lets one turn run at a
time per lane. This module runs the lanes:

- **The main drain** (``continue_main_lane``) takes only main-lane work: the
  answers that resume paused turns, waiting messages (worker results,
  answers, credential results), runtime mailbox events and queued tasks.
- **Parallel lanes.** A retained pass (``start_parallel_lanes``, started by
  ``pending_work_runtime.schedule_parallel_lanes`` on every wake-up) starts one
  worker per parallel lane with work waiting, up to ``MAX_PARALLEL_LANES``. A
  worker holds its lane and runs its request's work in the same order as the
  main drain until none is left. A lane's release, and the end of a worker
  that work arrived for while it ran, run the pass again, so neither a lane
  that waited for the limit nor work that arrived as a worker ended is lost.
  A lane whose waiting message failed is not started again before a backoff
  delay (``pending_work_runtime.retry_lane_after_failure``).
- **Each lane's Strands agent** (``strands_parts_for_lane``): a parallel lane
  builds its own, since Strands refuses two calls on one agent; the main lane
  keeps the agent's main one.

Kept outside ``UniversalAgent``, whose size is capped
(``tests/test_runtime_architecture_boundaries.py``). Lane state lives in one
``ParallelLanes`` record on the agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Optional

from custom_agents.execution_lanes import MAX_PARALLEL_LANES, is_parallel_lane, lane_for_task, lanes_for_messages
from custom_agents.pending_work_runtime import retry_lane_after_failure, schedule_parallel_lanes, start_owned_task
from custom_agents.strands_parts import StrandsParts
from custom_agents.task_queue_safety import queued_parallel_lanes
from runtime_hosting.agent_execution import MAIN_LANE


@dataclass
class ParallelLanes:
    """An agent's parallel lanes: their Strands agents, workers and retry state (module docstring)."""

    # Each lane's own Strands agent while its worker runs.
    parts: dict[str, StrandsParts] = field(default_factory=dict)
    workers: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    # A failing lane is not started again before this event-loop time.
    retry_after: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    # Lanes whose work arrived while their worker still ran; its end looks again.
    wakes: set[str] = field(default_factory=set)
    # The retained pass that starts workers, and whether a wake-up waits for it.
    task: Optional[asyncio.Task[Any]] = None
    requested: bool = False


async def lane_of(agent: Any, task_id: Optional[str]) -> str:
    """The execution lane of ``task_id``, read on the task manager's operations lane."""

    # Bind the manager: the operations lane deep-copies call arguments.
    return await agent.task_manager.operations_runtime.run(partial(lane_for_task, agent.task_manager), task_id)


async def messages_in_lane(agent: Any, lane: str) -> set[str]:
    """The ids of waiting messages the given lane handles."""

    contents = [
        (envelope.msg_header.message_id, envelope.msg_content) for envelope in agent.deferred_inbox.messages
    ]
    if not contents:
        return set()
    lanes = await agent.task_manager.operations_runtime.run(partial(lanes_for_messages, agent.task_manager), contents)
    return {message_id for message_id, message_lane in lanes.items() if message_lane == lane}



async def interrupt_responses_in_lane(agent: Any, lane: str) -> list[str]:
    """Tasks of the given lane whose paused turns can resume now (an answer arrived)."""

    return [task_id for task_id in list(agent._pending_interrupt_responses) if await lane_of(agent, task_id) == lane]


async def mailbox_roots_in_lane(agent: Any, lane: str) -> list[str]:
    """Root tasks of the given lane with runtime mailbox events waiting."""

    coordinator = agent._runtime_coordinator()
    roots = await agent.task_manager.operations_runtime.run(coordinator.runtime.list_deferred_runtime_event_root_ids)
    return [root_task_id for root_task_id in roots if await lane_of(agent, root_task_id) == lane]


async def drain_lane_messages(agent: Any, lane: str, *, available: Callable[[], bool]) -> Optional[bool]:
    """Handle the lane's waiting messages: None when it has none, else whether the pass finished."""

    if not agent.deferred_inbox:
        return None
    waiting = await messages_in_lane(agent, lane)
    if not waiting:
        return None
    result: bool = await agent.deferred_inbox.drain(
        dispatch=agent._dispatch_deferred_envelope,
        available=available,
        select=lambda envelope: envelope.msg_header.message_id in waiting,
    )
    return result


async def drain_lane_mailbox(agent: Any, lane: str) -> bool:
    """Handle runtime mailbox events of the lane's roots; whether any were handled."""

    for root_task_id in await mailbox_roots_in_lane(agent, lane):
        if await agent._runtime_coordinator().drain_deferred_events(root_task_id):
            return True
    return False


async def continue_main_lane(agent: Any) -> bool:
    """One pass of the main drain (``UniversalAgent._continue_pending_work``).

    Returns False when a waiting message failed and was kept, so the drain
    stops instead of retrying it at once. Work of parallel requests is left to
    their own lanes.
    """

    while agent._can_continue_pending_work():
        resumable = await interrupt_responses_in_lane(agent, MAIN_LANE)
        if resumable:
            await agent._resume_interrupted_task_turn(resumable[0])
            continue
        drained = await drain_lane_messages(agent, MAIN_LANE, available=agent._can_continue_pending_work)
        if drained is False:
            return False
        if drained:
            continue
        if await agent._drain_due_turn_action_outbox():
            continue
        if await drain_lane_mailbox(agent, MAIN_LANE):
            continue
        if await agent._check_and_process_next_task():
            continue
        break
    return True


async def parallel_lanes_with_work(agent: Any) -> list[str]:
    """Parallel lanes with work waiting: queued tasks, answers, results and mailbox events."""

    run = agent.task_manager.operations_runtime.run
    lanes: list[str] = list(await run(partial(queued_parallel_lanes, agent.task_manager)))

    def add(lane: str) -> None:
        if is_parallel_lane(lane) and lane not in lanes:
            lanes.append(lane)

    for task_id in list(agent._pending_interrupt_responses):
        add(await lane_of(agent, task_id))
    if agent.deferred_inbox:
        contents = [
            (envelope.msg_header.message_id, envelope.msg_content) for envelope in agent.deferred_inbox.messages
        ]
        for lane in (await run(partial(lanes_for_messages, agent.task_manager), contents)).values():
            add(lane)
    coordinator = agent._runtime_coordinator()
    for root_task_id in await run(coordinator.runtime.list_deferred_runtime_event_root_ids):
        # Events wait while their request's action batch holds its lease.
        if await run(coordinator.runtime.get_active_runtime_lease, root_task_id) is None:
            add(await lane_of(agent, root_task_id))
    return lanes


async def start_parallel_lanes(agent: Any) -> None:
    """Start a worker for each parallel lane with work waiting, up to the limit (module docstring)."""

    if not agent.task_manager.parallel_lanes_enabled or agent.paused or agent._should_exit.is_set():
        return
    state: ParallelLanes = agent._parallel_lanes
    now = asyncio.get_running_loop().time()
    for lane in await parallel_lanes_with_work(agent):
        worker = state.workers.get(lane)
        if worker is not None and not worker.done():
            # The running worker may have just found nothing left: its end looks again.
            state.wakes.add(lane)
            continue
        if state.retry_after.get(lane, 0.0) > now:
            continue
        if sum(1 for task in state.workers.values() if not task.done()) >= MAX_PARALLEL_LANES:
            agent._logger.info(
                "A parallel request waits: the most parallel lanes run already. "
                "event=parallel_lane_limit_reached agent_id=%s lane=%s limit=%s",
                agent.id,
                lane,
                MAX_PARALLEL_LANES,
            )
            return
        task = start_owned_task(agent, partial(run_parallel_lane, agent, lane), name=f"{agent.id}:{lane}")
        if task is not None:
            state.workers[lane] = task
            task.add_done_callback(partial(forget_lane_worker, agent, lane))


def forget_lane_worker(agent: Any, lane: str, task: asyncio.Task[Any]) -> None:
    """A lane's worker ended; look again when work arrived for the lane while it ran.

    Such work can arrive after the worker found nothing left and before it
    ended (it still reports the agent's state); the pass that work started saw
    the worker running and left the lane to it.
    """

    state: ParallelLanes = agent._parallel_lanes
    if state.workers.get(lane) is task:
        del state.workers[lane]
    if lane in state.wakes:
        state.wakes.discard(lane)
        schedule_parallel_lanes(agent)


async def run_parallel_lane(agent: Any, lane: str) -> None:
    """Run one parallel request's waiting work, one turn at a time, beside the main lane."""

    try:
        async with agent._execution_scope(lane):
            agent._logger.info(
                "A parallel request's lane started. event=parallel_lane_started agent_id=%s lane=%s",
                agent.id,
                lane,
            )
            while not (agent.paused or agent._should_exit.is_set()):
                if not await run_lane_step(agent, lane):
                    break
    finally:
        # Its Strands agent goes with it; an interrupted session keeps its own.
        agent._parallel_lanes.parts.pop(lane, None)


async def run_lane_step(agent: Any, lane: str) -> bool:
    """Do the lane's next piece of work in the order the main drain uses; False when none is left."""

    resumable = await interrupt_responses_in_lane(agent, lane)
    if resumable:
        await agent._resume_interrupted_task_turn(resumable[0])
        return True
    drained = await drain_lane_messages(
        agent, lane, available=lambda: not (agent.paused or agent._should_exit.is_set())
    )
    if drained is False:
        retry_lane_after_failure(agent, lane)
        return False
    if drained:
        agent._parallel_lanes.failures.pop(lane, None)
        return True
    if await drain_lane_mailbox(agent, lane):
        return True
    started: bool = await agent._check_and_process_next_task(lane)
    return started


def strands_parts_for_lane(agent: Any, lane: Optional[str]) -> StrandsParts:
    """The Strands agent a turn in ``lane`` calls: the agent's main one, or the parallel lane's own.

    A parallel lane builds its own when it has none, when the model was rebuilt
    since, or when its agent still holds an interrupt (a paused turn keeps it
    for its resume). It is dropped when the lane's worker ends
    (``run_parallel_lane``). The main agent is rotated at a turn's start
    instead (``task_steering_runtime.ensure_fresh_strands_agent_for_new_turn``).
    """

    main: StrandsParts = agent._strands_parts
    if lane is None or not is_parallel_lane(lane):
        return main
    state: ParallelLanes = agent._parallel_lanes
    parts = state.parts.get(lane)
    held = parts is not None and any(
        session.strands is parts for session in agent._interrupted_turn_sessions.values()
    )
    if parts is None or parts.generation != agent._strands_generation or parts.replace_before_next_turn or held:
        parts = agent._new_strands_parts()
        state.parts[lane] = parts
        agent._logger.info(
            "A parallel lane got its own Strands agent. event=parallel_lane_strands_agent_built agent_id=%s lane=%s",
            agent.id,
            lane,
        )
    return parts


__all__ = [
    "ParallelLanes",
    "continue_main_lane",
    "drain_lane_mailbox",
    "drain_lane_messages",
    "forget_lane_worker",
    "interrupt_responses_in_lane",
    "lane_of",
    "mailbox_roots_in_lane",
    "messages_in_lane",
    "parallel_lanes_with_work",
    "run_lane_step",
    "run_parallel_lane",
    "start_parallel_lanes",
    "strands_parts_for_lane",
]
