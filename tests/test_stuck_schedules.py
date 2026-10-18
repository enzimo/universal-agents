"""People are told when a schedule is stuck, and when it works again.

Real SQLite schedule, binding, incident and access (Inbox) stores; the chat
side is the gateway-bound ``UserNotification`` the lead publishes, captured at
``team_comms``. Replaces the removed self-addressed TeamLead review task, whose
answer could reach nobody (2026-10-03: a day of blocked schedules, owner never
told).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.people import PERSON_ROLE_ADMIN
from custom_agents.schedule_repair_guidance import (
    LAUNCH_LIVENESS_UNKNOWN,
    SCHEDULE_EXECUTION_CHANGED,
)
from custom_agents.schedule_store import ScheduleStore
from custom_agents.stuck_schedules import report_stuck_schedules
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from storage.access_repository import AccessRepository
from storage.stuck_work_repository import StuckWorkRepository

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
GATEWAY_ROUTE = "gateway.instance.responses"


def make_lead(tmp_path):
    manager = TaskManager(db_path=str(tmp_path / "runtime.db"))
    manager.set_owner_identity(
        agent_id="lead", agent_name="lead", team_id="team", named_agent_type="TeamArchitect"
    )
    return SimpleNamespace(
        id="lead",
        name="lead",
        team_id="team",
        named_agent_type="TeamArchitect",
        is_team_lead=True,
        instance_config_path=None,
        task_manager=manager,
        _schedule_store=ScheduleStore(storage_settings=manager.storage_settings),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        _logger=logging.getLogger("stuck-schedules-test"),
        schedule_workers={},
    )


def add_schedule(lead, schedule_id="nightly", *, due=NOW - 2 * HOUR, route=True, **changes):
    fields = dict(
        schedule_id=schedule_id,
        schedule_name="crypto-strategy-research-nightly",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Research"),
        payload_header=MessageHeader(response_routing_key=GATEWAY_ROUTE if route else None),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id="jordan",
                session_key="telegram:jordan",
                channel_name="telegram",
                chat_id="42",
                scheduled_job_id=schedule_id,
                delivery_mode="scheduled",
            )
        ),
        cron_expression="45 3 * * *",
        repeat_count=None,  # recurring (the model's default, 1, means once)
        next_fire_at=due.isoformat(),
        claim_team_id="team",
        claim_named_agent_type="BackgroundOperationsAgent",
        cron_timezone="America/Chicago",
    )
    fields.update(changes)
    lead._schedule_store.add(ScheduledMessage(**fields))


def notices(lead, *, person_id="jordan", roles=()):
    repository = AccessRepository(lead.task_manager.storage_settings)
    return [notice for notice, _read in repository.notices_for("team", person_id=person_id, roles=roles)]


def chats(lead):
    return [call.kwargs["message"] for call in lead.team_comms.send_message.await_args_list]


@pytest.mark.asyncio
async def test_owner_is_told_once_after_the_wait_and_again_when_it_runs(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(lead, due=NOW - timedelta(minutes=30))
    await report_stuck_schedules(lead, now=NOW)  # stuck for 30 minutes: not yet
    assert not notices(lead) and not chats(lead)

    later = NOW + HOUR
    for minute in range(3):
        await report_stuck_schedules(lead, now=later + timedelta(minutes=minute))
    (notice,) = notices(lead)
    assert notice.title == 'Schedule "crypto-strategy-research-nightly" is not running'
    assert "no worker has picked it up" in notice.summary
    assert "Oct 3, 2026 at 3:30:00 AM CDT" in notice.summary  # the owner's time zone
    (admin_notice,) = notices(lead, person_id="admin-1", roles=(PERSON_ROLE_ADMIN,))
    assert admin_notice.title == notice.title  # only an administrator can fix this one
    (envelope,) = chats(lead)
    assert envelope.msg_header.to_routing_path == GATEWAY_ROUTE
    assert isinstance(envelope.msg_content, UserNotification)
    assert envelope.msg_content.reason == "work_stuck"
    context = envelope.msg_meta.gateway_context
    assert (context.session_key, context.chat_id, context.delivery_mode) == ("telegram:jordan", "42", "autonomous")
    assert context.scheduled_job_id is None  # not a scheduled run's result

    ran = lead._schedule_store.get_scheduled_message("nightly")
    ran.next_fire_at = (NOW + 20 * HOUR).isoformat()  # it ran; the next run is tomorrow
    lead._schedule_store.add(ran)
    await report_stuck_schedules(lead, now=later + timedelta(minutes=5))
    titles = [n.title for n in notices(lead)]
    assert titles == [
        'Schedule "crypto-strategy-research-nightly" is running again',
        'Schedule "crypto-strategy-research-nightly" is not running',
    ]
    assert chats(lead)[-1].msg_content.reason == "work_recovered"
    assert not StuckWorkRepository(lead.task_manager.storage_settings).open_incidents("team", "schedule")
    await report_stuck_schedules(lead, now=later + timedelta(minutes=6))
    assert len(chats(lead)) == 2


def record_block(lead, schedule_id: str, reason: str) -> None:
    """A saved worker policy with a recorded block, as the controller leaves it."""
    binding = ScheduleWorkerBindingV1(
        schedule_id=schedule_id, team_id="team", owner_canonical_user_id="jordan",
        target_agent_type="BackgroundOperationsAgent", definition_digest="d",
        execution_digest="e", revision=1, policy=ScheduleWorkerPolicySpecV1(),
        actor_principal_id="lead", authority_mode="runtime_default",
    )
    records = lead._schedule_store.worker_records
    with records._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_worker_bindings(schedule_id,team_id,owner_id,revision,payload_json) VALUES (?,?,?,?,?)",
            (schedule_id, "team", "jordan", 1, binding.model_dump_json()),
        )
        conn.commit()
    records.mark_blocked(schedule_id, "team", reason)


@pytest.mark.asyncio
async def test_a_block_the_owner_can_fix_goes_to_the_owner_only(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(lead)
    record_block(lead, "nightly", SCHEDULE_EXECUTION_CHANGED)
    await report_stuck_schedules(lead, now=NOW + HOUR)
    (notice,) = notices(lead)
    assert "waiting for you to confirm the change" in notice.summary
    assert not notices(lead, person_id="admin-1", roles=(PERSON_ROLE_ADMIN,))


@pytest.mark.asyncio
async def test_the_dead_start_block_tells_the_owner_and_administrators(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(lead)
    record_block(lead, "nightly", LAUNCH_LIVENESS_UNKNOWN)
    await report_stuck_schedules(lead, now=NOW + HOUR)
    (notice,) = notices(lead)
    assert "could not be confirmed as stopped" in notice.summary
    assert notices(lead, person_id="admin-1", roles=(PERSON_ROLE_ADMIN,))


@pytest.mark.asyncio
async def test_an_owner_who_is_an_administrator_gets_one_notice_per_report(tmp_path):
    """Owner and administrators read the same words, so one notice goes to both (no pile of copies)."""
    lead = make_lead(tmp_path)
    add_schedule(lead, due=NOW - timedelta(minutes=30))
    await report_stuck_schedules(lead, now=NOW + HOUR)
    (notice,) = notices(lead, roles=(PERSON_ROLE_ADMIN,))
    assert notice.recipient_person_id == "jordan"
    assert notice.recipient_roles == (PERSON_ROLE_ADMIN,)
    assert notices(lead, person_id="admin-1", roles=(PERSON_ROLE_ADMIN,)) == [notice]


@pytest.mark.asyncio
async def test_runtime_managed_schedule_is_reported_to_administrators(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(
        lead,
        "mem_rev_core",
        schedule_name="memory_review:coremem:instance",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM,
        claim_named_agent_type="MemReviewAgent",
    )
    await report_stuck_schedules(lead, now=NOW + HOUR)
    assert notices(lead, person_id="admin-1", roles=(PERSON_ROLE_ADMIN,))


@pytest.mark.asyncio
async def test_without_a_chat_route_the_owner_still_gets_the_inbox_notice(tmp_path, caplog):
    lead = make_lead(tmp_path)
    add_schedule(lead, route=False)
    with caplog.at_level(logging.WARNING):
        await report_stuck_schedules(lead, now=NOW + HOUR)
    assert notices(lead)
    assert not chats(lead)
    assert "event=stuck_work_notified" in caplog.text and "chat=False" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_chat_send_is_retried_without_a_second_inbox_notice(tmp_path, caplog):
    lead = make_lead(tmp_path)
    add_schedule(lead)
    lead.team_comms.send_message.side_effect = [ConnectionError("NATS down"), None]
    with caplog.at_level(logging.ERROR):
        await report_stuck_schedules(lead, now=NOW + HOUR)
    assert "event=stuck_work_report_failed" in caplog.text
    await report_stuck_schedules(lead, now=NOW + HOUR + timedelta(seconds=5))
    assert lead.team_comms.send_message.await_count == 2
    assert len(notices(lead)) == 1
    (incident,) = StuckWorkRepository(lead.task_manager.storage_settings).open_incidents("team", "schedule")
    assert incident.notified_at_utc is not None


@pytest.mark.asyncio
async def test_a_missed_one_time_reminder_is_turned_off_and_its_owner_told(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(
        lead,
        "crew-13",
        schedule_name="crew-13-launch-warning",
        cron_expression=None,
        cron_timezone=None,
        repeat_count=1,
        due=NOW - 2 * HOUR,
    )
    add_schedule(lead, "daily", schedule_name="daily-summary", due=NOW - 2 * HOUR)
    await report_stuck_schedules(lead, now=NOW)
    store = lead._schedule_store
    assert not store.get_scheduled_message("crew-13").enabled
    assert store.get_scheduled_message("daily").enabled  # recurring: reported, not turned off
    titles = {n.title for n in notices(lead)}
    assert 'Schedule "crew-13-launch-warning" was missed' in titles
    assert 'Schedule "daily-summary" is not running' in titles
    missed = next(m for m in chats(lead) if "was missed" in m.msg_content.text)
    assert missed.msg_content.reason == "schedule_run_missed"
    assert "turned off so it does not arrive late" in missed.msg_content.text


@pytest.mark.asyncio
async def test_a_missed_reminder_a_worker_claims_meanwhile_is_not_turned_off(tmp_path, monkeypatch):
    """The lead read the reminder as missed, then a worker claimed and started it (2026-10-17).

    The turn-off used to apply anyway: the run was delivered, its owner was
    told it was missed, and the row stayed turned off instead of finishing.
    """
    from custom_agents.stuck_schedules import StuckSchedules

    lead = make_lead(tmp_path)
    add_schedule(
        lead, "crew-13", cron_expression=None, cron_timezone=None, repeat_count=1, due=NOW - 2 * HOUR
    )
    read_missed = StuckSchedules.missed_final_runs

    def read_then_worker_claims(source, now, late_after):
        missed = read_missed(source, now, late_after)
        claim = lead._schedule_store.claim_next_due(
            claimer_agent_id="worker", claimer_named_agent_type="BackgroundOperationsAgent", claimer_team_id="team"
        )
        assert claim["schedule_id"] == "crew-13"
        return missed

    monkeypatch.setattr(StuckSchedules, "missed_final_runs", read_then_worker_claims)
    await report_stuck_schedules(lead, now=NOW)

    assert lead._schedule_store.get_scheduled_message("crew-13").enabled
    assert not [n for n in notices(lead) if "was missed" in n.title]
    assert not [m for m in chats(lead) if "was missed" in m.msg_content.text]


@pytest.mark.asyncio
async def test_a_one_time_reminder_inside_the_limit_stays_on(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(
        lead, "soon", cron_expression=None, cron_timezone=None, repeat_count=1, due=NOW - timedelta(minutes=20)
    )
    await report_stuck_schedules(lead, now=NOW)
    assert lead._schedule_store.get_scheduled_message("soon").enabled


@pytest.mark.asyncio
async def test_a_paused_or_claimed_schedule_is_not_stuck(tmp_path):
    lead = make_lead(tmp_path)
    add_schedule(lead, "claimed")
    claim = lead._schedule_store.claim_next_due(
        claimer_agent_id="worker", claimer_named_agent_type="BackgroundOperationsAgent", claimer_team_id="team"
    )
    assert claim["schedule_id"] == "claimed"
    add_schedule(lead, "paused", paused_until=(datetime.now(timezone.utc) + HOUR).isoformat())
    await report_stuck_schedules(lead, now=datetime.now(timezone.utc))
    stuck = {i.subject_id for i in StuckWorkRepository(lead.task_manager.storage_settings).open_incidents("team", "schedule")}
    assert not stuck & {"paused", "claimed"}


def test_the_schedules_card_projection_matches_the_notice(tmp_path):
    from custom_agents.manager_control_runtime import _schedule_worker_assignment

    lead = make_lead(tmp_path)
    add_schedule(lead)
    store = lead._schedule_store
    assignment = _schedule_worker_assignment(store, store.get_scheduled_message("nightly"), "jordan")
    assert assignment.not_running_since == (NOW - 2 * HOUR).isoformat()
    assert "no worker has picked it up" in assignment.not_running_explanation
    add_schedule(lead, "fine", due=datetime.now(timezone.utc) + HOUR)
    healthy = _schedule_worker_assignment(store, store.get_scheduled_message("fine"), "jordan")
    assert healthy.not_running_since is None and healthy.not_running_explanation is None
