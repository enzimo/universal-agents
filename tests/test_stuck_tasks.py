"""People are told when a task is stuck, and when its warning clears.

Real SQLite snapshot, agent-status, access, handback, incident and Inbox
stores; each agent's saved work is written through the real snapshot store, as
its runtime writes it. The chat side is the gateway-bound ``UserNotification``
the lead publishes, captured at ``team_comms``.
"""

from __future__ import annotations

import errno
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.people import PERSON_ROLE_ADMIN, PERSON_ROLE_OWNER
import custom_agents.stuck_tasks as stuck_tasks_module
from custom_agents import task_context_runtime
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.liveness_observation import observe_liveness
from custom_agents.schedule_store import ScheduleStore
from custom_agents.stuck_tasks import (
    TASK_FILES_SENT_BEFORE_UPGRADE,
    TASK_HANDBACK_UNCLAIMED,
    TASK_NO_PROGRESS,
    TASK_OWNER_GONE,
    TASK_WAITING_FOR_ACCESS,
    TASK_WAITING_FOR_ANSWER,
    report_stuck_tasks,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestV1,
    PersonStanding,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    TeamPersonV1,
    new_person_ref,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import (
    PlanStepTaskLink,
    RecoverablePauseRecord,
    RecoveryTaskLink,
    RuntimeSnapshotV3,
    TaskSnapshotRecord,
)
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskSteering
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
)
from custom_messages.enums import (
    ControlType,
    MessageMetaType,
    ProgressState,
    RequesterType,
    TaskHistoryType,
    TaskSteeringType,
)
from runtime_settings.instance_layout import InstanceLayout
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
        instance_root=str(tmp_path / "instance"),
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


def record(
    task_id, *, state=ProgressState.IN_PROGRESS, parent=None, signed=True, at=None, route=GATEWAY_ROUTE, files=()
):
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
        attachments=list(files),
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
            TeamPersonV1(person_ref=new_person_ref(), team_id="team", person_id=person_id, standing=PersonStanding.ACTIVE, roles=roles,
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


# -- files sent before the upgrade --------------------------------------------------


def stored_file(lead, attachment_id, filename, *, folder, exists=True):
    """A file record as the gateway saved it, its copy under ``folder`` of the instance."""
    path = InstanceLayout.at(lead.instance_root).root / folder / f"{attachment_id}_{filename}"
    if exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    return UserTaskAttachment(
        attachment_id=attachment_id, filename=filename, size_bytes=4, sha256="0" * 64, origin="telegram",
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
    )


# Where 0.9's adapter host kept a hosted chat app's files (``interactive_apps`` ``upload_dir``).
ADAPTER_HOST_FOLDER = "runtime/adapter-host-uploads/telegram/session-1"
UPLOADS_FOLDER = "state/uploads/telegram-jordan"


def no_storage_path(text):
    return "adapter-host-uploads" not in text and "state/uploads" not in text


@pytest.mark.asyncio
async def test_a_request_holding_files_sent_before_the_upgrade_tells_its_person_once(lead, caplog):
    caplog.set_level(logging.INFO)
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    new = stored_file(lead, "att_new", "report.pdf", folder=UPLOADS_FOLDER)
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING, files=[old, new])])
    save(lead, "worker-1", "WebResearchAgent", [record("child-a", parent="root", route="lead", files=[old])])
    seen(lead, "worker-1")
    now = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=now)

    (notice,) = notices(lead)
    assert notice.title == f'Your request "{REQUEST}" needs files sent again'
    assert "files you sent before the team was upgraded (photo.png)" in notice.summary
    assert "send them again in this chat" in notice.summary
    assert "report.pdf" not in notice.summary and no_storage_path(notice.summary)
    assert not admin_notices(lead)
    (envelope,) = chats(lead)
    assert envelope.msg_header.to_routing_path == GATEWAY_ROUTE
    assert envelope.msg_content.reason == "work_stuck"
    assert "photo.png" in envelope.msg_content.text and no_storage_path(envelope.msg_content.text)
    (incident,) = open_incidents(lead)
    assert (incident.subject_id, incident.reason) == ("root", TASK_FILES_SENT_BEFORE_UPGRADE)
    assert "event=stuck_work_notified" in caplog.text
    assert f"reason={TASK_FILES_SENT_BEFORE_UPGRADE}" in caplog.text

    await report_stuck_tasks(lead, now=now + timedelta(minutes=1))  # told once
    assert len(chats(lead)) == 1 and len(notices(lead)) == 1

    for owner in ("lead", "worker-1"):  # the request ended
        lead.task_manager.runtime_state_store.clear_task_runtime(owner)
    await report_stuck_tasks(lead, now=now + timedelta(minutes=2))
    assert not open_incidents(lead)
    assert len(chats(lead)) == 1 and len(notices(lead)) == 1  # closed with no "warning cleared"
    assert "event=stuck_work_closed_silently" in caplog.text



def guidance_09(files, key="corr-guidance-1"):
    """Guidance the person sent while the task ran, as 0.9 recorded its acceptance in the task's history.

    The keys are spelled out (not the runtime's constants) so the test pins
    what 0.9 wrote; workers record a forwarded copy the same way.
    """
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        conversation_id="conv-1",
        guidance_text="Use this receipt too",
        attachments=list(files),
        idempotency_key=key,
    )
    return TaskHistoryEntry(
        entry_type=TaskHistoryType.TASK_EVENT,
        summary="Mid-turn proactive steering accepted: Use this receipt too",
        details={
            "event": "task_steering_proactive_guidance_accepted",
            "steering_id": steering.steering_id,
            "conversation_id": "conv-1",
            "dedupe_key": key,
            "steering": steering.model_dump(mode="json"),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("holders", [("root",), ("child-a",), ("root", "child-a")])
async def test_a_file_sent_as_guidance_before_the_upgrade_is_named_once_in_its_requests_notice(lead, holders):
    """The request's own history, a worker's forwarded copy, or both: one notice naming the file once."""
    old = stored_file(lead, "att_guide", "receipt.png", folder=ADAPTER_HOST_FOLDER)
    root = record("root", state=ProgressState.WAITING)
    child = record("child-a", parent="root", route="lead")
    for held in (root, child):
        if held.task_id in holders:
            held.task.execution_history = [guidance_09([old])]
    save(lead, "lead", "TeamArchitect", [root])
    save(lead, "worker-1", "WebResearchAgent", [child])
    seen(lead, "worker-1")
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))

    (notice,) = notices(lead)
    assert notice.title == f'Your request "{REQUEST}" needs files sent again'
    assert "files you sent before the team was upgraded (receipt.png)." in notice.summary
    assert notice.summary.count("receipt.png") == 1 and no_storage_path(notice.summary)
    (envelope,) = chats(lead)
    assert envelope.msg_content.text.count("receipt.png") == 1
    assert [(incident.subject_id, incident.reason) for incident in open_incidents(lead)] == [
        ("root", TASK_FILES_SENT_BEFORE_UPGRADE)
    ]
    assert not admin_notices(lead)

@pytest.mark.asyncio
async def test_a_subtask_still_held_after_its_request_ended_sends_no_second_notice(lead):
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING, files=[old])])
    save(lead, "worker-1", "WebResearchAgent", [record("child-a", parent="root", route="lead", files=[old])])
    seen(lead, "worker-1")
    now = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=now)
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1

    # The person stopped the request; the stop has not reached the worker yet.
    lead.task_manager.runtime_state_store.clear_task_runtime("lead")
    await report_stuck_tasks(lead, now=now + timedelta(minutes=1))
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1 and not admin_notices(lead)
    assert not open_incidents(lead)

    lead.task_manager.runtime_state_store.clear_task_runtime("worker-1")
    await report_stuck_tasks(lead, now=now + timedelta(minutes=2))
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1 and not open_incidents(lead)


def step_link(step_id, request_id="root"):
    return PlanStepTaskLink(root_task_id=request_id, plan_id="plan-1", step_id=f"step-{step_id}", step_task_id=step_id)


def lead_step(step_id):
    """A plan step or recovery task: a task of the lead with no parent and no files of its own."""
    step = record(step_id, state=ProgressState.IN_PROGRESS)
    step.task.problem_statement = f"Step {step_id} of the plan: fetch daily candles"
    return step


def lead_task_delegates(lead, request, task_id, child_files, **links):
    """The lead holds ``request`` and its step ``task_id``; worker-1 holds the step's subtask."""
    for owner in ("lead", "worker-1"):
        lead.task_manager.runtime_state_store.clear_task_runtime(owner)
    save(lead, "lead", "TeamArchitect", [request, lead_step(task_id)], **links)
    # Delegated from the step: the subtask's parent is the step, not the request.
    save(lead, "worker-1", "WebResearchAgent", [record(f"child-{task_id}", parent=task_id, route="lead", files=child_files)])
    seen(lead, "worker-1")


@pytest.mark.asyncio
async def test_the_subtasks_of_each_plan_step_go_into_their_requests_one_notice(lead):
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    other = stored_file(lead, "att_other", "chart.png", folder=ADAPTER_HOST_FOLDER)
    request = record("root", state=ProgressState.WAITING, files=[old])
    now = datetime.now(timezone.utc)

    lead_task_delegates(lead, request, "step-1", [old, other], plan_step_task_links={"step-1": step_link("step-1")})
    await report_stuck_tasks(lead, now=now)
    (notice,) = notices(lead)
    assert notice.title == f'Your request "{REQUEST}" needs files sent again'
    assert "photo.png" in notice.summary and "chart.png" in notice.summary
    assert [incident.subject_id for incident in open_incidents(lead)] == ["root"]

    # Step 1 ended and step 2 runs with its own subtask: still the request's one notice.
    links = {"step-1": step_link("step-1"), "step-2": step_link("step-2")}
    lead_task_delegates(lead, request, "step-2", [old], plan_step_task_links=links)
    await report_stuck_tasks(lead, now=now + timedelta(minutes=1))
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1
    assert [incident.subject_id for incident in open_incidents(lead)] == ["root"]


@pytest.mark.asyncio
async def test_a_recovery_task_for_an_ended_step_counts_as_its_request(lead, caplog):
    caplog.set_level(logging.INFO)
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    request = record("root", state=ProgressState.WAITING)  # only the recovery's subtask holds an old file
    recovery = RecoveryTaskLink(
        original_task_id="step-2", original_plan_id="plan-1", recovery_task_id="recovery-1",
        branch_id="branch-1", failure_type="tool_failure", failure_summary="The exchange did not answer.",
    )
    # Step 2 has ended; its link to the request stays in the lead's saved work.
    lead_task_delegates(
        lead, request, "recovery-1", [old],
        plan_step_task_links={"step-2": step_link("step-2")}, recovery_links={"recovery-1": recovery},
    )
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))
    (notice,) = notices(lead)
    assert notice.title == f'Your request "{REQUEST}" needs files sent again'
    assert "photo.png" in notice.summary
    assert [incident.subject_id for incident in open_incidents(lead)] == ["root"]
    assert "event=stuck_task_request_links_loop" not in caplog.text


@pytest.mark.asyncio
async def test_plan_links_that_loop_are_logged_and_send_no_notice(lead, caplog):
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    lead_task_delegates(
        lead, record("root", state=ProgressState.WAITING), "step-1", [old],
        plan_step_task_links={"step-1": step_link("step-1", "step-2"), "step-2": step_link("step-2", "step-1")},
    )
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))
    assert not notices(lead) and not admin_notices(lead) and not open_incidents(lead)
    assert "event=stuck_task_request_links_loop task_id=child-step-1" in caplog.text


@pytest.mark.asyncio
async def test_a_plan_step_still_held_after_its_request_ended_sends_no_notice(lead):
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    # The request is gone from the lead's saved work; its step and the step's subtask remain.
    save(lead, "lead", "TeamArchitect", [lead_step("step-1")], plan_step_task_links={"step-1": step_link("step-1")})
    save(lead, "worker-1", "WebResearchAgent", [record("child-a", parent="step-1", route="lead", files=[old])])
    seen(lead, "worker-1")
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))
    assert not notices(lead) and not admin_notices(lead) and not chats(lead)
    assert not open_incidents(lead)


def verified_for_nobody(task_record, sender):
    """The request as a sender no person is verified for sent it.

    ``unsigned``: a gateway context with no signed task context (its
    ``canonical_user_id`` is only a claim). ``signed_for_nobody``: a chat app's
    request on a team with gateway users off, which the gateway signs for no
    person.
    """
    if sender == "signed_for_nobody":
        task_record.meta.gateway_context = task_record.meta.gateway_context.model_copy(
            update={
                "canonical_user_id": None,
                "task_context": context_signer().issue(team_id="team", person_id=None, person_roles=()),
            }
        )
    return task_record


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["unsigned", "signed_for_nobody"])
async def test_files_sent_before_the_upgrade_of_unverified_work_go_to_the_requests_own_chat(lead, sender):
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    root = verified_for_nobody(record("root", state=ProgressState.WAITING, signed=False, files=[old]), sender)
    save(lead, "lead", "TeamArchitect", [root])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))

    # No person to give an Inbox notice to: the chat the files came from gets the message.
    assert not notices(lead)
    (envelope,) = chats(lead)
    assert envelope.msg_header.to_routing_path == GATEWAY_ROUTE
    assert "photo.png" in envelope.msg_content.text and no_storage_path(envelope.msg_content.text)
    (admin,) = admin_notices(lead)
    assert admin.title.endswith("needs files sent again")
    assert "uses 1 file sent before the upgrade" in admin.summary
    assert "photo.png" not in admin.title + admin.summary
    assert REQUEST not in admin.title + admin.summary
    assert no_storage_path(admin.summary)


@pytest.mark.asyncio
async def test_unverified_work_with_no_chat_of_its_own_never_uses_a_configured_persons_chat(lead, monkeypatch):
    asked = []

    def configured_route(config_path, person_id):  # the configured user's chat, as instance config gives it
        asked.append(person_id)
        return "gateway.instance.owner", GatewayContext(session_key="telegram:owner", channel_name="telegram")

    monkeypatch.setattr(stuck_tasks_module, "configured_owner_route", configured_route)
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    other = stored_file(lead, "att_other", "chart.png", folder=ADAPTER_HOST_FOLDER)
    # Both requests' reply route is an agent's own, so neither has a chat of its own.
    nobodys = record("nobodys", state=ProgressState.WAITING, signed=False, route="lead", files=[old])
    jordans = record("jordans", state=ProgressState.WAITING, route="lead", files=[other])
    save(lead, "lead", "TeamArchitect", [nobodys, jordans])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))

    # Only the verified request falls back to its person's configured chat.
    assert asked == ["jordan"]
    (envelope,) = chats(lead)
    assert envelope.msg_header.to_routing_path == "gateway.instance.owner"
    assert envelope.msg_content.source_task_id == "jordans" and "chart.png" in envelope.msg_content.text
    (admin,) = admin_notices(lead)
    assert "uses 1 file sent before the upgrade" in admin.summary and "photo.png" not in admin.summary


@pytest.mark.asyncio
async def test_files_this_instance_stored_are_not_reported_as_sent_before_the_upgrade(lead):
    kept = stored_file(lead, "att_kept", "report.pdf", folder=UPLOADS_FOLDER)
    removed = stored_file(lead, "att_removed", "old.pdf", folder=UPLOADS_FOLDER, exists=False)  # retention ended
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING, files=[kept, removed])])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))
    assert not notices(lead) and not admin_notices(lead) and not chats(lead)
    assert not open_incidents(lead)



@pytest.mark.asyncio
async def test_the_check_reads_no_upload_so_a_fault_reading_one_is_not_logged_on_every_pass(lead, caplog, monkeypatch):
    kept = stored_file(lead, "att_kept", "report.pdf", folder=UPLOADS_FOLDER)
    real_stat, target = os.stat, os.path.realpath(kept.local_path())

    def denied(value, *args, **kwargs):  # the uploads disk refuses this file
        if os.fspath(value) == target:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_stat(value, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING, files=[kept])])
    caplog.set_level(logging.INFO)
    now = datetime.now(timezone.utc)
    for minute in range(2):
        await report_stuck_tasks(lead, now=now + timedelta(minutes=minute))

    assert "attachment_upload_unreadable" not in caplog.text
    assert not notices(lead) and not admin_notices(lead) and not chats(lead) and not open_incidents(lead)

@pytest.mark.asyncio
async def test_a_task_whose_files_cannot_be_checked_is_skipped_and_other_notices_still_go_out(lead, caplog):
    unreadable = stored_file(lead, "att_bad", "bad.png", folder=ADAPTER_HOST_FOLDER, exists=False)
    unreadable.storage_refs[0].path += "\x00"  # os.path.realpath raises ValueError
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    save(lead, "worker-1", "WebResearchAgent", [record("root", files=[unreadable])])  # no heartbeat: gone
    save(lead, "lead", "TeamArchitect", [record("other", state=ProgressState.WAITING, files=[old])])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc) + timedelta(minutes=11))

    reported = {(incident.subject_id, incident.reason) for incident in open_incidents(lead)}
    assert reported == {("root", TASK_OWNER_GONE), ("other", TASK_FILES_SENT_BEFORE_UPGRADE)}
    assert "event=stuck_task_upgrade_files_check_failed task_id=root error_type=ValueError" in caplog.text
    assert "event=stuck_task_scan_failed" not in caplog.text
    assert no_storage_path(caplog.text)
