"""A schedule whose runs fail is reported once per problem, with real stores and signatures.

The run's outcome is saved with its terminal snapshot (``storage.scheduled_run_problems``)
and the team lead reports it through the stuck-work notices
(``custom_agents.scheduled_run_failures``).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.scheduled_run_failures import FailingScheduledRuns, report_failing_scheduled_runs
from custom_agents.stuck_work import RECOVERED_NOTICE_REASON, STUCK_NOTICE_REASON
from custom_agents.task_wait_limit import TaskWaitLimitReached
from custom_agents.turn_failure_runtime import fail_task_after_turn_error
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from storage.access_repository import AccessRepository
from storage.scheduled_run_problems import ScheduledRunProblems
from storage.stuck_work_repository import StuckWorkRepository
from tests.broker_access_support import forget_task_context_keys
from tests.scheduled_run_support import ANN, SCHEDULE_ID, TEAM, build_world
from tests.test_schedule_final_runs import start_final_run

DAILY = 86_400_000
NAME = "Starship launch check"


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


def lead(world):
    return SimpleNamespace(
        id="manager", name="manager", team_id=TEAM, is_team_lead=True, instance_config_path=None,
        task_manager=world.tasks, _schedule_store=world.store, schedule_maintenance={},
        team_comms=SimpleNamespace(send_message=AsyncMock()), _logger=logging.getLogger("failure-test"),
    )


def notices(world, person=ANN, roles=()):
    return [notice for notice, _read in AccessRepository(world.tasks.storage_settings).notices_for(
        TEAM, person_id=person, roles=roles,
    )]


def report(world, manager=None):
    manager = manager or lead(world)
    asyncio.run(report_failing_scheduled_runs(manager))
    return [call.kwargs["message"] for call in manager.team_comms.send_message.await_args_list]


def finish(world, task_id, state=ProgressState.FAILED):
    assert world.tasks.update_main_task_state(task_id, state, datetime.now(timezone.utc).isoformat())


def another_run(world, run, task_id):
    """A later run of the same schedule, with the same signed identity."""
    world.tasks.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Check launches."),
        run.header, run.meta, owner_person_id=ANN,
    )
    return task_id


def problems(world):
    return ScheduledRunProblems(world.tasks.storage_settings).problems(TEAM)


@pytest.mark.parametrize("use_permit", [True, False])
def test_a_failed_one_time_reminder_is_reported_once_and_then_closes_without_a_message(world, use_permit, caplog):
    run = start_final_run(world)
    if not use_permit:
        # A run the manager signed itself; its context may have expired since.
        run.meta.gateway_context.schedule_run = None
        run.meta.gateway_context.task_context = world.signer.issue(
            team_id=TEAM, person_id=ANN, schedule_id=SCHEDULE_ID,
            now=datetime.now(timezone.utc) - timedelta(days=8),
        )
    finish(world, run.task.task_id)
    world.tasks._save_completed_task(run.task.task_id, ProgressState.FAILED)
    assert world.tasks.get_task_info(run.task.task_id) is None, "the report must survive immediate archival"
    # What the person's assistant reads by the run id in the notice (get_task_history_window).
    assert world.tasks.get_task_from_db(run.task.task_id, owner_canonical_user_id=ANN, include_all_agents=True)
    assert world.store.prune_expired() == 1, "the reminder's schedule is removed once its run ended"

    [message] = report(world)
    [notice] = notices(world)
    assert not notices(world, "usr_bob")
    assert message.msg_content.reason == STUCK_NOTICE_REASON
    assert message.msg_header.to_routing_path == "gateway.responses"
    assert message.msg_meta.gateway_context.scheduled_job_id is None  # a notice, not the run's result
    text = message.msg_content.text
    assert notice.title == f'Scheduled job "{NAME}" stopped before finishing' and notice.summary in text
    assert "It is not scheduled to run again." in text
    # No internal ids, but the run's, in the question the assistant reads the run's history by.
    assert SCHEDULE_ID not in text
    assert f"ask your assistant: “Why did the scheduled job \"{NAME}\" fail on " in text
    assert f"(run {run.task.task_id}), and what had it already done?”" in text

    caplog.set_level("INFO")
    assert report(world) == []
    assert "event=stuck_work_closed_silently" in caplog.text
    assert problems(world) == ()
    assert report(world) == [] and len(notices(world)) == 1


def test_a_failing_recurring_job_is_reported_once_per_problem_and_once_when_it_works_again(world):
    run = start_final_run(world, interval=DAILY, count=None)
    finish(world, run.task.task_id)
    [failed] = report(world)
    assert "you will not be told again" in failed.msg_content.text

    finish(world, another_run(world, run, "task_second_failure"))
    assert report(world) == [], "a further failure of the same problem says nothing"

    finish(world, another_run(world, run, "task_works"), ProgressState.COMPLETED)
    [recovered] = report(world)
    assert recovered.msg_content.reason == RECOVERED_NOTICE_REASON
    assert recovered.msg_content.text.startswith(f'Scheduled job "{NAME}" is working again')
    assert [notice.title for notice in notices(world)] == [
        f'Scheduled job "{NAME}" is working again', f'Scheduled job "{NAME}" stopped before finishing',
    ]
    assert report(world) == [] and problems(world) == ()

    finish(world, another_run(world, run, "task_new_problem"))
    assert len(report(world)) == 1, "a failure after it worked again is a new problem"


def test_a_job_turned_off_after_its_failure_was_told_closes_without_a_message(world):
    run = start_final_run(world, interval=DAILY, count=None)
    finish(world, run.task.task_id)
    assert len(report(world)) == 1
    world.store.set_enabled(SCHEDULE_ID, False)
    assert report(world) == [] and problems(world) == ()


def test_team_and_system_work_is_reported_to_administrators_in_the_inbox_only(world):
    run = start_final_run(world, interval=DAILY, count=None)
    run.meta.gateway_context.schedule_run = None
    run.meta.gateway_context.task_context = world.signer.issue(team_id=TEAM, person_id=None, schedule_id=SCHEDULE_ID)
    finish(world, run.task.task_id)
    assert report(world) == []
    [notice] = notices(world, "usr_admin", roles=("admin",))
    assert notice.title == f'Scheduled job "{NAME}" stopped before finishing'
    assert notices(world) == []


def test_a_run_stopped_at_the_wait_limit_gets_one_message(world):
    run = start_final_run(world, interval=DAILY, count=None)
    finish(world, run.task.task_id, ProgressState.IN_PROGRESS)
    worker = SimpleNamespace(
        id=world.worker.id, name="researcher", team_id=TEAM, named_agent_type=world.worker.named_agent_type,
        task_manager=world.tasks, team_comms=SimpleNamespace(send_message=AsyncMock()),
        _send_task_error_response=AsyncMock(), _notify_context_after_turn=AsyncMock(),
        _conversation_store=None, _conversation_compactor=None, _conversation_artifact_offloader=None,
        _conversation_summary_generator=None, _logger=logging.getLogger("wait-limit"), _strands_agent=None,
        _custody_release=None,
    )
    asyncio.run(fail_task_after_turn_error(
        worker, run.task.task_id, world.tasks.get_task_info(run.task.task_id),
        TaskWaitLimitReached(task_id=run.task.task_id, waits=5, limit=5),
    ))
    worker.team_comms.send_message.assert_not_awaited()
    assert len(report(world)) == 1


def test_a_chat_message_that_fails_is_retried_with_the_same_key_and_no_second_inbox_notice(world, caplog):
    run = start_final_run(world, interval=DAILY, count=None)
    finish(world, run.task.task_id)
    first = lead(world)
    first.team_comms.send_message.side_effect = RuntimeError("gateway unavailable")
    report(world, first)
    assert len(notices(world)) == 1
    assert "event=stuck_work_report_failed" in caplog.text
    key = first.team_comms.send_message.call_args.kwargs["message"].msg_header.idempotency_key
    [retried] = report(world)
    assert retried.msg_header.idempotency_key == key
    assert len(notices(world)) == 1
    assert report(world) == []


def test_a_rolled_back_terminal_snapshot_records_no_problem(world, monkeypatch):
    run = start_final_run(world)
    original = ScheduledRunProblems.record_outcome

    def record_then_fail(conn, dialect, task, team_id, agent_id):
        original(conn, dialect, task, team_id, agent_id)
        if task.state == ProgressState.FAILED:
            raise RuntimeError("snapshot commit failed")

    monkeypatch.setattr(ScheduledRunProblems, "record_outcome", staticmethod(record_then_fail))
    with pytest.raises(RuntimeError, match="snapshot commit failed"):
        finish(world, run.task.task_id)
    assert problems(world) == ()
    assert world.store.worker_records.for_task(run.task.task_id, TEAM).finished_at_utc is None


def test_saving_an_old_failure_again_does_not_reopen_a_problem_a_newer_run_resolved(world):
    run = start_final_run(world, interval=DAILY, count=None)
    finish(world, run.task.task_id)
    finish(world, another_run(world, run, "task_works"), ProgressState.COMPLETED)
    world.tasks.append_history_entry(run.task.task_id, "A late note on the failed run.")
    [problem] = problems(world)
    assert problem.failed.task_id == run.task.task_id and problem.recovered.task_id == "task_works"


@pytest.mark.parametrize("case", ["successful", "unsigned", "not_a_timer", "forged", "gateway_signed"])
def test_finished_or_unverified_runs_are_not_reported(world, case, caplog):
    from tests.broker_access_support import context_signer

    run = start_final_run(world)
    if case == "unsigned":
        run.meta.gateway_context.schedule_run = None
    elif case == "not_a_timer":
        run.meta.requester_type = RequesterType.USER_REQUESTER
    elif case == "forged":
        signed = run.meta.gateway_context.schedule_run.permit
        run.meta.gateway_context.schedule_run = run.meta.gateway_context.schedule_run.model_copy(update={
            "permit": signed.model_copy(update={"permit": signed.permit.model_copy(update={"person_id": "usr_bob"})}),
        })
    elif case == "gateway_signed":
        run.meta.gateway_context.schedule_run = None
        run.meta.gateway_context.task_context = context_signer().issue(
            team_id=TEAM, person_id=ANN, schedule_id=SCHEDULE_ID,
        )
    finish(world, run.task.task_id, ProgressState.COMPLETED if case == "successful" else ProgressState.FAILED)
    assert report(world) == []
    assert not notices(world) and not notices(world, "usr_bob")
    assert problems(world) == ()
    if case in ("forged", "gateway_signed"):
        assert "event=scheduled_run_failure_identity_refused" in caplog.text


def test_a_heartbeat_items_runs_count_as_its_own_job(world):
    """A heartbeat launcher's runs name their item, which runs again while it is turned on."""

    store = HeartbeatStore(storage_settings=world.tasks.storage_settings)
    item = store.add_item(source_id="source_1", title="Morning check-in", instruction="Check in.", conversation_id="c")
    source = FailingScheduledRuns(
        lead(world), ScheduledRunProblems(world.tasks.storage_settings),
        StuckWorkRepository(world.tasks.storage_settings),
    )
    now = datetime.now(timezone.utc)
    schedule = source.schedule(item.heartbeat_item_id, now)
    assert (schedule.label, schedule.runs_again) == ("Morning check-in", True)
    store.set_item_enabled(item.heartbeat_item_id, False)
    assert source.schedule(item.heartbeat_item_id, now).runs_again is False
    assert source.schedule("removed_schedule", now).runs_again is False
