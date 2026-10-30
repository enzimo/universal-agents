"""Final runs keep authority until completion, using real stores and signatures."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from access_control.identity import ContextVerifier, IdentityRejected
from access_control.task_identity import ScheduleRunLookup, job_schedule_id, resolve_task_identity
from custom_agents import access_runtime
from custom_agents.schedule_permits import renew_schedule_permits
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.access_v2 import (
    AccessLevel, GrantStatus, GrantV1, PersonStanding, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1,
)
from custom_agents import scheduled_runtime
from custom_agents.schedule_occurrences import prepare_occurrence
from custom_messages.contracts.runtime_v1 import MEM_REVIEW_AGENT_TYPE
from custom_messages.enums import ProgressState, ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import MEMORY_REVIEW_PREPARER, USERMEM_AUDIT_SCHEDULE_NAME_PREFIX
from storage.access_repository import AccessRepository
from tests.broker_access_support import forget_task_context_keys
from tests.scheduled_run_support import (
    ANN, SCHEDULE_ID, TEAM, access_agent, build_world, claim, fire, needs_cedar,
    queued_run, record_ann, save_schedule, subscribe,
)

USER = "user"
# A person's memory review: a system timer that an on-demand local worker runs.
MEMORY_REVIEW = "memory_review"


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


def start_final_run(world, *, interval=None, count=1, expires=None, followup=None, kind=USER, monkeypatch=None):
    record_ann(world, PersonStanding.ACTIVE)
    base = save_schedule(world)
    changed = base.model_copy(deep=True, update={
        "interval_ms": interval, "repeat_count": count, "expires_at_utc": expires,
    })
    changed.payload_header.response_routing_key = "gateway.responses"
    changed.payload_meta.followup_to_task_id = followup
    if kind == MEMORY_REVIEW:
        world.store.remove(SCHEDULE_ID)
        changed = changed.model_copy(update={
            "management_class": ScheduleManagementClass.SYSTEM,
            "system_schedule_kind": SystemScheduleKind.MEMORY_REVIEW_USERMEM,
            "schedule_name": USERMEM_AUDIT_SCHEDULE_NAME_PREFIX + ANN,
            "claim_named_agent_type": MEM_REVIEW_AGENT_TYPE,
        })
        world.store.add(changed)
        world.worker.named_agent_type = MEM_REVIEW_AGENT_TYPE
        world.tasks.set_owner_identity(
            agent_id=world.worker.id, agent_name="reviewer", team_id=TEAM, named_agent_type=MEM_REVIEW_AGENT_TYPE,
        )
        # The review's data preparation is not under test here.
        monkeypatch.setitem(
            scheduled_runtime.SYSTEM_SCHEDULE_PREPARERS, MEMORY_REVIEW_PREPARER, lambda _agent, _id, message: message,
        )
    else:
        world.store.update(changed, base=base)
    renew_schedule_permits(world.manager, signer=world.signer)

    async def scenario():
        await subscribe(world)
        claimed = world.store.claim_next_due(
            claimer_agent_id=world.worker.id, claimer_named_agent_type=world.worker.named_agent_type,
            claimer_team_id=TEAM,
        )
        return await fire(world, claimed)

    assert asyncio.run(scenario()) == {}
    return queued_run(world)


def resolve(world, run):
    return resolve_task_identity(
        verifier=ContextVerifier.from_env(), team_id=TEAM,
        task_context=run.meta.gateway_context.task_context,
        schedule_run=run.meta.gateway_context.schedule_run,
        schedule_runs=ScheduleRunLookup.from_storage(world.tasks.storage_settings),
    )


@pytest.mark.parametrize("interval,count", [(None, 1), (60_000, 1), (None, None), (None, 5)])
@pytest.mark.parametrize("kind", [USER, MEMORY_REVIEW])
def test_final_run_keeps_identity_and_cannot_fire_again_after_restart(world, monkeypatch, interval, count, kind):
    run = start_final_run(world, interval=interval, count=count, kind=kind, monkeypatch=monkeypatch)
    restarted = ScheduleStore(storage_settings=world.tasks.storage_settings)
    assert restarted.prune_expired() == 0
    row = restarted.get_row(SCHEDULE_ID)
    assert row["remaining_count"] == 0 and row["enabled"]
    assert resolve(world, run).person_id == ANN
    assert job_schedule_id(
        resolve(world, run), ScheduleRunLookup.from_storage(world.tasks.storage_settings),
        lead_agent_type="TeamArchitect",
    ) == (SCHEDULE_ID if kind == USER else None)
    assert restarted.get_next_due() is None
    assert restarted.claim_next_due(
        claimer_agent_id="replacement_worker", claimer_named_agent_type=world.worker.named_agent_type,
        claimer_team_id=TEAM,
    ) is None
    assert restarted.get_all_enabled() == []
    assert restarted.get_earliest_fire_time() is None


@needs_cedar
def test_final_run_passes_the_access_hook_and_broker(world):
    from access_control.caller_tokens import BrokerCaller, agent_caller_label
    from custom_tools.secure_cli_tool import trusted_broker_context
    from secure_tool_broker.access_gate import build_broker_access_gate
    from tests.scheduled_run_support import CEDAR, WORKER_TYPE
    from tests.task_work_support import set_agent_ref

    run = start_final_run(world)
    agent = access_agent(world, run)
    assert access_runtime.decide_call(agent, "exa_search", {}).allowed
    set_agent_ref(agent)
    try:
        context = trusted_broker_context()
    finally:
        set_agent_ref(None)
    gate = build_broker_access_gate(
        instance_root=None, team_id=TEAM, authorization_enabled=True,
        storage_settings=world.tasks.storage_settings, cedar_binary=str(CEDAR),
    )
    try:
        assert gate.check(
            BrokerCaller(agent_caller_label(WORKER_TYPE)), gate.catalog.resolve_function("exa_search", {}), context,
        ).allowed
    finally:
        gate.access.cedar.close()


@pytest.mark.parametrize("state", [ProgressState.COMPLETED, ProgressState.FAILED,
                                   ProgressState.CANCELLED, ProgressState.ABANDONED])
def test_terminal_snapshot_allows_cleanup_and_ends_job_grants(world, state):
    run = start_final_run(world)
    repository = AccessRepository(world.tasks.storage_settings)
    grant = repository.create(GrantV1(
        grant_id="grant_final_run", team_id=TEAM,
        subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=f"{TEAM}/{SCHEDULE_ID}"),
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:web_research"), level=AccessLevel.READ,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ANN),
        created_at_utc=datetime.now(timezone.utc),
    ))
    assert world.store.prune_expired() == 0
    assert repository.get(grant.grant_id).status == GrantStatus.ACTIVE
    assert world.tasks.update_main_task_state(run.task.task_id, state, datetime.now(timezone.utc).isoformat())
    assert world.store.prune_expired() == 1
    assert world.store.get_row(SCHEDULE_ID) is None
    assert repository.get(grant.grant_id).status == GrantStatus.REVOKED
    assert world.store.prune_expired() == 0


@pytest.mark.parametrize("control,reason", [("remove", "no longer exists"), ("disable", "turned off"),
                                           ("owner", "belongs to someone else"), ("person", "not active")])
def test_final_run_still_obeys_live_revocation(world, control, reason):
    run = start_final_run(world)
    if control == "remove":
        world.store.remove(SCHEDULE_ID)
    elif control == "disable":
        world.store.set_enabled(SCHEDULE_ID, False)
    elif control == "person":
        record_ann(world, PersonStanding.DISABLED, ())
    else:
        base = world.store.get_scheduled_message(SCHEDULE_ID)
        changed = base.model_copy(deep=True)
        changed.payload_meta.gateway_context.canonical_user_id = "usr_bob"
        world.store.update(changed, base=base)
    with pytest.raises(IdentityRejected, match=reason):
        resolve(world, run)


def test_expiration_during_a_run_waits_for_its_terminal_snapshot(world):
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    run = start_final_run(world, interval=86_400_000, count=None, expires=expires.isoformat())
    assert world.store.prune_expired(now=expires + timedelta(seconds=1)) == 0
    assert resolve(world, run).person_id == ANN
    assert world.tasks.update_main_task_state(run.task.task_id, ProgressState.COMPLETED, expires.isoformat())
    assert world.store.prune_expired(now=expires + timedelta(seconds=1)) == 1


def test_one_time_run_does_not_inherit_a_reply_obligation(world):
    from custom_agents.turn_action_outbox_runtime import _explicit_suppressed_task_response_invalid_reason
    from custom_messages.enums import MessageMetaType

    run = start_final_run(world, followup="task_that_created_the_reminder")
    assert run.meta.followup_to_task_id is None
    reason, _ = _explicit_suppressed_task_response_invalid_reason(
        incoming_content=run.task, incoming_meta=run.meta,
        outgoing_content=run.task.model_copy(update={"task_type": MessageMetaType.RESPONSE, "answer": "No change."}),
    )
    assert reason is None


def test_retiming_an_admitted_one_time_run_keeps_the_new_reminder(world):
    run = start_final_run(world)
    base = world.store.get_scheduled_message(SCHEDULE_ID)
    later = datetime.now(timezone.utc) + timedelta(days=1)
    world.store.update(base.model_copy(update={"next_fire_at": later.isoformat()}), base=base)
    assert world.tasks.update_main_task_state(run.task.task_id, ProgressState.COMPLETED, later.isoformat())
    assert world.store.prune_expired() == 0
    assert world.store.get_row(SCHEDULE_ID)["remaining_count"] == 1
    assert world.store.get_scheduled_message(SCHEDULE_ID).next_fire_at == later.isoformat()


def test_the_team_leads_own_system_timers_get_no_run_receipt(world):
    """They sign their runs and are never checked against the row, so nothing needs to keep it.

    A receipt would also count an Objective work run as started before its
    work session opened, so a failed opening would skip the run.
    """

    base = save_schedule(world)
    objective_work = base.model_copy(update={
        "management_class": ScheduleManagementClass.SYSTEM,
        "system_schedule_kind": SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        "claim_named_agent_type": "TeamArchitect",
    })
    worker_job = base.model_copy(update={"claim_named_agent_type": "WebResearchAgent"})
    assert prepare_occurrence(world.worker, objective_work, "claim") is None
    assert prepare_occurrence(world.worker, worker_job, "claim").schedule_id == SCHEDULE_ID


def test_an_objective_work_run_whose_session_cannot_open_stays_due(world, monkeypatch):
    """No receipt is kept for it, so the scheduler retries instead of counting the run as started."""

    async def active(*_args):
        return False

    monkeypatch.setattr(scheduled_runtime, "_objective_work_schedule_is_inactive", active)
    record_ann(world, PersonStanding.ACTIVE)
    base = save_schedule(world)
    world.store.remove(SCHEDULE_ID)
    # An Objective work schedule with no Objective id cannot open its session.
    world.store.add(base.model_copy(update={
        "management_class": ScheduleManagementClass.SYSTEM,
        "system_schedule_kind": SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        "repeat_count": 1,
    }))
    renew_schedule_permits(world.manager, signer=world.signer)
    due = world.store.get_row(SCHEDULE_ID)["next_fire_at"]

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    assert SCHEDULE_ID in asyncio.run(scenario())  # retried after the backoff
    row = world.store.get_row(SCHEDULE_ID)
    assert (row["next_fire_at"], row["remaining_count"], row["claimed_by_agent_id"]) == (due, 1, None)
    assert world.store.worker_records.occurrences_for_schedule(SCHEDULE_ID, TEAM) == ()
    assert world.tasks.list_task_infos() == []


def _age_receipts(world, *, days: int) -> None:
    """Date the schedule's run receipts ``days`` back, as if their run started then."""

    records = world.store.worker_records
    with records._connect() as conn:
        for record in records.occurrences_for_schedule(SCHEDULE_ID, TEAM):
            aged = record.model_copy(update={"admitted_at_utc": record.admitted_at_utc - timedelta(days=days)})
            conn.execute(
                "UPDATE schedule_occurrences SET payload_json=? WHERE occurrence_id=?",
                (aged.model_dump_json(), record.occurrence_id),
            )
        conn.commit()


def test_a_receipt_older_than_a_runs_identity_no_longer_keeps_the_schedule(world, caplog):
    """A run's identity lasts 7 days, so a receipt nothing closed cannot keep a schedule and its access forever."""

    run = start_final_run(world)
    repository = AccessRepository(world.tasks.storage_settings)
    grant = repository.create(GrantV1(
        grant_id="grant_final_run", team_id=TEAM,
        subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=f"{TEAM}/{SCHEDULE_ID}"),
        target=TargetRefV1(kind=TargetKind.TOOL, id="fn:web_research"), level=AccessLevel.READ,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ANN),
        created_at_utc=datetime.now(timezone.utc),
    ))
    _age_receipts(world, days=6)
    assert world.store.prune_expired() == 0
    _age_receipts(world, days=2)
    caplog.set_level("WARNING")
    assert world.store.prune_expired() == 1
    assert world.store.get_row(SCHEDULE_ID) is None
    assert repository.get(grant.grant_id).status == GrantStatus.REVOKED
    assert f"event=schedule_receipt_expired schedule_id={SCHEDULE_ID} task_id={run.task.task_id}" in caplog.text


def test_an_unreadable_receipt_does_not_keep_the_schedule(world, caplog):
    start_final_run(world)
    with world.store.worker_records._connect() as conn:
        conn.execute("UPDATE schedule_occurrences SET payload_json='{\"broken\": true}'")
        conn.commit()
    caplog.set_level("WARNING")
    assert world.store.prune_expired() == 1
    assert f"event=schedule_receipt_unreadable schedule_id={SCHEDULE_ID}" in caplog.text


def test_one_schedule_that_cannot_be_checked_does_not_stop_the_cleanup_of_others(world, monkeypatch, caplog):
    repository = world.store._repository
    for schedule_id in ("sched_a", "sched_b"):
        save_schedule(world, schedule_id=schedule_id)
        world.store.set_enabled(schedule_id, True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with repository._connect() as conn:
        conn.execute("UPDATE scheduled_messages SET expires_at_utc=?", (past,))
        conn.commit()
    locked = repository.write_locked

    def failing(schedule_id, decide, **options):
        if schedule_id == "sched_a":
            raise RuntimeError("database busy")
        return locked(schedule_id, decide, **options)

    monkeypatch.setattr(repository, "write_locked", failing)
    caplog.set_level("WARNING")
    assert world.store.prune_expired() == 1
    assert world.store.get_row("sched_a") is not None and world.store.get_row("sched_b") is None
    assert "event=schedule_prune_failed schedule_id=sched_a" in caplog.text


def test_a_later_end_date_set_during_the_last_run_lets_the_schedule_go_on(world):
    """Ending by its end date keeps the following run time, blocked by that date, not zero runs left."""

    end = datetime.now(timezone.utc) + timedelta(minutes=10)
    run = start_final_run(world, interval=86_400_000, count=None, expires=end.isoformat())
    row = world.store.get_row(SCHEDULE_ID)
    assert row["remaining_count"] is None and row["next_fire_at"] > end.isoformat()
    # The end date passes while the run still works: nothing is written.
    assert world.store.prune_expired(now=end + timedelta(seconds=1)) == 0
    base = world.store.get_scheduled_message(SCHEDULE_ID)
    later = (end + timedelta(days=30)).isoformat()
    world.store.update(base.model_copy(update={"expires_at_utc": later}), base=base)
    assert world.tasks.update_main_task_state(run.task.task_id, ProgressState.COMPLETED, end.isoformat())
    assert world.store.prune_expired(now=end + timedelta(seconds=1)) == 0
    row = world.store.get_row(SCHEDULE_ID)
    assert (row["remaining_count"], row["expires_at_utc"], row["next_fire_at"]) == (None, later, base.next_fire_at)


@pytest.mark.parametrize("repeat_count,remaining", [(1, 1), (None, None)])
def test_making_a_running_reminder_repeat_gives_it_its_runs_back(world, repeat_count, remaining):
    run = start_final_run(world)
    assert world.store.get_row(SCHEDULE_ID)["remaining_count"] == 0
    base = world.store.get_scheduled_message(SCHEDULE_ID)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    world.store.update(base.model_copy(update={
        "interval_ms": 86_400_000, "next_fire_at": tomorrow, "repeat_count": repeat_count,
    }), base=base)
    assert world.tasks.update_main_task_state(run.task.task_id, ProgressState.COMPLETED, tomorrow)
    assert world.store.prune_expired() == 0
    row = world.store.get_row(SCHEDULE_ID)
    assert (row["remaining_count"], row["next_fire_at"]) == (remaining, tomorrow)


def test_an_edit_that_adds_no_run_still_ends_the_schedule_after_its_run(world):
    run = start_final_run(world)
    base = world.store.get_scheduled_message(SCHEDULE_ID)
    world.store.update(base.model_copy(update={"schedule_name": "Renamed"}), base=base)
    assert world.store.get_row(SCHEDULE_ID)["remaining_count"] == 0
    assert world.tasks.update_main_task_state(run.task.task_id, ProgressState.COMPLETED, base.next_fire_at)
    assert world.store.prune_expired() == 1


def test_finishing_and_removal_are_logged_at_their_levels(world, caplog):
    caplog.set_level("INFO")
    run = start_final_run(world, interval=None, count=None)  # one-time with an unlimited count
    assert "event=schedule_final_run_pending" in caplog.text
    assert any(
        record.levelname == "WARNING" and "event=schedule_one_time_count_ignored" in record.getMessage()
        for record in caplog.records
    )
    caplog.clear()
    assert world.tasks.update_main_task_state(run.task.task_id, ProgressState.COMPLETED, datetime.now(timezone.utc).isoformat())
    assert world.store.prune_expired() == 1
    removal = [record for record in caplog.records if "event=schedule_finished_removed" in record.getMessage()]
    assert [record.levelname for record in removal] == ["INFO"]

