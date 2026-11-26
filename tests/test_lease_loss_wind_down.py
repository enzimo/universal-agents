"""A worker that loses its launch lease hands work back to the lead and exits."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_agents.lease_loss_wind_down as wind_down
import tests.test_access_request_tools as access_tests
from tests.task_work_support import TurnHost, register_turn
from custom_agents import access_runtime, paused_wait_resume
from custom_agents import timed_waits as timed_waits_rules
from custom_agents.control_runtime import (
    handle_interrupted_turn,
)
from custom_agents.pending_work_runtime import close_pending_work
from custom_agents.task_human_interaction_blockers import (
    answer_held,
    get_human_interaction_blocker,
    is_timed_wait_blocker,
    mark_answer_held,
    record_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_agents.timed_waits import WAIT_TOOL_NAME, timed_wait_record
from custom_agents.worker_handback_runtime import handle_worker_task_handback
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import TurnActionOutboxRecord
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
    handback_identity,
)
from custom_messages.enums import MessageMetaType, ProgressState
from custom_messages.contracts.access_v2 import AccessRequestStatus
from custom_tools.providers import access as access_tools
from tests.task_work_support import set_agent_ref
from runtime_settings.schedule_workers import ScheduleWorkersConfig
from strands.interrupt import InterruptException
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_verify_keys
from storage.worker_handback_repository import WorkerHandbackRepository

NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


class _Comms:
    def __init__(self, failures: int = 0) -> None:
        self.sent: list[tuple[str, MessageEnvelope]] = []
        self.failures = failures

    async def send_message(self, *, message, routing_key):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("nats unavailable")
        self.sent.append((routing_key, message))

    def handbacks(self) -> list[WorkerTaskHandbackV1]:
        return [envelope.msg_content for _key, envelope in self.sent]


class _Adapter:
    def __init__(self) -> None:
        self.roles: list[str] = []

    async def unsubscribe_consumer_role(self, role: str) -> int:
        self.roles.append(role)
        return 1


@pytest.fixture
def manager(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("worker_1", "Worker", "team", "WebResearchAgent")
    return manager


@pytest.fixture(autouse=True)
def fast_wind_down(monkeypatch):
    monkeypatch.setattr(wind_down, "WIND_DOWN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(wind_down, "HANDBACK_PUBLISH_RETRY_SECONDS", 0.01)


def _worker(manager, *, inbox=None, wait_seconds=1800.0, monkeypatch=None):
    agent = TurnHost(
        id="worker_1",
        name="Worker",
        team_id="team",
        named_agent_type="WebResearchAgent",
        named_agent_type_queue_map={"TeamArchitect": "team_TeamArchitect"},
        task_manager=manager,
        team_comms=_Comms(),
        deferred_inbox=inbox,
        _logger=logging.getLogger("test.wind_down"),
        _stop_reasons_by_task_id={},
        _lease_loss_wind_down=None,
        _should_exit=asyncio.Event(),
        _owned_tasks=set(),
        _strands_agent=None,
        _worker_launch=SimpleNamespace(
            launch_id="launch_1",
            lease_expires_at_utc=datetime.now(timezone.utc) - timedelta(seconds=5),
        ),
        _send_task_error_response=AsyncMock(),
        _cancel_task_tree_locally=AsyncMock(),
        exited=False,
    )
    adapter = _Adapter()
    agent.adapter = adapter
    agent._ensure_nats_transport_adapter = lambda: adapter
    agent._consume_stop_reason = lambda task_id: agent._stop_reasons_by_task_id.pop(task_id, None)

    async def exit_agent():
        agent.exited = True
        agent._should_exit.set()

    agent.exit_agent = exit_agent
    if monkeypatch is not None:
        monkeypatch.setattr(
            wind_down,
            "_worker_limits",
            lambda _agent: SimpleNamespace(lease_loss_wait_timeout_seconds=wait_seconds),
        )
    return agent


def _add(manager, task_id, state=None):
    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=f"Do {task_id}",
    )
    manager.add_task(
        task,
        MessageHeader(from_agent_id="lead", response_routing_key="lead"),
        MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id="alice",
                session_key="session_alice",
                scheduled_job_id="sched_1" if task_id == "running" else None,
            )
        ),
        owner_person_id="alice",
    )
    if state is not None:
        manager.update_main_task_state(task_id, state, NOW())
    return task


def _request_envelope(task_id: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="new work",
        ),
        msg_header=MessageHeader(from_agent_id="someone", to_routing_path="worker_1"),
        msg_meta=MessageMeta(),
    )


def _wind_down_state(**fields) -> wind_down.LeaseLossWindDown:
    return wind_down.LeaseLossWindDown(
        evidence=WorkerLeaseLossEvidenceV1(
            lease_lost_at_utc=datetime.now(timezone.utc), reason="stale_launch_lease"
        ),
        deadline=datetime.now(timezone.utc) + timedelta(minutes=30),
        **fields,
    )


async def _wait_for_exit(agent, timeout=5.0):
    await asyncio.wait_for(agent._lease_loss_wind_down.task, timeout=timeout)


# --- contract ---------------------------------------------------------------


def test_wait_timeout_setting_defaults_to_thirty_minutes_and_is_bounded():
    assert ScheduleWorkersConfig().lease_loss_wait_timeout_seconds == 1800
    with pytest.raises(ValueError):
        ScheduleWorkersConfig(lease_loss_wait_timeout_seconds=10)


# --- worker wind-down -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_running_command_ends_before_its_task_is_handed_back(
    manager, monkeypatch, tmp_path, commands_without_agent
):
    """Another worker reruns a handed-back task; the old command must not still be running."""

    from custom_agents.sdk.strands.turn_tool_stop import HANDBACK_REASON, invoke_with_tool_stop
    from custom_tools.tool_stop import ToolStop
    from tests.turn_tool_stop_support import (
        agents_folder,
        kill_if_alive,
        long_command,
        process_alive,
        shell_agent,
        turn_invoke_kwargs,
        wait_for_pid,
    )

    set_agent_ref(None)
    work_dir = agents_folder(tmp_path, monkeypatch)
    pid_file = work_dir / "cmd.pid"
    sdk = shell_agent([long_command(pid_file)], work_dir)
    _add(manager, "running", ProgressState.IN_PROGRESS)
    agent = _worker(manager, monkeypatch=monkeypatch)
    alive_at_handback: dict[str, bool] = {}
    publish = wind_down._publish_handback

    async def recording_publish(agent_, state, handed_back):
        alive_at_handback[handed_back.task_id] = process_alive(int(pid_file.read_text()))
        return await publish(agent_, state, handed_back)

    monkeypatch.setattr(wind_down, "_publish_handback", recording_publish)

    turn_stop = ToolStop(agent_id=agent.id, task_id="running")

    async def running_turn():
        try:
            await invoke_with_tool_stop(
                sdk,
                "go",
                tool_stop=turn_stop,
                invoke_kwargs=turn_invoke_kwargs("running"),
                callback_handler=lambda **_: None,
                logger=agent._logger,
            )
        except asyncio.CancelledError:
            await handle_interrupted_turn(agent, task_id="running", task_info=manager.get_task_info("running"))
            raise

    register_turn(agent, "running", asyncio.create_task(running_turn()), turn_stop)
    try:
        pid = await wait_for_pid(pid_file)

        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease", recent_scheduler_errors=[])
        await _wait_for_exit(agent, timeout=15)
    finally:
        kill_if_alive(pid_file)

    assert alive_at_handback == {"running": False}
    assert not process_alive(pid)
    assert turn_stop.reason == HANDBACK_REASON
    assert [item.handed_back.task_id for item in agent.team_comms.handbacks()] == ["running"]
    assert not manager.is_execution_cancelled("running")


@pytest.mark.asyncio
async def test_wind_down_hands_back_running_and_unstarted_work_then_exits(
    manager, monkeypatch
):
    _add(manager, "running", ProgressState.IN_PROGRESS)
    _add(manager, "queued")
    manager.execution_state.turn_action_outbox["act_1"] = TurnActionOutboxRecord(
        action_id="act_1",
        batch_id="batch_1",
        source_task_id="running",
        action_index=0,
        action_type="notify_user",
        payload_json={"message": "Sent the weekly summary."},
        status="dispatched",
    )
    agent = _worker(manager, monkeypatch=monkeypatch)
    turn_cancelled = asyncio.Event()

    async def running_turn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await handle_interrupted_turn(
                agent, task_id="running", task_info=manager.get_task_info("running")
            )
            turn_cancelled.set()
            raise

    register_turn(agent, "running", asyncio.create_task(running_turn()))
    await asyncio.sleep(0)

    wind_down.start_lease_loss_wind_down(
        agent, reason="stale_launch_lease", recent_scheduler_errors=["db locked"]
    )
    await _wait_for_exit(agent)

    assert agent.adapter.roles == ["named_worker"]
    assert turn_cancelled.is_set()
    agent._cancel_task_tree_locally.assert_not_awaited()
    handbacks = {item.handed_back.task_id: item for item in agent.team_comms.handbacks()}
    assert set(handbacks) == {"running", "queued"}
    running = handbacks["running"].handed_back
    assert running.disposition == "interrupted"
    assert running.schedule_id == "sched_1"
    assert [note.summary for note in running.dispatched_actions] == ["Sent the weekly summary."]
    assert running.task.task_state == []
    assert handbacks["queued"].handed_back.disposition == "not_started"
    assert handbacks["queued"].evidence.recent_scheduler_errors == ("db locked",)
    assert {key for key, _ in agent.team_comms.sent} == {"team_TeamArchitect"}
    for _key, envelope in agent.team_comms.sent:
        assert envelope.msg_header.idempotency_key == envelope.msg_content.handback_id
    # Handed-back tasks leave local custody without a cancellation record.
    assert manager.get_task_info("running") is None
    assert manager.get_task_info("queued") is None
    assert not manager.is_execution_cancelled("running")
    assert agent.exited


def test_only_a_sent_final_answer_counts_as_answered(manager):
    """Regression (2026-10-17 review): a held-back task_response was recorded
    completed; now it is held_back, and only a task in FINALIZING_PLAN, or an
    answer that reached the post-publish phase, has really been answered."""
    _add(manager, "t1", ProgressState.IN_PROGRESS)
    outbox = manager.execution_state.turn_action_outbox
    record = TurnActionOutboxRecord(
        action_id="a", batch_id="b", source_task_id="t1", action_index=0,
        action_type="task_response", payload_json={}, status="held_back", phase="complete",
    )
    outbox["a"] = record  # held back (final review, pending work, escalation): not sent
    assert not wind_down._final_answer_sent(manager, "t1")
    manager.update_main_task_state("t1", ProgressState.FINALIZING_PLAN, NOW())
    assert not wind_down._final_answer_sent(manager, "t1")
    outbox["a"] = record.model_copy(update={"status": "dispatching", "phase": "publish"})
    assert not wind_down._final_answer_sent(manager, "t1")  # publish outcome unknown
    # Sent; its after-action report is going out, so the phase is still publish.
    outbox["a"] = record.model_copy(update={"status": "dispatched", "phase": "publish"})
    assert wind_down._final_answer_sent(manager, "t1")
    outbox["a"] = record.model_copy(update={"status": "dispatched", "action_type": "notify_user"})
    assert not wind_down._final_answer_sent(manager, "t1")
    # Finishing up failed after the answer went out and waits for its retry.
    manager.update_main_task_state("t1", ProgressState.RETRYING, NOW())
    outbox["a"] = record.model_copy(update={"status": "retry_scheduled", "phase": "post_publish_finalize"})
    assert wind_down._final_answer_sent(manager, "t1")


@pytest.mark.parametrize(("latest_status", "sent"), [("dispatching", False), ("dispatched", True)])
def test_only_the_latest_answer_decides_whether_it_was_sent(manager, latest_status, sent):
    """Regression (2026-10-17 review round 2): an earlier answer the outbox held
    back counted as sent while the real answer was still being published, so a
    lost lease cancelled the publish and completed the task without an answer."""
    _add(manager, "job", ProgressState.IN_PROGRESS)
    outbox = manager.execution_state.turn_action_outbox
    outbox["old"] = TurnActionOutboxRecord(
        action_id="old", batch_id="b1", source_task_id="job", action_index=0, action_type="task_response",
        payload_json={"answer": "held back"}, status="dispatched", phase="publish",
        created_at="2026-10-17T10:00:00+00:00",
    )
    outbox["new"] = TurnActionOutboxRecord(
        action_id="new", batch_id="b2", source_task_id="job", action_index=0, action_type="task_response",
        payload_json={"answer": "real"}, status=latest_status, phase="publish",
        created_at="2026-10-17T10:05:00+00:00",
    )
    manager.update_main_task_state("job", ProgressState.FINALIZING_PLAN, NOW())
    assert wind_down._final_answer_sent(manager, "job") is sent


def test_the_next_worker_is_not_told_a_held_back_answer_was_sent(manager):
    """Regression (2026-10-17 review round 2): the handback notes told the next
    worker a held-back answer had been sent, so the person could get nothing."""
    _add(manager, "job", ProgressState.IN_PROGRESS)
    outbox = manager.execution_state.turn_action_outbox
    outbox["held"] = TurnActionOutboxRecord(
        action_id="held", batch_id="b1", source_task_id="job", action_index=0, action_type="task_response",
        payload_json={"answer": "Summary for the person"}, status="held_back", phase="complete",
    )
    outbox["sent"] = TurnActionOutboxRecord(
        action_id="sent", batch_id="b1", source_task_id="job", action_index=1, action_type="notify_user",
        payload_json={"message": "Started"}, status="completed", phase="complete",
    )
    notes = wind_down._dispatched_action_notes(manager, "job")
    assert [(note.action_id, note.status) for note in notes] == [("sent", "completed")]


@pytest.mark.asyncio
async def test_a_running_task_whose_answer_was_held_back_is_handed_back(manager, monkeypatch):
    """A final-review turn runs after the outbox held its first answer back
    (held_back record, nothing sent): the task goes back to the lead."""
    _add(manager, "reviewing", ProgressState.IN_PROGRESS)
    manager.execution_state.turn_action_outbox["act"] = TurnActionOutboxRecord(
        action_id="act", batch_id="b", source_task_id="reviewing", action_index=0,
        action_type="task_response", payload_json={"answer": "draft"}, status="held_back", phase="complete",
    )
    agent = _worker(manager, monkeypatch=monkeypatch)
    finished: list[str] = []

    async def complete_after_dispatch(*, task_id, after_action_report_sent=False):
        finished.append(task_id)
        return True

    agent._complete_local_task_finalization_after_dispatch = complete_after_dispatch

    async def review_turn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await handle_interrupted_turn(agent, task_id="reviewing", task_info=manager.get_task_info("reviewing"))
            raise

    register_turn(agent, "reviewing", asyncio.create_task(review_turn()))
    await asyncio.sleep(0)
    wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await _wait_for_exit(agent)
    assert finished == []
    assert [item.handed_back.task_id for item in agent.team_comms.handbacks()] == ["reviewing"]


@pytest.mark.asyncio
async def test_a_running_task_whose_answer_was_sent_is_finished_not_handed_back(
    manager, monkeypatch
):
    """Regression (2026-10-17): a task finishing up after its final answer went out
    (FINALIZING_PLAN, answer dispatched) was handed back and run again by another
    worker, which sent a second answer."""

    _add(manager, "unanswered")
    _add(manager, "answered", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("answered", ProgressState.FINALIZING_PLAN, NOW())
    manager.execution_state.turn_action_outbox["act_answered"] = TurnActionOutboxRecord(
        action_id="act_answered",
        batch_id="batch_answered",
        source_task_id="answered",
        action_index=0,
        action_type="task_response",
        payload_json={"answer": "Here is the summary."},
        status="dispatched",
    )
    agent = _worker(manager, monkeypatch=monkeypatch)

    async def complete_after_dispatch(*, task_id, after_action_report_sent=False):
        manager.complete_task(task_id, NOW())
        return True

    agent._complete_local_task_finalization_after_dispatch = complete_after_dispatch
    turn_cancelled = asyncio.Event()

    async def finishing_turn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await handle_interrupted_turn(
                agent, task_id="answered", task_info=manager.get_task_info("answered")
            )
            turn_cancelled.set()
            raise

    register_turn(agent, "answered", asyncio.create_task(finishing_turn()))
    await asyncio.sleep(0)

    wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await _wait_for_exit(agent)

    assert turn_cancelled.is_set()
    agent._cancel_task_tree_locally.assert_not_awaited()
    # Only the task that has not started goes back to the lead.
    assert [item.handed_back.task_id for item in agent.team_comms.handbacks()] == ["unanswered"]
    archived = manager.get_task_from_db("answered")
    assert archived is not None and archived["status"] == ProgressState.COMPLETED.value
    assert not manager.is_execution_cancelled("answered")


@pytest.mark.asyncio
async def test_diverted_delivery_is_acked_when_handed_back_and_refused_on_failure(
    manager, monkeypatch
):
    agent = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)
    delivery = SimpleNamespace(
        envelope=_request_envelope("new"), ack=AsyncMock(), nak=AsyncMock()
    )
    assert not await wind_down.divert_request_during_wind_down(agent, delivery)

    agent._lease_loss_wind_down = _wind_down_state()
    assert await wind_down.divert_request_during_wind_down(agent, delivery)
    delivery.ack.assert_awaited_once()

    failing = SimpleNamespace(
        envelope=_request_envelope("later"), ack=AsyncMock(), nak=AsyncMock()
    )
    agent._lease_loss_wind_down = _wind_down_state()
    agent._lease_loss_wind_down.deadline = datetime.now(timezone.utc)
    agent.team_comms = _Comms(failures=1)
    assert await wind_down.divert_request_during_wind_down(agent, failing)
    failing.nak.assert_awaited_once()
    failing.ack.assert_not_awaited()


# --- lead receipt -----------------------------------------------------------


def _handback(worker="worker_1", launch="launch_1", team="team") -> WorkerTaskHandbackV1:
    return WorkerTaskHandbackV1(
        handback_id=handback_identity(worker_agent_id=worker, launch_id=launch, task_id="t1"),
        team_id=team,
        worker_agent_id=worker,
        worker_named_agent_type="WebResearchAgent",
        evidence=WorkerLeaseLossEvidenceV1(
            launch_id=launch,
            lease_lost_at_utc=datetime.now(timezone.utc),
            reason="stale_launch_lease",
        ),
        handed_back=HandedBackTaskV1(
            task_id="t1",
            disposition="interrupted",
            prior_state="in_progress",
            task=Task(task_id="t1", task_type=MessageMetaType.REQUEST),
            header=MessageHeader(),
            meta=MessageMeta(),
        ),
    )


@pytest.fixture
def lead(tmp_path, monkeypatch):
    manager = TaskManager(db_path=str(tmp_path / "lead.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    launches = SimpleNamespace(
        get=lambda launch_id, team_id: (
            SimpleNamespace(agent_id="worker_1") if launch_id == "launch_1" else None
        )
    )
    monkeypatch.setattr(
        "custom_agents.worker_handback_runtime.AgentLaunchRepository",
        lambda _settings: launches,
    )
    return SimpleNamespace(
        id="lead",
        team_id="team",
        is_team_lead=True,
        task_manager=manager,
        _logger=logging.getLogger("test.lead"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handback", "sender", "is_lead"),
    [
        (_handback(team="other"), "worker_1", True),
        (_handback(), "impostor", True),
        (_handback(launch="launch_unknown"), "worker_1", True),
        (_handback(), "worker_1", False),
    ],
    ids=["other_team", "sender_mismatch", "unowned_launch", "not_lead"],
)
async def test_lead_rejects_handbacks_it_cannot_attribute(lead, handback, sender, is_lead):
    lead.is_team_lead = is_lead
    saved = await handle_worker_task_handback(
        lead,
        handback=handback,
        msg_header=MessageHeader(from_agent_id=sender),
        msg_meta=MessageMeta(),
    )
    assert saved is False
    assert WorkerHandbackRepository(lead.task_manager.storage_settings).list_for_task(
        handback.team_id, "t1"
    ) == ()


# --- access requests of failed waiting tasks ---------------------------------


@pytest.fixture
def access_storage(tmp_path, monkeypatch):
    """The manager database the access store shares, as in test_access_request_tools."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(None)


@pytest.mark.asyncio
@pytest.mark.skipif(not access_tests.BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
async def test_failing_a_waiting_task_at_the_deadline_withdraws_its_access_request(
    access_storage, tmp_path, monkeypatch
):
    """The task-ended cleanup every agent registers closes the person's review."""

    requester = access_tests.make_agent(access_storage, tmp_path)
    set_agent_ref(requester)
    access_runtime.AccessDecisionHook(requester)  # registers the cleanup, as for every agent
    context = access_tests.Context()
    with pytest.raises(InterruptException):
        access_tools.request_access(tool_id="fn:schedules", level="write", reason="Track", tool_context=context)
    request_id = context.raised[0]["access_request_id"]
    manager = requester.task_manager
    manager.update_main_task_state(requester.test_task_id, ProgressState.WAITING, NOW())
    agent = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)

    wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await _wait_for_exit(agent)

    request = access_runtime.runtime_requests(requester).service.store.get_request(request_id)
    assert request.status is AccessRequestStatus.WITHDRAWN
    assert "failed" in request.decision_note
    agent._send_task_error_response.assert_awaited_once()
    assert agent.team_comms.sent == []


@pytest.mark.asyncio
async def test_shutdown_stops_and_joins_the_wind_down(manager, monkeypatch):
    """Tasks not yet handed back stay saved for same-role recovery."""
    _add(manager, "queued")
    agent = _worker(manager, monkeypatch=monkeypatch)
    agent._pending_work_task = None
    agent._pending_work_timers = set()
    leaving, release = asyncio.Event(), asyncio.Event()

    async def slow_unsubscribe(role):
        leaving.set()
        await release.wait()
        return 1

    agent.adapter.unsubscribe_consumer_role = slow_unsubscribe
    state = wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease", recent_scheduler_errors=[])
    await asyncio.wait_for(leaving.wait(), 5)
    assert state.task in agent._owned_tasks

    await asyncio.wait_for(close_pending_work(agent), 5)
    assert state.task.done() and agent._owned_tasks == set()
    assert agent.exited  # its own exit path still ran
    assert agent.team_comms.sent == []
    assert manager.get_task_info("queued") is not None


@pytest.mark.asyncio
async def test_shutdown_during_a_handback_keeps_the_tasks_not_yet_handed_back(manager, monkeypatch):
    """Stopped while it hands back the running task (after its commands were stopped),
    the wind-down leaves that task and the unstarted one saved for same-role recovery."""
    _add(manager, "running", ProgressState.IN_PROGRESS)
    _add(manager, "queued")
    agent = _worker(manager, monkeypatch=monkeypatch)
    agent._pending_work_task = None
    agent._pending_work_timers = set()
    publishing = asyncio.Event()

    async def held_publish(_agent, _state, _handed_back):
        publishing.set()
        await asyncio.Event().wait()  # the handback is still being sent when shutdown starts

    monkeypatch.setattr(wind_down, "_publish_handback", held_publish)

    async def running_turn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await handle_interrupted_turn(agent, task_id="running", task_info=manager.get_task_info("running"))
            raise

    register_turn(agent, "running", asyncio.create_task(running_turn()))
    await asyncio.sleep(0)
    state = wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease", recent_scheduler_errors=[])
    await asyncio.wait_for(publishing.wait(), 5)

    await asyncio.wait_for(close_pending_work(agent), 5)
    assert state.task.done() and agent.exited
    assert agent.team_comms.handbacks() == []
    assert manager.get_task_info("running") is not None
    assert manager.get_task_info("queued") is not None


def test_winding_down_tells_the_agents_record_at_once() -> None:
    """Its signed record says it takes no more work before any later state refresh (custom_agents.agent_host)."""

    from custom_agents.agent_host import agent_record_state

    reported: list[tuple[str, bool]] = []
    agent = SimpleNamespace(id="worker_1", paused=False, is_executing=True, _worker_launch=None,
                            _logger=logging.getLogger("test.wind_down"), schedule_workers={})
    agent._report_record_state = lambda: reported.append(agent_record_state(agent))
    original = wind_down.start_owned_task
    wind_down.start_owned_task = lambda *args, **kwargs: None
    try:
        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    finally:
        wind_down.start_owned_task = original
    assert reported == [("busy", False)]


@pytest.mark.asyncio
async def test_a_wind_down_for_another_contract_names_that_reason_not_a_lost_lease(manager, monkeypatch, caplog):
    """A pool worker whose process runs another message contract winds down this way (custom_agents.contract_drain).

    It lost no lease, so its log lines, the failure its requester is told for a task still waiting at the deadline,
    and the evidence its handbacks carry name the actual reason.
    """

    from custom_messages.contracts.worker_handback_v1 import WIND_DOWN_OTHER_CONTRACT

    _add(manager, "queued")
    _add(manager, "waiting", ProgressState.WAITING)
    agent = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)

    with caplog.at_level(logging.INFO, logger="test.wind_down"):
        wind_down.start_lease_loss_wind_down(
            agent, reason="Its process runs another message contract than its installation.",
            cause=WIND_DOWN_OTHER_CONTRACT)
        await _wait_for_exit(agent)

    handbacks = agent.team_comms.handbacks()
    assert [item.handed_back.task_id for item in handbacks] == ["queued"]
    assert handbacks[0].evidence.cause == WIND_DOWN_OTHER_CONTRACT
    failure = agent._send_task_error_response.await_args.args[3]
    assert "another message contract" in failure and "lease" not in failure
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=worker_wind_down_started" in logged and "cause=other_message_contract" in logged
    assert "another message contract" in logged and "lease" not in logged.lower()


@pytest.mark.asyncio
async def test_wind_down_keeps_and_resumes_a_timed_wait_before_the_deadline(manager, monkeypatch):
    """A6: wind-down keeps a timed-wait task and resumes it when due even though the scheduler loop stopped."""
    start = datetime.now(timezone.utc)
    now_box = [start]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-wd",
            call_id="call-wd",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-wd",
            minutes=5,
            reason="Portainer redeploy",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(minutes=5)).isoformat(),
        ),
    )
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    resumed_called = asyncio.Event()

    def finish_resumed_task(_agent):
        resumed_called.set()
        manager.complete_task("waiting", now_box[0].isoformat())

    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", finish_resumed_task)

    state = wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await asyncio.sleep(0.03)
    # While the timer is still in the future, the task stays resumable on this worker (not handed back).
    assert "waiting" in state.resumable_task_ids
    assert agent.team_comms.handbacks() == []
    assert not resumed_called.is_set()
    assert not agent.exited

    # Timer comes due before the wind-down deadline: wind-down resumes it and exits once finished.
    now_box[0] = start + timedelta(minutes=6)
    await _wait_for_exit(agent)

    assert resumed_called.is_set()
    assert agent.exited
    assert agent.team_comms.handbacks() == []
    agent._send_task_error_response.assert_not_awaited()
    assert get_human_interaction_blocker(manager, "waiting") is None
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.COMPLETED.value


@pytest.mark.asyncio
async def test_wind_down_fails_hopeless_timed_wait_immediately_on_wind_down_start(
    manager, monkeypatch, caplog
):
    """#210 / P1 / C05: a WAITING timed wait whose resume_at outlasts the 30-minute deadline fails immediately."""
    from custom_agents.timed_wait_runtime import resume_due_timed_waits

    start = datetime.now(timezone.utc)
    now_box = [start]
    monkeypatch.setattr(timed_waits_rules, "utc_now", lambda: now_box[0])

    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    resume_at = (start + timedelta(hours=2)).isoformat()
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-late",
            call_id="call-late",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-late",
            minutes=120,
            reason="long redeploy",
            requested_at=start.isoformat(),
            resume_at=resume_at,
        ),
    )
    # Full 30-minute wind-down deadline: waiting for it would time out the test.
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    fake_strands = SimpleNamespace(replace_before_next_turn=False)
    agent._interrupted_turn_sessions = {
        "waiting": SimpleNamespace(
            task_id="waiting",
            interrupts=[SimpleNamespace(interrupt_id="int-late")],
            strands=fake_strands,
        )
    }
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
        await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    assert agent.team_comms.handbacks() == []
    agent._send_task_error_response.assert_awaited_once()
    sent_message = agent._send_task_error_response.await_args.args[3]
    assert sent_message == (
        "This task paused for 120 minutes, but its worker is shutting down before the wait ends, "
        "so the task was stopped. Ask again to rerun it."
    )
    assert "within 30 minutes" not in sent_message
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in caplog.text
    assert "agent_id=worker_1" in caplog.text
    assert "task_id=waiting" in caplog.text
    assert "cause=launch_lease_lost" in caplog.text
    assert f"resume_at={resume_at}" in caplog.text
    assert get_human_interaction_blocker(manager, "waiting") is None
    assert "waiting" not in agent._interrupted_turn_sessions
    assert fake_strands.replace_before_next_turn is True
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value

    # Moving past the original resume_at does not resume anything.
    now_box[0] = start + timedelta(hours=3)
    assert await resume_due_timed_waits(agent) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    ["launch_lease_lost", "other_message_contract"],
    ids=["lease_loss", "contract_drain"],
)
async def test_wind_down_fails_hopeless_timed_wait_started_after_classification_on_next_poll(
    manager, monkeypatch, caplog, cause
):
    """#210 / P1 (both causes): a resumable task that enters a hopeless timed wait after classification fails on the next poll."""
    start = datetime.now(timezone.utc)
    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    # Initially waiting on a person (not hopeless at classification).
    record_human_interaction_blocker(
        manager,
        "waiting",
        {"kind": "access_request", "access_request_id": "req-1"},
    )
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING):
        state = wind_down.start_lease_loss_wind_down(agent, reason="wind_down_reason", cause=cause)
        await asyncio.sleep(0.03)
        assert "waiting" in state.resumable_task_ids
        assert not agent.exited

        # The task now enters a 90-minute timed wait (past the 30-minute deadline).
        record_human_interaction_blocker(
            manager,
            "waiting",
            timed_wait_record(
                interrupt_id="int-after",
                call_id="call-after",
                tool=WAIT_TOOL_NAME,
                tool_use_id="tu-after",
                minutes=90,
                reason="long migration",
                requested_at=start.isoformat(),
                resume_at=(start + timedelta(minutes=90)).isoformat(),
            ),
        )
        await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    agent._send_task_error_response.assert_awaited_once()
    sent_message = agent._send_task_error_response.await_args.args[3]
    assert sent_message == (
        "This task paused for 90 minutes, but its worker is shutting down before the wait ends, "
        "so the task was stopped. Ask again to rerun it."
    )
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in caplog.text
    assert f"cause={cause}" in caplog.text
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value


@pytest.mark.asyncio
async def test_wind_down_keeps_deadline_message_when_timed_wait_expires_at_deadline(
    manager, monkeypatch, caplog
):
    """C05: a timed wait still unfinished at the deadline uses the deadline-expiry message, not the early message."""
    start = datetime.now(timezone.utc)
    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    # Held answer prevents early failure even if resume_at is after the short test deadline.
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-held-expire",
            call_id="call-held-expire",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-held-expire",
            minutes=5,
            reason="short wait",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(minutes=5)).isoformat(),
        ),
    )
    mark_answer_held(
        manager,
        "waiting",
        "timed_wait",
        "int-held-expire",
        holder_id="worker_1",
        at=start.isoformat(),
    )
    agent = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
        await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    agent._send_task_error_response.assert_awaited_once()
    sent_message = agent._send_task_error_response.await_args.args[3]
    assert sent_message.startswith(
        "This task was paused on a timed wait when its worker lost its launch lease. "
        "The wait did not finish within "
    )
    assert "event=worker_wind_down_wait_expired" in caplog.text
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" not in caplog.text
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value


@pytest.mark.asyncio
async def test_wind_down_does_not_early_fail_held_answer_or_person_waits(manager, monkeypatch):
    """#210 / P1: a held timed-wait answer is not failed early, and person/setup waits keep their normal timeout."""
    start = datetime.now(timezone.utc)
    _add(manager, "held_wait", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("held_wait", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "held_wait",
        timed_wait_record(
            interrupt_id="int-held",
            call_id="call-held",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-held",
            minutes=120,
            reason="already answered",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(hours=2)).isoformat(),
        ),
    )
    mark_answer_held(
        manager,
        "held_wait",
        "timed_wait",
        "int-held",
        holder_id="worker_1",
        at=start.isoformat(),
    )

    _add(manager, "person_wait", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("person_wait", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "person_wait",
        {"kind": "access_request", "access_request_id": "req-keep"},
    )

    _add(manager, "setup_wait", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("setup_wait", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "setup_wait",
        {"kind": "tool_setup_run", "run_id": "run-keep", "interrupt_id": "int-setup"},
    )

    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    state = wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await asyncio.sleep(0.04)

    # None of the three tasks were failed early.
    assert state.resumable_task_ids == {"held_wait", "person_wait", "setup_wait"}
    assert answer_held(get_human_interaction_blocker(manager, "held_wait"), "worker_1")
    agent._send_task_error_response.assert_not_awaited()
    assert not agent.exited

    # All three finish normally before the 30-minute deadline; worker exits cleanly.
    for task_id in ("held_wait", "person_wait", "setup_wait"):
        manager.complete_task(task_id, start.isoformat())
    await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    agent._send_task_error_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_wind_down_isolates_hopeless_timed_wait_error_from_handbacks_and_retries_on_poll(
    manager, monkeypatch, caplog
):
    """C04: a failure while stopping a hopeless timed wait never blocks handbacks and retries on the next poll."""
    start = datetime.now(timezone.utc)
    _add(manager, "queued")
    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-flaky",
            call_id="call-flaky",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-flaky",
            minutes=120,
            reason="flaky db",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(hours=2)).isoformat(),
        ),
    )
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    orig_fail = wind_down.fail_timed_wait_outlasting_deadline
    attempts = [0]

    async def fail_once(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("sqlite busy")
        return await orig_fail(*args, **kwargs)

    monkeypatch.setattr(wind_down, "fail_timed_wait_outlasting_deadline", fail_once)

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
        await _wait_for_exit(agent, timeout=2.0)

    assert attempts[0] == 2
    assert agent.exited
    assert [item.handed_back.task_id for item in agent.team_comms.handbacks()] == ["queued"]
    assert "event=worker_wind_down_timed_wait_fail_error" in caplog.text
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in caplog.text
    agent._send_task_error_response.assert_awaited_once()
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value


@pytest.mark.asyncio
async def test_wind_down_early_failure_stops_pending_subtasks(manager, monkeypatch):
    """C13: failing a hopeless timed wait early still sends stop requests for its pending delegated subtasks."""
    start = datetime.now(timezone.utc)
    _add(manager, "parent", ProgressState.IN_PROGRESS)
    subtask = Task(
        task_id="child",
        parent_task_id="parent",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Do child work",
    )
    manager.add_subtask(
        "parent",
        subtask,
        MessageHeader(from_agent_id="worker_1", to_routing_path="specialist_1"),
        MessageMeta(),
    )
    manager.update_main_task_state("parent", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "parent",
        timed_wait_record(
            interrupt_id="int-parent",
            call_id="call-parent",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-parent",
            minutes=120,
            reason="long redeploy",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(hours=2)).isoformat(),
        ),
    )

    stop_requests: list[tuple[str, str, str]] = []

    async def record_stop(_agent, *, parent_task_id, subtask_info, reason):
        stop_requests.append((parent_task_id, subtask_info.task.task_id, reason))

    monkeypatch.setattr(wind_down, "request_subtask_stop", record_stop)
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    archived = manager.get_task_from_db("parent")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value
    expected_message = (
        "This task paused for 120 minutes, but its worker is shutting down before the wait ends, "
        "so the task was stopped. Ask again to rerun it."
    )
    assert stop_requests == [("parent", "child", expected_message)]
    agent._send_task_error_response.assert_awaited_once()
    assert agent._send_task_error_response.await_args.args[3] == expected_message


@pytest.mark.asyncio
async def test_wind_down_for_another_contract_fails_hopeless_timed_wait_immediately(
    manager, monkeypatch, caplog
):
    """P1 on release/0.10: a hopeless timed wait during contract-drain wind-down fails at once with cause logged."""
    from custom_messages.contracts.worker_handback_v1 import WIND_DOWN_OTHER_CONTRACT

    start = datetime.now(timezone.utc)
    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    subtask = Task(
        task_id="child-drain",
        parent_task_id="waiting",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Child work",
    )
    manager.add_subtask(
        "waiting",
        subtask,
        MessageHeader(from_agent_id="worker_1", to_routing_path="specialist_1"),
        MessageMeta(),
    )
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    resume_at = (start + timedelta(hours=2)).isoformat()
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-contract-early",
            call_id="call-contract-early",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-contract-early",
            minutes=120,
            reason="long redeploy",
            requested_at=start.isoformat(),
            resume_at=resume_at,
        ),
    )
    stop_requests: list[tuple[str, str, str]] = []

    async def record_stop(_agent, *, parent_task_id, subtask_info, reason):
        stop_requests.append((parent_task_id, subtask_info.task.task_id, reason))

    monkeypatch.setattr(wind_down, "request_subtask_stop", record_stop)
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING, logger="test.wind_down"):
        wind_down.start_lease_loss_wind_down(
            agent,
            reason="Its process runs another message contract than its installation.",
            cause=WIND_DOWN_OTHER_CONTRACT,
        )
        await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    agent._send_task_error_response.assert_awaited_once()
    sent_message = agent._send_task_error_response.await_args.args[3]
    expected_message = (
        "This task paused for 120 minutes, but its worker is shutting down before the wait ends, "
        "so the task was stopped. Ask again to rerun it."
    )
    assert sent_message == expected_message
    assert "lease" not in sent_message.lower()
    assert stop_requests == [("waiting", "child-drain", expected_message)]
    assert get_human_interaction_blocker(manager, "waiting") is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in logged
    assert "cause=other_message_contract" in logged
    assert f"resume_at={resume_at}" in logged
    assert "lease" not in logged.lower()
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value


@pytest.mark.asyncio
async def test_wind_down_for_another_contract_fails_late_timed_wait_naming_that_cause(
    manager, monkeypatch, caplog
):
    """On release/0.10, a held timed wait that expires at the contract-drain deadline names that cause, not a lost lease."""
    from custom_messages.contracts.worker_handback_v1 import WIND_DOWN_OTHER_CONTRACT

    start = datetime.now(timezone.utc)
    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-contract",
            call_id="call-contract",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-contract",
            minutes=5,
            reason="short redeploy",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(minutes=5)).isoformat(),
        ),
    )
    mark_answer_held(
        manager,
        "waiting",
        "timed_wait",
        "int-contract",
        holder_id="worker_1",
        at=start.isoformat(),
    )
    agent = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING, logger="test.wind_down"):
        wind_down.start_lease_loss_wind_down(
            agent,
            reason="Its process runs another message contract than its installation.",
            cause=WIND_DOWN_OTHER_CONTRACT,
        )
        await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    agent._send_task_error_response.assert_awaited_once()
    sent_message = agent._send_task_error_response.await_args.args[3]
    assert "timed wait" in sent_message
    assert "another message contract" in sent_message and "lease" not in sent_message.lower()
    assert "event=worker_wind_down_wait_expired" in caplog.text
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" not in caplog.text
    archived = manager.get_task_from_db("waiting")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    ["launch_lease_lost", "other_message_contract"],
    ids=["lease_loss", "contract_drain"],
)
async def test_f1_wind_down_treats_wait_in_last_poll_margin_as_hopeless(manager, monkeypatch, caplog, cause):
    """F1 (both causes): a wait due inside the final poll margin fails immediately as hopeless."""
    monkeypatch.setattr(wind_down, "WIND_DOWN_POLL_SECONDS", 5.0)
    start = datetime.now(timezone.utc)
    _add(manager, "near_deadline", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("near_deadline", ProgressState.WAITING, start.isoformat())
    agent = _worker(manager, wait_seconds=60.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    state = wind_down.start_lease_loss_wind_down(agent, reason="wind_down_reason", cause=cause)
    state.deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
    resume_at = state.deadline - timedelta(seconds=1)
    record_human_interaction_blocker(
        manager,
        "near_deadline",
        timed_wait_record(
            interrupt_id="int-near",
            call_id="call-near",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-near",
            minutes=1,
            reason="near deadline",
            requested_at=(resume_at - timedelta(minutes=1)).isoformat(),
            resume_at=resume_at.isoformat(),
        ),
    )

    with caplog.at_level(logging.WARNING):
        # Must fail immediately (< 2s) as hopeless instead of sleeping 5s and hitting the deadline.
        await asyncio.wait_for(state.task, timeout=2.0)

    assert agent.exited
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in caplog.text
    assert f"cause={cause}" in caplog.text
    assert "event=worker_wind_down_wait_expired" not in caplog.text
    agent._send_task_error_response.assert_awaited_once()
    assert "shutting down before the wait ends" in agent._send_task_error_response.await_args.args[3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    ["launch_lease_lost", "other_message_contract"],
    ids=["lease_loss", "contract_drain"],
)
async def test_wind_down_poll_after_deadline_fails_due_wait_with_timed_wait_message_without_resuming(
    manager, monkeypatch, cause
):
    """A poll that slips past the deadline fails a due timed wait with the timed-wait message; it does not resume it first."""
    start = datetime.now(timezone.utc)
    _add(manager, "due_at_deadline", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("due_at_deadline", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "due_at_deadline",
        timed_wait_record(
            interrupt_id="int-due-dl",
            call_id="call-due-dl",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-due-dl",
            minutes=1,
            reason="already due",
            requested_at=(start - timedelta(minutes=2)).isoformat(),
            resume_at=(start - timedelta(seconds=1)).isoformat(),
        ),
    )
    agent = _worker(manager, wait_seconds=0.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}
    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)

    state = wind_down.start_lease_loss_wind_down(agent, reason="wind_down_reason", cause=cause)
    state.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    await asyncio.wait_for(state.task, timeout=2.0)

    assert woken == []
    assert agent.exited
    archived = manager.get_task_from_db("due_at_deadline")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value
    agent._send_task_error_response.assert_awaited_once()
    assert agent._send_task_error_response.await_args.args[3].startswith(
        f"This task was paused on a timed wait when its worker {wind_down._WHAT_HAPPENED[cause]}."
    )


@pytest.mark.asyncio
async def test_f5_wind_down_early_failure_finishes_followups_when_post_fail_write_raises(
    manager, monkeypatch, caplog
):
    """F5: if a history write raises after fail_task committed FAILED, still stop subtasks and notify requester."""
    start = datetime.now(timezone.utc)
    _add(manager, "waiting", ProgressState.IN_PROGRESS)
    subtask = Task(
        task_id="child_f5",
        parent_task_id="waiting",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Subtask under hopeless wait",
    )
    manager.add_subtask(
        "waiting",
        subtask,
        MessageHeader(from_agent_id="worker_1", to_routing_path="specialist_1"),
        MessageMeta(),
    )
    manager.update_main_task_state("waiting", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "waiting",
        timed_wait_record(
            interrupt_id="int-f5",
            call_id="call-f5",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-f5",
            minutes=120,
            reason="long",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(hours=2)).isoformat(),
        ),
    )
    stop_requests: list[tuple[str, str]] = []

    async def record_stop(_agent, *, parent_task_id, subtask_info, reason):
        stop_requests.append((parent_task_id, subtask_info.task.task_id))

    monkeypatch.setattr(wind_down, "request_subtask_stop", record_stop)
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    orig_append = manager.append_history_entry
    failed_once = [False]

    def flaky_history(task_id, summary, **kw):
        if summary.startswith("Task failed:") and not failed_once[0]:
            failed_once[0] = True
            raise RuntimeError("sqlite busy after FAILED was saved")
        return orig_append(task_id, summary, **kw)

    monkeypatch.setattr(manager, "append_history_entry", flaky_history)

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
        await _wait_for_exit(agent, timeout=3.0)

    assert agent.exited
    assert failed_once[0] is True
    assert stop_requests == [("waiting", "child_f5")]
    agent._send_task_error_response.assert_awaited_once()
    assert "event=timed_wait_wind_down_fail_post_write_error" in caplog.text
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in caplog.text
    assert "event=worker_wind_down_timed_wait_fail_error" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    ["launch_lease_lost", "other_message_contract"],
    ids=["lease_loss", "contract_drain"],
)
async def test_c06_discard_steering_failure_after_saved_fail_still_notifies_requester_and_stops_subtasks(
    manager, monkeypatch, caplog, cause
):
    """C06: raising discard_task_steering_state after fail_task saved still finishes subtask stop, requester response, and remaining deadline tasks for both causes."""
    start = datetime.now(timezone.utc)
    # Part 1: early hopeless-wait path.
    _add(manager, "early_parent", ProgressState.IN_PROGRESS)
    subtask = Task(
        task_id="early_child",
        parent_task_id="early_parent",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Child work",
    )
    manager.add_subtask(
        "early_parent",
        subtask,
        MessageHeader(from_agent_id="worker_1", to_routing_path="specialist_1"),
        MessageMeta(),
    )
    manager.update_main_task_state("early_parent", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "early_parent",
        timed_wait_record(
            interrupt_id="int-c06-early",
            call_id="call-c06-early",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-c06-early",
            minutes=120,
            reason="long",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(hours=2)).isoformat(),
        ),
    )
    stop_requests: list[tuple[str, str]] = []

    async def record_stop(_agent, *, parent_task_id, subtask_info, reason):
        stop_requests.append((parent_task_id, subtask_info.task.task_id))

    async def broken_discard(*_args, **_kwargs):
        raise RuntimeError("sqlite busy during steering discard")

    monkeypatch.setattr(wind_down, "request_subtask_stop", record_stop)
    monkeypatch.setattr(wind_down, "discard_task_steering_state", broken_discard)

    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent, reason="wind_down_reason", cause=cause)
        await _wait_for_exit(agent, timeout=2.0)

    assert agent.exited
    assert stop_requests == [("early_parent", "early_child")]
    agent._send_task_error_response.assert_awaited_once()
    assert "event=timed_wait_wind_down_discard_steering_failed" in caplog.text
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" in caplog.text
    assert "event=worker_wind_down_timed_wait_fail_error" not in caplog.text

    # Part 2: deadline path with TWO waiting tasks — steering discard failure on the first must still
    # notify its requester AND proceed to fail and notify the second task.
    caplog.clear()
    for tid in ("dl_one", "dl_two"):
        _add(manager, tid, ProgressState.IN_PROGRESS)
        manager.update_main_task_state(tid, ProgressState.WAITING, start.isoformat())
        record_human_interaction_blocker(
            manager,
            tid,
            {"kind": "access_request", "access_request_id": f"req-{tid}"},
        )
    agent2 = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)
    agent2._interrupted_turn_sessions = {}
    agent2._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent2, reason="wind_down_reason", cause=cause)
        await _wait_for_exit(agent2, timeout=2.0)

    assert agent2.exited
    notified_ids = {call.args[0].task_id for call in agent2._send_task_error_response.await_args_list}
    assert notified_ids == {"dl_one", "dl_two"}
    assert "event=worker_wind_down_wait_expired_discard_steering_failed" in caplog.text
    assert caplog.text.count("event=worker_wind_down_wait_expired ") == 2
    for tid in ("dl_one", "dl_two"):
        archived = manager.get_task_from_db(tid)
        assert archived is not None and archived["status"] == ProgressState.FAILED.value


@pytest.mark.asyncio
async def test_wind_down_leaves_a_taken_over_timed_wait_alone_without_answering_or_stopping_subtasks(
    manager, tmp_path, monkeypatch, caplog
):
    """C23: another agent of the type adopted the worker's saved work; the wind-down must not fail, answer or stop it."""
    start = datetime.now(timezone.utc)
    _add(manager, "parent", ProgressState.IN_PROGRESS)
    manager.add_subtask(
        "parent",
        Task(task_id="child", parent_task_id="parent", task_type=MessageMetaType.REQUEST, problem_statement="Fetch"),
        MessageHeader(from_agent_id="worker_1", to_routing_path="team_Other"),
        MessageMeta(to_named_agent_type="Other"),
    )
    manager.update_main_task_state("parent", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "parent",
        timed_wait_record(
            interrupt_id="int-parent",
            call_id="call-parent",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-parent",
            minutes=120,
            reason="long",
            requested_at=start.isoformat(),
            resume_at=(start + timedelta(hours=2)).isoformat(),
        ),
    )
    # The real custody transfer: a second agent of the same type adopts the saved row.
    new_owner = TaskManager(db_path=str(tmp_path / "tasks.db"))
    new_owner.set_owner_identity("worker_2", "Worker", "team", "WebResearchAgent")
    assert new_owner.restore_runtime_state_for_owner_ids(["worker_1"])
    stop_requests: list[str] = []

    async def record_stop(_agent, *, parent_task_id, subtask_info, reason):
        stop_requests.append(subtask_info.task.task_id)

    monkeypatch.setattr(wind_down, "request_subtask_stop", record_stop)
    agent = _worker(manager, wait_seconds=1800.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}

    with caplog.at_level(logging.WARNING):
        wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
        await _wait_for_exit(agent, timeout=5.0)

    assert agent.exited
    agent._send_task_error_response.assert_not_awaited()
    assert stop_requests == []
    assert "event=timed_wait_wind_down_custody_moved" in caplog.text
    assert "event=timed_wait_wind_down_fail_post_write_error" not in caplog.text
    assert "event=worker_wind_down_timed_wait_outlasts_deadline" not in caplog.text
    assert new_owner.get_task_info("parent").state == ProgressState.WAITING
    assert is_timed_wait_blocker(get_human_interaction_blocker(new_owner, "parent"))
    assert new_owner.get_task_from_db("parent") is None


@pytest.mark.asyncio
async def test_contract_drain_deadline_poll_fails_a_due_wait_with_its_message_despite_a_scheduler_pass(
    manager, monkeypatch
):
    """Review F2: during a contract drain the scheduler pass still runs; at the deadline poll it resumes nothing.

    The pass runs right between the poll's snapshot and the step that fails what
    remains (the interleaving the review forced), so the due timed wait is failed
    with the timed-wait message instead of being resumed and then failed as a
    person's wait.
    """
    from custom_agents import scheduled_runtime

    cause = "other_message_contract"
    start = datetime.now(timezone.utc)
    _add(manager, "due", ProgressState.IN_PROGRESS)
    manager.update_main_task_state("due", ProgressState.WAITING, start.isoformat())
    record_human_interaction_blocker(
        manager,
        "due",
        timed_wait_record(
            interrupt_id="int-due",
            call_id="call-due",
            tool=WAIT_TOOL_NAME,
            tool_use_id="tu-due",
            minutes=1,
            reason="already due",
            requested_at=(start - timedelta(minutes=2)).isoformat(),
            resume_at=(start - timedelta(seconds=1)).isoformat(),
        ),
    )
    agent = _worker(manager, wait_seconds=0.0, monkeypatch=monkeypatch)
    agent._interrupted_turn_sessions = {}
    agent._pending_interrupt_responses = {}
    woken: list = []
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", woken.append)
    end_at_deadline = wind_down._end_at_deadline

    async def scheduler_pass_step_then_end(agent_, state_, remaining):
        await scheduled_runtime._resume_due_timed_waits_step(agent_)
        await end_at_deadline(agent_, state_, remaining)

    monkeypatch.setattr(wind_down, "_end_at_deadline", scheduler_pass_step_then_end)
    state = wind_down.start_lease_loss_wind_down(agent, reason="wind_down_reason", cause=cause)
    state.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    await asyncio.wait_for(state.task, timeout=2.0)

    assert woken == []
    assert agent.exited
    archived = manager.get_task_from_db("due")
    assert archived is not None and archived["status"] == ProgressState.FAILED.value
    agent._send_task_error_response.assert_awaited_once()
    assert agent._send_task_error_response.await_args.args[3].startswith(
        f"This task was paused on a timed wait when its worker {wind_down._WHAT_HAPPENED[cause]}."
    )
