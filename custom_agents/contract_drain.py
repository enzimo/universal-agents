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
   ``custom_agents.schedule_worker_controller``), and no draining agent claims a due
   schedule, whose run it would admit as a task of its own
   (``scheduled_runtime._schedule_claim_scope``): the schedule stays due for the next team
   lead on the installation's contract, which fires it once. Its own route, which carries
   the replies to what it holds, and its controls are still read.
2. **The turns in progress finish.** A running turn is not stopped.
3. **What it has not finished goes back where this release has a way.** A pool worker (one
   started with a launch record) starts no task it has not started yet and, once startup
   recovery and its running turns ended, winds down as after a lost launch lease
   (``custom_agents.lease_loss_wind_down``), naming its own cause in its logs and texts and
   counted as no interruption, nor as a lost worker by a delegated task's requester
   (``custom_agents.delegated_work_runtime``): the tasks it has not started go back to the
   team lead, which sends them out again on the type's queue; tasks that wait on a person
   or a subtask finish here until that wind-down's deadline, because their replies come to
   this agent; then the worker exits. Pool workers run in the team lead's process, so the
   lead drains with them: what they return waits in the lead's queue for a team lead on the
   installation's contract. The team lead and startup agents have no way to hand
   a task back in this phase (``task.handback`` comes with the queued work of protocol 1):
   they finish here what they already admitted and take nothing new.

``holds_work`` tells the host whether an agent still holds an unfinished task or a request
waiting to be admitted, so the host gives an agent up only once it holds nothing.

**No agent starts in a draining process.** Every start goes through
``AgentInstantiationService.launch``, which refuses while the agent that asks drains (reason
``LAUNCH_REFUSED_OTHER_CONTRACT``, one log line): a new agent would take new work here, and a
replacement for a dead agent would adopt that agent's saved work. Only the team lead's process
starts agents, and the lead drains with its process. The periodic callers that would otherwise
be refused at every pass, the worker pool and scheduled-worker controllers and the lead's
takeover of dead agents' work, ask first and do nothing while it drains.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_agents.contract_drain_state import ContractDrain, is_pool_worker
from custom_transport.nats_adapter import TEAM_LEAD_CONSUMER_ROLE

# How often a pool worker checks whether its running turns ended before it winds down.
TURN_POLL_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)


async def drain_agent(agent: Any, reason: str) -> None:
    """Drain one agent (module docstring); on its own loop, once. Returns when it took no more new work."""

    state = getattr(agent, "_contract_drain", None)
    worker = is_pool_worker(agent)
    if state is None:
        state = ContractDrain(reason=reason, started_at=datetime.now(timezone.utc), holds_back_unstarted=worker)
        agent._contract_drain = state
        report = getattr(agent, "_report_record_state", None)
        if callable(report):
            report()
    # Startup may have been subscribing when the drain began; a repeat leaves those queues too.
    state.left_queues += await _leave_shared_queues(agent)
    if not state.announced:
        agent._logger.warning(
            "This agent's process runs another message contract than its installation: it takes no new work and "
            "finishes or hands back what it holds. event=agent_contract_drain_started agent_id=%s pool_worker=%s "
            "queues_left=%s reason=%s", agent.id, worker, state.left_queues, reason)
        state.announced = True
    if worker and state.task is None:
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
    """After recovery and running turns finish, return unstarted work to the team lead."""

    from custom_agents.lease_loss_wind_down import start_lease_loss_wind_down
    from custom_agents.turn_registry import executing_task_ids
    from custom_messages.contracts.worker_handback_v1 import WIND_DOWN_OTHER_CONTRACT

    while (not getattr(agent, "_worker_ready", False) or executing_task_ids(agent)) and not agent._should_exit.is_set():
        await asyncio.sleep(TURN_POLL_SECONDS)
    if not agent._should_exit.is_set():
        start_lease_loss_wind_down(agent, reason=state.reason, cause=WIND_DOWN_OTHER_CONTRACT)


async def holds_work(agent: Any) -> bool:
    """Keep startup attached until recovery can reveal its saved work, then inspect what it holds."""

    if not getattr(agent, "_worker_ready", False):
        return True

    task_manager = agent.task_manager
    unfinished = await task_manager.operations_runtime.run(partial(_unfinished_task_ids, task_manager))
    inbox = getattr(agent, "deferred_inbox", None)
    return bool(unfinished) or bool(inbox is not None and list(inbox.messages))


def _unfinished_task_ids(task_manager: Any) -> list[str]:
    """Tasks this agent runs and has not finished; the records of subtasks it delegated are not its own work."""

    delegated_out = set(task_manager.task_state.subtask_to_parent_map)
    return [info.task.task_id for info in task_manager.list_task_infos()
            if info.task.task_id not in delegated_out and not task_manager.is_terminal_state(info.state)]
