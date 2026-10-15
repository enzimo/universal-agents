"""The schedule checks before each maintenance run (``custom_agents.stale_schedules``).

Real SQLite schedule, task and access stores. The gateway's copy of the team's
people says who is active, disabled or removed; the check first ends departed
people's work (their schedules deleted, team schedules moved to an
administrator, ``custom_agents.person_departure_runtime``), then tells owners
and administrators about the rest through Inbox notices, once per list.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.people_directory import PersonNow
from custom_agents.internal_improvement_runtime import check_schedules_for_maintenance
from custom_agents.schedule_store import ScheduleStore
from custom_agents.stale_schedules import (
    ScheduleFacts,
    StaleAction,
    StaleCheckInputs,
    check_schedules,
    check_stale_schedules,
    classify,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import AccessNoticeKind, PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from runtime_settings.stale_schedules import StaleScheduleSettings
from storage.access_repository import AccessRepository

TEAM = "team_1"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
ANN, BEN, CAROL, ADMIN = "usr_ann", "usr_ben", "usr_carol", "usr_admin"


def _schedule(schedule_id: str, owner: str, *, system: bool = False, **fields) -> ScheduledMessage:
    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=f"name of {schedule_id}",
        management_class=ScheduleManagementClass.SYSTEM if system else ScheduleManagementClass.USER,
        system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM if system else None,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Do it."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="manager_1"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner)),
        interval_ms=86_400_000,
        **fields,
    )


@pytest.fixture
def world(tmp_path: Path):
    tasks = TaskManager(db_path=str(tmp_path / "tasks.db"))
    tasks.set_owner_identity(agent_id="manager_1", agent_name="manager", team_id=TEAM, named_agent_type="TeamArchitect")
    store = ScheduleStore(db_path=str(tmp_path / "schedules.db"))
    agent = SimpleNamespace(
        id="manager_1",
        team_id=TEAM,
        task_manager=tasks,
        _schedule_store=store,
        _ensure_schedule_store=lambda: store,
        is_team_lead=True,
        schedule_maintenance={},
        # The definition lookup the team's composition gives the manager.
        _local_team_runtime=SimpleNamespace(
            resolve_definition=lambda agent_type: None if agent_type == "GoneAgent" else {"agent_type": agent_type}
        ),
        _logger=logging.getLogger("test.stale.manager"),
    )
    access = AccessRepository(tasks.storage_settings)
    return SimpleNamespace(agent=agent, store=store, access=access)


def _people(world, **standings: PersonStanding) -> None:
    roles = {PersonStanding.ACTIVE: ("member",)}
    records = [
        TeamPersonV1(team_id=TEAM, person_id=person, standing=standing, roles=roles.get(standing, ()), changed_at_utc=NOW)
        for person, standing in {ADMIN: PersonStanding.ACTIVE, **standings}.items()
    ]
    records[0] = records[0].model_copy(update={"roles": ("admin",)})
    world.access.replace_people(TEAM, records, at=NOW)


def _notices(world, *, person_id: str, roles: tuple[str, ...] = ()):
    return [
        notice
        for notice, _read in world.access.notices_for(TEAM, person_id=person_id, roles=roles)
        if notice.kind is AccessNoticeKind.SCHEDULES_TO_REVIEW
    ]


def test_departed_owners_are_handled_first_and_unused_schedules_reported_once(world) -> None:
    _people(world, **{ANN: PersonStanding.REMOVED, BEN: PersonStanding.DISABLED, CAROL: PersonStanding.ACTIVE})
    world.store.add(_schedule("sched_ann", ANN))
    world.store.add(_schedule("sched_ann_system", ANN, system=True))
    world.store.add(_schedule("sched_ben", BEN))
    world.store.add(_schedule("sched_carol_paused", CAROL, paused_until=(NOW + timedelta(days=200)).isoformat()))
    world.store.add(_schedule("sched_carol_fine", CAROL))
    gone = _schedule("sched_carol_gone", CAROL)
    gone.payload_meta.to_named_agent_type = "GoneAgent"
    world.store.add(gone)

    sentence = asyncio.run(check_schedules(world.agent))

    # Ann was removed and Ben disabled: their own schedules are deleted, and
    # the team schedule Ann owned now runs for the administrator.
    assert world.store.get_scheduled_message("sched_ann") is None
    assert world.store.get_scheduled_message("sched_ben") is None
    moved = world.store.get_scheduled_message("sched_ann_system")
    assert moved is not None and moved.payload_meta.gateway_context.canonical_user_id == ADMIN
    (moved_notice,) = _notices(world, person_id=ADMIN, roles=("admin",))
    assert "now belong to usr_admin" in moved_notice.title and "sched_ann_system" in moved_notice.summary
    carol_notice, = _notices(world, person_id=CAROL)
    assert "sched_carol_paused" in carol_notice.summary and "sched_carol_fine" not in carol_notice.summary
    assert "(sched_carol_gone): it runs GoneAgent, which no longer exists" in carol_notice.summary
    assert "/delete schedule" in carol_notice.summary
    assert not _notices(world, person_id=BEN)
    assert "ended the work of 2 disabled or removed person(s)" in sentence
    assert "Trusted code checked every schedule" in sentence
    assert "sched_" not in sentence  # the model gets counts only

    check_stale_schedules(world.agent, now=NOW)
    assert len(_notices(world, person_id=CAROL)) == 1  # same list, no new notice

    world.store.add(_schedule("sched_carol_old", CAROL, enabled=False, next_fire_at=(NOW - timedelta(days=45)).isoformat()))
    check_stale_schedules(world.agent, now=NOW)
    assert len(_notices(world, person_id=CAROL)) == 2  # the list changed


def test_a_departed_owners_schedule_that_is_still_here_is_reported_to_administrators(world) -> None:
    # Ending the person's work failed (or no administrator could take a team
    # schedule over): the check reports what is left.
    _people(world, **{ANN: PersonStanding.REMOVED})
    world.store.add(_schedule("sched_ann", ANN))

    check_stale_schedules(world.agent, now=NOW)

    assert world.store.get_scheduled_message("sched_ann") is not None
    (notice,) = _notices(world, person_id=ADMIN, roles=("admin",))
    assert "(sched_ann): its owner is removed and it could not be deleted or moved" in notice.summary


def test_nothing_is_deleted_or_reported_before_the_gateway_records_the_people(world, caplog) -> None:
    world.store.add(_schedule("sched_ann", ANN))

    with caplog.at_level(logging.WARNING, logger="custom_agents.stale_schedules"):
        report = check_stale_schedules(world.agent, now=NOW)

    assert world.store.get_scheduled_message("sched_ann") is not None
    assert report.skipped_reason and "has not recorded" in report.instruction_sentence()
    assert "event=stale_schedule_check_waiting_for_people" in caplog.text


def test_the_maintenance_run_is_told_the_counts(world) -> None:
    _people(world, **{ANN: PersonStanding.REMOVED})
    world.store.add(_schedule("sched_ann", ANN))

    sentence = asyncio.run(check_schedules_for_maintenance(world.agent, "maintenance_schedule"))

    assert world.store.get_scheduled_message("sched_ann") is None
    assert "ended the work of 1 disabled or removed person(s)" in sentence
    assert "do not look for or report them" in sentence


def _facts(**fields) -> ScheduleFacts:
    base = dict(
        schedule_id="sched",
        name="Reminder",
        owner_id=ANN,
        system_managed=False,
        enabled=True,
        next_fire_at=NOW + timedelta(days=1),
        paused_until=None,
        target_agent_type=None,
        objective_id=None,
    )
    return ScheduleFacts(**{**base, **fields})


def _inputs(**fields) -> StaleCheckInputs:
    base = dict(
        people={ANN: PersonNow(ANN, PersonStanding.ACTIVE, ("member",))},
        now=NOW,
        settings=StaleScheduleSettings(),
        missing_agent_types=frozenset({"GoneAgent"}),
        closed_objectives=frozenset({"obj_done"}),
    )
    return StaleCheckInputs(**{**base, **fields})


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (_facts(enabled=False, next_fire_at=NOW - timedelta(days=31)), "turned off since at least 2026-08-25"),
        (_facts(paused_until=NOW + timedelta(days=91)), "paused until 2026-12-25"),
        (_facts(target_agent_type="GoneAgent"), "it runs GoneAgent, which no longer exists"),
        (_facts(objective_id="obj_done"), "its objective obj_done is closed"),
    ],
)
def test_an_active_persons_unused_schedule_is_reported_to_them(facts, reason) -> None:
    finding = classify(facts, _inputs())
    assert finding is not None and (finding.action, finding.reason) == (StaleAction.TELL_OWNER, reason)


@pytest.mark.parametrize(
    "facts",
    [
        _facts(),
        _facts(enabled=False, next_fire_at=NOW - timedelta(days=29)),  # not yet 30 days
        _facts(paused_until=NOW + timedelta(days=89)),
        _facts(target_agent_type="ResearchAgent"),
        _facts(objective_id="obj_open"),
        _facts(system_managed=True, enabled=False, next_fire_at=NOW - timedelta(days=400)),  # an operator's choice
    ],
)
def test_schedules_in_use_or_deliberately_off_are_left_alone(facts) -> None:
    assert classify(facts, _inputs()) is None


def test_the_agent_type_rule_is_skipped_when_the_definitions_cannot_be_read() -> None:
    assert classify(_facts(target_agent_type="GoneAgent"), _inputs(missing_agent_types=None)) is None


def test_an_owner_the_gateway_does_not_know_is_reported_to_administrators() -> None:
    finding = classify(_facts(owner_id="usr_stranger"), _inputs())
    assert finding is not None and finding.action is StaleAction.TELL_ADMINISTRATORS
