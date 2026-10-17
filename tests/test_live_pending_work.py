"""Live NATS checks for the agent's retained queue drain.

A real ``UniversalAgent`` (with a stand-in model) connects to NATS. The queue
check that would start a model turn is replaced by one that holds the
execution slot until the test lets it go, so each scenario can act at an exact
point while the transport, deliveries and shutdown are the production code.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest
from live_nats_client import LiveNatsClient, resolve_live_transport_settings

from custom_agents import task_message_runtime
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from id_generation import new_id
from runtime_settings.projection import _project_transport

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


async def _wait_until(condition, *, timeout: float = 10.0, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}.")


def _worker(tmp_path, team_id: str):
    from custom_agents.strands_agent import UniversalAgent

    with (
        patch("custom_agents.strands_agent.Agent", return_value=MagicMock()),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        return UniversalAgent(
            **_project_transport(transport_cfg=resolve_live_transport_settings().model_dump()),
            role="Live Pending Work Worker",
            tools=[],
            team_id=team_id,
            named_agent_type="WebResearchAgent",
            is_team_lead=False,
            skills_enabled=False,
            task_manager=TaskManager(db_path=str(tmp_path / "pending_work.db")),
        )


@pytest.mark.asyncio
async def test_live_exit_during_resumed_drain_joins_before_transport_close(tmp_path, monkeypatch):
    """A stop request during a turn resumed at startup reaches cleanup.

    Before 2026-10-17 the startup drain ran inline in ``connect()``, so the
    exit request waited for the whole turn, and a ``resume`` continuation was
    never joined before the transport closed.
    """
    events: list[str] = []
    turn_started = asyncio.Event()

    async def turn_until_stopped(_agent, *_args, **_kwargs):
        turn_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("turn_stopped")
            raise
        return False

    monkeypatch.setattr(task_message_runtime, "_check_and_process_next_task", turn_until_stopped)
    team_id = new_id("live_pending")
    agent = _worker(tmp_path, team_id)
    # The client creates the team's streams, as the launcher does before agents start.
    async with LiveNatsClient(team_id, transport=resolve_live_transport_settings()):
        run = asyncio.create_task(agent.connect())
        try:
            await asyncio.wait_for(turn_started.wait(), timeout=15)
            assert agent.is_executing, "the resumed turn holds the execution slot"
            adapter = agent._ensure_nats_transport_adapter()
            close_transport = adapter.close

            async def recorded_close():
                events.append("transport_closed")
                await close_transport()

            adapter.close = recorded_close
            agent.set_exit_flag()
            await asyncio.wait_for(run, timeout=15)
        finally:
            if not run.done():
                agent.set_exit_flag()
                run.cancel()
                with contextlib.suppress(BaseException):
                    await run

    assert events == ["turn_stopped", "transport_closed"]
    assert agent._pending_work_task is None
    leftovers = [task for task in asyncio.all_tasks() if task.get_name() == f"pending-work:{agent.id}"]
    assert all(task.done() for task in leftovers)


@pytest.mark.asyncio
async def test_live_worker_dispatches_result_saved_during_empty_check(tmp_path, monkeypatch):
    """A delegated result that arrives during the drain's final empty check is handled.

    Before 2026-10-17 its wake-up was merged into the finishing pass and
    dropped, so a quiet worker left the result in its deferred inbox until
    unrelated traffic arrived.
    """
    holding, release = asyncio.Event(), asyncio.Event()
    hold_next = {"armed": False}

    async def empty_queue_check(_agent, *_args, **_kwargs):
        if hold_next["armed"]:
            hold_next["armed"] = False
            holding.set()
            await release.wait()
        return False  # the queue is empty, so no turn starts

    monkeypatch.setattr(task_message_runtime, "_check_and_process_next_task", empty_queue_check)
    team_id = new_id("live_wake")
    agent = _worker(tmp_path, team_id)
    dispatched: list[str] = []

    async def record_dispatch(**kwargs):
        dispatched.append(kwargs["msg_content"].task_id)

    object.__setattr__(agent, "_dispatch_message", record_dispatch)
    async with LiveNatsClient(team_id, transport=resolve_live_transport_settings()) as client:
        run = asyncio.create_task(agent.connect())
        try:
            await _wait_until(lambda: getattr(agent, "_worker_ready", False), what="the agent to finish NATS setup")
            await _wait_until(
                lambda: agent._pending_work_task is None or agent._pending_work_task.done(),
                what="the startup drain to finish",
            )
            hold_next["armed"] = True
            schedule_pending_work(agent)
            await asyncio.wait_for(holding.wait(), timeout=10)
            assert agent.is_executing, "the drain holds the slot for its empty check"

            result = Task(
                task_type=MessageMetaType.RESPONSE,
                task_id=new_id("task_id"),
                parent_task_id="task_parent",
                answers="done",
            )
            await client.publish(
                agent.id,
                MessageEnvelope(
                    msg_content=result,
                    msg_header=MessageHeader(from_agent_id="agent_other", to_routing_path=agent.id),
                    msg_meta=MessageMeta(),
                ),
            )
            await _wait_until(lambda: len(agent.deferred_inbox) == 1, what="the result to be saved")
            release.set()
            await _wait_until(lambda: dispatched == [result.task_id], what="the saved result to be handled")
            assert len(agent.deferred_inbox) == 0
        finally:
            release.set()
            agent.set_exit_flag()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(run, timeout=15)
