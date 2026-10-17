"""When a waiting requester wakes for its delegated work, and the progress notes meanwhile.

Real TaskManager and SQLite delegated-task records; the model turn
(``resume_task_with_directive``) and the transport are captured.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import delegation_wake, subtask_watchdog_runtime
from custom_agents.task_resume_runtime import resume_task_with_directive as real_resume_task_with_directive
from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.delegated_work_v1 import TaskFailureCategory, TaskFailureV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from runtime_settings.worker_pools import WorkerPoolsConfig

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ExecutionGate:
    """Owner-aware like runtime_hosting.agent_execution: a nested holder passes through."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: asyncio.Task | None = None
        self.waiting = asyncio.Event()

    @contextlib.asynccontextmanager
    async def scope(self):
        me = asyncio.current_task()
        if self.owner is me:
            yield
            return
        self.waiting.set()
        async with self.lock:
            self.owner = me
            try:
                yield
            finally:
                self.owner = None


def _lead(tmp_path, monkeypatch, *, children: int, interactive: bool = True):
    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "lead", "team", "TeamArchitect")
    context = GatewayContext(
        canonical_user_id="jordan",
        session_key="s",
        delivery_mode="interactive" if interactive else "background",
    )
    manager.add_task(
        Task(task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Research"),
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway_reply"),
        MessageMeta(gateway_context=context, requester_type=RequesterType.USER_REQUESTER),
        owner_person_id="jordan",
    )
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, _iso())
    agent = SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team",
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=logging.getLogger("delegation-wake-test"),
    )
    agent._execution_gate = _ExecutionGate()
    agent._execution_scope = agent._execution_gate.scope
    for index in range(children):
        child_id = f"child_{index}"
        manager.add_subtask(
            "root",
            Task(task_id=child_id, parent_task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Fetch"),
            MessageHeader(from_agent_id="lead", to_routing_path="team_WebResearchAgent"),
            MessageMeta(to_named_agent_type="WebResearchAgent", gateway_context=context),
        )
        record_publication(
            agent,
            subtask_id=child_id,
            parent_task_id="root",
            named_agent_type="WebResearchAgent",
            target_agent_name=None,
            publish_sequence=index + 1,
        )
    manager.update_main_task_state("root", ProgressState.WAITING, _iso())
    monkeypatch.setattr(
        "custom_agents.worker_pool_settings.current_worker_pool_settings",
        lambda _agent: WorkerPoolsConfig(progress_notices={"min_tasks": 3, "interval_minutes": 5}),
    )
    resume = AsyncMock()
    monkeypatch.setattr("custom_agents.task_resume_runtime.resume_task_with_directive", resume)
    return agent, resume


def _notes(agent) -> list[str]:
    return [
        call.kwargs["message"].msg_content.text
        for call in agent.team_comms.send_message.await_args_list
        if isinstance(call.kwargs["message"].msg_content, UserNotification)
    ]


@pytest.mark.asyncio
async def test_progress_notes_send_counts_at_most_every_interval_and_only_on_change(tmp_path, monkeypatch):
    agent, _ = _lead(tmp_path, monkeypatch, children=4)
    assert await delegation_wake.send_progress_notes(agent, now=NOW) == 1
    assert _notes(agent) == ["Progress on your request: 0 of 4 done, 4 waiting for a free worker."]
    sent = agent.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == "gateway_reply"
    assert sent["message"].msg_content.reason == delegation_wake.PROGRESS_NOTICE_REASON

    # Too soon, then unchanged: nothing more.
    assert await delegation_wake.send_progress_notes(agent, now=NOW + timedelta(minutes=1)) == 0
    assert await delegation_wake.send_progress_notes(agent, now=NOW + timedelta(minutes=6)) == 0

    repository = delegated_tasks(agent)
    repository.record_claim(team_id="team", task_id="child_0", worker_agent_id="w1", worker_agent_name="w1", claimed_at=NOW)
    repository.mark_closed(team_id="team", task_id="child_1", status="finished")
    assert await delegation_wake.send_progress_notes(agent, now=NOW + timedelta(minutes=12)) == 1
    assert _notes(agent)[-1] == "Progress on your request: 1 of 4 done, 1 running, 2 waiting for a free worker."


@pytest.mark.asyncio
async def test_small_or_background_requests_get_no_progress_notes(tmp_path, monkeypatch):
    few, _ = _lead(tmp_path / "few", monkeypatch, children=2)
    assert await delegation_wake.send_progress_notes(few, now=NOW) == 0
    background, _ = _lead(tmp_path / "background", monkeypatch, children=5, interactive=False)
    assert await delegation_wake.send_progress_notes(background, now=NOW) == 0


@pytest.mark.asyncio
async def test_a_check_back_time_wakes_the_waiting_request_once(tmp_path, monkeypatch):
    agent, resume = _lead(tmp_path, monkeypatch, children=3)
    repository = delegated_tasks(agent)
    repository.set_check_back(team_id="team", parent_task_id="root", requester_agent_id="lead", at=NOW + timedelta(minutes=10))
    assert await delegation_wake.wake_due_check_backs(agent, now=NOW) == 0
    resume.assert_not_awaited()

    assert await delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=11)) == 1
    directive = resume.await_args.args[2]
    assert "check-back time" in directive
    assert "0 of 3 done, 3 waiting for a free worker" in directive
    assert repository.get_wait("team", "root").check_back_at_utc is None
    assert await delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=20)) == 0


@pytest.mark.asyncio
async def test_a_typed_failure_wakes_the_request_while_other_work_runs(tmp_path, monkeypatch):
    agent, resume = _lead(tmp_path, monkeypatch, children=3)
    manager = agent.task_manager
    failed = manager.get_task_info("child_0")
    failed.task.failure = TaskFailureV1(category=TaskFailureCategory.PROVIDER_LIMITED, attempts=3)

    assert await delegation_wake.wake_for_failed_delegation(agent, parent_task_id="root", subtask_info=failed)
    directive = resume.await_args.args[2]
    assert "child_0 failed because the model provider kept refusing calls" in directive
    assert "cancel_delegated_task" in directive


@pytest.mark.asyncio
async def test_an_untyped_failure_or_a_running_request_does_not_wake(tmp_path, monkeypatch):
    agent, resume = _lead(tmp_path, monkeypatch, children=2)
    manager = agent.task_manager
    assert not await delegation_wake.wake_for_failed_delegation(
        agent, parent_task_id="root", subtask_info=manager.get_task_info("child_0")
    )
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, _iso())
    assert not await delegation_wake.wake_parent_for_decision(
        agent, parent_task_id="root", directive="x", reason="test"
    )
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_wake_skips_a_parent_another_path_resumed_while_it_waited(tmp_path, monkeypatch):
    """Regression (2026-10-17): the check-back wake read the parent before taking the
    execution slot; the last child's result, holding the slot, resumed and completed
    the parent, and the wake then ran a second model turn for a finished task."""

    agent, _ = _lead(tmp_path, monkeypatch, children=1)
    monkeypatch.setattr(
        "custom_agents.task_resume_runtime.resume_task_with_directive", real_resume_task_with_directive
    )
    manager = agent.task_manager
    gate = agent._execution_gate
    turns: list[tuple[str, str]] = []

    async def process_llm_interaction(msg_content, msg_header, msg_meta):
        async with agent._execution_scope():
            info = manager.get_task_info(msg_content.task_id)
            turns.append((asyncio.current_task().get_name(), info.state.value if info else "gone"))
            return Task(task_id=msg_content.task_id, task_type=MessageMetaType.RESPONSE, answer="done")

    async def complete(task_id, timestamp):
        await manager.operations_runtime.run(manager.complete_task, task_id, timestamp)

    agent.process_llm_interaction = process_llm_interaction
    agent._complete_task_and_schedule_aar_review = complete
    agent._refresh_self_agent_state = AsyncMock()
    agent._notify_context_after_turn = AsyncMock()
    agent._conversation_store = agent._conversation_compactor = None
    agent._conversation_artifact_offloader = agent._conversation_summary_generator = None
    agent._strands_agent = SimpleNamespace(messages=[])
    agent.named_agent_type = "TeamArchitect"
    delegated_tasks(agent).set_check_back(
        team_id="team", parent_task_id="root", requester_agent_id="lead", at=NOW
    )

    async def result_delivery():
        # A TaskResponse delivery holds the execution slot while it is handled.
        async with agent._execution_scope():
            gate.waiting.clear()
            await gate.waiting.wait()  # the wake is now waiting for the slot
            manager.try_transition_task_state(
                "child_0", allowed_from={ProgressState.ACCEPTED, ProgressState.IN_PROGRESS},
                to_state=ProgressState.COMPLETED, timestamp=_iso(), reason="result", source="test",
            )
            await subtask_watchdog_runtime._resume_parent_task_if_ready(agent, "root")

    delivery = asyncio.create_task(result_delivery(), name="result_delivery")
    await asyncio.sleep(0)
    woken = await asyncio.create_task(
        delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=1)), name="check_back"
    )
    await delivery
    assert woken == 0
    assert turns == [("result_delivery", "in_progress")]
    assert manager.get_task_info("root") is None
