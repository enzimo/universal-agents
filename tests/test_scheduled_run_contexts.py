"""A worker process without the scheduler key gets its scheduled runs signed by the manager.

This is the fallback for a schedule that has no run permit yet (the
manager's issuing pass has not run in these tests); with a permit the worker
asks nobody (``tests/test_schedule_permits.py``).

Real SQLite (schedule rows, claims, the team's people, the task store), real
Ed25519 signing and verification, the real fire path
(``scheduled_runtime._fire_claimed_schedule``) and real Cedar for the tool
decision. The two processes share one test process, so which of them holds
the scheduler key is set explicitly: the environment is the worker's (it
trusts the scheduler key but holds none), and the manager's answer is given
the signer the manager process would read from its own environment. Messages
cross an in-memory bus that serializes them like the wire; the last test
sends them over a live NATS server instead.

Covers:
- the manager signs a claimed run for the schedule's owner with the owner's
  current roles, and the run's tool call is then allowed;
- the manager refuses a wrong claim token, a claim held by another agent, a
  sender that is not the agent named in the request, and a disabled or
  removed owner; the worker then does not start the run and releases the claim;
- no answer in time: the run is not started and the claim is released;
- an agent with the key in its own process signs locally and sends nothing;
- a heartbeat schedule's items are signed one by one, for the item.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_control.identity import ENV_VERIFY_KEYS, ISSUER_SCHEDULER
from custom_agents import access_runtime, scheduled_run_contexts
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.scheduled_run_contexts import (
    answer_run_context_request,
    asks_manager,
    subscribe_run_context_routes,
)
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.scheduled_run_context_v1 import (
    ScheduledRunContextRefusal,
    ScheduledRunContextRequestV1,
)
from custom_transport.subjects import NatsSubjectMapper
from tests.broker_access_support import context_verify_keys, forget_task_context_keys, use_scheduler_key
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
    fire,
    queued,
    queued_run,
    record_ann,
    run_context,
    save_schedule,
    subscribe,
    transport,
    worker_agent,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A worker process's view: it trusts the scheduler key but holds none (``scheduled_run_support``)."""

    yield build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


# -- the claiming side decides who signs ----------------------------------------------


def test_only_a_worker_without_the_key_that_trusts_the_scheduler_asks(world, monkeypatch) -> None:
    assert asks_manager(world.worker)
    assert not asks_manager(world.manager), "the team lead is the manager and signs itself"
    # A process that trusts no scheduler key keeps the local path.
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    assert not asks_manager(world.worker)


# -- the manager signs a claimed run --------------------------------------------------


@pytest.mark.skipif(not CEDAR.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_the_manager_signs_a_workers_claimed_run_and_its_tool_calls_are_allowed(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE, ("member",))  # her current role, not the one she had
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        deadlines = asyncio.run(scenario())

    assert deadlines == {}, "the run was accepted"
    requests = [content for _subject, content in world.bus.published if isinstance(content, ScheduledRunContextRequestV1)]
    assert [(request.agent_id, request.schedule_id, request.scheduled_job_id) for request in requests] == [
        (WORKER_ID, SCHEDULE_ID, SCHEDULE_ID)
    ]
    assert "event=scheduled_run_context_signed" in caplog.text
    run = queued_run(world)
    context = run_context(run)
    assert (context.issuer, context.person_id, context.person_roles, context.schedule_id) == (
        ISSUER_SCHEDULER,
        ANN,
        ("member",),
        SCHEDULE_ID,
    )
    assert claim_row(world) == (None, None), "the fire advanced the schedule and ended the claim"

    agent = access_agent(world, run)
    assert access_runtime.decide_call(agent, "exa_search", {}).allowed
    # The same run without the manager's signature is refused, as before the fix.
    run.meta.gateway_context.task_context = None
    refused = access_runtime.decide_call(agent, "exa_search", {})
    assert not refused.allowed and "Nobody verifiably asked" in refused.reason


# -- the manager refuses; the worker does not start the run and releases the claim ----


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
        # The row keeps the real token; this worker shows another one.
        return await fire(world, {**claimed, "claim_token": "sched_claim_forged"})

    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        deadlines = asyncio.run(scenario())

    assert "event=scheduled_run_context_refused" in caplog.text
    assert queued(world) == []
    assert SCHEDULE_ID in deadlines
    assert "reason=claim_not_held" in caplog.text
    # The forged token cannot release the real claim; it expires on its own.
    assert claim_row(world)[0] == WORKER_ID


def test_a_claim_held_by_another_agent_is_refused(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    other = worker_agent(world.tasks, world.store, world.bus, "worker_2")

    async def scenario():
        await subscribe(world)
        await subscribe_run_context_routes(other, *transport(other))
        claimed = claim(world)  # worker_1 holds it
        return await fire(world, claimed, agent=other)

    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        asyncio.run(scenario())

    assert queued(world) == []
    assert "event=scheduled_run_context_refused" in caplog.text and "reason=claim_not_held" in caplog.text
    assert claim_row(world)[0] == WORKER_ID, "only the holder can release its claim"


def test_the_manager_refuses_a_sender_that_is_not_the_agent_in_the_request(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    claimed = claim(world)
    request = ScheduledRunContextRequestV1(
        request_id="run_context_1",
        team_id=TEAM,
        agent_id=WORKER_ID,
        schedule_id=SCHEDULE_ID,
        claim_token=claimed["claim_token"],
        scheduled_job_id=SCHEDULE_ID,
    )

    with caplog.at_level(logging.WARNING, logger="custom_agents.scheduled_run_contexts"):
        reply = asyncio.run(answer_run_context_request(world.manager, request, sender="worker_2", signer=world.signer))
        honest = asyncio.run(answer_run_context_request(world.manager, request, sender=WORKER_ID, signer=world.signer))
        keyless = asyncio.run(answer_run_context_request(world.manager, request, sender=WORKER_ID, signer=None))

    assert (reply.task_context, reply.refusal) == (None, ScheduledRunContextRefusal.SENDER_MISMATCH)
    assert honest.task_context is not None and honest.refusal is None
    assert keyless.refusal is ScheduledRunContextRefusal.SIGNER_MISSING, "a manager without the key says so"
    assert "reason=sender_mismatch" in caplog.text and "sender=worker_2" in caplog.text


@pytest.mark.parametrize("standing", [PersonStanding.DISABLED, PersonStanding.REMOVED])
def test_the_manager_refuses_a_run_for_a_disabled_or_removed_owner(world, caplog, standing) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    claimed = claim(world)
    # Ann leaves after the worker checked her standing and before the manager signs.
    record_ann(world, standing, ())
    request = ScheduledRunContextRequestV1(
        request_id="run_context_2",
        team_id=TEAM,
        agent_id=WORKER_ID,
        schedule_id=SCHEDULE_ID,
        claim_token=claimed["claim_token"],
        scheduled_job_id=SCHEDULE_ID,
    )

    with caplog.at_level(logging.INFO):
        reply = asyncio.run(answer_run_context_request(world.manager, request, sender=WORKER_ID, signer=world.signer))

    assert reply.refusal is ScheduledRunContextRefusal.OWNER_INACTIVE
    assert "event=scheduled_run_skipped_owner_inactive" in caplog.text


def test_the_manager_refuses_a_context_that_names_another_schedule(world) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    claimed = claim(world)
    request = ScheduledRunContextRequestV1(
        request_id="run_context_3",
        team_id=TEAM,
        agent_id=WORKER_ID,
        schedule_id=SCHEDULE_ID,
        claim_token=claimed["claim_token"],
        scheduled_job_id="sched_someone_elses",
    )

    reply = asyncio.run(answer_run_context_request(world.manager, request, sender=WORKER_ID, signer=world.signer))

    assert reply.refusal is ScheduledRunContextRefusal.SCHEDULED_JOB_MISMATCH


def test_a_refusal_releases_the_claim_so_the_schedule_is_retried(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        # The owner leaves between the worker's check and the manager's.
        world.manager._schedule_store = _StandingChangesOnRead(world)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        deadlines = asyncio.run(scenario())

    _assert_not_started_and_released(world, deadlines, caplog, "owner_inactive")


@pytest.mark.parametrize("control", ["pause", "turn_off"])
def test_a_schedule_paused_or_turned_off_after_its_claim_does_not_run_and_frees_the_claim(world, caplog, control) -> None:
    """Control changes keep the claim (2026-10-17), so the worker checks the schedule before its run starts."""
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
    assert not world.bus.published, "the manager was not asked to sign a run that does not start"


def test_the_manager_refuses_a_run_paused_after_the_workers_check(world, caplog) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)

    async def scenario():
        await subscribe(world)
        claimed = claim(world)
        world.manager._schedule_store = _PausedOnRead(world)
        return await fire(world, claimed)

    with caplog.at_level(logging.INFO, logger="custom_agents.scheduled_run_contexts"):
        asyncio.run(scenario())

    assert queued(world) == [], "no run was started"
    assert claim_row(world) == (None, None), "the claim was released"
    [skipped] = [r for r in caplog.records if "event=scheduled_run_skipped_schedule_stopped" in r.getMessage()]
    assert skipped.levelno == logging.INFO and "retried" not in skipped.getMessage()
    assert "event=scheduled_run_not_started_unsigned" not in caplog.text
    assert "reason=schedule_stopped" in caplog.text  # the manager's refusal, logged where it decides


class _PausedOnRead:
    """The manager's schedule store, where the person pauses the schedule just before the manager reads it."""

    def __init__(self, world) -> None:
        self._world = world

    def __getattr__(self, name):
        return getattr(self._world.store, name)

    def get_scheduled_message(self, schedule_id):
        self._world.store.set_paused_until(schedule_id, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        return self._world.store.get_scheduled_message(schedule_id)


class _StandingChangesOnRead:
    """The manager's schedule store, where the owner is disabled just before the manager reads the row."""

    def __init__(self, world) -> None:
        self._world = world

    def __getattr__(self, name):
        return getattr(self._world.store, name)

    def get_scheduled_message(self, schedule_id):
        record_ann(self._world, PersonStanding.DISABLED, ())
        return self._world.store.get_scheduled_message(schedule_id)


# -- no answer in time ----------------------------------------------------------------


def test_no_answer_in_time_releases_the_claim_and_starts_nothing(world, caplog, monkeypatch) -> None:
    record_ann(world, PersonStanding.ACTIVE)
    save_schedule(world)
    monkeypatch.setattr(scheduled_run_contexts, "REPLY_TIMEOUT_SECONDS", 0.2)

    async def scenario():
        # Only the worker listens: the manager is down.
        await subscribe_run_context_routes(world.worker, *transport(world.worker))
        return await fire(world, claim(world))

    with caplog.at_level(logging.WARNING, logger="custom_agents.scheduled_run_contexts"):
        deadlines = asyncio.run(scenario())

    _assert_not_started_and_released(world, deadlines, caplog, "no_answer")
    assert [type(content).__name__ for _subject, content in world.bus.published] == ["ScheduledRunContextRequestV1"]


# -- the key in this process: sign locally --------------------------------------------


def test_an_agent_with_the_key_signs_locally_and_sends_nothing(world, monkeypatch) -> None:
    use_scheduler_key(monkeypatch)  # this process holds the key, like the manager process
    record_ann(world, PersonStanding.ACTIVE, ("member",))
    save_schedule(world)

    async def scenario():
        return await fire(world, claim(world))

    assert asyncio.run(scenario()) == {}
    assert world.bus.published == []
    context = run_context(queued_run(world))
    assert (context.issuer, context.person_id, context.person_roles) == (ISSUER_SCHEDULER, ANN, ("member",))


# -- heartbeat items ------------------------------------------------------------------


def test_each_heartbeat_item_is_signed_by_the_manager_for_that_item(world) -> None:
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
        run_context(info).schedule_id: (run_context(info).person_id, run_context(info).conversation_id)
        for info in queued(world)
    }
    assert signed == {item.heartbeat_item_id: (ANN, item.conversation_id) for item in items}


# -- over a live NATS server ------------------------------------------------------------


@pytest.mark.live
@pytest.mark.nats
def test_over_live_nats_the_manager_signs_and_the_worker_starts_the_run(world) -> None:
    from live_nats_client import nats_transport_config_from_env

    from custom_transport.nats_adapter import NatsTransportAdapter
    from id_generation import new_id

    record_ann(world, PersonStanding.ACTIVE, ("member",))
    save_schedule(world)
    # A fresh subject prefix per run keeps this team's subjects apart from others on the server.
    mapper = NatsSubjectMapper(team_id=TEAM, subject_prefix=new_id("runctx_live"))
    config = nats_transport_config_from_env()
    manager_adapter = NatsTransportAdapter(config=config, mapper=mapper)
    worker_adapter = NatsTransportAdapter(config=config, mapper=mapper)
    world.manager._ensure_nats_transport_adapter = lambda: manager_adapter
    world.worker._ensure_nats_transport_adapter = lambda: worker_adapter

    async def scenario():
        await manager_adapter.connect(reconcile_topology=True)
        await worker_adapter.connect()
        try:
            await subscribe_run_context_routes(world.manager, manager_adapter, mapper)
            await subscribe_run_context_routes(world.worker, worker_adapter, mapper)
            return await fire(world, claim(world))
        finally:
            await worker_adapter.close()
            await manager_adapter.close()

    assert asyncio.run(scenario()) == {}
    context = run_context(queued_run(world))
    assert (context.issuer, context.person_id, context.person_roles, context.schedule_id) == (
        ISSUER_SCHEDULER,
        ANN,
        ("member",),
        SCHEDULE_ID,
    )
