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
from custom_agents.conversation_attachments import (
    ESCALATION_RESULT_MESSAGE_TYPE,
    QUESTION_ANSWER_MESSAGE_TYPE,
    task_files_details,
)
from custom_agents.liveness_observation import observe_liveness
from custom_agents.durable_owner_scope import manager_durable_scope_id
from custom_agents.runtime_task_controls import QUEUE_PAUSE_EVENT
from custom_agents.schedule_store import ScheduleStore
from custom_agents.stuck_tasks import (
    TASK_FILES_SENT_BEFORE_UPGRADE,
    TASK_HANDBACK_UNCLAIMED,
    TASK_NO_PROGRESS,
    TASK_OWNER_GONE,
    TASK_WAITING_FOR_ACCESS,
    TASK_WAITING_FOR_ANSWER,
    TASK_WAITING_ON_NOTHING,
    report_stuck_tasks,
)
from custom_agents.task_human_interaction_blockers import (
    clear_human_interaction_blocker,
    record_human_interaction_blocker,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.attachments_v1 import AttachmentStorageRef, UserTaskAttachment
from custom_agents.task_message_runtime import SECURE_CREDENTIAL_RESULT_EVENT
from custom_agents.task_steering_runtime import (
    GUIDANCE_ACCEPTED_EVENT,
    INTERRUPT_REQUEST_EVENT,
    INTERRUPT_RESUMED_EVENT,
    INTERRUPT_WAITS_ON_REQUESTER_KEY,
)
from custom_agents.tool_setup_runs import RUN_FINISHED, TOOL_SETUP_RUN_KIND, ToolSetupRunV1
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
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
    BATCH_WAITING_EXTERNAL_RESULT,
    PlanStepTaskLink,
    RecoverablePauseRecord,
    RecoveryTaskLink,
    RuntimeMailboxEventRecord,
    RuntimeSnapshotV3,
    TaskRuntimeLeaseRecord,
    TaskSnapshotRecord,
    TurnActionBatchRecord,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry, TaskQuery, TaskSteering
from custom_messages.contracts.worker_handback_v1 import (
    HandedBackTaskV1,
    WorkerLeaseLossEvidenceV1,
    WorkerTaskHandbackV1,
)
from custom_messages.enums import (
    ControlType,
    ExecutionPlanItemStatus,
    MessageMetaType,
    ProgressState,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
    TaskHistoryType,
    TaskSteeringType,
)
from custom_messages.records.plans import PlanRecord, PlanStepRecord
from custom_messages.system_schedules import PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.stuck_work_repository import StuckWorkRepository
from storage.tool_setup_run_repository import ToolSetupRunRepository
from storage.worker_handback_repository import HANDBACK_STATUS_REDISPATCHED, WorkerHandbackRepository
from tests.broker_access_support import context_signer, use_scheduler_key
from tests.change_approval_support import use_signed_contexts
from time_presentation import format_user_timestamp

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


def lead_waits_on(lead, *children):
    """The lead's request waiting on work it delegated; a parent saves a record of each delegation."""
    delegated = [record(child, parent="root", route="lead") for child in children]
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING), *delegated],
         subtask_to_parent_map={child: "root" for child in children})


@pytest.mark.asyncio
async def test_a_request_whose_worker_stopped_is_reported_once_as_the_persons_request(lead):
    lead_waits_on(lead, "child-a", "child-b")
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
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)],
         human_interaction_blockers={"root": {"kind": "access_request", "access_request_id": "req-1"}})
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
    lead_waits_on(lead, "child-a")
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


def incidents_besides_waiting_on_nothing(lead):
    """The open incidents these tests look at.

    Their requests wait with no saved reason, so the waiting-on-nothing check
    also opens its own incident for them; it tells nobody for two hours and is
    tested in its own section below.
    """
    return [incident for incident in open_incidents(lead) if incident.reason != TASK_WAITING_ON_NOTHING]


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
    (incident,) = incidents_besides_waiting_on_nothing(lead)
    assert (incident.subject_id, incident.reason) == ("root", TASK_FILES_SENT_BEFORE_UPGRADE)
    assert "event=stuck_work_notified" in caplog.text
    assert f"reason={TASK_FILES_SENT_BEFORE_UPGRADE}" in caplog.text

    await report_stuck_tasks(lead, now=now + timedelta(minutes=1))  # told once
    assert len(chats(lead)) == 1 and len(notices(lead)) == 1

    for owner in ("lead", "worker-1"):  # the request ended
        lead.task_manager.runtime_state_store.clear_task_runtime(owner)
    await report_stuck_tasks(lead, now=now + timedelta(minutes=2))
    assert not incidents_besides_waiting_on_nothing(lead)
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
    assert [(incident.subject_id, incident.reason) for incident in incidents_besides_waiting_on_nothing(lead)] == [
        ("root", TASK_FILES_SENT_BEFORE_UPGRADE)
    ]
    assert not admin_notices(lead)

@pytest.mark.asyncio
async def test_files_kept_by_0_9_12_for_guidance_answers_and_escalation_results_are_named(lead):
    """0.9.12 and 0.9.13 keep a task's later files in files entries (``task_files_details``), read as the tools do."""
    guidance = stored_file(lead, "att_guide", "receipt.png", folder=ADAPTER_HOST_FOLDER)
    answer = stored_file(lead, "att_answer", "invoice.pdf", folder=ADAPTER_HOST_FOLDER)
    result = stored_file(lead, "att_result", "scan.png", folder=ADAPTER_HOST_FOLDER)
    kept = stored_file(lead, "att_kept", "notes.txt", folder=UPLOADS_FOLDER)
    root = record("root", state=ProgressState.WAITING)
    root.task.execution_history = [
        TaskHistoryEntry(
            entry_type=TaskHistoryType.TASK_EVENT,
            summary="Files kept",
            details=task_files_details(files, message_id=message_id, message_type=message_type),
        )
        for files, message_id, message_type in (
            ([guidance, kept], "steer-1", "guidance"),
            ([answer], "query-1", QUESTION_ANSWER_MESSAGE_TYPE),
            ([result], "child-a", ESCALATION_RESULT_MESSAGE_TYPE),
        )
    ]
    save(lead, "lead", "TeamArchitect", [root])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))

    (notice,) = notices(lead)
    assert "(receipt.png, invoice.pdf, scan.png)" in notice.summary
    assert "notes.txt" not in notice.summary and no_storage_path(notice.summary)


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
    assert not incidents_besides_waiting_on_nothing(lead)

    lead.task_manager.runtime_state_store.clear_task_runtime("worker-1")
    await report_stuck_tasks(lead, now=now + timedelta(minutes=2))
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1 and not incidents_besides_waiting_on_nothing(lead)


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
    assert [incident.subject_id for incident in incidents_besides_waiting_on_nothing(lead)] == ["root"]

    # Step 1 ended and step 2 runs with its own subtask: still the request's one notice.
    links = {"step-1": step_link("step-1"), "step-2": step_link("step-2")}
    lead_task_delegates(lead, request, "step-2", [old], plan_step_task_links=links)
    await report_stuck_tasks(lead, now=now + timedelta(minutes=1))
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1
    assert [incident.subject_id for incident in incidents_besides_waiting_on_nothing(lead)] == ["root"]


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
    assert [incident.subject_id for incident in incidents_besides_waiting_on_nothing(lead)] == ["root"]
    assert "event=stuck_task_request_links_loop" not in caplog.text


@pytest.mark.asyncio
async def test_plan_links_that_loop_are_logged_and_send_no_notice(lead, caplog):
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    lead_task_delegates(
        lead, record("root", state=ProgressState.WAITING), "step-1", [old],
        plan_step_task_links={"step-1": step_link("step-1", "step-2"), "step-2": step_link("step-2", "step-1")},
    )
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc))
    assert not notices(lead) and not admin_notices(lead) and not incidents_besides_waiting_on_nothing(lead)
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
    assert not incidents_besides_waiting_on_nothing(lead)


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

    # Only the verified request falls back to its person's configured chat (each
    # of its findings looks it up, the untold waiting-on-nothing one too).
    assert set(asked) == {"jordan"}
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
    assert not incidents_besides_waiting_on_nothing(lead)



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
    assert not notices(lead) and not admin_notices(lead) and not chats(lead) and not incidents_besides_waiting_on_nothing(lead)

@pytest.mark.asyncio
async def test_a_task_whose_files_cannot_be_checked_is_skipped_and_other_notices_still_go_out(lead, caplog):
    unreadable = stored_file(lead, "att_bad", "bad.png", folder=ADAPTER_HOST_FOLDER, exists=False)
    unreadable.storage_refs[0].path += "\x00"  # os.path.realpath raises ValueError
    old = stored_file(lead, "att_old", "photo.png", folder=ADAPTER_HOST_FOLDER)
    save(lead, "worker-1", "WebResearchAgent", [record("root", files=[unreadable])])  # no heartbeat: gone
    save(lead, "lead", "TeamArchitect", [record("other", state=ProgressState.WAITING, files=[old])])
    await report_stuck_tasks(lead, now=datetime.now(timezone.utc) + timedelta(minutes=11))

    reported = {(incident.subject_id, incident.reason) for incident in incidents_besides_waiting_on_nothing(lead)}
    assert reported == {("root", TASK_OWNER_GONE), ("other", TASK_FILES_SENT_BEFORE_UPGRADE)}
    assert "event=stuck_task_upgrade_files_check_failed task_id=root error_type=ValueError" in caplog.text
    assert "event=stuck_task_scan_failed" not in caplog.text
    assert no_storage_path(caplog.text)


# -- a task waiting on nothing (task_waiting_on_nothing) -------------------------------

# The threshold is no_progress_after_seconds (2 hours by default), counted from
# the first pass that sees the task, which must then see it on every pass.
PAST_THRESHOLD = timedelta(hours=2, minutes=1)
STRANDED_TITLE = f'Your request "{REQUEST}" will not continue on its own'


def waiting_reasons(lead):
    return {incident.reason for incident in open_incidents(lead)}


def alive(lead, agent_id, when, agent_type="WebResearchAgent"):
    seen(lead, agent_id, agent_type)
    status = lead._agent_state_store.get_by_id(agent_id)
    status.last_seen_at = iso(when)
    lead._agent_state_store.upsert(status)


def with_history(held, *entries):
    task = held.task.model_copy(update={"execution_history": [*held.task.execution_history, *entries]})
    return held.model_copy(update={"task": task})


def moved_to(held, state, when):
    """The same request (same signed identity) after a state change."""
    task = held.task.model_copy(update={"task_state": [*held.task.task_state, (state, iso(when))]})
    return held.model_copy(update={"state": state, "task": task})


def guidance(when):
    return TaskHistoryEntry(
        summary="Mid-turn proactive steering accepted: why did you stop?",
        timestamp=iso(when),
        details={"event": GUIDANCE_ACCEPTED_EVENT},
    )


def strand_the_incidents_request(lead, started):
    """The incident's sequence through the real TaskManager, which saves the lead's work itself.

    It ran, paused for set_up_tools and resumed; an earlier action batch waited
    for a credential form and a later one finished; the saved workflow queued it
    to continue, and its turn's result then set it WAITING, which removed the
    queue entry. Its setup run is finished and its access request granted.
    """
    manager = lead.task_manager
    held = record("root", state=ProgressState.ACCEPTED, at=started)
    assert manager.add_task(held.task.model_copy(update={"task_state": []}), held.header, held.meta, owner_person_id="jordan")

    def moment(minutes):
        return iso(started + timedelta(minutes=minutes))

    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, moment(0))
    record_human_interaction_blocker(manager, "root", {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-1", "interrupt_id": "i1"})
    manager.append_history_entry(
        "root", "Strands turn interrupted for requester input.", timestamp=moment(1),
        details={"event": INTERRUPT_REQUEST_EVENT, INTERRUPT_WAITS_ON_REQUESTER_KEY: False},
    )
    manager.update_main_task_state("root", ProgressState.WAITING, moment(1))
    clear_human_interaction_blocker(manager, "root")
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, moment(10))
    manager.append_history_entry("root", "Resumed.", timestamp=moment(10), details={"event": INTERRUPT_RESUMED_EVENT})
    form = manager.create_turn_action_batch("root", [{"action_type": "secure_credential_collection_request"}])
    manager.mark_turn_action_batch_status(form.batch_id, BATCH_WAITING_EXTERNAL_RESULT)
    answer = manager.create_turn_action_batch("root", [{"action_type": "task_response"}])
    manager.mark_turn_action_batch_status(answer.batch_id, "completed")
    manager.update_main_task_state("root", ProgressState.ACCEPTED, moment(20))
    assert manager.enqueue_task("root", front=True)
    manager.update_main_task_state("root", ProgressState.WAITING, moment(21))
    assert "root" not in manager.task_state.task_queue

    ToolSetupRunRepository(manager.storage_settings).create(
        ToolSetupRunV1(
            run_id="run-1", team_id="team", person_id="jordan", task_id="root", agent_id="lead",
            agent_type="TeamArchitect", tool_ids=("http:registrar",), status=RUN_FINISHED,
        )
    )
    AccessRepository(manager.storage_settings).create_request(
        AccessRequestV1(
            request_id="req-1", team_id="team", requester_agent_id="lead", requester_agent_type="TeamArchitect",
            person_id="jordan", subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:registrar"), level=AccessLevel.READ,
            reason="Check prices", task_id="root", created_at_utc=started, status=AccessRequestStatus.GRANTED,
        )
    )
    return started + timedelta(minutes=21)


@pytest.mark.asyncio
async def test_the_incidents_stranded_request_is_reported_once_and_cleared_when_it_moves(lead, caplog):
    stopped = strand_the_incidents_request(lead, datetime.now(timezone.utc) - timedelta(hours=1))
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    assert not notices(lead) and not chats(lead)  # not told before it has lasted the threshold
    assert waiting_reasons(lead) == {TASK_WAITING_ON_NOTHING}

    caplog.set_level(logging.WARNING)
    for minute in range(3):
        await report_stuck_tasks(lead, now=first + PAST_THRESHOLD + timedelta(minutes=minute))
    (notice,) = notices(lead)
    assert notice.title == STRANDED_TITLE
    assert format_user_timestamp(stopped, timezone_name=None) in notice.summary
    assert "/stop task root" in notice.summary and "Settings > Tasks" in notice.summary
    assert "send your request again" in notice.summary and "ask your assistant" not in notice.summary
    assert not admin_notices(lead)  # the person can recover; the warning records it for operators
    (envelope,) = chats(lead)
    assert envelope.msg_content.reason == "work_stuck"
    assert envelope.msg_header.to_routing_path == GATEWAY_ROUTE
    assert caplog.text.count("event=stuck_work_notified") == 1
    assert "reason=task_waiting_on_nothing" in caplog.text
    assert "detail=task_id=root owner_agent_id=lead owner_agent_type=TeamArchitect state=waiting" in caplog.text

    manager = lead.task_manager
    manager.update_main_task_state("root", ProgressState.ACCEPTED, iso(first + PAST_THRESHOLD))
    assert manager.enqueue_task("root")
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD + timedelta(minutes=5))
    assert chats(lead)[-1].msg_content.reason == "work_recovered"
    assert f'The warning for your request "{REQUEST}" has cleared' in chats(lead)[-1].msg_content.text
    assert not open_incidents(lead)


@pytest.mark.asyncio
async def test_a_task_stranded_after_its_form_result_and_a_later_setup_pause_is_reported(lead):
    """Review F1: the form's batch keeps its waiting status after the result, and set_up_tools adds no batch."""
    manager = lead.task_manager
    held = record("root", state=ProgressState.ACCEPTED)
    assert manager.add_task(held.task.model_copy(update={"task_state": []}), held.header, held.meta, owner_person_id="jordan")
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, iso(datetime.now(timezone.utc) - timedelta(seconds=1)))
    form = manager.create_turn_action_batch("root", [{"action_type": "secure_credential_collection_request"}])
    manager.mark_turn_action_batch_status(form.batch_id, BATCH_WAITING_EXTERNAL_RESULT)
    asked = datetime.fromisoformat(form.created_at)

    def after(minutes):
        return iso(asked + timedelta(minutes=minutes))

    manager.update_main_task_state("root", ProgressState.WAITING, after(0.5))
    # The form's result arrives and the task runs again.
    manager.append_history_entry(
        "root", "Secure credential result: stored.", timestamp=after(1), details={"event": SECURE_CREDENTIAL_RESULT_EVENT}
    )
    manager.update_main_task_state("root", ProgressState.IN_PROGRESS, after(1))
    # set_up_tools pauses the turn: a blocker and WAITING, but no new batch.
    record_human_interaction_blocker(manager, "root", {"kind": TOOL_SETUP_RUN_KIND, "run_id": "run-1", "interrupt_id": "i1"})
    manager.append_history_entry(
        "root", "Strands turn interrupted for requester input.", timestamp=after(2),
        details={"event": INTERRUPT_REQUEST_EVENT, INTERRUPT_WAITS_ON_REQUESTER_KEY: False},
    )
    manager.update_main_task_state("root", ProgressState.WAITING, after(2))
    # The setup run ends, but the wake-up is lost.
    clear_human_interaction_blocker(manager, "root")
    assert manager.get_turn_action_batch(form.batch_id).status == BATCH_WAITING_EXTERNAL_RESULT

    first = asked + timedelta(minutes=3)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    (notice,) = notices(lead)
    assert notice.title == STRANDED_TITLE


@pytest.mark.asyncio
async def test_a_received_mailbox_record_does_not_hide_a_stranded_request(lead):
    """Review F2: a stop recorded during the tree's batch was handled when it arrived."""
    stop = RuntimeMailboxEventRecord(
        event_id="e1", root_task_id="root", event_type="control_stop", event_class="preemptive", status="received",
    )
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)], runtime_mailbox_events={"e1": stop})
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    (notice,) = notices(lead)
    assert notice.title == STRANDED_TITLE


@pytest.mark.asyncio
async def test_guidance_neither_restarts_nor_clears_the_wait(lead):
    """The person's "why did you stop?" adds history to the stranded task without moving it."""
    stopped = datetime.now(timezone.utc) - timedelta(hours=5)
    stranded = record("root", state=ProgressState.WAITING, at=stopped)
    save(lead, "lead", "TeamArchitect", [stranded])
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    asked = first + timedelta(hours=1)
    save(lead, "lead", "TeamArchitect", [with_history(stranded, guidance(asked))])
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    (notice,) = notices(lead)
    assert format_user_timestamp(stopped, timezone_name=None) in notice.summary
    assert format_user_timestamp(asked, timezone_name=None) not in notice.summary

    save(lead, "lead", "TeamArchitect", [with_history(stranded, guidance(asked), guidance(first + PAST_THRESHOLD))])
    await report_stuck_tasks(lead, now=first + timedelta(hours=3))
    assert [envelope.msg_content.reason for envelope in chats(lead)] == ["work_stuck"]
    assert waiting_reasons(lead) == {TASK_WAITING_ON_NOTHING}


@pytest.mark.asyncio
async def test_a_short_wait_between_steps_is_never_reported(lead):
    first = datetime.now(timezone.utc)
    waiting = record("root", state=ProgressState.WAITING, at=first)
    save(lead, "lead", "TeamArchitect", [waiting])
    await report_stuck_tasks(lead, now=first)
    assert waiting_reasons(lead) == {TASK_WAITING_ON_NOTHING}
    running = moved_to(waiting, ProgressState.IN_PROGRESS, first + timedelta(seconds=30))
    save(lead, "lead", "TeamArchitect", [running])
    await report_stuck_tasks(lead, now=first + timedelta(minutes=1))
    assert not open_incidents(lead)  # closed without telling anyone

    save(lead, "lead", "TeamArchitect", [moved_to(running, ProgressState.WAITING, first + timedelta(minutes=2))])
    await report_stuck_tasks(lead, now=first + timedelta(minutes=2))
    await report_stuck_tasks(lead, now=first + timedelta(hours=2))  # 1 h 58 min since this wait was first seen
    assert not notices(lead) and not chats(lead)


def preempted():
    paused = record("root", state=ProgressState.PAUSED)
    return [with_history(paused, TaskHistoryEntry(summary="Task paused because task other was preempted.")
                         )], {}


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(preempted, id="paused after preemption"),
        pytest.param(lambda: ([record("root", state=ProgressState.ACCEPTED)], {}), id="ready but not queued"),
        pytest.param(
            lambda: ([record("root", state=ProgressState.WAITING)], {"final_review_pending_task_ids": ["root"]}),
            id="final review left waiting",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_same_gap_in_other_states_is_reported(lead, build):
    records, sections = build()
    save(lead, "lead", "TeamArchitect", records, **sections)
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    (notice,) = notices(lead)
    assert notice.title == STRANDED_TITLE


@pytest.mark.asyncio
async def test_a_stopped_agents_stranded_task_is_reported_as_stopped(lead):
    save(lead, "worker-1", "WebResearchAgent", [record("root", state=ProgressState.WAITING)])
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    assert waiting_reasons(lead) == {TASK_OWNER_GONE}
    (notice,) = notices(lead)
    assert notice.title == f'Your request "{REQUEST}" stopped'


@pytest.mark.asyncio
async def test_a_workers_stranded_task_is_reported_as_its_request_naming_the_worker(lead, caplog):
    """The lead's delegation record waits on it; the warning names the agent that runs it (review C06)."""
    lead_waits_on(lead, "child-a")
    save(lead, "worker-1", "WebResearchAgent", [record("child-a", state=ProgressState.WAITING, parent="root", route="lead")])
    first = datetime.now(timezone.utc)
    alive(lead, "worker-1", first)
    await report_stuck_tasks(lead, now=first)
    caplog.set_level(logging.WARNING)
    alive(lead, "worker-1", first + PAST_THRESHOLD)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    # The person's request, not the delegated part. (The lead's own copy of the
    # delegation, left in progress here, also reports no progress.)
    (notice,) = [item for item in notices(lead) if item.title == STRANDED_TITLE]
    assert "/stop task root" in notice.summary
    assert "task_id=child-a owner_agent_id=worker-1 owner_agent_type=WebResearchAgent" in caplog.text


@pytest.mark.asyncio
async def test_when_the_worker_holding_it_stops_the_person_hears_stopped_and_not_cleared(lead):
    """Review C09: the request did not move, so its warning must not read as cleared."""
    lead_waits_on(lead, "child-a")
    save(lead, "worker-1", "WebResearchAgent", [record("child-a", state=ProgressState.WAITING, parent="root", route="lead")])
    first = datetime.now(timezone.utc)
    alive(lead, "worker-1", first)
    await report_stuck_tasks(lead, now=first)
    alive(lead, "worker-1", first + PAST_THRESHOLD)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    assert STRANDED_TITLE in [notice.title for notice in notices(lead)]

    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD + timedelta(hours=1))  # no status for an hour
    titles = [notice.title for notice in notices(lead)]
    assert f'Your request "{REQUEST}" stopped' in titles
    assert not [title for title in titles if "has cleared" in title]
    assert "work_recovered" not in [envelope.msg_content.reason for envelope in chats(lead)]
    assert TASK_WAITING_ON_NOTHING not in waiting_reasons(lead)


@pytest.mark.asyncio
async def test_without_a_verified_person_administrators_hear_without_the_request(lead):
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING, signed=False)])
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    assert not notices(lead)
    (admin,) = admin_notices(lead)
    assert admin.title == "A task will not continue on its own"
    assert "TeamArchitect" in admin.summary and "task root" in admin.summary
    assert REQUEST not in admin.title + admin.summary


@pytest.mark.asyncio
async def test_stopping_the_request_as_told_closes_the_warning_quietly(lead):
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)])
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1
    lead.task_manager.runtime_state_store.clear_task_runtime("lead")  # stopped: no saved work holds it
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD + timedelta(minutes=1))
    assert len(notices(lead)) == 1 and len(chats(lead)) == 1
    assert not open_incidents(lead)


# -- every legitimate wait is left alone (review A2) ----------------------------------------


def blocker(kind, **fields):
    return {"human_interaction_blockers": {"root": {"kind": kind, **fields}}}


def outside_wait(content_type):
    """The outbox barrier after a credential form or signup automation request."""
    held = with_history(
        record("root", state=ProgressState.WAITING),
        TaskHistoryEntry(summary="Waiting for the result.", details={
            "event": "turn_action_external_wait_barrier", "batch_id": "b1", "content_type": content_type,
        }),
    )
    batch = TurnActionBatchRecord(
        batch_id="b1", source_task_id="root", status=BATCH_WAITING_EXTERNAL_RESULT, created_at=iso(datetime.now(timezone.utc))
    )
    return [held], {"turn_action_batches": {"b1": batch}}


def waiting_access_request(lead):
    AccessRepository(lead.task_manager.storage_settings).create_request(
        AccessRequestV1(
            request_id="req-1", team_id="team", requester_agent_id="lead", requester_agent_type="TeamArchitect",
            person_id="jordan", subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id="TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:registrar"), level=AccessLevel.READ,
            reason="Check prices", task_id="root", created_at_utc=datetime.now(timezone.utc),
        )
    )
    return [record("root", state=ProgressState.WAITING)], {}


def waiting_setup_run(lead):
    ToolSetupRunRepository(lead.task_manager.storage_settings).create(
        ToolSetupRunV1(
            run_id="run-1", team_id="team", person_id="jordan", task_id="root", agent_id="lead",
            agent_type="TeamArchitect", tool_ids=("http:registrar",),
        )
    )
    return [record("root", state=ProgressState.WAITING)], {}


def running_plan(lead):
    lead.task_manager.plan_repository.save_plan(
        PlanRecord(
            plan_id="plan-1", owner_agent_id=manager_durable_scope_id(team_id="team", named_agent_type="TeamArchitect"),
            team_id="team", root_task_id="root", active_execution_task_id="root",
            plan_steps=[PlanStepRecord(title="Compare prices", objective="Both registrars", status=ExecutionPlanItemStatus.PLANNED)],
        )
    )
    return [record("root", state=ProgressState.WAITING)], {"task_to_plan_id": {"root": "plan-1"}}


def retry_timer(lead):
    held = record("root", state=ProgressState.PAUSED)
    lead._schedule_store.add(
        ScheduledMessage(
            schedule_id="retry-1", schedule_name=f"{PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX}root:pause-1",
            management_class=ScheduleManagementClass.SYSTEM, system_schedule_kind=SystemScheduleKind.PROVIDER_OVERLOAD_RETRY,
            payload=held.task, payload_header=held.header, payload_meta=held.meta, repeat_count=1,
            next_fire_at=iso(datetime.now(timezone.utc) + timedelta(minutes=5)),
        ),
        claim_agent_id="lead", claim_named_agent_type="TeamArchitect", claim_team_id="team",
    )
    return [held], {}


def saved_only(records, **sections):
    return lambda lead: (records(), sections)


W = ProgressState.WAITING
LEGITIMATE_WAITS = {
    "queued": saved_only(lambda: [record("root", state=ProgressState.ACCEPTED)], task_queue=["root"]),
    "delegated work": lambda lead: (
        [record("root", state=W), record("child-a", parent="root", route="lead")], {"subtask_to_parent_map": {"child-a": "root"}}
    ),
    "question": saved_only(
        lambda: [record("root", state=W)],
        task_queries={"root": [TaskQuery(query_type=MessageMetaType.REQUEST, message="Which registrar?", parent_task_id="root")]},
    ),
    "recoverable pause": saved_only(
        lambda: [record("root", state=ProgressState.PAUSED)],
        recoverable_pause_records={"p1": RecoverablePauseRecord(task_id="root", pause_id="p1", trigger="tool_call_budget")},
    ),
    "access request blocker": saved_only(lambda: [record("root", state=W)], **blocker("access_request", access_request_id="req-1")),
    "setup run blocker": saved_only(lambda: [record("root", state=W)], **blocker(TOOL_SETUP_RUN_KIND, run_id="run-1")),
    "secure credential form": lambda lead: outside_wait("SecureCredentialCollectionRequest"),
    "signup automation": lambda lead: outside_wait("SignupAutomationRequest"),
    "interrupt sent to the requester": lambda lead: (
        [with_history(record("root", state=W), TaskHistoryEntry(
            summary="Strands turn interrupted for requester input.",
            details={"event": INTERRUPT_REQUEST_EVENT, INTERRUPT_WAITS_ON_REQUESTER_KEY: True},
        ))],
        {},
    ),
    "running action batch": saved_only(
        lambda: [record("root", state=W)],
        runtime_leases={"l1": TaskRuntimeLeaseRecord(lease_id="l1", root_task_id="root", source_task_id="root", owner_agent_id="lead")},
    ),
    "held reply": saved_only(
        lambda: [record("root", state=W)],
        runtime_mailbox_events={"e1": RuntimeMailboxEventRecord(
            event_id="e1", root_task_id="root", event_type="task_response", event_class="deferrable", status="deferred",
        )},
    ),
    "isolated recovery": saved_only(
        lambda: [record("root", state=W), record("rec", state=ProgressState.ACCEPTED, signed=False, route="lead")],
        task_queue=["rec"],
        recovery_links={"rec": RecoveryTaskLink(
            original_task_id="root", original_plan_id="plan-1", recovery_task_id="rec", branch_id="b",
            failure_type="tool_error", failure_summary="A tool failed.",
        )},
    ),
    "queue pause": lambda lead: (
        [with_history(
            record("root", state=ProgressState.PAUSED, at=PAUSED_AT),
            TaskHistoryEntry(summary="Queue pause: later", details={"event": QUEUE_PAUSE_EVENT, "paused_at": iso(PAUSED_AT)}),
        )],
        {},
    ),
    "final review queued": saved_only(
        lambda: [record("root", state=ProgressState.PARTIAL)], task_queue=["root"], final_review_pending_task_ids=["root"]
    ),
    "access request in the store": waiting_access_request,
    "setup run in the store": waiting_setup_run,
    "plan in the store": running_plan,
    "retry timer in the schedule store": retry_timer,
}
PAUSED_AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("name", list(LEGITIMATE_WAITS))
@pytest.mark.asyncio
async def test_a_legitimate_wait_is_never_reported_as_waiting_on_nothing(lead, name):
    records, sections = LEGITIMATE_WAITS[name](lead)
    save(lead, "lead", "TeamArchitect", records, **sections)
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    assert TASK_WAITING_ON_NOTHING not in waiting_reasons(lead)
    assert STRANDED_TITLE not in [notice.title for notice in notices(lead)]


# -- a store that cannot be read (review C02) -------------------------------------------------


def damaged(*_args, **_kwargs):
    raise ValueError("The stored row does not match this version's model.")


@pytest.mark.parametrize("store", ["plans", "tool_setup_runs"])
@pytest.mark.asyncio
async def test_an_unreadable_row_keeps_its_warning_and_the_other_notices_go_on(lead, monkeypatch, caplog, store):
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)], task_to_plan_id={"root": "plan-1"})
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    (told,) = open_incidents(lead)
    assert told.reason == TASK_WAITING_ON_NOTHING and told.notified_at_utc is not None

    if store == "plans":
        monkeypatch.setattr(lead.task_manager.plan_repository, "get_plan", damaged)
    else:
        monkeypatch.setattr(ToolSetupRunRepository, "for_task", damaged)
    save(lead, "worker-1", "WebResearchAgent", [record("other")])  # its agent never reported in
    caplog.set_level(logging.WARNING)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD + timedelta(minutes=11))

    assert f"event=stuck_task_wait_reason_unreadable task_id=root owner_agent_id=lead store={store} error_type=ValueError" in caplog.text
    kept = next(incident for incident in open_incidents(lead) if incident.subject_id == "root")
    assert kept == told  # neither told again nor closed
    assert "work_recovered" not in [envelope.msg_content.reason for envelope in chats(lead)]
    assert (lambda reasons: TASK_OWNER_GONE in reasons)({i.reason for i in open_incidents(lead) if i.subject_id == "other"})


@pytest.mark.asyncio
async def test_an_unreadable_team_wide_list_stops_the_pass_and_keeps_every_warning(lead, monkeypatch, caplog):
    save(lead, "lead", "TeamArchitect", [record("root", state=ProgressState.WAITING)])
    first = datetime.now(timezone.utc)
    await report_stuck_tasks(lead, now=first)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD)
    told = open_incidents(lead)

    def locked():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(lead._schedule_store, "all_rows", locked)
    caplog.set_level(logging.WARNING)
    await report_stuck_tasks(lead, now=first + PAST_THRESHOLD + timedelta(minutes=1))
    assert "event=stuck_task_scan_failed" in caplog.text
    assert open_incidents(lead) == told
    assert [envelope.msg_content.reason for envelope in chats(lead)] == ["work_stuck"]
