"""Scheduled runs get protocol 1 authority tokens from ``svc.scheduler`` when no standing permit exists.

Covers:
- ``team_services.scheduler.SchedulerOperations`` signs a claimed run for the schedule's
  vouched active owner, and tool calls are decided by real Cedar with the owner's live roles;
- ``svc.scheduler`` refuses a wrong claim token, a claim held by another agent, a frame
  issuer that does not match the claim holder (``claim_not_held``), a missing scheduler
  certificate (``signer_missing``), a disabled/removed/unvouched owner (``owner_inactive``),
  and a schedule job mismatch (``scheduled_job_mismatch``);
- a refusal or timeout releases the claim and starts nothing;
- a schedule paused or turned off after claiming does not run and releases the claim;
- a retimed claim waits for the new due time;
- neither the team lead nor any worker holds a signing key (both follow permit-first -> ``svc.scheduler``);
- claimless ``scheduled_message`` mailbox replays use the schedule's standing permit (logged)
  and refuse to start unsigned when no permit exists (C02);
- heartbeat items without a standing permit are signed individually by ``svc.scheduler``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_agents import access_runtime
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.scheduled_run_contexts import ScheduledRun, sign_scheduled_run
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.wire.authority_bindings import SCHEDULER_ISSUER_ROLE
from custom_transport.protocol1.live import NO_ANSWER_IN_TIME, ServiceUnavailable
from tests.broker_access_support import forget_task_context_keys
from tests.scheduled_run_support import (
    ANN,
    CEDAR,
    SCHEDULE_ID,
    TEAM,
    WORKER_ID,
    access_agent,
    build_world,
    claim,
    claim_row,
    fake_verified_frame,
    fire,
    issue_permits,
    queued,
    queued_run,
    record_ann,
    run_context,
    save_schedule,
    subscribe,
    worker_agent,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yield build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


def _sign_body(
    claimed: dict,
    *,
    schedule_id: str = SCHEDULE_ID,
    scheduled_job_id: str = SCHEDULE_ID,
    claim_token: str | None = None,
    conversation_id: str = "conv_1",
    heartbeat_item_id: str | None = None,
) -> dict:
    body = {
        "request_id": "run_context_1",
        "schedule_id": schedule_id,
        "claim_token": claim_token if claim_token is not None else claimed["claim_token"],
        "scheduled_job_id": scheduled_job_id,
        "conversation_id": conversation_id,
    }
    if heartbeat_item_id is not None:
        body["heartbeat_item_id"] = heartbeat_item_id
    return body


# -- svc.scheduler signs a claimed run -------------------------------------------------


@pytest.mark.skipif(not CEDAR.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_svc_scheduler_signs_a_workers_claimed_run_and_its_tool_calls_are_allowed(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE, ("member",))
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO):
        deadlines = asyncio.run(scenario())

    assert deadlines == {}, "the run was accepted"
    assert len(world.host.requests) == 1
    sender, body = world.host.requests[0]
    assert (sender, body["schedule_id"], body["scheduled_job_id"]) == (WORKER_ID, SCHEDULE_ID, SCHEDULE_ID)
    assert "event=scheduler_run_context_signed" in caplog.text
    run = queued_run(world)
    context = run_context(run, world)
    assert (context.issuer, context.person_id, context.person_roles, context.schedule_id) == (
        SCHEDULER_ISSUER_ROLE,
        ANN,
        ("member",),
        SCHEDULE_ID,
    )
    assert claim_row(world) == (None, None), "the fire advanced the schedule and ended the claim"

    agent = access_agent(world, run)
    assert access_runtime.decide_call(agent, "exa_search", {}).allowed
    # The same run without the scheduler's token is refused.
    run.meta.gateway_context.task_context = None
    refused = access_runtime.decide_call(agent, "exa_search", {})
    assert not refused.allowed and "Nobody verifiably asked" in refused.reason


# -- svc.scheduler refuses; the worker does not start the run and releases the claim ---


def _assert_not_started_and_released(world, deadlines: dict, caplog, reason: str) -> None:
    assert queued(world) == [], "no run was started"
    assert SCHEDULE_ID in deadlines, "the schedule waits before it is claimed again"
    assert claim_row(world) == (None, None), "the claim was released"
    assert "event=scheduled_run_not_started_unsigned" in caplog.text
    assert f"reason={reason}" in caplog.text


def test_a_wrong_claim_token_is_refused_and_the_run_does_not_start(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        return await fire(world, {**claimed, "claim_token": "sched_claim_forged"})

    with caplog.at_level(logging.INFO):
        deadlines = asyncio.run(scenario())

    assert "event=scheduler_run_context_refused" in caplog.text
    assert queued(world) == []
    assert SCHEDULE_ID in deadlines
    assert "outcome=claim_not_held" in caplog.text
    assert claim_row(world)[0] == WORKER_ID


def test_a_claim_held_by_another_agent_is_refused(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    other = worker_agent(world.tasks, world.store, "worker_2")

    async def scenario():
        await subscribe(world)
        claimed = claim(world)  # worker_1 holds it
        return await fire(world, claimed, agent=other)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    assert queued(world) == []
    assert "event=scheduler_run_context_refused" in caplog.text and "outcome=claim_not_held" in caplog.text
    assert claim_row(world)[0] == WORKER_ID, "only the holder can release its claim"


def test_svc_scheduler_checks_frame_issuer_agent_and_signer_presence(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    claimed = claim(world)
    body = _sign_body(claimed)

    with caplog.at_level(logging.WARNING):
        wrong_agent = world.scheduler.answer(fake_verified_frame("run_context.sign", body, sender_agent="worker_2"))
        honest = world.scheduler.answer(fake_verified_frame("run_context.sign", body, sender_agent=WORKER_ID))
        world.scheduler_state["identity"] = None
        keyless = world.scheduler.answer(fake_verified_frame("run_context.sign", body, sender_agent=WORKER_ID))

    assert wrong_agent["outcome"] == "claim_not_held"
    assert honest["outcome"] == "ok" and honest["system_work"] is False and "authority" in honest
    assert keyless["outcome"] == "signer_missing"


@pytest.mark.parametrize("standing", [PersonStanding.DISABLED, PersonStanding.REMOVED])
def test_svc_scheduler_refuses_a_run_for_a_disabled_or_removed_owner(world, caplog, standing) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    claimed = claim(world)
    record_ann(world, standing, ())

    with caplog.at_level(logging.INFO):
        reply = world.scheduler.answer(fake_verified_frame("run_context.sign", _sign_body(claimed), sender_agent=WORKER_ID))

    assert reply["outcome"] == "owner_inactive"
    assert "event=scheduled_run_skipped_owner_inactive" in caplog.text


def test_svc_scheduler_refuses_unvouched_owner(world) -> None:
    record_ann(world, PersonStanding.ACTIVE, ("admin",))
    save_schedule(world, vouched=False)
    claimed = claim(world)

    reply = world.scheduler.answer(fake_verified_frame("run_context.sign", _sign_body(claimed), sender_agent=WORKER_ID))
    assert reply["outcome"] == "owner_inactive"


def test_svc_scheduler_refuses_a_context_that_names_another_schedule(world) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    claimed = claim(world)

    reply = world.scheduler.answer(
        fake_verified_frame(
            "run_context.sign",
            _sign_body(claimed, scheduled_job_id="sched_someone_elses"),
            sender_agent=WORKER_ID,
        )
    )
    assert reply["outcome"] == "scheduled_job_mismatch"


def test_a_refusal_releases_the_claim_so_the_schedule_is_retried(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        world.scheduler._store = _StandingChangesOnRead(world)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        deadlines = asyncio.run(scenario())

    _assert_not_started_and_released(world, deadlines, caplog, "owner_inactive")


@pytest.mark.parametrize("control", ["pause", "turn_off"])
def test_a_schedule_paused_or_turned_off_after_its_claim_does_not_run_and_frees_the_claim(world, caplog, control) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        if control == "pause":
            assert world.store.set_paused_until(SCHEDULE_ID, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        else:
            assert world.store.set_enabled(SCHEDULE_ID, False)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO, logger="test.worker"):
        asyncio.run(scenario())

    assert queued(world) == [], "no run was started"
    assert claim_row(world) == (None, None), "the claim was released"
    assert "event=scheduled_run_skipped_schedule_stopped" in caplog.text
    assert f"schedule_id={SCHEDULE_ID}" in caplog.text
    assert not world.host.requests, "svc.scheduler was not asked for a run that does not start"


@pytest.mark.parametrize("one_time", [False, True])
def test_a_claim_retimed_before_admission_waits_and_counts_only_the_actual_run(world, monkeypatch, caplog, one_time):
    from custom_agents import schedule_store, scheduled_runtime
    from custom_messages.contracts.schedule_worker_v1 import occurrence_identity
    from storage import schedule_worker_repository

    record_ann(world, PersonStanding.ACTIVE)
    saved = save_schedule(world)
    world.store.update(
        saved.model_copy(update={"interval_ms": None if one_time else saved.interval_ms, "repeat_count": 1 if one_time else 2}),
        base=saved,
    )
    original_claim = claim(world)
    base = world.store.get_scheduled_message(SCHEDULE_ID)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    world.store.update(base.model_copy(update={"next_fire_at": future.isoformat()}), base=base)

    async def scenario():
        await subscribe(world)
        await fire(world, original_claim)

    with caplog.at_level(logging.INFO, logger="test.worker"):
        asyncio.run(scenario())

    future_id = occurrence_identity(SCHEDULE_ID, future.isoformat())
    assert queued(world) == []
    assert world.store.worker_records.get_occurrence(future_id, TEAM) is None
    assert claim_row(world) == (None, None)
    assert world.store.get_row(SCHEDULE_ID)["remaining_count"] == (1 if one_time else 2)
    assert "event=scheduled_run_skipped_schedule_retimed" in caplog.text

    class AtFutureRun(datetime):
        @classmethod
        def now(cls, tz=None):
            return (future + timedelta(seconds=1)).astimezone(tz)

    monkeypatch.setattr(schedule_store, "datetime", AtFutureRun)
    monkeypatch.setattr(scheduled_runtime, "datetime", AtFutureRun)
    monkeypatch.setattr(schedule_worker_repository, "database_now", lambda _conn: AtFutureRun.now(timezone.utc))
    world.scheduler._clock = lambda: AtFutureRun.now(timezone.utc)
    world.verifier._clock = lambda: AtFutureRun.now(timezone.utc)
    asyncio.run(fire(world, claim(world)))

    assert len(queued(world)) == 1
    assert world.store.worker_records.get_occurrence(future_id, TEAM) is not None
    row = world.store.get_row(SCHEDULE_ID)
    if one_time:
        assert row["remaining_count"] == 0, "the final task still needs its schedule and access"
    else:
        assert row["remaining_count"] == 1
        assert row["next_fire_at"] > future.isoformat()


def test_svc_scheduler_refuses_a_run_paused_after_the_workers_check(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        world.scheduler._store = _PausedOnRead(world)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())

    assert queued(world) == [], "no run was started"
    assert claim_row(world) == (None, None), "the claim was released"
    [skipped] = [r for r in caplog.records if "event=scheduled_run_skipped_schedule_stopped" in r.getMessage()]
    assert skipped.levelno == logging.INFO and "retried" not in skipped.getMessage()
    assert "event=scheduled_run_not_started_unsigned" not in caplog.text
    assert "outcome=schedule_stopped" in caplog.text


class _PausedOnRead:
    def __init__(self, world) -> None:
        self._world = world

    def __getattr__(self, name):
        return getattr(self._world.store, name)

    def get_scheduled_message(self, schedule_id):
        self._world.store.set_paused_until(schedule_id, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        return self._world.store.get_scheduled_message(schedule_id)


class _StandingChangesOnRead:
    def __init__(self, world) -> None:
        self._world = world

    def __getattr__(self, name):
        return getattr(self._world.store, name)

    def get_scheduled_message(self, schedule_id):
        record_ann(self._world, PersonStanding.DISABLED, ())
        return self._world.store.get_scheduled_message(schedule_id)


# -- no answer in time ----------------------------------------------------------------


def test_no_answer_in_time_releases_the_claim_and_starts_nothing(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def _timeout(_kind, _body, _sender):
        raise ServiceUnavailable("run_context.sign", "svc.scheduler", NO_ANSWER_IN_TIME)

    world.host.custom_handler = _timeout

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    with caplog.at_level(logging.WARNING, logger="custom_agents.scheduled_run_contexts"):
        deadlines = asyncio.run(scenario())

    _assert_not_started_and_released(world, deadlines, caplog, "no_answer")
    assert len(world.host.requests) == 1


# -- claimless mailbox replay (C02) ---------------------------------------------------


def test_claimless_mailbox_replay_uses_standing_permit_or_skips_unsigned_c02(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE, ("member",))
    schedule = save_schedule(world)
    owner = world.people.person(ANN)

    # Without a standing permit and without a claim, mailbox replay skips instead of running unsigned.
    meta_no_permit = MessageMeta(
        conversation_id="conv_replay_1",
        gateway_context=GatewayContext(canonical_user_id=ANN),
    )
    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        ok_without = asyncio.run(
            sign_scheduled_run(
                world.manager,
                meta_no_permit,
                ScheduledRun(schedule, None, SCHEDULE_ID, owner, from_runtime_mailbox=True),
            )
        )
    assert ok_without is False
    assert "event=scheduled_run_mailbox_replay_unpermitted" in caplog.text
    assert world.host.requests == []

    # With a standing permit, claimless mailbox replay attaches it and logs the named path.
    issue_permits(world)
    meta_with_permit = MessageMeta(
        conversation_id="conv_replay_2",
        gateway_context=GatewayContext(canonical_user_id=ANN),
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        ok_with = asyncio.run(
            sign_scheduled_run(
                world.manager,
                meta_with_permit,
                ScheduledRun(schedule, None, SCHEDULE_ID, owner, from_runtime_mailbox=True),
            )
        )
    assert ok_with is True
    assert "event=scheduled_run_mailbox_replay_permit" in caplog.text
    assert meta_with_permit.gateway_context.schedule_run is not None
    assert world.host.requests == []


# -- heartbeat items ------------------------------------------------------------------


def test_each_heartbeat_item_is_signed_by_svc_scheduler_for_that_item(world) -> None:
    record_ann(world, PersonStanding.ACTIVE, ("member",))
    heartbeats = HeartbeatStore(db_path=world.db_path, storage_settings=world.tasks.storage_settings)
    heartbeats.upsert_source(source_id="hb_ann", title="Ann's checks")
    items = [
        heartbeats.add_item(source_id="hb_ann", title=title, instruction=title, conversation_id=f"conv_{index}")
        for index, title in enumerate(("Check launches", "Check weather"))
    ]
    save_schedule(world, heartbeat_source_id="hb_ann")

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    assert asyncio.run(scenario()) == {}
    signed = {
        run_context(info, world).schedule_id: (run_context(info, world).person_id, info.meta.conversation_id)
        for info in queued(world)
    }
    assert signed == {item.heartbeat_item_id: (ANN, item.conversation_id) for item in items}
