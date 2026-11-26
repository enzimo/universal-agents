"""One scheduler-signed protocol 1 permit per schedule; agents start runs without asking ``svc.scheduler``.

Real SQLite (schedule rows, the team's people, the task store), real Ed25519 protocol 1
host certificates, the real fire path (``scheduled_runtime._fire_claimed_schedule``) and
real Cedar where access is decided. Only ``team_services.scheduler.SchedulerOperations``
holds the ``scheduler`` certificate; neither the lead nor any worker holds any signing key.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_control.authority_verifier import IdentityRejected
from access_control.caller_tokens import BrokerCaller, agent_caller_label
from access_control.task_identity import ScheduleRunLookup, job_schedule_id, resolve_task_identity
from custom_agents import access_runtime
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_permits import sign_system_run
from custom_messages.contracts.access_v2 import PersonStanding, ScheduleRunIdentityV1, TaskContextV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_messages.wire.authority_bindings import (
    AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS,
    AuthorityTokenRecord,
    SCHEDULER_ISSUER_ROLE,
)
from custom_messages.wire.values import parse_timestamp
from custom_tools.secure_cli_tool import trusted_broker_context
from custom_transport.protocol1.authority_tokens import root_task_token, schedule_permit_token
from id_generation import new_id
from secure_tool_broker.access_gate import build_broker_access_gate
from team_services.scheduler import PermitPassResult
from tests.broker_access_support import (
    broker_gateway_identity,
    broker_person_ref,
    broker_scheduler_identity,
    forget_task_context_keys,
)
from tests.scheduled_run_support import (
    ANN,
    CEDAR,
    SCHEDULE_ID,
    TEAM,
    WORKER_TYPE,
    access_agent,
    build_world,
    claim,
    fire,
    issue_permits,
    needs_cedar,
    queued,
    queued_run,
    record_ann,
    record_people,
    run_context,
    save_schedule,
    subscribe,
)
from tests.system_schedule_kind_support import (
    CURATOR_AGENT_TYPE,
    NO_PERSON_KIND,
    PERSON_KIND,
    STAND_IN_KIND,
    TEAM_NAME_PREFIX,
    use_kind,
)
from tests.task_work_support import set_agent_ref

BOB = "usr_bob"
CAROL = "usr_carol"
DAN = "usr_dan"
MAINTENANCE_ID = "sched_internal_improvement"
ACTIVE = PersonStanding.ACTIVE


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    yield build_world(tmp_path, monkeypatch)
    forget_task_context_keys()


def _issue(world, now: datetime | None = None) -> PermitPassResult:
    """One scheduler permit upkeep pass."""

    return issue_permits(world, now=now)


def _stored(world, schedule_id: str = SCHEDULE_ID) -> AuthorityTokenRecord | None:
    return world.store.run_permit(schedule_id)


def _verified(world, permit: AuthorityTokenRecord):
    return world.verifier.verify_schedule_permit(permit, team_id=TEAM)


def _save_maintenance(world, owner: str = ANN) -> None:
    """The runtime's own maintenance schedule for InternalImprovementAgent, owned by the first user."""

    world.store.add(
        ScheduledMessage(
            schedule_id=MAINTENANCE_ID,
            schedule_name=f"internal_improvement:maintenance:{TEAM}",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
            payload=Task(task_id="task_maintenance", task_type=MessageMetaType.REQUEST, problem_statement="Review."),
            payload_header=MessageHeader(from_agent_id="manager_1", to_routing_path="manager_1"),
            payload_meta=MessageMeta(
                to_named_agent_type="InternalImprovementAgent",
                gateway_context=GatewayContext(canonical_user_id=owner),
            ),
            interval_ms=86_400_000,
            next_fire_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),  # not due
        ),
        claim_team_id=TEAM,
        claim_named_agent_type="TeamArchitect",
    )


def _start_with_permit(world) -> object:
    """Issue Ann's permit, then let the worker claim and fire her schedule; the queued run."""

    record_ann(world, ACTIVE, ("member",))
    save_schedule(world)
    _issue(world)

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    assert asyncio.run(scenario()) == {}, "the run was accepted"
    return queued_run(world)


# -- team_services.scheduler issues and renews -----------------------------------------


def test_the_scheduler_issues_permits_only_for_vouched_active_owners_and_system_work(world, caplog) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (BOB, ACTIVE, ("member",)), (CAROL, PersonStanding.DISABLED, ()))
    save_schedule(world)  # Ann's, saved with a gateway-signed protocol 1 token
    save_schedule(world, schedule_id="sched_bob", owner=BOB, vouched=False)
    save_schedule(world, schedule_id="sched_carol", owner=CAROL)
    _save_maintenance(world)

    with caplog.at_level(logging.INFO, logger="test.scheduler"):
        result = _issue(world)

    assert sorted(result.issued) == sorted([SCHEDULE_ID, MAINTENANCE_ID])
    assert result.refused == {"sched_bob": "owner_not_vouched", "sched_carol": "owner_not_active"}
    anns = _verified(world, _stored(world))
    assert (anns.issuer_kind, anns.person_id, anns.schedule_id) == (SCHEDULER_ISSUER_ROLE, ANN, SCHEDULE_ID)
    assert anns.expires_at - anns.issued_at == timedelta(seconds=AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS)
    maintenance = _verified(world, _stored(world, MAINTENANCE_ID))
    assert maintenance.person_id is None, "maintenance runs are system work for no person"
    # Maintenance schedule with an active admin owner also gets an owner_permit_json for escalations.
    owner_perm = world.store.owner_permit(MAINTENANCE_ID)
    assert owner_perm is not None
    verified_owner = _verified(world, owner_perm)
    assert (verified_owner.person_id, verified_owner.schedule_id) == (ANN, MAINTENANCE_ID)
    assert _stored(world, "sched_bob") is None and _stored(world, "sched_carol") is None
    assert "event=schedule_permit_issued" in caplog.text and "event=schedule_permit_refused" in caplog.text
    # The permit is bookkeeping on the row, never part of the schedule itself.
    assert "schedule_permit" not in world.store._repository.get_schedule_row(SCHEDULE_ID)[0]
    # Nothing to do on the next pass: refusals wait before they are checked again.
    assert _issue(world) == PermitPassResult()


def test_permits_are_renewed_within_two_days_of_expiry_and_reissued_when_the_owner_changes(world) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (DAN, ACTIVE, ("member",)))
    save_schedule(world)
    assert _issue(world, now=datetime.now(timezone.utc) - timedelta(days=6)).issued == [SCHEDULE_ID]
    first = _stored(world)
    assert first is not None

    # A save (a run recording schedule memory saves the schedule) keeps the permit.
    world.store.add(world.store.get_scheduled_message(SCHEDULE_ID))
    assert _stored(world) == first

    # One day before the 7-day permit expires (< 2 days remaining), the next pass renews it.
    assert _issue(world).renewed == [SCHEDULE_ID]
    second = _stored(world)
    assert second is not None
    assert second.token_id != first.token_id
    assert parse_timestamp(second.expires_at) > datetime.now(timezone.utc) + timedelta(days=6)
    assert _issue(world) == PermitPassResult()

    # The schedule is saved again from Dan's request: the next pass issues his permit.
    save_schedule(world, owner=DAN)
    assert _verified(world, _stored(world)).person_id == ANN, "until then the old permit stays, and checks refuse it"
    assert _issue(world).renewed == [SCHEDULE_ID]
    assert _verified(world, _stored(world)).person_id == DAN


def test_only_a_scheduler_certificate_issues_permits(world) -> None:
    record_ann(world, ACTIVE)
    save_schedule(world)
    # Without a scheduler identity, no permits are issued.
    world.scheduler_state["identity"] = None
    assert _issue(world) == PermitPassResult()
    assert _stored(world) is None
    # A gateway certificate lacks the scheduler role and issues no permits.
    world.scheduler_state["identity"] = broker_gateway_identity(TEAM)
    assert _issue(world) == PermitPassResult()
    assert _stored(world) is None
    # Restoring the team_services+scheduler certificate issues the permit.
    world.scheduler_state["identity"] = broker_scheduler_identity(TEAM)
    assert _issue(world).issued == [SCHEDULE_ID]
    assert _stored(world) is not None


# -- a worker starts runs with the standing permit -------------------------------------


@needs_cedar
def test_a_worker_starts_a_run_with_the_permit_and_asks_nobody(world, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_permits"):
        run = _start_with_permit(world)

    assert world.host.requests == [], "svc.scheduler was not asked"
    gateway = run.meta.gateway_context
    assert gateway.task_context is None and gateway.schedule_run is not None
    assert gateway.schedule_run.permit == _stored(world)
    assert "event=scheduled_run_permit_attached" in caplog.text

    agent = access_agent(world, run)
    assert access_runtime.decide_call(agent, "exa_search", {}).allowed
    context = access_runtime.verified_task_context(agent)
    assert (context.issuer, context.person_id, context.person_roles, context.schedule_id, context.context_id) == (
        SCHEDULER_ISSUER_ROLE,
        ANN,
        ("member",),
        SCHEDULE_ID,
        gateway.schedule_run.context_id,
    )

    # The broker's gate accepts the same run, from the context the agent sends it.
    set_agent_ref(agent)
    try:
        broker_context = trusted_broker_context()
    finally:
        set_agent_ref(None)
    assert "schedule_run" in broker_context and "task_context" not in broker_context
    gate = build_broker_access_gate(
        instance_root=None,
        team_id=TEAM,
        authorization_enabled=True,
        storage_settings=world.tasks.storage_settings,
        cedar_binary=str(CEDAR),
        verifier=world.verifier,
    )
    caller = BrokerCaller(agent_caller_label(WORKER_TYPE))
    try:
        assert gate.check(caller, gate.catalog.resolve_function("exa_search", {}), broker_context).allowed
        # Ann is disabled: the gate refuses the run's next request.
        record_ann(world, PersonStanding.DISABLED, ())
        gate.schedule_runs.clock = lambda: 10.0**9
        refused = gate.check(caller, gate.catalog.resolve_function("exa_search", {}), broker_context)
        assert not refused.allowed and "not active" in refused.message
    finally:
        gate.access.cedar.close()


@needs_cedar
def test_each_check_uses_the_owners_current_roles_and_the_schedule_as_it_is_now(world) -> None:
    agent = access_agent(world, _start_with_permit(world))
    clock = [1_000.0]
    access_runtime.runtime_access(agent).schedule_runs.clock = lambda: clock[0]

    def roles() -> tuple[str, ...]:
        return access_runtime.verified_task_context(agent).person_roles

    def later() -> None:
        clock[0] += 31

    assert roles() == ("member",)
    record_ann(world, ACTIVE, ("admin", "member"))
    assert roles() == ("member",), "read at most every 30 seconds"
    later()
    assert roles() == ("admin", "member")

    record_ann(world, PersonStanding.DISABLED, ())
    later()
    refused = access_runtime.decide_call(agent, "exa_search", {})
    assert not refused.allowed and "not active" in refused.reason

    record_ann(world, ACTIVE, ("member",))
    later()
    assert access_runtime.decide_call(agent, "exa_search", {}).allowed

    world.store.set_enabled(SCHEDULE_ID, False)
    later()
    refused = access_runtime.decide_call(agent, "exa_search", {})
    assert not refused.allowed and "turned off" in refused.reason

    world.store.set_enabled(SCHEDULE_ID, True)
    world.store.remove(SCHEDULE_ID)
    later()
    refused = access_runtime.decide_call(agent, "exa_search", {})
    assert not refused.allowed and "no longer exists" in refused.reason


def test_heartbeat_items_skip_launcher_permit_and_get_per_item_tokens(world) -> None:
    """C24: Even when the launcher holds a standing permit, each heartbeat item gets a per-item token from svc.scheduler."""
    record_ann(world, ACTIVE, ("member",))
    heartbeats = HeartbeatStore(db_path=world.db_path, storage_settings=world.tasks.storage_settings)
    heartbeats.upsert_source(source_id="hb_ann", title="Ann's checks")
    items = [
        heartbeats.add_item(source_id="hb_ann", title=title, instruction=title, conversation_id=f"conv_{index}")
        for index, title in enumerate(("Check launches", "Check weather"))
    ]
    save_schedule(world, heartbeat_source_id="hb_ann")
    _issue(world)

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    assert asyncio.run(scenario()) == {}
    assert len(world.host.requests) == 2
    runs = {info.meta.gateway_context.scheduled_job_id: info for info in queued(world)}
    assert set(runs) == {item.heartbeat_item_id for item in items}
    for item in items:
        info = runs[item.heartbeat_item_id]
        assert info.meta.gateway_context.schedule_run is None
        assert info.meta.gateway_context.task_context is not None
        verified = run_context(info, world)
        assert verified.schedule_id == item.heartbeat_item_id
        assert verified.person_id == ANN
        assert info.meta.conversation_id == item.conversation_id
    assert len({run_context(info, world).context_id for info in runs.values()}) == 2, "each run has its own context id"


def test_a_maintenance_run_carries_the_permit_for_no_person(world) -> None:
    record_ann(world, ACTIVE, ("admin", "owner"))
    _save_maintenance(world)
    _issue(world)
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, session_key="ann-chat"))

    assert sign_system_run(world.manager, meta, schedule_id=MAINTENANCE_ID) is True

    gateway = meta.gateway_context
    assert (gateway.canonical_user_id, gateway.session_key, gateway.task_context) == (None, None, None)
    context = _resolve(world, schedule_run=gateway.schedule_run)
    assert (context.person_id, context.person_roles, context.schedule_id) == (None, (), MAINTENANCE_ID)


def test_a_new_no_person_kind_gets_a_permit_and_accepts_an_explicitly_system_signed_run(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_kind(monkeypatch, NO_PERSON_KIND)
    record_ann(world, ACTIVE, ("admin", "owner"))
    schedule_id = "sched_knowledge_team"
    world.store.add(
        ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=f"{TEAM_NAME_PREFIX}{TEAM}",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=STAND_IN_KIND,
            payload=Task(task_id="task_knowledge", task_type=MessageMetaType.REQUEST, problem_statement="Curate."),
            payload_header=MessageHeader(from_agent_id="manager_1", to_routing_path="manager_1"),
            payload_meta=MessageMeta(
                to_named_agent_type=CURATOR_AGENT_TYPE,
                gateway_context=GatewayContext(canonical_user_id=ANN),
            ),
            interval_ms=86_400_000,
            next_fire_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),  # not due
        ),
        claim_team_id=TEAM,
        claim_named_agent_type=CURATOR_AGENT_TYPE,
    )

    assert schedule_id in _issue(world).issued
    assert _verified(world, _stored(world, schedule_id)).person_id is None
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, session_key="ann-chat"))
    assert sign_system_run(world.manager, meta, schedule_id=schedule_id) is True
    context = _resolve(world, schedule_run=meta.gateway_context.schedule_run)
    assert (context.person_id, context.person_roles, context.schedule_id) == (None, (), schedule_id)

    # As a per-person kind its runs are not system work: the same permit for no person is refused.
    use_kind(monkeypatch, PERSON_KIND)
    with pytest.raises(IdentityRejected, match="valid only for the runtime's own maintenance schedule"):
        _resolve(world, schedule_run=meta.gateway_context.schedule_run)


def test_only_a_persons_schedule_a_worker_runs_counts_as_a_job_run(world) -> None:
    save_schedule(world)
    world.store.add(
        ScheduledMessage(
            schedule_id="sched_lead_digest",
            schedule_name="Morning digest",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_id="task_digest", task_type=MessageMetaType.REQUEST, problem_statement="Digest."),
            payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="manager_1"),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN)),
            interval_ms=86_400_000,
            next_fire_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        ),
        claim_team_id=TEAM,
        claim_named_agent_type="TeamArchitect",
    )
    _save_maintenance(world)
    lookup = ScheduleRunLookup.from_storage(world.tasks.storage_settings)
    now = datetime.now(timezone.utc)

    def job(schedule_id: str) -> str | None:
        context = TaskContextV1(
            context_id=new_id("ctx"), issuer=SCHEDULER_ISSUER_ROLE, issuer_roles=(SCHEDULER_ISSUER_ROLE,),
            team_id=TEAM, person_id=ANN,
            schedule_id=schedule_id, issued_at_utc=now, expires_at_utc=now + timedelta(hours=1),
        )
        return job_schedule_id(context, lookup, lead_agent_type="TeamArchitect")

    assert job(SCHEDULE_ID) == SCHEDULE_ID, "the research worker's daily check"
    assert job("sched_lead_digest") is None, "the team lead's schedule"
    assert job(MAINTENANCE_ID) is None, "a runtime-managed schedule"
    assert job("heartbeat_item_1") is None, "a heartbeat item has no schedule row"


# -- refused identities -----------------------------------------------------------------


def _resolve(world, *, schedule_run=None, task_context=None, team_id: str = TEAM, lookup: object = "default"):
    return resolve_task_identity(
        verifier=world.verifier,
        team_id=team_id,
        task_context=task_context,
        schedule_run=schedule_run,
        schedule_runs=ScheduleRunLookup.from_storage(world.tasks.storage_settings) if lookup == "default" else lookup,
    )


def _run(permit: AuthorityTokenRecord, *, started: datetime | None = None) -> ScheduleRunIdentityV1:
    return ScheduleRunIdentityV1(
        permit=permit, context_id=new_id("schedule_run"), started_at_utc=started or datetime.now(timezone.utc)
    )


def _permit(
    world,
    *,
    person_id: str | None = ANN,
    team_id: str = TEAM,
    now: datetime | None = None,
    expires_at: datetime | None = None,
) -> AuthorityTokenRecord:
    return AuthorityTokenRecord.model_validate(
        schedule_permit_token(
            broker_scheduler_identity(team_id),
            team=team_id,
            schedule_id=SCHEDULE_ID,
            person_ref=broker_person_ref(person_id) if person_id is not None else None,
            issued=now or datetime.now(timezone.utc),
            expires_at=expires_at,
        )
    )


def _refused(world, fragment: str, **kwargs) -> None:
    with pytest.raises((IdentityRejected, ValueError)) as caught:
        _resolve(world, **kwargs)
    assert fragment in str(caught.value), str(caught.value)


def test_forged_expired_foreign_and_mismatched_permits_are_refused(world) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (BOB, ACTIVE, ("member",)))
    save_schedule(world)
    assert _resolve(world, schedule_run=_run(_permit(world))).person_id == ANN, "the honest permit passes"

    genuine = _permit(world)
    forged = genuine.model_copy(update={"person": broker_person_ref(BOB)})
    _refused(world, "signature", schedule_run=_run(forged))
    expired = _permit(
        world,
        now=datetime.now(timezone.utc) - timedelta(minutes=30),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    _refused(world, "not valid", schedule_run=_run(expired))
    _refused(world, "team", schedule_run=_run(_permit(world, team_id="team_2")))
    _refused(world, "someone else", schedule_run=_run(_permit(world, person_id=BOB)))
    _refused(world, "maintenance schedule", schedule_run=_run(_permit(world, person_id=None)))
    _refused(world, "future", schedule_run=_run(genuine, started=datetime.now(timezone.utc) + timedelta(hours=1)))
    _refused(world, "cannot check scheduled runs", schedule_run=_run(genuine), lookup=None)
    gateway_signed = AuthorityTokenRecord.model_validate(
        root_task_token(
            broker_gateway_identity(TEAM),
            team=TEAM,
            root_task_id="task_1",
            request_id="task_context_1",
            person_ref=broker_person_ref(ANN),
            issued=datetime.now(timezone.utc),
        )
    )
    _refused(world, "both", schedule_run=_run(genuine), task_context=gateway_signed)
    with pytest.raises(ValueError):
        ScheduleRunIdentityV1(permit=genuine, context_id="task_context_1", started_at_utc=datetime.now(timezone.utc))


def test_a_permit_never_passes_as_a_task_context_or_the_other_way_round(world) -> None:
    record_ann(world, ACTIVE, ("member",))
    save_schedule(world)
    permit = _permit(world)
    run_token = AuthorityTokenRecord.model_validate(
        root_task_token(
            broker_scheduler_identity(TEAM),
            team=TEAM,
            root_task_id="run_1",
            request_id="schedule_run_1",
            person_ref=broker_person_ref(ANN),
            schedule_id=SCHEDULE_ID,
            issued=datetime.now(timezone.utc),
        )
    )

    # A bare permit token is refused when presented as task_context.
    with pytest.raises(IdentityRejected):
        _resolve(world, task_context=permit)
    # A normal root-task token is refused when presented as a schedule permit inside ScheduleRunIdentityV1.
    with pytest.raises(IdentityRejected):
        _resolve(world, schedule_run=_run(run_token))
    # Only a scheduler certificate can sign a schedule permit token.
    with pytest.raises(ValueError):
        schedule_permit_token(
            broker_gateway_identity(TEAM),
            team=TEAM,
            schedule_id=SCHEDULE_ID,
            person_ref=broker_person_ref(ANN),
            issued=datetime.now(timezone.utc),
        )


# -- without a usable permit, the worker asks svc.scheduler ----------------------------


def test_without_a_permit_the_worker_asks_svc_scheduler(world) -> None:
    record_ann(world, ACTIVE, ("member",))
    save_schedule(world)  # permit upkeep has not run yet

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    assert asyncio.run(scenario()) == {}
    assert len(world.host.requests) == 1
    run = queued_run(world)
    assert run.meta.gateway_context.schedule_run is None
    assert run_context(run, world).person_id == ANN


def test_a_permit_for_the_previous_owner_is_not_used(world, caplog) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (DAN, ACTIVE, ("member",)))
    save_schedule(world)
    _issue(world)
    save_schedule(world, owner=DAN)  # permit upkeep has not re-checked it yet

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    with caplog.at_level(logging.INFO):
        assert asyncio.run(scenario()) == {}

    assert "event=schedule_permit_unusable" in caplog.text
    assert len(world.host.requests) == 1
    context = run_context(queued_run(world), world)
    assert (context.person_id, context.person_roles) == (DAN, ("member",))


@needs_cedar
def test_a_permit_run_records_its_notes_with_notify_user_as_with_record_scheduled_run(world) -> None:
    from custom_tools.providers.communication import _record_schedule_notification_if_active
    from custom_tools.providers.schedules import record_scheduled_run

    run = _start_with_permit(world)
    agent = access_agent(world, run)
    agent._schedule_store = world.store
    run.meta.gateway_context.scheduled_job_id = SCHEDULE_ID
    set_agent_ref(agent)
    try:
        noted = _record_schedule_notification_if_active(agent, message="Launch moved.", notification_id="note_permit", reason="")
        recorded = json.loads(record_scheduled_run(outcome="no_change", state_summary="No launch today."))
    finally:
        set_agent_ref(None)

    assert noted == {"recorded": True, "schedule_id": SCHEDULE_ID}
    assert recorded["recorded"] is True
    memory = world.store.get_scheduled_message(SCHEDULE_ID).schedule_memory
    assert (memory.ledger.total_notifications, memory.ledger.total_runs) == (1, 1)
