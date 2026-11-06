"""An agent whose process runs another message contract takes no new work (custom_agents.contract_drain).

Contract README "Versions" and plan 13B.3: the host drains, finishes the turns in progress
and hands back what it has not finished. Queued work still runs on today's queues, so each
agent leaves the queues it shares with its type, a pool worker winds down to the team lead
once its running turns end, and nobody starts new workers. The agents here are stand-ins
with the parts the drain uses.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_agents import contract_drain, lease_loss_wind_down, turn_registry
from custom_agents.contract_drain import drain_agent, holds_work, is_contract_draining
from custom_agents.lease_loss_wind_down import may_start_next_task
from custom_agents.task_manager import TaskManager
from custom_messages.enums import ProgressState
from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE

REASON = "Its process runs another message contract than its installation."


class _Adapter:
    def __init__(self) -> None:
        self.left: list[str] = []

    async def unsubscribe_consumer_role(self, role: str) -> int:
        self.left.append(role)
        return 1


def _info(task_id: str, state: ProgressState, history: tuple[ProgressState, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(task=SimpleNamespace(task_id=task_id, task_state=[(item.value, "t") for item in history]),
                           state=state)


def _agent(*, worker: bool = False, infos: tuple[SimpleNamespace, ...] = (), delegated: tuple[str, ...] = (),
           queue: tuple[str, ...] = (), inbox: tuple[Any, ...] = ()) -> SimpleNamespace:
    adapter = _Adapter()

    async def run(function, *args):
        return function(*args)

    by_id = {info.task.task_id: info for info in infos}
    task_manager = SimpleNamespace(
        operations_runtime=SimpleNamespace(run=run), list_task_infos=lambda: list(infos),
        task_state=SimpleNamespace(subtask_to_parent_map={task_id: "parent" for task_id in delegated},
                                   task_queue=list(queue)),
        is_terminal_state=TaskManager.is_terminal_state, is_executing_state=TaskManager.is_executing_state,
        get_task_info=by_id.get)
    agent = SimpleNamespace(id="agent_1", _logger=logging.getLogger("test.contract_drain"), _should_exit=asyncio.Event(),
                            _owned_tasks=set(), task_manager=task_manager, adapter=adapter,
                            deferred_inbox=SimpleNamespace(messages=list(inbox)))
    agent._ensure_nats_transport_adapter = lambda: adapter
    if worker:
        agent._worker_launch = SimpleNamespace(launch_id="launch-1")
    return agent


@pytest.mark.asyncio
async def test_an_agent_leaves_the_queues_its_type_shares_and_keeps_what_it_holds(caplog) -> None:
    """The team lead and startup agents have no way to hand a task back yet: they finish what they admitted."""

    agent = _agent(infos=(_info("t1", ProgressState.ACCEPTED),), queue=("t1",))
    with caplog.at_level(logging.WARNING):
        await drain_agent(agent, REASON)
        await drain_agent(agent, REASON)  # once
    assert is_contract_draining(agent)
    assert agent.adapter.left == [NAMED_WORKER_CONSUMER_ROLE, contract_drain.TEAM_LEAD_CONSUMER_ROLE]
    assert caplog.text.count("event=agent_contract_drain_started") == 1 and "pool_worker=False" in caplog.text
    # Its own tasks still start here; nothing hands them back.
    assert await may_start_next_task(agent) is True
    assert agent._contract_drain.task is None


@pytest.mark.asyncio
async def test_a_pool_worker_starts_nothing_new_and_winds_down_once_its_running_turns_end(monkeypatch) -> None:
    running = {"t-running"}
    wound_down: list[str] = []
    monkeypatch.setattr(contract_drain, "TURN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(turn_registry, "executing_task_ids", lambda agent: set(running))
    monkeypatch.setattr(lease_loss_wind_down, "start_lease_loss_wind_down",
                        lambda agent, *, reason: wound_down.append(reason))
    started = _info("t-waiting", ProgressState.WAITING, (ProgressState.IN_PROGRESS, ProgressState.WAITING))
    agent = _agent(worker=True, infos=(_info("t-new", ProgressState.ACCEPTED), started), queue=("t-new",))

    await drain_agent(agent, REASON)
    assert agent._contract_drain.holds_back_unstarted
    # A task that has not started here does not start; one that already started may resume.
    assert await may_start_next_task(agent) is False
    agent.task_manager.task_state.task_queue = ["t-waiting"]
    assert await may_start_next_task(agent) is True
    # The running turn is not stopped: the wind-down waits for it.
    await asyncio.sleep(0.05)
    assert wound_down == []
    running.clear()
    await asyncio.wait_for(agent._contract_drain.task, timeout=5)
    assert wound_down == [REASON]


@pytest.mark.asyncio
async def test_an_agent_holds_work_while_it_has_an_unfinished_task_or_a_waiting_request() -> None:
    finished = _info("t-done", ProgressState.COMPLETED)
    assert await holds_work(_agent(infos=(finished,))) is False
    assert await holds_work(_agent(infos=(finished, _info("t-open", ProgressState.WAITING)))) is True
    # The record of a subtask it delegated is not work of its own.
    assert await holds_work(_agent(infos=(_info("sub-1", ProgressState.IN_PROGRESS),), delegated=("sub-1",))) is False
    assert await holds_work(_agent(inbox=("a request",))) is True


@pytest.mark.asyncio
async def test_a_draining_team_lead_starts_no_worker(monkeypatch) -> None:
    from custom_agents import schedule_worker_controller, worker_pool_controller

    started: list[str] = []

    class _Controller:
        def __init__(self, agent) -> None:
            pass

        async def reconcile(self) -> None:
            started.append("reconciled")

    monkeypatch.setattr(worker_pool_controller, "WorkerPoolController", _Controller)
    monkeypatch.setattr(schedule_worker_controller, "ScheduleWorkerController", _Controller)
    lead = SimpleNamespace(is_team_lead=True, _local_team_runtime=object(), team_id="team_one",
                           _logger=logging.getLogger("test"))
    await worker_pool_controller.reconcile_worker_pools(lead)
    await schedule_worker_controller.reconcile_schedule_workers(lead)
    assert started == ["reconciled", "reconciled"]
    lead._contract_drain = SimpleNamespace(holds_back_unstarted=False)
    await worker_pool_controller.reconcile_worker_pools(lead)
    await schedule_worker_controller.reconcile_schedule_workers(lead)
    assert started == ["reconciled", "reconciled"]
