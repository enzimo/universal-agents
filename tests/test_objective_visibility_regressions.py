"""Objective ownership holds across linked work and delayed decisions.

These tests use real task, Objective, heartbeat and schedule repositories and
signed contexts. The source task of a timer can legitimately retain its team
Plan after its person's role changes; tools still need current authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents import scheduled_runtime
from custom_agents.scheduled_runtime import enqueue_heartbeat_items
from custom_agents.task_context_runtime import scheduler_signer
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_tools.providers.heartbeat import (
    add_heartbeat_item,
    disable_heartbeat_item,
    enable_heartbeat_item,
    list_heartbeat_items,
    remove_heartbeat_item,
    update_heartbeat_item,
)
from custom_tools.providers.objectives import create_objective, objectives, update_execution_plan_status
from custom_tools.providers.schedules import create_scheduled_job
from custom_tools.runtime_context import get_agent_ref
from storage.access_repository import AccessRepository
from tests.broker_access_support import context_signer, use_scheduler_key
from tests.test_objective_tools import (
    _act_as,
    _add_no_person_task,
    _people_and_objectives as _people_fixture,
    _signed_task_contexts,  # noqa: F401 -- autouse fixture
)

_people_and_objectives = _people_fixture


@pytest.mark.parametrize(
    "inputs",
    [{"action": "get"}, {"action": "update_progress", "progress_summary": "x"},
     {"action": "update_status", "status": "paused"}],
)
def test_hidden_and_missing_objectives_have_identical_replies(_people_and_objectives, inputs) -> None:
    manager, tasks, made = _people_and_objectives
    _act_as(manager, tasks["a"])
    hidden = json.loads(objectives(objective_id=made["b"].objective_id, **inputs))
    missing = json.loads(objectives(objective_id="objective_missing", **inputs))
    assert hidden == missing


def test_hidden_and_missing_plans_have_identical_replies(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    _act_as(manager, tasks["a"])
    hidden = json.loads(update_execution_plan_status("cancelled", "x", plan_id=made["b"].current_plan_id))
    missing = json.loads(update_execution_plan_status("cancelled", "x", plan_id="plan_missing"))
    assert hidden == missing


def test_unsigned_current_task_cannot_create_an_objective(_people_and_objectives) -> None:
    manager, _tasks, _made = _people_and_objectives
    task = _add_no_person_task(manager, "unsigned_current", signed=False)
    _act_as(manager, task)
    before = manager.objective_repository.list_objectives(limit=100, owner_agent_id=manager.durable_manager_scope_id())
    result = json.loads(create_objective(title="Unsigned", desired_outcome="x", why="y"))
    assert "no verified identity" in result["error"]
    assert manager.get_task_plan_id(task.task_id) is None
    assert manager.objective_repository.list_objectives(limit=100, owner_agent_id=manager.durable_manager_scope_id()) == before


def _team_timer(manager, tasks, team, *, roles: tuple[str, ...] = ("member",)) -> Task:
    """An administrator's authorized timer runs with its person's current roles."""

    _act_as(manager, tasks["admin"])
    created = json.loads(create_scheduled_job(
        instruction="Check team work.", delay_seconds=60, objective_id=team.objective_id,
    ))
    assert "error" not in created
    schedule = get_agent_ref()._schedule_store.get_scheduled_message(created["schedule_id"])
    meta = schedule.payload_meta.model_copy(deep=True)
    meta.gateway_context.task_context = context_signer().issue(
        team_id="team_1", person_id="usr_admin", person_roles=roles,
    )
    run = Task(task_id="demoted_timer_run", task_type=MessageMetaType.REQUEST, problem_statement="Check team work.")
    assert manager.add_task(run, schedule.payload_header, meta, owner_person_id="usr_admin")
    _act_as(manager, run)
    assert manager.get_task_plan_id(run.task_id) == team.current_plan_id
    return run


@pytest.mark.parametrize("roles", [("member",), ("admin",)])
def test_creation_cannot_move_a_team_plan_into_personal_ownership(_people_and_objectives, roles) -> None:
    manager, tasks, made = _people_and_objectives
    team = made["team"]
    run = _team_timer(manager, tasks, team, roles=roles)
    before = manager.get_plan(team.current_plan_id).model_dump(mode="json")
    result = json.loads(create_objective(title="Personal", desired_outcome="x", why="y"))
    assert "error" in result
    assert manager.get_plan(team.current_plan_id).model_dump(mode="json") == before
    # Future member work still cannot control the team's original Plan.
    manager.get_task_info(run.task_id).meta.gateway_context.task_context = context_signer().issue(
        team_id="team_1", person_id="usr_admin", person_roles=("member",),
    )
    cancelled = json.loads(update_execution_plan_status("cancelled", "x", plan_id=team.current_plan_id))
    assert "error" in cancelled
    assert manager.get_plan(team.current_plan_id).status.value == "active"


def test_rejected_inherited_objective_is_removed_from_schedule_payload(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    team = made["team"]
    _team_timer(manager, tasks, team)
    created = json.loads(create_scheduled_job(instruction="A separate check.", delay_seconds=60))
    assert "error" not in created
    schedule = get_agent_ref()._schedule_store.get_scheduled_message(created["schedule_id"])
    assert schedule.objective_id is None
    assert schedule.payload_meta.objective_id is None
    run = Task(task_id="independent_timer", task_type=MessageMetaType.REQUEST, problem_statement="Check.")
    assert manager.add_task(run, schedule.payload_header, schedule.payload_meta, owner_person_id="usr_admin")
    assert manager.get_task_objective_id(run.task_id) is None
    assert manager.get_task_plan_id(run.task_id) is None
    assert run.task_id not in manager.get_objective(team.objective_id).related_task_ids


def test_administrator_can_spawn_independent_objective_work_from_a_team_task(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    team = made["team"]
    _team_timer(manager, tasks, team, roles=("admin",))
    created = json.loads(create_objective(title="Separate", desired_outcome="x", why="y", creation_mode="spawn_task"))
    assert "error" not in created
    assert created["plan_id"] != team.current_plan_id
    assert manager.get_plan(team.current_plan_id).objective_id == team.objective_id
    assert manager.get_objective(team.objective_id).current_plan_id == team.current_plan_id


@pytest.mark.parametrize("operation", [update_heartbeat_item, remove_heartbeat_item, enable_heartbeat_item, disable_heartbeat_item])
def test_existing_private_heartbeat_link_is_checked_before_mutation(_people_and_objectives, operation) -> None:
    manager, tasks, made = _people_and_objectives
    _act_as(manager, tasks["a"])
    item = json.loads(add_heartbeat_item(instruction="A's private follow-up."))
    _act_as(manager, tasks["b"])
    assert json.loads(list_heartbeat_items())["items"] == []
    kwargs = {"instruction": "B's replacement."} if operation is update_heartbeat_item else {}
    refused = json.loads(operation(item["heartbeat_item_id"], **kwargs))
    if operation is remove_heartbeat_item:
        assert refused["removed"] is False
    else:
        assert refused["error"] == f"Heartbeat item not found: {item['heartbeat_item_id']}"
    store = HeartbeatStore(db_path=manager._db_path, storage_settings=manager.storage_settings)
    saved = store.get_item(item["heartbeat_item_id"])
    assert saved.instruction == "A's private follow-up."
    assert saved.enabled
    assert saved.objective_id == made["a"].objective_id
    # Naming a permitted replacement must not authorize changing the stored link.
    relink = json.loads(update_heartbeat_item(item["heartbeat_item_id"], objective_id=made["b"].objective_id))
    assert "not found" in relink["error"]


def test_team_heartbeat_is_readable_but_only_administrators_change_it(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    _act_as(manager, tasks["admin"])
    item = json.loads(add_heartbeat_item(instruction="Team check.", objective_id=made["team"].objective_id))
    _act_as(manager, tasks["a"])
    assert json.loads(list_heartbeat_items())["returned"] == 1
    assert "error" in json.loads(disable_heartbeat_item(item["heartbeat_item_id"]))
    _act_as(manager, tasks["admin"])
    assert json.loads(disable_heartbeat_item(item["heartbeat_item_id"]))["enabled"] is False


@pytest.mark.parametrize("objective_key,owner_key,allowed,signature", [
    ("a", "b", False, "current"), ("a", "a", True, "current"),
    ("team", "a", False, "current"), ("team", "admin", True, "current"),
    ("team", "admin", False, "member"), ("a", "a", False, "unsigned"),
])
def test_heartbeat_checks_its_objective_before_any_run_state_changes(
    _people_and_objectives, monkeypatch, caplog, objective_key, owner_key, allowed, signature,
) -> None:
    manager, tasks, made = _people_and_objectives
    use_scheduler_key(monkeypatch)
    owner_id = "usr_admin" if owner_key == "admin" else f"usr_{owner_key}"
    roles = ("admin",) if owner_key == "admin" else ("member",)
    AccessRepository(manager.storage_settings).record_person(TeamPersonV1(
        team_id="team_1", person_id=owner_id, standing=PersonStanding.ACTIVE,
        roles=roles, changed_at_utc=datetime.now(timezone.utc),
    ))
    store = HeartbeatStore(db_path=manager._db_path, storage_settings=manager.storage_settings)
    store.ensure_source(source_id="hb_source_objective", title="Shared checks")
    protected = store.add_item(
        source_id="hb_source_objective", title="Objective check", instruction="Private work.",
        conversation_id="protected_conversation", objective_id=made[objective_key].objective_id,
    )
    sibling = store.add_item(
        source_id="hb_source_objective", title="Shared check", instruction="Shared work.", conversation_id="shared_conversation",
    )
    launcher = ScheduledMessage(
        schedule_id="shared_launcher", schedule_name="heartbeat:shared", interval_ms=10_000,
        management_class=ScheduleManagementClass.SYSTEM, system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Tick"),
        payload_meta=manager.get_task_info(tasks[owner_key].task_id).meta.model_copy(deep=True),
    )
    agent = get_agent_ref()
    agent._logger = logging.getLogger("test.objective.heartbeat")
    agent._conversation_store = None
    agent.paused = False
    agent.is_executing = True
    # The wake-up reaches a mock drain, so the run stays queued for the test.
    agent._should_exit = asyncio.Event()
    agent._pending_work_task = None
    agent._continue_pending_work = AsyncMock()
    before = manager.get_objective(made[objective_key].objective_id).model_dump(mode="json")
    before_ids = set(manager.task_state.tasks)
    if signature != "current":
        async def sign_with_new_identity(_agent, meta, run):
            # A manager reply may reflect newer roles than preparation saw.
            # Produce a real, verifiable signature for those returned roles.
            meta.gateway_context.schedule_run = None
            meta.gateway_context.task_context = (
                scheduler_signer().issue(
                    team_id="team_1", person_id=owner_id, person_roles=("member",),
                    schedule_id=run.scheduled_job_id, conversation_id=meta.conversation_id,
                ) if signature == "member" else None
            )
            return True
        monkeypatch.setattr(scheduled_runtime, "sign_scheduled_run", sign_with_new_identity)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(enqueue_heartbeat_items(agent, launcher))
    runs = [info for key, info in manager.task_state.tasks.items() if key not in before_ids]
    assert {info.meta.gateway_context.scheduled_job_id for info in runs} == (
        {protected.heartbeat_item_id, sibling.heartbeat_item_id} if allowed else {sibling.heartbeat_item_id}
    )
    if not allowed:
        assert store.get_item(protected.heartbeat_item_id) == protected
        assert manager.get_objective(made[objective_key].objective_id).model_dump(mode="json") == before
        event = "heartbeat_objective_identity_unverified" if signature == "unsigned" else "heartbeat_objective_access_refused"
        assert f"event={event}" in caplog.text
        assert "Private work." not in caplog.text
