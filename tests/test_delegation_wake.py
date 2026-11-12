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

from tests.task_work_support import TurnHost
from custom_agents import delegation_wake, subtask_watchdog_runtime
from custom_agents.task_resume_runtime import resume_task_with_directive as real_resume_task_with_directive
from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.delegated_work_v1 import TaskFailureCategory, TaskFailureV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from runtime_hosting.agent_execution import MAIN_LANE, AgentExecutionLanes
from runtime_settings.worker_pools import WorkerPoolsConfig

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ExecutionGate:
    """The agent's real execution lanes (``runtime_hosting.agent_execution``).

    It records when a holder starts waiting for a busy lane. A stand-in that
    ignored the lane once hid a wake-up that asked for the main lane while
    its turn needed the request's parallel lane.
    """

    def __init__(self) -> None:
        self.lanes = AgentExecutionLanes()
        self.waiting = asyncio.Event()

    @contextlib.asynccontextmanager
    async def scope(self, lane: str = MAIN_LANE):
        if self.lanes.lane_busy(lane) and not self.lanes.owned_by_current_task(lane):
            self.waiting.set()
        async with self.lanes.hold(lane):
            yield


def _lead(tmp_path, monkeypatch, *, children: int, interactive: bool = True, parallel: bool = False):
    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "lead", "team", "TeamArchitect")
    # Only the team lead opens parallel lanes (custom_agents.execution_lanes).
    manager.parallel_lanes_enabled = parallel
    context = GatewayContext(
        canonical_user_id="jordan",
        session_key="s",
        delivery_mode="interactive" if interactive else "background",
        parallel_thread=parallel,
    )
    manager.add_task(
        Task(task_id="root", task_type=MessageMetaType.REQUEST, problem_statement="Research"),
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway_reply"),
        MessageMeta(gateway_context=context, requester_type=RequesterType.USER_REQUESTER),
        owner_person_id="jordan",
    )
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, _iso())
    agent = TurnHost(
        id="lead",
        name="lead",
        team_id="team",
        task_manager=manager,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=logging.getLogger("delegation-wake-test"),
    )
    agent._execution_gate = _ExecutionGate()
    agent._execution_lanes = agent._execution_gate.lanes
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
async def test_recovered_work_finds_its_workers_progress_and_check_backs_under_a_new_agent_id(tmp_path, monkeypatch):
    agent, resume = _lead(tmp_path, monkeypatch, children=4)
    repository = delegated_tasks(agent)
    repository.record_claim(team_id="team", task_id="child_0", worker_agent_id="worker_old",
                            worker_agent_name="Researcher", claimed_at=NOW)
    repository.set_check_back(team_id="team", parent_task_id="root", requester_agent_id="lead", at=NOW)
    # The saved task tree belongs to this runtime after recovery, but the delegated
    # rows and check-back still name the earlier requester.
    agent.id = "lead_after_restart"
    holders = await subtask_watchdog_runtime._delegated_holders(agent)
    assert holders["child_0"].holder_agent_id == "worker_old"
    assert await delegation_wake.send_progress_notes(agent, now=NOW) == 1
    assert "1 running" in _notes(agent)[0]
    assert await delegation_wake.wake_due_check_backs(agent, now=NOW) == 1
    assert resume.await_count == 1


@pytest.mark.asyncio
async def test_custody_before_the_claim_reply_starts_the_watchdogs_clock_at_actual_admission(tmp_path, monkeypatch):
    agent, _ = _lead(tmp_path, monkeypatch, children=1)
    admitted = NOW + timedelta(minutes=10)
    monkeypatch.setattr(agent.task_manager.runtime_state_store, "get_task_custody", lambda *_: {
        "child_0": SimpleNamespace(holder_agent_id="replacement_worker", claimed_at=admitted.isoformat())})
    records = await subtask_watchdog_runtime._delegated_holders(agent)
    child = agent.task_manager.get_task_info("child_0")
    assert records["child_0"].holder_agent_id == "replacement_worker"
    assert await subtask_watchdog_runtime._queue_tracking(agent, records, child) == (False, admitted.isoformat())


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
    the parent, and the wake then ran a second model turn for a finished task. The
    check-back pass now skips a busy lane, so the claim handler's path (a worker lost
    too often) shows the race."""

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
        delegation_wake.wake_parent_for_decision(
            agent, parent_task_id="root", directive="decide", reason="delegated task failed (worker_lost)"
        ),
        name="claim_handler",
    )
    await delivery
    assert woken is False
    assert turns == [("result_delivery", "in_progress")]
    assert manager.get_task_info("root") is None


@pytest.mark.asyncio
async def test_a_check_back_waits_for_a_later_pass_while_its_request_runs_a_turn(tmp_path, monkeypatch):
    """The scheduler pass never waits for a turn: a busy request keeps its check-back time."""

    agent, resume = _lead(tmp_path, monkeypatch, children=2)
    repository = delegated_tasks(agent)
    repository.set_check_back(team_id="team", parent_task_id="root", requester_agent_id="lead", at=NOW)
    async with agent._execution_lanes.hold(MAIN_LANE):
        woken = await asyncio.create_task(
            delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=1))
        )
    assert woken == 0
    resume.assert_not_awaited()
    assert repository.get_wait("team", "root").check_back_at_utc is not None

    assert await delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=2)) == 1
    resume.assert_awaited_once()


def _add_waiting_request(agent, *, root_id: str, child_id: str) -> None:
    """A second request of the same person, waiting on one delegated task."""

    manager = agent.task_manager
    context = GatewayContext(canonical_user_id="jordan", session_key=f"s_{root_id}", delivery_mode="interactive")
    manager.add_task(
        Task(task_id=root_id, task_type=MessageMetaType.REQUEST, problem_statement="Other"),
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway_reply"),
        MessageMeta(gateway_context=context, requester_type=RequesterType.USER_REQUESTER),
        owner_person_id="jordan",
    )
    manager.update_main_task_state(root_id, ProgressState.IN_PROGRESS, _iso())
    manager.add_subtask(
        root_id,
        Task(task_id=child_id, parent_task_id=root_id, task_type=MessageMetaType.REQUEST, problem_statement="Fetch"),
        MessageHeader(from_agent_id="lead", to_routing_path="team_WebResearchAgent"),
        MessageMeta(to_named_agent_type="WebResearchAgent", gateway_context=context),
    )
    record_publication(agent, subtask_id=child_id, parent_task_id=root_id, named_agent_type="WebResearchAgent",
                       target_agent_name=None, publish_sequence=99)
    manager.update_main_task_state(root_id, ProgressState.WAITING, _iso())


@pytest.mark.asyncio
async def test_a_check_back_moved_while_an_earlier_wake_ran_its_turn_keeps_its_new_time(tmp_path, monkeypatch):
    """Regression (PR #116 review): the pass read every wait once, then ran each wake's turn
    before the next. A request whose own turn meanwhile set a later check-back time had that
    time erased and was woken at once from the pass's old reading."""

    agent, _ = _lead(tmp_path, monkeypatch, children=1)
    _add_waiting_request(agent, root_id="root2", child_id="child2")
    repository = delegated_tasks(agent)
    for parent in ("root", "root2"):
        repository.set_check_back(team_id="team", parent_task_id=parent, requester_agent_id="lead", at=NOW)
    later = NOW + timedelta(minutes=30)
    woken: list[str] = []

    async def turn(_agent, parent, _directive):
        woken.append(parent.task.task_id)
        if parent.task.task_id == "root":
            # While root's turn runs, root2's own turn asks to be looked at in 30 minutes.
            repository.set_check_back(team_id="team", parent_task_id="root2", requester_agent_id="lead", at=later)

    monkeypatch.setattr("custom_agents.task_resume_runtime.resume_task_with_directive", turn)
    assert await delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=1)) == 1
    assert woken == ["root"]
    assert repository.get_wait("team", "root2").check_back_at_utc == later

    assert await delegation_wake.wake_due_check_backs(agent, now=later) == 1
    assert woken == ["root", "root2"]


def _capture_lanes(agent, monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []

    async def turn(*_args, **_kwargs):
        seen.append(agent._execution_lanes.held_lanes())

    monkeypatch.setattr("custom_agents.task_resume_runtime.resume_task_with_directive", turn)
    return seen


@pytest.mark.asyncio
async def test_a_parallel_requests_wake_from_the_scheduler_runs_in_its_own_lane(tmp_path, monkeypatch):
    """Regression (PR #57 review): the wake held the main lane, and the turn then asked for the
    request's parallel lane, which raised and failed the whole request."""

    agent, _ = _lead(tmp_path, monkeypatch, children=2, parallel=True)
    seen = _capture_lanes(agent, monkeypatch)
    delegated_tasks(agent).set_check_back(team_id="team", parent_task_id="root", requester_agent_id="lead", at=NOW)

    assert await delegation_wake.wake_due_check_backs(agent, now=NOW + timedelta(minutes=1)) == 1
    assert seen == [["parallel:root"]]


@pytest.mark.asyncio
async def test_a_typed_failure_handled_in_a_parallel_lane_wakes_its_request(tmp_path, monkeypatch):
    """Regression (PR #57 review): a worker's typed failure handled by the request's lane worker
    asked for the main lane too; the error dropped the decision wake."""

    agent, _ = _lead(tmp_path, monkeypatch, children=2, parallel=True)
    seen = _capture_lanes(agent, monkeypatch)
    failed = agent.task_manager.get_task_info("child_0")
    failed.task.failure = TaskFailureV1(category=TaskFailureCategory.PROVIDER_LIMITED, attempts=3)

    # The lane's worker holds the lane while it handles the result (execution_lanes_runtime).
    async with agent._execution_lanes.hold("parallel:root"):
        assert await delegation_wake.wake_for_failed_delegation(agent, parent_task_id="root", subtask_info=failed)
    assert seen == [["parallel:root"]]
