"""An on-demand worker never exits on its idle timer while it holds work.

Real TaskManager. Before 2026-10-04 only runnable top-level tasks counted, so a
worker exited after ten idle minutes while its task waited on an access
decision, a question or a subtask, and a task delegated to it never counted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_agents.strands_team import worker_holds_work
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker(tmp_path, *, deferred=()):
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity("worker-1", "worker", "team", "WebResearchAgent")
    return SimpleNamespace(task_manager=manager, deferred_inbox=list(deferred))


def delegated(manager, task_id="work", state=ProgressState.WAITING):
    """A task the lead delegated to this worker (it has a parent elsewhere)."""
    manager.add_task(
        Task(task_id=task_id, parent_task_id="lead-root", task_type=MessageMetaType.REQUEST, problem_statement="x"),
        MessageHeader(from_agent_id="lead", response_routing_key="lead"),
        MessageMeta(),
        owner_person_id=None,
    )
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, now())
    manager.update_main_task_state(task_id, state, now())


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ProgressState.WAITING, ProgressState.PAUSED])
async def test_a_waiting_or_paused_delegated_task_keeps_the_worker(tmp_path, state):
    agent = worker(tmp_path)
    delegated(agent.task_manager, state=state)
    assert agent.task_manager.current_task_id is None
    assert await worker_holds_work(agent)


@pytest.mark.asyncio
async def test_a_worker_with_only_finished_work_may_exit(tmp_path):
    agent = worker(tmp_path)
    assert not await worker_holds_work(agent)
    delegated(agent.task_manager)
    agent.task_manager.fail_task("work", "stopped", now())
    assert not await worker_holds_work(agent)


@pytest.mark.asyncio
async def test_deferred_messages_keep_the_worker(tmp_path):
    assert await worker_holds_work(worker(tmp_path, deferred=["message"]))
