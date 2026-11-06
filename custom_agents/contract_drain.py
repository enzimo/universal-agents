"""An agent whose process runs another message contract than its installation stops taking new work.

While the protocol label is a pre-release, hosts of one installation refuse a peer whose
contract digest differs (contract README "Versions"). The process's host
(``custom_agents.agent_host``) learns that its process runs another contract, from the team
services' host record or from ``protocol_mismatch``, and drains each agent it runs here, on
the agent's own loop. Queued task work still runs on today's queues in this phase, so
draining an agent means:

1. **No new work.** The agent leaves the queues every agent of a type shares: its type's
   queue and, for the team lead, the team's. Their messages stay there, unread by this
   agent, for another agent of the type (the team lead's worker pools start one when tasks
   wait) or for the next team lead; the team lead's process starts no pool or scheduled
   worker while it drains (``custom_agents.worker_pool_controller``,
   ``custom_agents.schedule_worker_controller``). Its own route, which carries the replies
   to what it holds, and its controls are still read.
2. **The turns in progress finish.** A running turn is not stopped.
3. **What it has not finished goes back where this release has a way.** A pool worker (one
   started with a launch record) starts no task it has not started yet and, once its
   running turns ended, winds down as after a lost launch lease
   (``custom_agents.lease_loss_wind_down``): the tasks it has not started go back to the
   team lead, which sends them out again on the type's queue; tasks that wait on a person
   or a subtask finish here until that wind-down's deadline, because their replies come to
   this agent; then the worker exits. Pool workers run in the team lead's process, so the
   lead drains with them: what they return waits in the lead's queue for a team lead on the
   installation's contract. The team lead and startup agents have no way to hand
   a task back in this phase (``task.handback`` comes with the queued work of protocol 1):
   they finish here what they already admitted and take nothing new.

``holds_work`` tells the host whether an agent still holds an unfinished task or a request
waiting to be admitted, so the host gives an agent up only once it holds nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any, Optional

# The consumer role of the team lead's own queue (``UniversalAgent._connect_nats``).
TEAM_LEAD_CONSUMER_ROLE = "team_lead"
# How often a pool worker checks whether its running turns ended before it winds down.
TURN_POLL_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)


@dataclass
class ContractDrain:
    """Why and since when an agent drains (module docstring)."""

    reason: str
    started_at: datetime
    # A pool worker starts no task it has not started (``lease_loss_wind_down.may_start_next_task``).
    holds_back_unstarted: bool = False
    left_queues: int = 0
    task: Optional[asyncio.Task[Any]] = field(default=None, repr=False)


def is_contract_draining(agent: Any) -> bool:
    return getattr(agent, "_contract_drain", None) is not None


def is_pool_worker(agent: Any) -> bool:
    """A worker started with a launch record, which the team lead takes tasks back from."""

    return getattr(agent, "_worker_launch", None) is not None


async def drain_agent(agent: Any, reason: str) -> None:
    """Drain one agent (module docstring); on its own loop, once. Returns when it took no more new work."""

    if is_contract_draining(agent):
        return
    worker = is_pool_worker(agent)
    state = ContractDrain(reason=reason, started_at=datetime.now(timezone.utc), holds_back_unstarted=worker)
    agent._contract_drain = state
    state.left_queues = await _leave_shared_queues(agent)
    agent._logger.warning(
        "This agent's process runs another message contract than its installation: it takes no new work and "
        "finishes or hands back what it holds. event=agent_contract_drain_started agent_id=%s pool_worker=%s "
        "queues_left=%s reason=%s", agent.id, worker, state.left_queues, reason)
    if worker:
        from custom_agents.pending_work_runtime import start_owned_task

        state.task = start_owned_task(agent, partial(_wind_down_after_running_turns, agent, state),
                                      name=f"{agent.id}:contract_drain")


async def _leave_shared_queues(agent: Any) -> int:
    from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE

    adapter = agent._ensure_nats_transport_adapter()
    stopped = 0
    for role in (NAMED_WORKER_CONSUMER_ROLE, TEAM_LEAD_CONSUMER_ROLE):
        stopped += int(await adapter.unsubscribe_consumer_role(role))
    return stopped


async def _wind_down_after_running_turns(agent: Any, state: ContractDrain) -> None:
    """A pool worker: once no turn runs, return what it has not started to the team lead (module docstring)."""

    from custom_agents.lease_loss_wind_down import start_lease_loss_wind_down
    from custom_agents.turn_registry import executing_task_ids

    while executing_task_ids(agent) and not agent._should_exit.is_set():
        await asyncio.sleep(TURN_POLL_SECONDS)
    if not agent._should_exit.is_set():
        start_lease_loss_wind_down(agent, reason=state.reason)


async def holds_work(agent: Any) -> bool:
    """Whether the agent still holds a task it has not finished, or a request waiting to be admitted."""

    task_manager = agent.task_manager
    unfinished = await task_manager.operations_runtime.run(partial(_unfinished_task_ids, task_manager))
    inbox = getattr(agent, "deferred_inbox", None)
    return bool(unfinished) or bool(inbox is not None and list(inbox.messages))


def _unfinished_task_ids(task_manager: Any) -> list[str]:
    """Tasks this agent runs and has not finished; the records of subtasks it delegated are not its own work."""

    delegated_out = set(task_manager.task_state.subtask_to_parent_map)
    return [info.task.task_id for info in task_manager.list_task_infos()
            if info.task.task_id not in delegated_out and not task_manager.is_terminal_state(info.state)]
