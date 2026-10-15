from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from custom_agents.task_manager import TaskManager
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, ScheduleManagementClass, SystemScheduleKind
from custom_tools.providers.heartbeat import add_heartbeat_item
from custom_tools.providers.objectives import (
    create_objective,
    objectives,
    start_objective_task,
    update_execution_plan_status,
)
from custom_tools.providers.schedules import (
    create_scheduled_job,
    list_scheduled_jobs,
)
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tools act for the person in the task's gateway-signed context."""

    use_signed_contexts(monkeypatch)


class _FakeAgent:
    def __init__(
        self,
        task_manager: TaskManager,
        *,
        agent_id: str = "manager_agent",
        name: str = "Manager",
        named_agent_type: str = "TeamArchitect",
    ):
        self.id = agent_id
        self.name = name
        self.named_agent_type = named_agent_type
        self.team_id = "team_1"
        # The per-call access hook is not part of these tests.
        self.authorization_config = {"enabled": False}
        self.task_manager = task_manager
        self._schedule_store = ScheduleStore(
            db_path=task_manager._db_path,
            storage_settings=task_manager.storage_settings,
        )
        self._scheduler_wake = type("FakeWake", (), {"set": lambda self: None})()


def _task_manager(
    tmp_path: Path, *, named_agent_type: str = "TeamArchitect"
) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_objective_tools"),
        storage_settings=settings,
    )
    manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type=named_agent_type,
    )
    return manager


def _add_current_task(manager: TaskManager) -> Task:
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Create a durable objective via tool.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(
            from_agent_name="user",
            conversation_id="conversation_1",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session_objective",
                canonical_user_id="objective-user",
                heartbeat_source_id="hb_source_objective",
                task_context=context_signer().issue(
                    team_id="team_1", person_id="objective-user", person_roles=("owner",)
                ),
            ),
        ),
        owner_person_id="objective-user",
    )
    manager.task_state.current_in_progress_task_id = task.task_id
    return task


def test_objective_tools_create_list_get_update_and_complete(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_current_task(manager)
    set_agent_ref(_FakeAgent(manager))

    created = json.loads(
        create_objective(
            title="Maintain objective coverage",
            desired_outcome="Objective tools can create and update durable Objective records.",
            why="The manager needs an explicit promotion mechanism.",
            horizon="long_horizon",
            priority="high",
            completion_policy="user_confirms",
            success_criteria_json=json.dumps(
                [
                    {
                        "criterion_id": "criterion_tools",
                        "description": "Tool path is covered.",
                    }
                ]
            ),
            constraints_json=json.dumps(
                ["Create only the plan needed for the Objective root."]
            ),
            requires_user_confirmation=True,
        )
    )

    objective_id = created["objective_id"]
    assert created["task_id"] == task.task_id
    assert created["created_by_task_id"] == task.task_id
    assert created["promoted_from_task_id"] == task.task_id
    assert created["creation_mode"] == "promote_task"
    assert created["plan_id"] is not None
    assert created["objective"]["horizon"] == "long_horizon"
    assert manager.get_task_plan_id(task.task_id) == created["plan_id"]

    listed = json.loads(objectives(action="list"))
    assert [row["objective_id"] for row in listed["objectives"]] == [objective_id]

    fetched = json.loads(objectives(action="get"))
    assert fetched["objective_id"] == objective_id
    assert "## Objective" in fetched["markdown"]

    updated = json.loads(
        objectives(action="update_progress", progress_summary="Tool objective progress updated.", current_focus="Complete tool-level tests.", criterion_updates_json=json.dumps(
                [
                    {
                        "criterion_id": "criterion_tools",
                        "status": "met",
                        "evidence_summary": "The test exercised the tool.",
                    }
                ]
            ))
    )
    assert (
        updated["objective"]["progress_summary"] == "Tool objective progress updated."
    )
    assert updated["objective"]["success_criteria"][0]["status"] == "met"

    blocked_completion = json.loads(objectives(action="update_status", status="satisfied"))
    assert "requires user confirmation" in blocked_completion["error"]

    completed = json.loads(
        objectives(action="update_status", status="satisfied", user_confirmation_summary="User confirmed tool objective completion.", satisfied_by_current_task=True)
    )
    assert completed["objective"]["status"] == "satisfied"
    assert completed["objective"]["satisfied_by_task_id"] == task.task_id


def test_objective_tools_continue_across_task_and_plan_attempt_failures(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    first_task = _add_current_task(manager)
    set_agent_ref(_FakeAgent(manager))
    created = json.loads(
        create_objective(
            title="Continue durable Objective",
            desired_outcome="Replacement Tasks continue the Objective-owned Plan.",
            why="Execution attempts are replaceable.",
            horizon="long_horizon",
        )
    )
    first_plan_id = created["plan_id"]

    manager.fail_task(
        first_task.task_id,
        "First execution attempt failed.",
        "2026-05-23T10:00:00+00:00",
    )
    controller = Task(
        task_id="task_controller",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Continue an existing Objective.",
    )
    assert manager.add_task(
        controller,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_2"),
        owner_person_id=None,
    )
    manager.task_state.current_in_progress_task_id = controller.task_id

    continued = json.loads(
        start_objective_task(
            objective_id=created["objective_id"],
            work_brief="Resume the unfinished Plan.",
        )
    )
    assert continued["queued"] is True
    assert continued["plan_id"] == first_plan_id
    assert manager.get_task_plan_id(continued["task_id"]) == first_plan_id

    failed_plan = json.loads(
        update_execution_plan_status(
            "failed",
            "This Plan attempt is no longer viable.",
            plan_id=first_plan_id,
        )
    )
    assert failed_plan["plan"]["status"] == "failed"
    assert manager.get_objective(created["objective_id"]).status.value == "active"

    restarted = json.loads(
        start_objective_task(
            objective_id=created["objective_id"],
            work_brief="Start a revised Plan attempt.",
        )
    )
    assert restarted["queued"] is True
    assert restarted["plan_id"] != first_plan_id
    assert manager.get_plan(restarted["plan_id"]).status.value == "active"


def test_objective_tools_are_manager_only(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path, named_agent_type="CodeWritingAgent")
    _add_current_task(manager)
    set_agent_ref(_FakeAgent(manager, named_agent_type="CodeWritingAgent"))

    result = json.loads(
        create_objective(
            title="Specialist objective",
            desired_outcome="Should not be created.",
            why="Specialists do not own Objective primitives.",
        )
    )

    assert result["manager_only"] is True
    assert "manager-only" in result["error"]


def test_objective_links_schedule_and_heartbeat_lifecycle(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)

    created = json.loads(
        create_objective(
            title="Run recurring objective checks",
            desired_outcome="Recurring schedule and heartbeat work remain tied to one Objective.",
            why="Recurring work is the core long-standing objective use case.",
            horizon="recurring",
        )
    )
    objective_id = created["objective_id"]

    scheduled = json.loads(
        create_scheduled_job(
            instruction="Check objective status.",
            schedule_name="objective schedule",
            interval_seconds=60,
        )
    )
    schedule_id = scheduled["schedule_id"]
    assert scheduled["objective_id"] == objective_id
    stored_schedule = agent._schedule_store.get_scheduled_message(schedule_id)
    assert stored_schedule is not None
    assert stored_schedule.objective_id == objective_id
    assert stored_schedule.payload_meta.objective_id == objective_id

    listing = json.loads(list_scheduled_jobs())
    assert listing["schedules"][0]["objective_id"] == objective_id

    heartbeat = json.loads(
        add_heartbeat_item(
            instruction="Run heartbeat objective check.",
            title="Objective heartbeat",
        )
    )
    heartbeat_item_id = heartbeat["heartbeat_item_id"]
    assert heartbeat["objective_id"] == objective_id

    objective = manager.get_objective(objective_id)
    assert objective is not None
    assert [link.schedule_id for link in objective.schedule_links] == [schedule_id]
    assert [link.heartbeat_item_id for link in objective.heartbeat_links] == [
        heartbeat_item_id
    ]

    paused = json.loads(objectives(action="update_status", status="paused", objective_id=objective_id))
    assert paused["objective"]["status"] == "paused"
    assert agent._schedule_store.get_scheduled_message(schedule_id).enabled is False
    heartbeat_store = HeartbeatStore(
        db_path=manager._db_path,
        storage_settings=manager.storage_settings,
    )
    assert heartbeat_store.get_item(heartbeat_item_id).enabled is False

    resumed = json.loads(objectives(action="update_status", status="active", objective_id=objective_id))
    assert resumed["objective"]["status"] == "active"
    assert agent._schedule_store.get_scheduled_message(schedule_id).enabled is True
    assert heartbeat_store.get_item(heartbeat_item_id).enabled is True
    assert manager.get_task_objective_id(task.task_id) == objective_id


def test_objective_lifecycle_cannot_disable_linked_system_schedule(
    tmp_path: Path,
) -> None:
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    created = json.loads(
        create_objective(
            title="Protect runtime schedule",
            desired_outcome="Objective changes cannot bypass system schedule authority.",
            why="Runtime infrastructure has a separate authorization boundary.",
        )
    )
    objective_id = created["objective_id"]
    schedule_id = "hb_sched_objective_guard"
    agent._schedule_store.add(
        ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name="heartbeat:objective-guard",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
            objective_id=objective_id,
            claim_team_id="team_1",
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement="Launch heartbeat work.",
            ),
            payload_meta=MessageMeta(
                objective_id=objective_id,
                gateway_context=GatewayContext(
                    canonical_user_id="objective-user",
                ),
            ),
            interval_ms=60_000,
        )
    )
    manager.link_schedule_to_objective(
        objective_id,
        schedule_id,
        managed_by_objective=True,
    )

    paused = json.loads(objectives(action="update_status", status="paused", objective_id=objective_id))

    restored = agent._schedule_store.get_scheduled_message(schedule_id)
    assert paused["objective"]["status"] == "paused"
    assert restored is not None
    assert restored.enabled is True
    objective = manager.get_objective(objective_id)
    assert objective is not None
    assert objective.schedule_links[0].disabled_by_objective_pause is False
