"""One manager-signed permit per schedule; a worker without the key starts runs without asking the manager.

Real SQLite (schedule rows, the team's people, the task store), real Ed25519
keys, the real fire path (``scheduled_runtime._fire_claimed_schedule``) and
real Cedar where access is decided. The environment is a worker process's: it
trusts the scheduler key but holds none; the manager's pass is handed the
manager's signer (``tests/scheduled_run_support.py``).

Covers:
- the manager's pass issues a permit for a vouched person's schedule and for
  the maintenance schedule (system work, no person), none for an unvouched or
  disabled owner; it renews a permit in its last 7 days and issues a new one
  when the owner changes; saving a schedule keeps its permit;
- a worker without the key starts a run with the permit and sends the
  manager nothing; the access hook and the broker's gate both accept the run;
- each check reads the owner's current roles and standing and the schedule
  as it is now (after the 30-second cache);
- refused: a forged, expired, foreign-key or other-team permit, a permit for
  someone other than the row's owner, a permit presented as a task context
  and a task context presented as a permit (in both signature domains);
- without a usable permit the worker asks the manager as before;
- a new kind the system schedule table marks as work for no person (a
  stand-in, ``tests/system_schedule_kind_support.py``) gets a permit for no
  person and an explicitly system-signed run is accepted. This checks permit
  issuance and identity verification, not automatic fired-run admission.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from access_control import identity
from access_control.caller_tokens import BrokerCaller, agent_caller_label
from access_control.identity import (
    ISSUER_GATEWAY,
    ISSUER_SCHEDULER,
    ContextSigner,
    ContextVerifier,
    IdentityRejected,
    generate_signing_key,
)
from access_control.task_identity import ScheduleRunLookup, resolve_task_identity
from custom_agents import access_runtime
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_permits import (
    PermitPassResult,
    issues_permits,
    renew_schedule_permits,
    sign_system_run,
)
from custom_messages.contracts.access_v2 import (
    PersonStanding,
    ScheduleRunIdentityV1,
    SignedSchedulePermitV1,
    SignedTaskContextV1,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.scheduled_run_context_v1 import ScheduledRunContextRequestV1
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_tools.runtime_context import set_agent_ref
from custom_tools.secure_cli_tool import trusted_broker_context
from id_generation import new_id
from secure_tool_broker.access_gate import build_broker_access_gate
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key
from tests.system_schedule_kind_support import (
    CURATOR_AGENT_TYPE,
    NO_PERSON_KIND,
    PERSON_KIND,
    STAND_IN_KIND,
    TEAM_NAME_PREFIX,
    use_kind,
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
    needs_cedar,
    queued,
    queued_run,
    record_ann,
    record_people,
    run_context,
    save_schedule,
    subscribe,
)

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
    """The manager's issuing pass, with the key the manager process holds."""

    return renew_schedule_permits(world.manager, signer=world.signer, now=now)


def _stored(world, schedule_id: str = SCHEDULE_ID) -> SignedSchedulePermitV1 | None:
    return world.store.run_permit(schedule_id)


def _verified(permit: SignedSchedulePermitV1):
    return ContextVerifier.from_env().verify_permit(permit, team_id=TEAM)


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


# -- the manager issues and renews ----------------------------------------------------


def test_the_manager_issues_permits_only_for_vouched_active_owners_and_system_work(world, caplog) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (BOB, ACTIVE, ("member",)), (CAROL, PersonStanding.DISABLED, ()))
    save_schedule(world)  # Ann's, saved from her request (the gateway signed it for her)
    save_schedule(world, schedule_id="sched_bob", owner=BOB, vouched=False)
    save_schedule(world, schedule_id="sched_carol", owner=CAROL)
    _save_maintenance(world)

    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_permits"):
        result = _issue(world)

    assert sorted(result.issued) == sorted([SCHEDULE_ID, MAINTENANCE_ID])
    assert result.refused == {"sched_bob": "owner_not_vouched", "sched_carol": "owner_not_active"}
    anns = _verified(_stored(world))
    assert (anns.issuer, anns.person_id, anns.schedule_id) == (ISSUER_SCHEDULER, ANN, SCHEDULE_ID)
    assert anns.expires_at_utc - anns.issued_at_utc == timedelta(days=30)
    maintenance = _verified(_stored(world, MAINTENANCE_ID))
    assert maintenance.person_id is None, "maintenance runs are system work for no person"
    assert _stored(world, "sched_bob") is None and _stored(world, "sched_carol") is None
    assert "event=schedule_permit_issued" in caplog.text and "event=schedule_permit_refused" in caplog.text
    # The permit is bookkeeping on the row, never part of the schedule itself.
    assert "schedule_permit" not in world.store._repository.get_schedule_row(SCHEDULE_ID)[0]
    # Nothing to do on the next pass: refusals wait before they are checked again.
    assert _issue(world) == PermitPassResult()


def test_permits_are_renewed_in_their_last_week_and_reissued_when_the_owner_changes(world) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (DAN, ACTIVE, ("member",)))
    save_schedule(world)
    assert _issue(world, now=datetime.now(timezone.utc) - timedelta(days=24)).issued == [SCHEDULE_ID]
    first = _stored(world)

    # A save (a run recording schedule memory saves the schedule) keeps the permit.
    world.store.add(world.store.get_scheduled_message(SCHEDULE_ID))
    assert _stored(world) == first

    # Six days before it expires, the next pass renews it.
    assert _issue(world).renewed == [SCHEDULE_ID]
    second = _stored(world)
    assert second.permit.permit_id != first.permit.permit_id
    assert second.permit.expires_at_utc > datetime.now(timezone.utc) + timedelta(days=29)
    assert _issue(world) == PermitPassResult()

    # The schedule is saved again from Dan's request: the next pass issues his permit.
    save_schedule(world, owner=DAN)
    assert _stored(world).permit.person_id == ANN, "until then the old permit stays, and checks refuse it"
    assert _issue(world).renewed == [SCHEDULE_ID]
    assert _stored(world).permit.person_id == DAN


def test_only_the_team_lead_in_the_process_with_the_key_issues_permits(world, monkeypatch) -> None:
    record_ann(world, ACTIVE)
    save_schedule(world)
    assert not issues_permits(world.manager), "the key is not in this process"
    assert renew_schedule_permits(world.manager) == PermitPassResult(), "no key, no permits"
    assert _stored(world) is None
    use_scheduler_key(monkeypatch)
    assert issues_permits(world.manager)
    assert not issues_permits(world.worker), "a worker never issues permits, even with the key"


# -- a worker without the key starts runs with the permit --------------------------------


@needs_cedar
def test_a_worker_without_the_key_starts_a_run_with_the_permit_and_asks_nobody(world, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_permits"):
        run = _start_with_permit(world)

    assert world.bus.published == [], "the manager was not asked"
    gateway = run.meta.gateway_context
    assert gateway.task_context is None and gateway.schedule_run is not None
    assert gateway.schedule_run.permit == _stored(world)
    assert "event=scheduled_run_permit_attached" in caplog.text

    agent = access_agent(world, run)
    assert access_runtime.decide_call(agent, "exa_search", {}).allowed
    context = access_runtime.verified_task_context(agent)
    assert (context.issuer, context.person_id, context.person_roles, context.schedule_id, context.context_id) == (
        ISSUER_SCHEDULER,
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


def test_heartbeat_items_carry_the_launcher_schedules_permit(world) -> None:
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
    assert world.bus.published == []
    runs = {info.meta.gateway_context.scheduled_job_id: info.meta.gateway_context.schedule_run for info in queued(world)}
    assert set(runs) == {item.heartbeat_item_id for item in items}
    assert {run.permit.permit.schedule_id for run in runs.values()} == {SCHEDULE_ID}
    assert {run.conversation_id for run in runs.values()} == {item.conversation_id for item in items}
    assert len({run.context_id for run in runs.values()}) == 2, "each run has its own context id"


def test_a_maintenance_run_without_the_key_carries_the_permit_for_no_person(world) -> None:
    record_ann(world, ACTIVE, ("admin", "owner"))
    _save_maintenance(world)
    _issue(world)
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, session_key="ann-chat"))

    sign_system_run(world.manager, meta, schedule_id=MAINTENANCE_ID)

    gateway = meta.gateway_context
    assert (gateway.canonical_user_id, gateway.session_key, gateway.task_context) == (None, None, None)
    context = _resolve(world, schedule_run=gateway.schedule_run)
    assert (context.person_id, context.person_roles, context.schedule_id) == (None, (), MAINTENANCE_ID)



def test_a_new_no_person_kind_gets_a_permit_and_accepts_an_explicitly_system_signed_run(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row controls permit issuance and acceptance of a no-person identity.

    The future kind's fire path still has to call ``sign_system_run`` as this
    test does explicitly; ordinary admission signs for the schedule's owner.
    """

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
    assert _verified(_stored(world, schedule_id)).person_id is None
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, session_key="ann-chat"))
    sign_system_run(world.manager, meta, schedule_id=schedule_id)
    context = _resolve(world, schedule_run=meta.gateway_context.schedule_run)
    assert (context.person_id, context.person_roles, context.schedule_id) == (None, (), schedule_id)

    # As a per-person kind its runs are not system work: the same permit for no person is refused.
    use_kind(monkeypatch, PERSON_KIND)
    with pytest.raises(IdentityRejected, match="valid only for the runtime's own maintenance schedule"):
        _resolve(world, schedule_run=meta.gateway_context.schedule_run)


def test_only_a_persons_schedule_a_worker_runs_counts_as_a_job_run(world) -> None:
    """Agents in the team lead's schedules and the runtime's timers work like a
    person's request; only a worker's job is a run of its schedule
    (``task_identity.job_schedule_id``), read from the stored schedule rows."""

    from access_control.identity import ISSUER_SCHEDULER
    from access_control.task_identity import job_schedule_id
    from custom_messages.contracts.access_v2 import TaskContextV1

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
            context_id=new_id("ctx"), issuer=ISSUER_SCHEDULER, team_id=TEAM, person_id=ANN,
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
        verifier=ContextVerifier.from_env(),
        team_id=team_id,
        task_context=task_context,
        schedule_run=schedule_run,
        schedule_runs=ScheduleRunLookup.from_storage(world.tasks.storage_settings) if lookup == "default" else lookup,
    )


def _run(permit: SignedSchedulePermitV1, *, started: datetime | None = None) -> ScheduleRunIdentityV1:
    return ScheduleRunIdentityV1(
        permit=permit, context_id=new_id("schedule_run"), started_at_utc=started or datetime.now(timezone.utc)
    )


def _permit(world, *, person_id: str | None = ANN, team_id: str = TEAM, now: datetime | None = None):
    return world.signer.issue_permit(team_id=team_id, schedule_id=SCHEDULE_ID, person_id=person_id, now=now)


def _refused(world, fragment: str, **kwargs) -> None:
    with pytest.raises((IdentityRejected, ValueError)) as caught:
        _resolve(world, **kwargs)
    assert fragment in str(caught.value), str(caught.value)


def test_forged_expired_foreign_and_mismatched_permits_are_refused(world) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (BOB, ACTIVE, ("member",)))
    save_schedule(world)
    assert _resolve(world, schedule_run=_run(_permit(world))).person_id == ANN, "the honest permit passes"

    genuine = _permit(world)
    forged = genuine.model_copy(update={"permit": genuine.permit.model_copy(update={"person_id": BOB})})
    _refused(world, "does not match", schedule_run=_run(forged))
    expired = _permit(world, now=datetime.now(timezone.utc) - timedelta(days=31))
    _refused(world, "expired", schedule_run=_run(expired))
    _refused(world, "another team", schedule_run=_run(_permit(world, team_id="team_2")))
    _refused(world, "someone else", schedule_run=_run(_permit(world, person_id=BOB)))
    _refused(world, "maintenance schedule", schedule_run=_run(_permit(world, person_id=None)))
    stranger = ContextSigner.from_value(ISSUER_SCHEDULER, generate_signing_key(ISSUER_SCHEDULER)[0])
    _refused(world, "unknown key", schedule_run=_run(stranger.issue_permit(team_id=TEAM, schedule_id=SCHEDULE_ID, person_id=ANN)))
    _refused(world, "future", schedule_run=_run(genuine, started=datetime.now(timezone.utc) + timedelta(hours=1)))
    _refused(world, "cannot check scheduled runs", schedule_run=_run(genuine), lookup=None)
    gateway_signed = context_signer().issue(team_id=TEAM, person_id=ANN)
    _refused(world, "both", schedule_run=_run(genuine), task_context=gateway_signed)
    with pytest.raises(ValueError):
        ScheduleRunIdentityV1(permit=genuine, context_id="task_context_1", started_at_utc=datetime.now(timezone.utc))


def test_a_permit_never_passes_as_a_task_context_or_the_other_way_round(world) -> None:
    record_ann(world, ACTIVE, ("member",))
    save_schedule(world)
    verifier = ContextVerifier.from_env()
    permit = _permit(world)
    context = world.signer.issue(team_id=TEAM, person_id=ANN, schedule_id=SCHEDULE_ID)

    # Shapes: neither envelope reads as the other.
    with pytest.raises(ValueError):
        verifier.verify(permit.model_dump(mode="json"), team_id=TEAM)
    with pytest.raises(ValueError):
        verifier.verify_permit(context.model_dump(mode="json"), team_id=TEAM)
    with pytest.raises(ValueError):
        _resolve(world, task_context=permit.model_dump(mode="json"))

    # Signatures: the same key signs each kind under its own domain, so a
    # signature made for one kind never checks out for the other.
    key = world.signer.private_key
    permit_under_task_domain = SignedSchedulePermitV1(
        permit=permit.permit,
        key_id=permit.key_id,
        signature=identity._b64(key.sign(identity._DOMAIN + identity._canonical_json(permit.permit))),
    )
    with pytest.raises(IdentityRejected, match="does not match"):
        verifier.verify_permit(permit_under_task_domain, team_id=TEAM)
    context_under_permit_domain = SignedTaskContextV1(
        context=context.context,
        key_id=context.key_id,
        signature=identity._b64(key.sign(identity._PERMIT_DOMAIN + identity._canonical_json(context.context))),
    )
    with pytest.raises(IdentityRejected, match="does not match"):
        verifier.verify(context_under_permit_domain, team_id=TEAM)

    # Only the scheduler's key signs permits.
    gateway = context_signer()
    with pytest.raises(ValueError):
        gateway.issue_permit(team_id=TEAM, schedule_id=SCHEDULE_ID, person_id=ANN)
    by_gateway_key = SignedSchedulePermitV1(
        permit=permit.permit,
        key_id=gateway.key_id,
        signature=identity._b64(gateway.private_key.sign(identity._canonical_permit(permit.permit))),
    )
    with pytest.raises(IdentityRejected, match="Only the scheduler"):
        verifier.verify_permit(by_gateway_key, team_id=TEAM)
    assert gateway.issuer == ISSUER_GATEWAY and json.loads(permit.model_dump_json())["permit"]["record_kind"] == (
        "schedule_permit"
    )


# -- without a usable permit, the worker asks the manager -------------------------------


def test_without_a_permit_the_worker_asks_the_manager(world) -> None:
    record_ann(world, ACTIVE, ("member",))
    save_schedule(world)  # the manager has not looked at it yet

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    assert asyncio.run(scenario()) == {}
    requests = [content for _subject, content in world.bus.published if isinstance(content, ScheduledRunContextRequestV1)]
    assert len(requests) == 1
    run = queued_run(world)
    assert run.meta.gateway_context.schedule_run is None
    assert run_context(run).person_id == ANN


def test_a_permit_for_the_previous_owner_is_not_used(world, caplog) -> None:
    record_people(world, (ANN, ACTIVE, ("member",)), (DAN, ACTIVE, ("member",)))
    save_schedule(world)
    _issue(world)
    save_schedule(world, owner=DAN)  # the manager has not re-checked it yet

    async def scenario():
        await subscribe(world)
        return await fire(world, claim(world))

    with caplog.at_level(logging.INFO):
        assert asyncio.run(scenario()) == {}

    assert "event=schedule_permit_unusable" in caplog.text
    assert [type(content).__name__ for _subject, content in world.bus.published][0] == "ScheduledRunContextRequestV1"
    context = run_context(queued_run(world))
    assert (context.person_id, context.person_roles) == (DAN, ("member",))
