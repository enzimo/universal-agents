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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.identity import ISSUER_SCHEDULER, ContextVerifier
from custom_agents.scheduled_runtime import enqueue_fired_scheduled_task
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from storage.access_repository import AccessRepository
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key

TEAM = "team_1"
ANN = "usr_ann"
TASK_ID = "task_reminder"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    use_scheduler_key(monkeypatch)
    tasks = TaskManager(db_path=str(tmp_path / "tasks.db"))
    tasks.set_owner_identity(agent_id="manager_id", agent_name="manager", team_id=TEAM, named_agent_type="TeamArchitect")
    agent = SimpleNamespace(
        id="manager_id",
        name="manager",
        team_id=TEAM,
        task_manager=tasks,
        paused=False,
        is_executing=True,  # queue the run instead of starting a model turn
        # The wake-up reaches a mock drain, so the run stays queued for the test.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.manager"),
    )
    yield SimpleNamespace(tasks=tasks, agent=agent)
    forget_task_context_keys()


def _record_ann(world, standing: PersonStanding, roles: tuple[str, ...] = ()) -> None:
    AccessRepository(world.tasks.storage_settings).replace_people(
        TEAM,
        [TeamPersonV1(team_id=TEAM, person_id=ANN, standing=standing, roles=roles, changed_at_utc=NOW)],
        at=NOW,
    )


def _anns_schedule(*, vouched: bool = True) -> ScheduledMessage:
    """Ann's reminder, saved from the task that made it: then the gateway signed it for her as an administrator."""

    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("admin",)) if vouched else None
    return ScheduledMessage(
        schedule_id="sched_ann_reminder",
        schedule_name="Water the plants",
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_id=TASK_ID, task_type=MessageMetaType.REQUEST, problem_statement="Remind Ann."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="manager_id"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, task_context=signed)),
        repeat_count=1,
    )


def _fire(world, schedule: ScheduledMessage) -> bool:
    return asyncio.run(enqueue_fired_scheduled_task(world.agent, schedule.schedule_id, schedule))


def _run_context(world):
    queued = world.tasks.get_task_info(TASK_ID)
    assert queued is not None
    return ContextVerifier.from_env().verify(queued.meta.gateway_context.task_context, team_id=TEAM)


def test_a_persons_schedule_runs_with_their_current_roles(world) -> None:
    _record_ann(world, PersonStanding.ACTIVE, ("member",))  # demoted since she made it

    assert _fire(world, _anns_schedule()) is True

    context = _run_context(world)
    assert (context.issuer, context.person_id, context.person_roles) == (ISSUER_SCHEDULER, ANN, ("member",))


def test_a_person_field_the_gateway_never_signed_for_gets_no_roles(world, caplog) -> None:
    _record_ann(world, PersonStanding.ACTIVE, ("admin",))

    with caplog.at_level(logging.WARNING, logger="custom_agents.task_context_runtime"):
        assert _fire(world, _anns_schedule(vouched=False)) is True

    assert (_run_context(world).person_id, _run_context(world).person_roles) == (ANN, ())
    assert "event=scheduled_owner_not_vouched" in caplog.text


@pytest.mark.parametrize("standing", [PersonStanding.DISABLED, PersonStanding.REMOVED])
def test_a_disabled_or_removed_owners_schedule_does_not_run(world, caplog, standing) -> None:
    _record_ann(world, standing)

    with caplog.at_level(logging.INFO, logger="custom_agents.task_context_runtime"):
        assert _fire(world, _anns_schedule()) is True

    assert world.tasks.get_task_info(TASK_ID) is None
    assert f"Skipped a scheduled run because its person is {standing.value}" in caplog.text
    assert "event=scheduled_run_skipped_owner_inactive" in caplog.text
