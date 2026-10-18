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
from custom_agents import access_runtime
from custom_agents.control_runtime import (
    handle_interrupted_turn,
)
from custom_agents.task_manager import TaskManager
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
from custom_tools.runtime_context import set_agent_ref
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
    agent = SimpleNamespace(
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
        _active_execution_task=None,
        _active_execution_task_id=None,
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

    agent._active_execution_task = asyncio.create_task(running_turn())
    agent._active_execution_task_id = "running"
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

    agent._active_execution_task = asyncio.create_task(review_turn())
    agent._active_execution_task_id = "reviewing"
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

    agent._active_execution_task = asyncio.create_task(finishing_turn())
    agent._active_execution_task_id = "answered"
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
    manager.update_main_task_state(requester._active_execution_task_id, ProgressState.WAITING, NOW())
    agent = _worker(manager, wait_seconds=0.05, monkeypatch=monkeypatch)

    wind_down.start_lease_loss_wind_down(agent, reason="stale_launch_lease")
    await _wait_for_exit(agent)

    request = access_runtime.runtime_requests(requester).service.store.get_request(request_id)
    assert request.status is AccessRequestStatus.WITHDRAWN
    assert "failed" in request.decision_note
    agent._send_task_error_response.assert_awaited_once()
    assert agent.team_comms.sent == []
