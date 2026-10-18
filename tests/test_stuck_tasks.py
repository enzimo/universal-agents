"""People are told when a task is stuck, and when its warning clears.

Real SQLite snapshot, agent-status, access, handback, incident and Inbox
stores; each agent's saved work is written through the real snapshot store, as
its runtime writes it. The chat side is the gateway-bound ``UserNotification``
the lead publishes, captured at ``team_comms``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.people import PERSON_ROLE_ADMIN, PERSON_ROLE_OWNER
from custom_agents import task_context_runtime
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.liveness_observation import observe_liveness
from custom_agents.schedule_store import ScheduleStore
from custom_agents.stuck_tasks import (
    TASK_HANDBACK_UNCLAIMED,
    TASK_NO_PROGRESS,
    TASK_OWNER_GONE,
    TASK_WAITING_FOR_ACCESS,
    TASK_WAITING_FOR_ANSWER,
    report_stuck_tasks,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestV1,
    PersonStanding,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    TeamPersonV1,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import (
    RecoverablePauseRecord,
    RuntimeSnapshotV3,
    TaskSnapshotRecord,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
)
from custom_messages.enums import ControlType, MessageMetaType, ProgressState, RequesterType
from storage.access_repository import AccessRepository
from storage.stuck_work_repository import StuckWorkRepository
from storage.worker_handback_repository import HANDBACK_STATUS_REDISPATCHED, WorkerHandbackRepository
from tests.broker_access_support import context_signer, use_scheduler_key
from tests.change_approval_support import use_signed_contexts

GATEWAY_ROUTE = "gateway.instance.responses"
REQUEST = "Compare ETH/BTC momentum strategies and report the backtest results"


@pytest.fixture
def lead(tmp_path, monkeypatch):
    use_signed_contexts(monkeypatch)
    task_context_runtime._verifier.cache_clear()
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity(agent_id="lead", agent_name="lead", team_id="team", named_agent_type="TeamArchitect")
    yield SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team",
        is_team_lead=True,
        instance_config_path=None,
        schedule_maintenance={},
        task_manager=manager,
        _schedule_store=ScheduleStore(storage_settings=manager.storage_settings),
        _agent_state_store=AgentStateStore(storage_settings=manager.storage_settings, logger=logging.getLogger("t")),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=logging.getLogger("stuck-tasks-test"),
    )
    task_context_runtime._verifier.cache_clear()


def iso(moment: datetime) -> str:
    return moment.isoformat()


def record(task_id, *, state=ProgressState.IN_PROGRESS, parent=None, signed=True, at=None, route=GATEWAY_ROUTE):
    at = at or datetime.now(timezone.utc)
    context = GatewayContext(
        canonical_user_id="jordan",
        session_key="telegram:jordan",
        channel_name="telegram",
        chat_id="42",
        task_context=context_signer().issue(team_id="team", person_id="jordan") if signed else None,
    )
    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=REQUEST if parent is None else "Fetch daily candles",
        parent_task_id=parent,
        task_state=[(ProgressState.ACCEPTED, iso(at)), (state, iso(at))],
    )
    return TaskSnapshotRecord(
        task_id=task_id,
        state=state,
        task=task,
        header=MessageHeader(response_routing_key=route),
        meta=MessageMeta(gateway_context=context),
        owner_person_id="jordan" if signed else None,
    )


def save(lead, owner, agent_type, records, **sections):
    snapshot = RuntimeSnapshotV3(schema_version=3, tasks=list(records), **sections)
    lead.task_manager.runtime_state_store.save_task_runtime(
        expect_existing=False,
        owner_agent_id=owner,
        owner_agent_name=owner,
        team_id="team",
        owner_named_agent_type=agent_type,
        runtime_state_json=snapshot.model_dump_json(),
    )


def seen(lead, agent_id, agent_type="WebResearchAgent"):
    lead._agent_state_store.upsert(
        AgentStatus(
            status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
            named_agent_type=agent_type, self_id=agent_id, is_paused=False, is_executing=True,
            has_execution_capacity=False, is_busy=True, name=agent_id, tools=[], team_id="team",
            agent_role="worker",
        )
    )


def notices(lead, person_id="jordan", roles=()):
    return [n for n, _ in AccessRepository(lead.task_manager.storage_settings).notices_for("team", person_id=person_id, roles=roles)]


def admin_notices(lead):
    return notices(lead, person_id="admin-1", roles=(PERSON_ROLE_ADMIN,))


def record_people(lead, *people):
    AccessRepository(lead.task_manager.storage_settings).replace_people(
        "team",
        [
            TeamPersonV1(team_id="team", person_id=person_id, standing=PersonStanding.ACTIVE, roles=roles,
                         changed_at_utc=datetime.now(timezone.utc))
            for person_id, roles in people
        ],
        at=datetime.now(timezone.utc),
    )


def chats(lead):
    return [call.kwargs["message"] for call in lead.team_comms.send_message.await_args_list]


def open_incidents(lead):
    return StuckWorkRepository(lead.task_manager.storage_settings).open_incidents("team", "task")


@pytest.mark.asyncio
async def test_a_request_whose_worker_stopped_is_reported_once_as_the_persons_request(lead):
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)])
    save(lead, "worker-1", "WebResearchAgent", [
        record("child-a", parent="root", route="lead"), record("child-b", parent="root", route="lead"),
    ])
    soon = datetime.now(timezone.utc) + timedelta(minutes=2)
    await report_stuck_tasks(lead, now=soon)  # not gone long enough yet
    assert not notices(lead)

    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    for second in range(3):
        await report_stuck_tasks(lead, now=later + timedelta(seconds=second))
    (notice,) = notices(lead)
    assert notice.title == f'Your request "{REQUEST}" stopped'
    assert "starts another agent to take the work over" in notice.summary
    (admin,) = admin_notices(lead)
    assert REQUEST not in admin.title + admin.summary  # a person's task stays private
    assert "WebResearchAgent" in admin.summary
    (envelope,) = chats(lead)
    assert envelope.msg_header.to_routing_path == GATEWAY_ROUTE
    assert envelope.msg_content.reason == "work_stuck"
    assert envelope.msg_meta.gateway_context.session_key == "telegram:jordan"
    (incident,) = open_incidents(lead)
    assert (incident.subject_id, incident.reason) == ("root", TASK_OWNER_GONE)

    lead.task_manager.runtime_state_store.clear_task_runtime("worker-1")  # taken over
    await report_stuck_tasks(lead, now=later + timedelta(minutes=1))
    assert chats(lead)[-1].msg_content.reason == "work_recovered"
    assert f'The warning for your request "{REQUEST}" has cleared' in chats(lead)[-1].msg_content.text
    assert not open_incidents(lead)


@pytest.mark.asyncio
async def test_a_live_task_with_no_progress_tells_only_its_person(lead):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    save(lead, "worker-1", "WebResearchAgent", [record("root", at=long_ago)])
    seen(lead, "worker-1")
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))
    (notice,) = notices(lead)
    assert notice.title.endswith("has made no progress")
    assert not admin_notices(lead)
    assert open_incidents(lead)[0].reason == TASK_NO_PROGRESS


@pytest.mark.asyncio
async def test_an_unanswered_question_reminds_the_person_after_a_day(lead):
    now = datetime.now(timezone.utc)
    pause = RecoverablePauseRecord(
        task_id="root", pause_id="p1", trigger="delegated_subtask_timeout", created_at=iso(now - timedelta(days=2))
    )
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.PAUSED)], recoverable_pause_records={"p1": pause})
    await report_stuck_tasks(lead, now=now)
    (notice,) = notices(lead)
    assert notice.title.endswith("is waiting for your answer")
    assert open_incidents(lead)[0].reason == TASK_WAITING_FOR_ANSWER


@pytest.mark.asyncio
async def test_an_undecided_access_request_reminds_the_person_and_administrators(lead):
    now = datetime.now(timezone.utc)
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)])
    AccessRepository(lead.task_manager.storage_settings).create_request(
        AccessRequestV1(
            request_id="req-1", team_id="team", requester_agent_id="lead", requester_agent_type="TeamArchitect",
            person_id="jordan",
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="cli:github"), level=AccessLevel.READ,
            reason="Read the repository", task_id="root", created_at_utc=now - timedelta(days=1, minutes=5),
        )
    )
    await report_stuck_tasks(lead, now=now)
    (notice,) = notices(lead)
    assert notice.title.endswith("is waiting for an access decision")
    assert "Settings > Inbox" in notice.summary
    assert admin_notices(lead)
    assert open_incidents(lead)[0].reason == TASK_WAITING_FOR_ACCESS


@pytest.mark.asyncio
async def test_a_handed_back_task_nobody_picked_up_is_reported(lead):
    now = datetime.now(timezone.utc)
    held = record("root")
    handback = WorkerTaskHandbackV1(
        handback_id="hb-1", team_id="team", worker_agent_id="worker-1", worker_named_agent_type="WebResearchAgent",
        evidence=WorkerLeaseLossEvidenceV1(lease_lost_at_utc=now - timedelta(hours=1), reason="lease lost"),
        handed_back=HandedBackTaskV1(
            task_id="root", disposition="interrupted", prior_state="in_progress",
            task=held.task, header=held.header, meta=held.meta,
        ),
    )
    repository = WorkerHandbackRepository(lead.task_manager.storage_settings)
    repository.record(handback)
    repository.set_status("hb-1", "team", HANDBACK_STATUS_REDISPATCHED)
    await report_stuck_tasks(lead, now=now + timedelta(hours=1))
    (notice,) = notices(lead)
    assert notice.title.endswith("is waiting for an agent")
    assert admin_notices(lead)
    assert open_incidents(lead)[0].reason == TASK_HANDBACK_UNCLAIMED


@pytest.mark.asyncio
async def test_an_owner_who_is_an_administrator_gets_only_the_administrators_notice(lead):
    """Administrators read other words here, so the owner who is one does not get both copies."""
    record_people(lead, ("jordan", (PERSON_ROLE_OWNER,)))
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)])
    save(lead, "worker-1", "WebResearchAgent", [record("child-a", parent="root", route="lead")])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc) + timedelta(minutes=11))
    (notice,) = notices(lead, roles=(PERSON_ROLE_ADMIN,))
    assert notice.recipient_roles == (PERSON_ROLE_ADMIN,)
    assert notice.recipient_person_id is None
    assert "WebResearchAgent" in notice.summary


@pytest.mark.asyncio
async def test_without_a_verified_person_only_administrators_are_told(lead):
    save(lead, "worker-1", "WebResearchAgent", [record("root", signed=False)])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc) + timedelta(minutes=11))
    assert not notices(lead)  # no Inbox notice for an unverified person
    assert admin_notices(lead)
    (envelope,) = chats(lead)  # the chat the request came from still hears about it
    assert envelope.msg_header.to_routing_path == GATEWAY_ROUTE


@pytest.mark.asyncio
async def test_the_check_can_be_turned_off(lead):
    lead.schedule_maintenance = {"stuck_tasks": {"enabled": False}}
    save(lead, "worker-1", "WebResearchAgent", [record("root")])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc) + timedelta(minutes=11))
    assert not notices(lead) and not admin_notices(lead)


def maintenance_record(monkeypatch, at):
    use_scheduler_key(monkeypatch)
    held = record("maintenance", signed=False, route="lead", at=at)
    held.meta.requester_type = RequesterType.TIMER_REQUESTER
    task_context_runtime.sign_system_context(held.meta, team_id="team", schedule_id="maintenance-schedule")
    return held


def heartbeat_at(lead, when):
    seen(lead, "maintenance-worker", "InternalImprovementAgent")
    status = lead._agent_state_store.get_by_id("maintenance-worker")
    status.last_seen_at = iso(when)
    lead._agent_state_store.upsert(status)


@pytest.mark.asyncio
async def test_host_resume_does_not_report_live_maintenance_as_stopped(lead, monkeypatch, caplog):
    """The Oct 9 run resumed five seconds after the lead checked the old heartbeat."""
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    before = datetime(2026, 10, 9, 19, 36, 49, tzinfo=timezone.utc)
    woke = datetime(2026, 10, 9, 19, 54, 15, tzinfo=timezone.utc)
    save(lead, "maintenance-worker", "InternalImprovementAgent", [maintenance_record(monkeypatch, before)])
    heartbeat_at(lead, before)
    observe_liveness(lead, now=before)
    await report_stuck_tasks(lead, now=before)

    clock[0] += 1  # Linux's monotonic clock did not count the host's sleep.
    observe_liveness(lead, now=woke)
    await report_stuck_tasks(lead, now=woke)
    assert "event=liveness_monitor_paused" in caplog.text

    heartbeat_at(lead, woke + timedelta(seconds=5))
    clock[0] += 60
    observe_liveness(lead, now=woke + timedelta(seconds=60))
    await report_stuck_tasks(lead, now=woke + timedelta(seconds=60))
    assert not admin_notices(lead) and not notices(lead) and not chats(lead)
    assert not open_incidents(lead)


@pytest.mark.asyncio
async def test_maintenance_without_a_new_heartbeat_is_reported_after_resume_grace(lead, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    before = datetime.now(timezone.utc)
    woke = before + timedelta(minutes=18)
    save(lead, "maintenance-worker", "InternalImprovementAgent", [maintenance_record(monkeypatch, before)])
    heartbeat_at(lead, before)
    observe_liveness(lead, now=before)
    clock[0] += 1
    observe_liveness(lead, now=woke)
    await report_stuck_tasks(lead, now=woke)
    assert not admin_notices(lead)

    clock[0] += 60
    observe_liveness(lead, now=woke + timedelta(seconds=60))
    await report_stuck_tasks(lead, now=woke + timedelta(seconds=60))
    [notice] = admin_notices(lead)
    assert notice.title == "System maintenance stopped"
    assert "system maintenance" in notice.summary
    assert "person's task" not in notice.summary
    assert not notices(lead) and not chats(lead)
    assert open_incidents(lead)[0].reason == TASK_OWNER_GONE

    # A heartbeat clears the warning, but does not establish new task progress.
    snapshot = lead.task_manager.runtime_state_store.peek_task_runtime_by_team("team")[0].runtime_state_json
    heartbeat_at(lead, woke + timedelta(seconds=61))
    await report_stuck_tasks(lead, now=woke + timedelta(seconds=61))
    assert snapshot == lead.task_manager.runtime_state_store.peek_task_runtime_by_team("team")[0].runtime_state_json
    recovered = next(n for n in admin_notices(lead) if n.title == "System maintenance warning cleared")
    assert "latest progress" in recovered.summary and "work continues" not in recovered.summary
    assert not open_incidents(lead)


@pytest.mark.asyncio
async def test_resume_grace_does_not_close_an_existing_stuck_incident(lead, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    now = datetime.now(timezone.utc)
    save(lead, "worker", "WebResearchAgent", [record("root")])
    await report_stuck_tasks(lead, now=now + timedelta(minutes=11))
    [incident] = open_incidents(lead)
    observe_liveness(lead, now=now + timedelta(minutes=11))
    clock[0] += 1
    woke = now + timedelta(minutes=30)
    observe_liveness(lead, now=woke)
    await report_stuck_tasks(lead, now=woke)
    assert open_incidents(lead)[0].incident_id == incident.incident_id
    assert len(notices(lead)) == 1 and len(admin_notices(lead)) == 1
