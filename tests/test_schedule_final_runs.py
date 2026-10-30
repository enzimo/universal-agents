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
from custom_messages.enums import ProgressState, ScheduleManagementClass
from storage.access_repository import AccessRepository
from tests.broker_access_support import forget_task_context_keys
from tests.scheduled_run_support import (
    ANN, SCHEDULE_ID, TEAM, access_agent, build_world, claim, fire, needs_cedar,
    queued_run, record_ann, save_schedule, subscribe,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


def start_final_run(world, *, interval=None, count=1, expires=None, followup=None,
                    management=ScheduleManagementClass.USER):
    record_ann(world, PersonStanding.ACTIVE)
    base = save_schedule(world)
    changed = base.model_copy(deep=True, update={
        "interval_ms": interval, "repeat_count": count, "expires_at_utc": expires, "management_class": management,
    })
    changed.payload_header.response_routing_key = "gateway.responses"
    changed.payload_meta.followup_to_task_id = followup
    world.store.update(changed, base=base)
    renew_schedule_permits(world.manager, signer=world.signer)

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

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
@pytest.mark.parametrize("management", [ScheduleManagementClass.USER, ScheduleManagementClass.SYSTEM])
def test_final_run_keeps_identity_and_cannot_fire_again_after_restart(world, interval, count, management):
    run = start_final_run(world, interval=interval, count=count, management=management)
    restarted = ScheduleStore(storage_settings=world.tasks.storage_settings)
    assert restarted.prune_expired() == 0
    row = restarted.get_row(SCHEDULE_ID)
    assert row["remaining_count"] == 0 and row["enabled"]
    assert resolve(world, run).person_id == ANN
    assert job_schedule_id(
        resolve(world, run), ScheduleRunLookup.from_storage(world.tasks.storage_settings),
        lead_agent_type="TeamArchitect",
    ) == (SCHEDULE_ID if management == ScheduleManagementClass.USER else None)
    assert restarted.get_next_due() is None
    assert restarted.claim_next_due(
        claimer_agent_id="replacement_worker", claimer_named_agent_type="WebResearchAgent", claimer_team_id=TEAM,
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
