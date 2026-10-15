"""Schedules no longer stay stuck after a container is replaced or a run writes notes.

Real SQLite stores and the real scheduled-worker controller, with the fake
host adapter of ``test_schedule_worker_service``. Each test reproduces a block
seen on a live instance on 2026-10-03:

- a worker start left "unknown" by a recreated container blocked every later
  run of its worker type (three schedules);
- a run's first ``notify_user`` (run notes) changed the schedule's fingerprint
  and blocked its next run, as did an owner's own edit and the gateway moving a
  memory-review schedule to the person's current chat.
"""
# ruff: noqa: F811 -- imported pytest fixtures are injected by name

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest

from custom_agents.schedule_changes import (
    ScheduleChangeRequestV1,
    ScheduleUpdateV1,
    prepare_schedule_change,
)
from custom_agents.schedule_execution_digest_upgrade import (
    _old_execution_digest,
    run_execution_digest_upgrade,
    upgrade_execution_digests,
)
from custom_agents.schedule_execution_identity import execution_digest
from custom_agents.schedule_repair_guidance import (
    LAUNCH_LIVENESS_UNKNOWN,
    SCHEDULE_EXECUTION_CHANGED,
)
from custom_agents.schedule_worker_controller import (
    ScheduleWorkerController,
    schedule_ready_for_admission,
)
from custom_agents.strands_team import inspect_worker_launch
from custom_messages.contracts.schedule_v1 import ScheduleTaskMemory
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState, utc_now
from runtime_hosting import process_run
from tests.test_local_schedule_runtime import memory_timer
from tests.test_schedule_worker_service import configure, lead  # noqa: F401

# The start record from the live incident: written before run ids existed, by a
# container whose hostname no longer exists.
OLD_CONTAINER_HOST_ID = "3b1d714a967e:71:2756b2e986ae485abb2493584e306cc1"


def bindings(lead):
    return lead._schedule_store.worker_records


def binding(lead, schedule_id: str = "job"):
    return bindings(lead).get_binding(schedule_id, team_id=lead.team_id)


def use_real_host_inspection(lead, monkeypatch, *, proven: bool) -> None:
    """Inspect starts with the production rule, as a supervised run would."""
    current = process_run.current_process_identity()
    monkeypatch.setattr(
        process_run,
        "current_process_identity",
        lambda: process_run.ProcessIdentity("run-now", current.hostname, current.pid, current.start_ticks),
    )
    monkeypatch.setattr(process_run, "current_run_proven", lambda: proven)
    lead._local_team_runtime.inspect_launch = inspect_worker_launch


def later(monkeypatch, minutes: int) -> None:
    """Move the controller's clock past start retry backoff."""
    moved = utc_now() + timedelta(minutes=minutes)
    monkeypatch.setattr("custom_agents.schedule_worker_controller.utc_now", lambda: moved)


async def start_then_lose_container(lead):
    """The first start, then the container is replaced before the run is claimed."""
    configure(lead)
    await ScheduleWorkerController(lead).reconcile()
    assert len(lead.starts) == 1
    launches = lead.task_manager.storage_settings
    from storage.agent_launch_repository import AgentLaunchRepository

    repository = AgentLaunchRepository(launches)
    (record,) = repository.list(lead.team_id)
    return repository, repository.update(
        record,
        state=AgentLaunchState.UNKNOWN,
        host_id=OLD_CONTAINER_HOST_ID,
        lease_expires_at_utc=utc_now() - timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_start_left_by_a_replaced_container_is_settled_and_the_run_starts_again(
    lead, monkeypatch, caplog
):
    repository, stuck = await start_then_lose_container(lead)
    use_real_host_inspection(lead, monkeypatch, proven=True)
    later(monkeypatch, minutes=10)
    with caplog.at_level(logging.WARNING):
        await ScheduleWorkerController(lead).reconcile()
    settled = repository.get(stuck.launch_id, lead.team_id)
    assert settled.state == AgentLaunchState.FAILED
    assert len(lead.starts) == 2, binding(lead).blocked_reason
    assert binding(lead).blocked_reason is None
    assert "event=worker_launch_unknown_settled" in caplog.text
    assert stuck.launch_id in caplog.text


@pytest.mark.asyncio
async def test_without_proof_the_unknown_start_is_kept_and_named(lead, monkeypatch):
    repository, stuck = await start_then_lose_container(lead)
    use_real_host_inspection(lead, monkeypatch, proven=False)
    later(monkeypatch, minutes=10)
    await ScheduleWorkerController(lead).reconcile()
    assert repository.get(stuck.launch_id, lead.team_id).state == AgentLaunchState.UNKNOWN
    assert len(lead.starts) == 1
    assert binding(lead).blocked_reason == LAUNCH_LIVENESS_UNKNOWN


def test_run_notes_written_by_a_run_do_not_block_its_next_run(lead, monkeypatch):
    from custom_tools.providers import communication

    configure(lead)
    assert lead._schedule_store.get_scheduled_message("job").schedule_memory is None
    monkeypatch.setattr(communication, "_resolve_active_schedule_id", lambda agent: "job")
    recorded = communication._record_schedule_notification_if_active(
        lead, message="Your report is ready.", notification_id="n1", reason="report"
    )
    assert recorded["recorded"]
    schedule = lead._schedule_store.get_scheduled_message("job")
    assert isinstance(schedule.schedule_memory, ScheduleTaskMemory)
    assert schedule_ready_for_admission(lead, schedule)
    assert binding(lead).blocked_reason is None


def test_run_note_policy_is_not_part_of_the_fingerprint(lead):
    configure(lead)
    schedule = lead._schedule_store.get_scheduled_message("job")
    before = execution_digest(schedule, "definition")
    schedule.schedule_memory = ScheduleTaskMemory()
    schedule.schedule_memory.policy.notify = "always"
    assert execution_digest(schedule, "definition") == before


def test_owners_own_edit_moves_the_worker_policy_along(lead, caplog):
    configure(lead)
    store = lead._schedule_store
    request = ScheduleChangeRequestV1(
        schedule_id="job", action="update", update=ScheduleUpdateV1(instruction="Read the weekly report")
    )
    with caplog.at_level(logging.INFO):
        prepare_schedule_change(store, request, person_id="owner").commit(None)
    schedule = store.get_scheduled_message("job")
    assert schedule.payload.problem_statement == "Read the weekly report"
    assert schedule_ready_for_admission(lead, schedule)
    saved = binding(lead)
    assert saved.execution_digest == execution_digest(schedule, saved.definition_digest)
    assert "event=schedule_worker_policy_followed" in caplog.text


def test_a_change_made_outside_an_authorized_path_still_waits_for_review(lead):
    configure(lead)
    store = lead._schedule_store
    schedule = store.get_scheduled_message("job")
    schedule.payload.problem_statement = "Something nobody approved"
    store.add(schedule)
    assert not schedule_ready_for_admission(lead, store.get_scheduled_message("job"))
    assert binding(lead).blocked_reason == SCHEDULE_EXECUTION_CHANGED


def _refresh_with(lead, **gateway_fields):
    from custom_agents.schedule_context_refresh import refresh_schedule_gateway_context

    incoming = lead._schedule_store.get_scheduled_message("job").model_copy(deep=True)
    for name, value in gateway_fields.items():
        setattr(incoming.payload_meta.gateway_context, name, value)
    return refresh_schedule_gateway_context(lead, incoming)


def test_memory_review_schedule_moved_to_the_persons_current_chat_keeps_running(lead):
    from custom_agents.local_schedule_upgrade import apply_local_schedule_upgrade

    memory_timer(lead)
    apply_local_schedule_upgrade(lead)
    assert _refresh_with(lead, session_key="telegram:owner-new-chat", channel_name="telegram")
    schedule = lead._schedule_store.get_scheduled_message("job")
    assert schedule.payload_meta.gateway_context.session_key == "telegram:owner-new-chat"
    assert schedule_ready_for_admission(lead, schedule)
    assert binding(lead).blocked_reason is None


def test_memory_review_schedule_given_to_another_person_waits_for_review(lead):
    from custom_agents.local_schedule_upgrade import apply_local_schedule_upgrade

    memory_timer(lead)
    apply_local_schedule_upgrade(lead)
    assert _refresh_with(lead, canonical_user_id="someone-else")
    assert not schedule_ready_for_admission(lead, lead._schedule_store.get_scheduled_message("job"))
    assert binding(lead).blocked_reason == SCHEDULE_EXECUTION_CHANGED


def _save_with_old_fingerprint(lead, *, policy):
    """A binding saved before 2026-10-04, when run notes counted."""
    saved = binding(lead)
    schedule = lead._schedule_store.get_scheduled_message("job")
    old = _old_execution_digest(schedule, saved.definition_digest, policy)
    assert bindings(lead).rebind_execution(
        "job", lead.team_id, expected_digest=saved.execution_digest, new_digest=old, owner_id="owner"
    )
    return old


@pytest.mark.asyncio
async def test_one_time_upgrade_at_start_re_saves_schedules_changed_only_by_run_notes(lead):
    """The team lead's start runs it on the task lane, which copies data arguments.

    A live agent holds running asyncio tasks, which cannot be copied: passing
    the agent as an argument failed every start from 0.8.0 to 0.9.1 with
    "cannot pickle '_asyncio.Task' object", so the upgrade never ran.
    """
    configure(lead)
    _save_with_old_fingerprint(lead, policy=None)
    store = lead._schedule_store
    schedule = store.get_scheduled_message("job")
    schedule.schedule_memory = ScheduleTaskMemory()  # what the first notify_user did
    store.add(schedule)
    bindings(lead).mark_blocked("job", lead.team_id, SCHEDULE_EXECUTION_CHANGED)
    lead.running_work = asyncio.create_task(asyncio.Event().wait())  # as a live agent holds
    try:
        assert await run_execution_digest_upgrade(lead) == ("job",)
        saved = binding(lead)
        assert saved.blocked_reason is None
        assert saved.execution_digest == execution_digest(store.get_scheduled_message("job"), saved.definition_digest)
        assert await run_execution_digest_upgrade(lead) == ()  # repeat starts change nothing
    finally:
        lead.running_work.cancel()


def test_one_time_upgrade_leaves_a_real_change_for_review(lead):
    configure(lead)
    _save_with_old_fingerprint(lead, policy=None)
    store = lead._schedule_store
    schedule = store.get_scheduled_message("job")
    schedule.payload.problem_statement = "A different job"
    store.add(schedule)
    before = binding(lead).execution_digest
    assert upgrade_execution_digests(lead) == ()
    assert binding(lead).execution_digest == before
