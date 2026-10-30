"""Saved failures produce one owner notice, with durable retry and attribution."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents.scheduled_run_failures import report_scheduled_run_failures
from custom_messages.enums import ProgressState, RequesterType
from storage.access_repository import AccessRepository
from tests.scheduled_run_support import ANN, SCHEDULE_ID, TEAM
from tests.broker_access_support import forget_task_context_keys
from tests.scheduled_run_support import build_world
from tests.test_schedule_final_runs import start_final_run


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


def notices(world, person=ANN):
    return AccessRepository(world.tasks.storage_settings).notices_for(TEAM, person_id=person, roles=())


def finish(world, run, state=ProgressState.FAILED):
    assert world.tasks.update_main_task_state(run.task.task_id, state, datetime.now(timezone.utc).isoformat())


@pytest.mark.parametrize("use_permit", [False, True])
def test_failure_is_reported_once_after_the_schedule_is_removed_and_the_lead_restarts(world, use_permit):
    run = start_final_run(world)
    if not use_permit:
        # Both supported identity forms authorize attribution; no live row is needed.
        run.meta.gateway_context.schedule_run = None
        run.meta.gateway_context.task_context = world.signer.issue(
            team_id=TEAM, person_id=ANN, schedule_id=SCHEDULE_ID,
            now=datetime.now(timezone.utc) - timedelta(days=2),
        )
    finish(world, run)
    world.tasks._save_completed_task(run.task.task_id, ProgressState.FAILED)
    assert world.tasks.get_task_info(run.task.task_id) is None, "failure notices must survive immediate archival"
    assert world.store.prune_expired() == 1
    first = lead(world)
    asyncio.run(report_scheduled_run_failures(first))
    assert len(notices(world)) == 1 and not notices(world, "usr_bob")
    assert first.team_comms.send_message.await_count == 1
    message = first.team_comms.send_message.call_args.kwargs["message"]
    assert message.msg_content.reason == "schedule_run_failed"
    assert message.msg_content.source_task_id == run.task.task_id
    assert message.msg_meta.gateway_context.scheduled_job_id is None
    assert message.msg_header.to_routing_path == "gateway.responses"
    assert "not confirmed" in message.msg_content.text
    restarted = lead(world)
    asyncio.run(report_scheduled_run_failures(restarted))
    assert len(notices(world)) == 1
    assert restarted.team_comms.send_message.await_count == 0


def test_chat_failure_is_retried_with_the_same_delivery_key_and_no_second_inbox_notice(world, caplog):
    run = start_final_run(world)
    finish(world, run)
    first = lead(world)
    first.team_comms.send_message.side_effect = RuntimeError("gateway unavailable")
    asyncio.run(report_scheduled_run_failures(first))
    assert len(notices(world)) == 1
    assert "event=scheduled_run_failure_notice_failed" in caplog.text
    first_key = first.team_comms.send_message.call_args.kwargs["message"].msg_header.idempotency_key
    restarted = lead(world)
    asyncio.run(report_scheduled_run_failures(restarted))
    assert restarted.team_comms.send_message.await_count == 1
    assert restarted.team_comms.send_message.call_args.kwargs["message"].msg_header.idempotency_key == first_key
    assert len(notices(world)) == 1


def test_a_rolled_back_terminal_snapshot_cannot_queue_a_false_failure_notice(world, monkeypatch):
    from storage.scheduled_run_failure_repository import ScheduledRunFailureRepository

    run = start_final_run(world)
    original = ScheduledRunFailureRepository.record

    def record_then_fail(conn, dialect, task, team_id, agent_id):
        original(conn, dialect, task, team_id, agent_id)
        if task.state == ProgressState.FAILED:
            raise RuntimeError("snapshot commit failed")

    monkeypatch.setattr(ScheduledRunFailureRepository, "record", staticmethod(record_then_fail))
    with pytest.raises(RuntimeError, match="snapshot commit failed"):
        finish(world, run)
    outbox = ScheduledRunFailureRepository(world.tasks.storage_settings)
    assert not outbox.pending(TEAM)
    assert world.store.worker_records.for_task(run.task.task_id, TEAM).finished_at_utc is None


def test_inbox_failure_prevents_chat_and_retries_the_pending_notice(world, monkeypatch):
    from storage.scheduled_run_failure_repository import ScheduledRunFailureRepository

    run = start_final_run(world)
    finish(world, run)
    manager = lead(world)
    original = AccessRepository.create_notice_if_absent

    def fail_store(*_args, **_kwargs):
        raise RuntimeError("access store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(AccessRepository, "create_notice_if_absent", fail_store)
        asyncio.run(report_scheduled_run_failures(manager))
    assert manager.team_comms.send_message.await_count == 0
    assert len(ScheduledRunFailureRepository(world.tasks.storage_settings).pending(TEAM)) == 1
    assert AccessRepository.create_notice_if_absent is original
    asyncio.run(report_scheduled_run_failures(manager))
    assert len(notices(world)) == 1
    assert manager.team_comms.send_message.await_count == 1


@pytest.mark.parametrize("case", ["successful", "unsigned", "not_a_timer", "forged", "gateway_signed"])
def test_success_and_unverified_schedule_claims_do_not_create_failure_notices(world, case):
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
    finish(world, run, ProgressState.COMPLETED if case == "successful" else ProgressState.FAILED)
    manager = lead(world)
    asyncio.run(report_scheduled_run_failures(manager))
    assert not notices(world) and not notices(world, "usr_bob")
    assert manager.team_comms.send_message.await_count == 0
