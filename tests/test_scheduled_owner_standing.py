"""A scheduled run works for its owner with the owner's current standing and roles.

Real TaskManager and SQLite (the access store lives beside the task store, as
in the manager), the real fire path and signed task contexts. The gateway's
copy of the team's people (``access_control.people_directory``) decides:

- a person's schedule runs with their current roles, not the ones they had
  when they made it;
- a person field the gateway never signed for gets no roles;
- a disabled or removed owner's schedule does not run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.task_identity import ScheduleRunLookup, resolve_task_identity
from custom_agents.schedule_store import ScheduleStore
from custom_agents.scheduled_runtime import enqueue_fired_scheduled_task
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from custom_messages.wire.authority_bindings import SCHEDULER_ISSUER_ROLE
from storage.access_repository import AccessRepository
from tests.broker_access_support import broker_person_ref, forget_task_context_keys, signed_task_token
from tests.scheduled_run_support import bind_test_scheduler

TEAM = "team_1"
ANN = "usr_ann"
TASK_ID = "task_reminder"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = str(tmp_path / "tasks.db")
    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity(agent_id="manager_id", agent_name="manager", team_id=TEAM, named_agent_type="TeamArchitect")
    store = ScheduleStore(db_path=db_path, storage_settings=tasks.storage_settings)
    sched = bind_test_scheduler(monkeypatch, store=store, team_id=TEAM, storage_settings=tasks.storage_settings)
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id=TEAM,
        named_agent_type="TeamArchitect",
        task_manager=tasks,
        _schedule_store=store,
        paused=False,
        is_executing=True,  # queue the run instead of starting a model turn
        main_lane_busy=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.manager"),
    )
    yield SimpleNamespace(
        tasks=tasks,
        store=store,
        agent=agent,
        scheduler=sched.scheduler,
        verifier=sched.verifier,
    )
    forget_task_context_keys()


def _record_ann(world, standing: PersonStanding, roles: tuple[str, ...] = ()) -> None:
    AccessRepository(world.tasks.storage_settings).replace_people(
        TEAM,
        [TeamPersonV1(person_ref=broker_person_ref(ANN), team_id=TEAM, person_id=ANN, standing=standing, roles=roles, changed_at_utc=NOW)],
        at=NOW,
    )


def _anns_schedule(*, vouched: bool = True) -> ScheduledMessage:
    """Ann's reminder, saved from the task that made it: then the gateway signed it for her as an administrator."""

    signed = signed_task_token(ANN, team_id=TEAM, roles=("admin",)) if vouched else None
    return ScheduledMessage(
        schedule_id="sched_ann_reminder",
        schedule_name="Water the plants",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="Remind Ann."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="manager_id"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, task_context=signed)),
        repeat_count=1,
        next_fire_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )


def _fire(world, schedule: ScheduledMessage, *, owner_vouched: bool = True) -> bool:
    world.store.add(
        schedule,
        claim_team_id=TEAM,
        claim_named_agent_type="TeamArchitect",
        owner_vouched=owner_vouched,
    )
    world.scheduler.renew_permits()
    claim_token = None
    if world.store.run_permit(schedule.schedule_id) is None:
        claimed = world.store.claim_next_due(
            claimer_agent_id="manager_id", claimer_named_agent_type="TeamArchitect", claimer_team_id=TEAM
        )
        if claimed is not None:
            claim_token = claimed["claim_token"]
    return asyncio.run(
        enqueue_fired_scheduled_task(world.agent, schedule.schedule_id, schedule, claim_token=claim_token)
    )


def _run_context(world):
    queued = world.tasks.get_task_info(TASK_ID)
    assert queued is not None
    gw = queued.meta.gateway_context
    return resolve_task_identity(
        verifier=world.verifier,
        team_id=TEAM,
        task_context=gw.task_context,
        schedule_run=gw.schedule_run,
        schedule_runs=ScheduleRunLookup.from_storage(world.tasks.storage_settings),
    )


def test_a_persons_schedule_runs_with_their_current_roles(world) -> None:
    _record_ann(world, PersonStanding.ACTIVE, ("member",))  # demoted since she made it

    assert _fire(world, _anns_schedule()) is True

    context = _run_context(world)
    assert (context.issuer, context.person_id, context.person_roles) == (SCHEDULER_ISSUER_ROLE, ANN, ("member",))


def test_a_person_field_the_gateway_never_signed_for_gets_no_roles(world) -> None:
    _record_ann(world, PersonStanding.ACTIVE, ("admin",))

    assert _fire(world, _anns_schedule(vouched=False), owner_vouched=False) is True

    context = _run_context(world)
    assert (context.issuer, context.person_id, context.person_roles) == (SCHEDULER_ISSUER_ROLE, ANN, ())


@pytest.mark.parametrize("standing", [PersonStanding.DISABLED, PersonStanding.REMOVED])
def test_a_disabled_or_removed_owners_schedule_does_not_run(world, caplog, standing) -> None:
    _record_ann(world, standing)

    with caplog.at_level(logging.INFO, logger="custom_agents.task_context_runtime"):
        assert _fire(world, _anns_schedule()) is True

    assert world.tasks.get_task_info(TASK_ID) is None
    assert f"Skipped a scheduled run because its person is {standing.value}" in caplog.text
    assert "event=scheduled_run_skipped_owner_inactive" in caplog.text
