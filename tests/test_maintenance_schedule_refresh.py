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

from access_control.identity import ContextVerifier
from access_control.people import effective_roles
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
    context_signer,
    forget_task_context_keys,
    use_scheduler_key,
)
from tests.test_internal_improvement_runtime import _Manager, _Worker

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = _Manager.team_id
FIRST_USER = "user_123"  # the maintenance schedule's owner (_Manager's bootstrap user)
MAINTENANCE_ID = internal_improvement_schedule_id(TEAM)
MEMORY_REVIEW_ID = "mem_rev_core_test"
ESCALATION_TASK = "escalation_task"

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


def _gateway(identity: _Identity, route: _ManagerRoute, *, memory_review: bool = False) -> GatewayMaintenanceScheduleApplication:
    session = SimpleNamespace(
        owner_canonical_user_id=FIRST_USER,
        conversation_id="conversation_owner",
        session_key="session_owner",
        channel_name="webchat",
        user_id=FIRST_USER,
        chat_id="chat_owner",
        thread_id=None,
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
            context_signer=context_signer(),
        ),
        delivery=SimpleNamespace(upsert_scheduled_target=lambda **_: None),
        manager=route,  # type: ignore[arg-type]
        id_generator=SimpleNamespace(new_correlation_id=lambda: "correlation"),
    )


async def _gateway_start(gateway: GatewayMaintenanceScheduleApplication, route: _ManagerRoute, manager: _Manager) -> None:
    """The gateway starts (ensure_all) and the manager handles what it sent, in order."""

    await gateway.ensure_all()
    for envelope in route.published:
        await handle_scheduled_message(manager, envelope.msg_content)
    route.published.clear()


async def _install_maintenance(manager: _Manager) -> None:
    """How an InternalImprovementAgent's startup creates the schedule (ensure)."""

    schedule = build_internal_improvement_schedule(_Worker("worker_install"))
    assert schedule is not None
    await handle_scheduled_message(manager, schedule)


def _verified(signed: Any) -> Any:
    return ContextVerifier.from_env().verify(signed, team_id=TEAM)


def _record_people(world) -> None:
    """What the gateway keeps in the access store after a change: everyone's current roles."""

    now = datetime.now(timezone.utc)
    people = [
        TeamPersonV1(
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
    use_scheduler_key(monkeypatch)
    manager = _Manager(tmp_path / "schedules.db")
    route = _ManagerRoute()
    identity = _Identity()
    # One task store for every escalation, as in the manager: the access store lives beside it.
    tasks = TaskManager(db_path=str(tmp_path / "tasks.db"))
    tasks.set_owner_identity("agent-TeamArchitect", "TeamArchitect", TEAM, "TeamArchitect")
    world = SimpleNamespace(tmp_path=tmp_path, manager=manager, route=route, identity=identity, tasks=tasks)
    _record_people(world)
    yield world
    forget_task_context_keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [60, 120])
async def test_gateway_restart_keeps_heartbeat_due_time_and_configured_cadence(world, interval_seconds):
    application = _gateway(world.identity, world.route)
    application._settings = replace(application._settings, heartbeat=GatewayHeartbeatScheduleSettings(
        enabled=True, source_id="heartbeat_source", schedule_id="heartbeat_launcher", interval_seconds=60,
    ))
    await application.ensure_heartbeat()
    for envelope in world.route.published:
        await handle_scheduled_message(world.manager, envelope.msg_content)
    before = world.manager._schedule_store.get_scheduled_message("heartbeat_launcher")
    assert before is not None
    world.route.published.clear()
    application._settings = replace(application._settings, heartbeat=replace(application._settings.heartbeat, interval_seconds=interval_seconds))
    await application.ensure_heartbeat()
    for envelope in world.route.published:
        await handle_scheduled_message(world.manager, envelope.msg_content)
    after = world.manager._schedule_store.get_scheduled_message("heartbeat_launcher")
    assert after is not None
    assert (after.next_fire_at, after.interval_ms) == (before.next_fire_at, interval_seconds * 1000)


def _escalation_for(world, *, task_id: str = ESCALATION_TASK) -> tuple[Any, Any]:
    """A maintenance run's escalation as TeamArchitect admits it, and the team lead handling it."""

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
        _active_execution_task_id=task_id,
        _schedule_store=world.manager._schedule_store,
    )
    admitted = sign_system_escalation(lead, meta, task_id=task_id)
    tasks.add_task(Task(task_id=task_id, task_type=MessageMetaType.REQUEST), MessageHeader(), admitted, owner_person_id=None)
    return admitted, lead


def test_an_existing_maintenance_schedule_gets_the_first_users_session_at_gateway_start(world, caplog) -> None:
    asyncio.run(_install_maintenance(world.manager))
    assert world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context.task_context is None

    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_context_refresh"):
        asyncio.run(_gateway_start(_gateway(world.identity, world.route), world.route, world.manager))

    stored = world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context
    signed = _verified(stored.task_context)
    assert (signed.issuer, signed.person_id, signed.person_roles) == ("gateway", FIRST_USER, ("admin", "owner"))
    assert (stored.canonical_user_id, stored.session_key, stored.delivery_mode) == (
        FIRST_USER,
        "session_owner",
        INTERNAL_IMPROVEMENT_DELIVERY_MODE,
    )
    assert "event=schedule_context_refreshed" in caplog.text and MAINTENANCE_ID in caplog.text

    admitted, lead = _escalation_for(world)
    context = _verified(admitted.gateway_context.task_context)
    assert (context.issuer, context.person_id, context.person_roles) == ("scheduler", FIRST_USER, ("admin", "owner"))
    assert admitted.gateway_context.session_key == "session_owner"
    assert decide_call(lead, "notify_user", {}).allowed


def test_a_schedule_created_after_the_gateways_last_refresh_works_for_its_owner(world, caplog) -> None:
    """The gateway started first; the manager created the schedule later (the old gap)."""

    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_context_refresh"):
        asyncio.run(_gateway_start(_gateway(world.identity, world.route), world.route, world.manager))
    assert "reason=schedule_missing" in caplog.text
    asyncio.run(_install_maintenance(world.manager))

    admitted, lead = _escalation_for(world)

    context = _verified(admitted.gateway_context.task_context)
    assert (context.person_id, context.person_roles) == (FIRST_USER, ("admin", "owner"))
    assert decide_call(lead, "notify_user", {}).allowed


def test_a_role_change_applies_at_the_next_escalation_without_a_refresh(world) -> None:
    asyncio.run(_install_maintenance(world.manager))
    asyncio.run(_gateway_start(_gateway(world.identity, world.route), world.route, world.manager))
    world.identity.roles[FIRST_USER] = "member"
    _record_people(world)
    first, _lead = _escalation_for(world, task_id="escalation_member")

    world.identity.roles[FIRST_USER] = "admin"
    _record_people(world)
    second, lead = _escalation_for(world, task_id="escalation_admin")

    assert _verified(first.gateway_context.task_context).person_roles == ("member",)
    assert _verified(second.gateway_context.task_context).person_roles == ("admin",)
    assert decide_call(lead, "notify_user", {}).allowed


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

    asyncio.run(_gateway_start(_gateway(world.identity, world.route), world.route, world.manager))

    assert _operator_state(world.manager) == before
    stored = world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context
    assert _verified(stored.task_context).person_id == FIRST_USER


def test_a_refresh_of_a_missing_schedule_creates_nothing(world, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_context_refresh"):
        asyncio.run(_gateway_start(_gateway(world.identity, world.route), world.route, world.manager))

    assert world.manager._schedule_store.get_all() == []
    assert "event=schedule_context_refresh_skipped reason=schedule_missing" in caplog.text


def test_the_managers_own_installers_never_replace_a_refreshed_context(world) -> None:
    asyncio.run(_install_maintenance(world.manager))
    asyncio.run(_gateway_start(_gateway(world.identity, world.route), world.route, world.manager))
    refreshed = world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context

    # An InternalImprovementAgent starting again, and the memory-review cleanup scan.
    asyncio.run(_install_maintenance(world.manager))
    cleanup_install = prepare_internal_improvement_schedule_for_manager(
        world.manager, build_internal_improvement_schedule(_Worker("worker_cleanup"))
    )
    assert world.manager._schedule_store.add_if_absent(cleanup_install) is False

    kept = world.manager._schedule_store.get_scheduled_message(MAINTENANCE_ID).payload_meta.gateway_context
    assert kept.task_context == refreshed.task_context and kept.session_key == "session_owner"


def test_a_paused_memory_review_schedule_stays_paused_across_gateway_restarts(world) -> None:
    gateway = _gateway(world.identity, world.route, memory_review=True)
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

    asyncio.run(_gateway_start(gateway, world.route, world.manager))

    after = store.get_scheduled_message(MEMORY_REVIEW_ID)
    assert (after.paused_until, after.enabled, after.next_fire_at, after.interval_ms) == (
        before.paused_until,
        before.enabled,
        before.next_fire_at,
        before.interval_ms,
    )
    # Its person and roles are still refreshed.
    assert _verified(after.payload_meta.gateway_context.task_context).person_roles == ("admin",)
