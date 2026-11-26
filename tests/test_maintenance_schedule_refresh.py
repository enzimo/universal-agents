"""The gateway refreshes the person and chat session on the runtime's maintenance schedules.

Real gateway application code, manager schedule handling, SQLite schedule store,
Cedar and signed task contexts. At each gateway start (and when the manager
becomes available again) the gateway sends ``refresh`` for the maintenance
schedules; the manager replaces only the stored person and session
(``custom_agents.schedule_context_refresh``). Roles do not come from the
refresh: runs and escalations read the owner's current roles from the
gateway's copy of the team's people (``access_control.people_directory``). So:

- an existing internal-improvement schedule gets the first user's chat session,
  and a maintenance escalation reaches TeamArchitect as that person, with their
  current roles, who may be told with ``notify_user``;
- a schedule the manager creates after the gateway's last refresh already
  works for its owner with their roles;
- a role change applies at the next escalation, with no refresh;
- a paused or disabled schedule keeps its state, cadence and next fire time;
- a refresh never creates a schedule, and the manager's own installers never
  replace a refreshed context;
- memory-review schedules are installed once (``ensure``) and refreshed, so a
  paused one stays paused across gateway restarts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_control.people import effective_roles
from access_control.task_identity import ScheduleRunLookup, resolve_task_identity
from assistant_gateway.application.maintenance_schedules import (
    GatewayHeartbeatScheduleSettings,
    GatewayInternalImprovementScheduleSettings,
    GatewayMaintenanceScheduleApplication,
    GatewayMaintenanceScheduleSettings,
    GatewaySpecialistScheduleSettings,
    GatewaySystemScheduleDefinition,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestContextSettings,
)
from assistant_gateway.application.team_commands import GatewayTeamCommandService
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from tests.team_services_support import checked_reply, stand_in_frame
from tests.task_work_support import acting_as
from custom_agents.access_runtime import decide_call
from custom_agents.internal_improvement_runtime import (
    INTERNAL_IMPROVEMENT_DELIVERY_MODE,
    build_internal_improvement_schedule,
    enqueue_internal_improvement_job,
    internal_improvement_schedule_id,
    internal_improvement_schedule_name,
    prepare_internal_improvement_schedule_for_manager,
)
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    build_schedule_mutation_permit,
)
from custom_agents.scheduled_runtime import handle_scheduled_message
from custom_agents.task_context_runtime import sign_system_escalation
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import ManagerEscalationContext, MessageHeader
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, SystemScheduleKind
from storage.access_repository import AccessRepository
from tests.broker_access_support import (
    BrokerGatewayTokenSigner,
    broker_person_ref,
    forget_task_context_keys,
)
from tests.scheduled_run_support import bind_test_scheduler
from tests.test_internal_improvement_runtime import _Manager, _Worker

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = _Manager.team_id
FIRST_USER = "user_123"  # the maintenance schedule's owner (_Manager's bootstrap user)
MAINTENANCE_ID = internal_improvement_schedule_id(TEAM)
MEMORY_REVIEW_ID = "mem_rev_core_test"
ESCALATION_TASK = "escalation_task"

def decide_for(agent, function, arguments):
    """The access hook's decision for ``agent`` in the task it works on (tests.task_work_support)."""

    with acting_as(agent):
        return decide_call(agent, function, arguments)


pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class _Identity:
    """The gateway's membership records: each person's current role in the team."""

    def __init__(self) -> None:
        self.roles = {FIRST_USER: "owner"}

    def resolve_user_by_id(self, person_id: str) -> Any:
        role = self.roles.get(person_id)
        if role is None:
            return None
        membership = SimpleNamespace(team_id=TEAM, status="active", role=role)
        return SimpleNamespace(user=SimpleNamespace(canonical_user_id=person_id), memberships=[membership])


class _ManagerRoute:
    """The gateway's manager client: what it publishes goes to the manager's inbox."""

    manager_id = "manager_1"

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish_durable(self, envelope: Any, *, routing_key: str) -> None:
        self.published.append(envelope)


class _InProcessScheduleCommands:
    """In-process gateway ``team_commands`` port routing ``schedules.install`` to ``ScheduleOperations``."""

    def __init__(self, schedule_ops: Any) -> None:
        self._ops = schedule_ops

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str) -> dict[str, Any]:
        frame = stand_in_frame(TEAM, kind, body, person_ref=person_ref, request_id=request_id)
        return checked_reply(kind, await self._ops.handle(frame))


def _gateway(
    identity: _Identity,
    route: _ManagerRoute,
    *,
    memory_review: bool = False,
    schedule_ops: Any = None,
    delivery_refs: Any = None,
) -> GatewayMaintenanceScheduleApplication:
    from uuid import uuid4

    session = SimpleNamespace(
        owner_canonical_user_id=FIRST_USER,
        conversation_id="conversation_owner",
        session_key="session_owner",
        channel_name="webchat",
        user_id=FIRST_USER,
        chat_id="chat_owner",
        thread_id=None,
    )
    refs = delivery_refs or DeliveryRefs(TEAM, b"m" * 32)
    team_commands = GatewayTeamCommandService(
        team_services=_InProcessScheduleCommands(schedule_ops),
        references=SimpleNamespace(person_ref=broker_person_ref),
        request_ids=lambda: f"cmd-{uuid4().hex}",
    )
    return GatewayMaintenanceScheduleApplication(
        settings=GatewayMaintenanceScheduleSettings(
            instance_name="test",
            response_route="gateway_responses",
            user_display_name="Owner",
            request_timezone="UTC",
            default_owner_canonical_user_id=FIRST_USER,
            heartbeat=GatewayHeartbeatScheduleSettings(enabled=False, source_id="hb", schedule_id="hb", interval_seconds=60),
            specialist_schedules=(
                GatewaySpecialistScheduleSettings(
                    name="memory review",
                    enabled=memory_review,
                    instruction_frame="Memory review tick for schedule_id={schedule_id}. {instruction}",
                    definitions=(
                        GatewaySystemScheduleDefinition(
                            schedule_id=MEMORY_REVIEW_ID,
                            schedule_name="memory_review:coremem:test",
                            interval_seconds=86_400,
                            instruction="Review active coremem facts.",
                            system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM,
                        ),
                    ),
                ),
            ),
            internal_improvement=GatewayInternalImprovementScheduleSettings(
                schedule_id=MAINTENANCE_ID,
                schedule_name=internal_improvement_schedule_name(TEAM),
                interval_seconds=21_600,
            ),
        ),
        proactive_sessions=SimpleNamespace(resolve_session=lambda: session),
        # Team schedules only here; per-person memory review is covered in
        # tests/test_person_memory_review_schedules.py.
        person_sessions=SimpleNamespace(active_person_ids=lambda: ()),  # type: ignore[arg-type]
        request_context=GatewayRequestContextService(
            settings=GatewayRequestContextSettings(
                instance_name="test",
                team_id=TEAM,
                default_canonical_user_id=FIRST_USER,
                default_display_name="Owner",
                default_timezone="UTC",
                heartbeat_source_id=None,
                users_enabled=True,
            ),
            identity=identity,  # type: ignore[arg-type]
            token_signer=BrokerGatewayTokenSigner(team_id=TEAM),
            person_ref_resolver=broker_person_ref,
        ),
        delivery=SimpleNamespace(upsert_scheduled_target=lambda **_: None),
        manager=route,  # type: ignore[arg-type]
        id_generator=SimpleNamespace(new_correlation_id=lambda: "correlation"),
        team_commands=team_commands,
        delivery_refs=refs,
    )


async def _gateway_start(gateway: GatewayMaintenanceScheduleApplication, route: _ManagerRoute, manager: _Manager) -> None:
    """The gateway starts (ensure_all), installing and refreshing maintenance schedules via ``schedules.install``."""

    await gateway.ensure_all()


async def _install_maintenance(manager: _Manager) -> None:
    """How an InternalImprovementAgent's startup creates the schedule (ensure)."""

    schedule = build_internal_improvement_schedule(_Worker("worker_install"))
    assert schedule is not None
    await handle_scheduled_message(manager, schedule)


def _verified(world, gateway_context: Any) -> Any:
    return resolve_task_identity(
        verifier=world.verifier,
        team_id=TEAM,
        task_context=gateway_context.task_context,
        schedule_run=gateway_context.schedule_run,
        schedule_runs=ScheduleRunLookup.from_storage(world.tasks.storage_settings),
    )


def _record_people(world) -> None:
    """What the gateway keeps in the access store after a change: everyone's current roles."""

    now = datetime.now(timezone.utc)
    people = [
        TeamPersonV1(
            person_ref=broker_person_ref(person_id),
            team_id=TEAM,
            person_id=person_id,
            standing=PersonStanding.ACTIVE,
            roles=effective_roles([role]),
            changed_at_utc=now,
        )
        for person_id, role in world.identity.roles.items()
    ]
    AccessRepository(world.tasks.storage_settings).replace_people(TEAM, people, at=now)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "tasks.db"
    tasks = TaskManager(db_path=str(db_path))
    tasks.set_owner_identity("agent-TeamArchitect", "TeamArchitect", TEAM, "TeamArchitect")
    manager = _Manager(db_path)
    route = _ManagerRoute()
    identity = _Identity()
    sched = bind_test_scheduler(
        monkeypatch, store=manager._schedule_store, team_id=TEAM, storage_settings=tasks.storage_settings
    )
    world = SimpleNamespace(
        tmp_path=tmp_path,
        manager=manager,
        route=route,
        identity=identity,
        tasks=tasks,
        scheduler=sched.scheduler,
        schedule_ops=sched.schedule_ops,
        delivery_refs=DeliveryRefs(TEAM, b"m" * 32),
        verifier=sched.verifier,
    )
    _record_people(world)
    yield world
    forget_task_context_keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [60, 120])
async def test_gateway_restart_keeps_heartbeat_due_time_and_configured_cadence(world, interval_seconds):
    application = _gateway(
        world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs
    )
    application._settings = replace(application._settings, heartbeat=GatewayHeartbeatScheduleSettings(
        enabled=True, source_id="heartbeat_source", schedule_id="heartbeat_launcher", interval_seconds=60,
    ))
    await application.ensure_heartbeat()
    before = world.manager._schedule_store.get_scheduled_message("heartbeat_launcher")
    assert before is not None
    application._settings = replace(application._settings, heartbeat=replace(application._settings.heartbeat, interval_seconds=interval_seconds))
    await application.ensure_heartbeat()
    after = world.manager._schedule_store.get_scheduled_message("heartbeat_launcher")
    assert after is not None
    assert (after.next_fire_at, after.interval_ms) == (before.next_fire_at, interval_seconds * 1000)


def _escalation_for(world, *, task_id: str = ESCALATION_TASK) -> tuple[Any, Any]:
    """A maintenance run's escalation as TeamArchitect admits it, and the team lead handling it."""

    world.scheduler.issue_permit_for_schedule(MAINTENANCE_ID)
    schedule = world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID)
    schedule.next_fire_at = "2026-09-25T12:00:00+00:00"
    world.manager.team_comms.sent.clear()
    assert asyncio.run(enqueue_internal_improvement_job(world.manager, MAINTENANCE_ID, schedule))
    meta = world.manager.team_comms.sent[0][0].msg_meta.model_copy(deep=True)
    meta.requester_type = RequesterType.AGENT_REQUESTER
    meta.manager_escalation = ManagerEscalationContext(escalation_type="worker_result")
    tasks = world.tasks
    lead = SimpleNamespace(
        id="agent-TeamArchitect",
        name="TeamArchitect",
        named_agent_type="TeamArchitect",
        team_id=TEAM,
        is_team_lead=True,
        access_role="worker",
        authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
        task_manager=tasks,
        instance_root=None,
        test_task_id=task_id,
        _schedule_store=world.manager._schedule_store,
    )
    admitted = sign_system_escalation(lead, meta, task_id=task_id)
    tasks.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(), admitted, owner_person_id=None)
    return admitted, lead


def test_an_existing_maintenance_schedule_gets_the_first_users_session_at_gateway_start(world, caplog) -> None:
    asyncio.run(_install_maintenance(world.manager))
    assert world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context.task_context is None

    with caplog.at_level(logging.INFO, logger="test.team_schedules"):
        asyncio.run(
            _gateway_start(
                _gateway(world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs),
                world.route,
                world.manager,
            )
        )

    store = world.manager._schedule_store
    stored = store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context
    assert store.is_owner_vouched(MAINTENANCE_ID) is True
    assert store.delivery_ref_of(MAINTENANCE_ID) == world.delivery_refs.ref("session_owner")
    assert (stored.canonical_user_id, stored.delivery_mode) == (
        FIRST_USER,
        INTERNAL_IMPROVEMENT_DELIVERY_MODE,
    )
    assert "event=team_schedule_installed" in caplog.text and MAINTENANCE_ID in caplog.text

    admitted, lead = _escalation_for(world)
    context = _verified(world, admitted.gateway_context)
    assert (context.issuer, context.person_id, context.person_roles) == ("scheduler", FIRST_USER, ("admin", "owner"))
    assert decide_for(lead, "notify_user", {}).allowed


def test_a_schedule_created_after_the_gateways_last_refresh_works_for_its_owner(world, caplog) -> None:
    """The gateway started first; the manager created the schedule later (the old gap)."""

    with caplog.at_level(logging.INFO, logger="assistant_gateway.application.maintenance_schedules"):
        asyncio.run(
            _gateway_start(
                _gateway(world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs),
                world.route,
                world.manager,
            )
        )
    assert "outcome=not_found" in caplog.text
    asyncio.run(_install_maintenance(world.manager))

    admitted, lead = _escalation_for(world)

    context = _verified(world, admitted.gateway_context)
    assert (context.person_id, context.person_roles) == (FIRST_USER, ("admin", "owner"))
    assert decide_for(lead, "notify_user", {}).allowed


def test_a_role_change_applies_at_the_next_escalation_without_a_refresh(world) -> None:
    asyncio.run(_install_maintenance(world.manager))
    asyncio.run(
        _gateway_start(
            _gateway(world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs),
            world.route,
            world.manager,
        )
    )
    world.identity.roles[FIRST_USER] = "member"
    _record_people(world)
    first, _lead = _escalation_for(world, task_id="escalation_member")
    first_roles = _verified(world, first.gateway_context).person_roles

    world.identity.roles[FIRST_USER] = "admin"
    _record_people(world)
    second, lead = _escalation_for(world, task_id="escalation_admin")

    assert first_roles == ("member",)
    assert _verified(world, second.gateway_context).person_roles == ("admin",)
    assert decide_for(lead, "notify_user", {}).allowed


def _pause_or_disable(manager: _Manager, how: str) -> None:
    store = manager._schedule_store
    stored = store.get_scheduled_message(MAINTENANCE_ID)
    changed = stored.model_copy(deep=True)
    if how == "paused":
        changed.paused_until = "2030-01-01T00:00:00+00:00"
        operation = ScheduleMutationOperation.PAUSE
    else:
        changed.enabled = False
        operation = ScheduleMutationOperation.DISABLE
    permit = build_schedule_mutation_permit(
        schedule=stored,
        operation=operation,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id=FIRST_USER,
        owner_canonical_user_id=FIRST_USER,
        team_id=TEAM,
        candidate=changed,
    )
    if how == "paused":
        assert store.set_paused_until(MAINTENANCE_ID, changed.paused_until, system_mutation_permit=permit)
    else:
        assert store.set_enabled(MAINTENANCE_ID, False, system_mutation_permit=permit)


def _operator_state(manager: _Manager) -> tuple[Any, ...]:
    row = manager._schedule_store.get_row(MAINTENANCE_ID)
    stored = manager._schedule_store.get_scheduled_message(MAINTENANCE_ID)
    return (
        stored.enabled,
        stored.paused_until,
        stored.interval_ms,
        stored.next_fire_at,
        row.get("remaining_count"),
        stored.payload.problem_statement,
        stored.schedule_memory.model_dump(mode="json"),
    )


@pytest.mark.parametrize("how", ["paused", "disabled"])
def test_a_refresh_keeps_pause_disable_cadence_and_next_fire_time(world, how) -> None:
    asyncio.run(_install_maintenance(world.manager))
    _pause_or_disable(world.manager, how)
    before = _operator_state(world.manager)

    asyncio.run(
        _gateway_start(
            _gateway(world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs),
            world.route,
            world.manager,
        )
    )

    assert _operator_state(world.manager) == before
    store = world.manager._schedule_store
    assert store.is_owner_vouched(MAINTENANCE_ID) is True
    assert store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context.canonical_user_id == FIRST_USER


def test_a_refresh_of_a_missing_schedule_creates_nothing(world, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="assistant_gateway.application.maintenance_schedules"):
        asyncio.run(
            _gateway_start(
                _gateway(world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs),
                world.route,
                world.manager,
            )
        )

    assert world.manager._schedule_store.get_all() == []
    assert "event=gateway_maintenance_schedule_install_refused" in caplog.text and "outcome=not_found" in caplog.text


def test_the_managers_own_installers_never_replace_a_refreshed_context(world) -> None:
    asyncio.run(_install_maintenance(world.manager))
    asyncio.run(
        _gateway_start(
            _gateway(world.identity, world.route, schedule_ops=world.schedule_ops, delivery_refs=world.delivery_refs),
            world.route,
            world.manager,
        )
    )
    refreshed_ref = world.manager._schedule_store.delivery_ref_of(MAINTENANCE_ID)

    # An InternalImprovementAgent starting again, and the memory-review cleanup scan.
    asyncio.run(_install_maintenance(world.manager))
    cleanup_install = prepare_internal_improvement_schedule_for_manager(
        world.manager, build_internal_improvement_schedule(_Worker("worker_cleanup"))
    )
    assert world.manager._schedule_store.add_if_absent(cleanup_install) is False

    assert world.manager._schedule_store.is_owner_vouched(MAINTENANCE_ID) is True
    assert world.manager._schedule_store.delivery_ref_of(MAINTENANCE_ID) == refreshed_ref


def test_a_paused_memory_review_schedule_stays_paused_across_gateway_restarts(world) -> None:
    gateway = _gateway(
        world.identity,
        world.route,
        memory_review=True,
        schedule_ops=world.schedule_ops,
        delivery_refs=world.delivery_refs,
    )
    asyncio.run(_gateway_start(gateway, world.route, world.manager))
    store = world.manager._schedule_store
    stored = store.get_scheduled_message(MEMORY_REVIEW_ID)
    paused = stored.model_copy(deep=True)
    paused.paused_until = "2030-01-01T00:00:00+00:00"
    permit = build_schedule_mutation_permit(
        schedule=stored,
        operation=ScheduleMutationOperation.PAUSE,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id=FIRST_USER,
        owner_canonical_user_id=FIRST_USER,
        team_id=TEAM,
        candidate=paused,
    )
    assert store.set_paused_until(MEMORY_REVIEW_ID, paused.paused_until, system_mutation_permit=permit)
    before = store.get_scheduled_message(MEMORY_REVIEW_ID)
    world.identity.roles[FIRST_USER] = "admin"
    _record_people(world)

    asyncio.run(_gateway_start(gateway, world.route, world.manager))

    after = store.get_scheduled_message(MEMORY_REVIEW_ID)
    assert (after.paused_until, after.enabled, after.next_fire_at, after.interval_ms) == (
        before.paused_until,
        before.enabled,
        before.next_fire_at,
        before.interval_ms,
    )
    # Its owner stays vouched and its scheduler-signed run permit reflects the current roles via PeopleDirectory.
    assert store.is_owner_vouched(MEMORY_REVIEW_ID) is True
    permit_token = store.run_permit(MEMORY_REVIEW_ID)
    assert permit_token is not None
    assert world.verifier.verify_schedule_permit(permit_token, team_id=TEAM).person_roles == ("admin",)


def test_maintenance_schedules_retry_after_transient_team_services_outage(world) -> None:
    """C30: maintenance installs skipped while svc.team is down are queued and retried on the background loop."""
    from assistant_gateway.application.team_commands import TeamServicesUnavailable

    gateway = _gateway(
        world.identity,
        world.route,
        memory_review=True,
        schedule_ops=world.schedule_ops,
        delivery_refs=world.delivery_refs,
    )
    real_request = gateway._team_commands.request
    outage = {"down": True}

    async def _flaky_request(kind: str, body: dict[str, Any], *, person_id: str) -> dict[str, Any]:
        if outage["down"]:
            raise TeamServicesUnavailable("team services are starting")
        return await real_request(kind, body, person_id=person_id)

    gateway._team_commands.request = _flaky_request  # type: ignore[method-assign]

    asyncio.run(gateway.ensure_all())
    assert world.manager._schedule_store.get_scheduled_message(MEMORY_REVIEW_ID) is None
    assert (MEMORY_REVIEW_ID, "ensure") in gateway._pending_retries
    assert (MEMORY_REVIEW_ID, "refresh") in gateway._pending_retries

    # Team services come back; the background retry loop installs and refreshes the queued schedules.
    outage["down"] = False

    async def _run_until_drained() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(gateway.run(stop, retry_interval_seconds=0.01))
        try:
            for _ in range(50):
                if not gateway._pending_retries:
                    break
                await asyncio.sleep(0.01)
        finally:
            stop.set()
            await task

    asyncio.run(_run_until_drained())
    assert not gateway._pending_retries
    stored = world.manager._schedule_store.get_scheduled_message(MEMORY_REVIEW_ID)
    assert stored is not None
    assert world.manager._schedule_store.is_owner_vouched(MEMORY_REVIEW_ID) is True
