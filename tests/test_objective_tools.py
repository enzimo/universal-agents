from __future__ import annotations

import json
import logging
import sqlite3
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


def _add_person_task(
    manager: TaskManager,
    task_id: str,
    person_id: str,
    *,
    conversation_id: str,
    roles: tuple[str, ...] = ("owner",),
) -> Task:
    """A task one person's gateway request started, with its signed context."""

    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Create a durable objective via tool.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(
            from_agent_name="user",
            conversation_id=conversation_id,
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key=f"session_{person_id}",
                canonical_user_id=person_id,
                heartbeat_source_id="hb_source_objective",
                task_context=context_signer().issue(
                    team_id="team_1", person_id=person_id, person_roles=roles
                ),
            ),
        ),
        owner_person_id=person_id,
    )
    return task


def _add_current_task(manager: TaskManager) -> Task:
    task = _add_person_task(manager, "task_root", "objective-user", conversation_id="conversation_1")
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
    # A later request from the same person continues the Objective.
    controller = _add_person_task(
        manager, "task_controller", "objective-user", conversation_id="conversation_2"
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
    assert paused["lifecycle_warnings"] == [
        f"Schedule {schedule_id} is managed by the system, so this status change of Objective "
        f"{objective_id} left it as it was. It changes only through a system schedule change "
        "(apply_change) or the person's Runtime controls; tell the person if it should follow."
    ]
    assert restored is not None
    assert restored.enabled is True
    objective = manager.get_objective(objective_id)
    assert objective is not None
    assert objective.schedule_links[0].disabled_by_objective_pause is False


def _add_no_person_task(manager: TaskManager, task_id: str, *, signed: bool = True) -> Task:
    """A task that works for no person; unsigned, it has no verified identity at all."""

    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="System work.")
    gateway_context = (
        GatewayContext(
            session_key="session_system",
            task_context=context_signer().issue(team_id="team_1", person_id=None),
        )
        if signed
        else None
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="system", agent_signature=""),
        MessageMeta(from_agent_name="system", conversation_id=f"conversation_{task_id}", gateway_context=gateway_context),
        owner_person_id=None,
    )
    return task


def _objective_from(manager: TaskManager, task: Task, title: str):
    """Trusted code makes the Objective; its owner is the task's person."""

    objective = manager.create_objective(
        task_id=task.task_id, title=title, desired_outcome=f"{title} is done.", why="Visibility test."
    )
    assert objective is not None
    return objective


def _act_as(manager: TaskManager, task: Task) -> None:
    manager.task_state.current_in_progress_task_id = task.task_id


def _listed(**filters) -> set[str]:
    return {row["objective_id"] for row in json.loads(objectives(action="list", **filters))["objectives"]}


_NO_OBJECTIVE = "No objective could be resolved from the supplied context."


@pytest.fixture
def _people_and_objectives(tmp_path: Path):
    """Two members, an administrator and system work, each with an Objective of their own."""

    manager = _task_manager(tmp_path)
    set_agent_ref(_FakeAgent(manager))
    tasks = {
        "a": _add_person_task(manager, "task_a", "usr_a", conversation_id="conv_a", roles=("member",)),
        "b": _add_person_task(manager, "task_b", "usr_b", conversation_id="conv_b", roles=("member",)),
        "admin": _add_person_task(manager, "task_admin", "usr_admin", conversation_id="conv_admin", roles=("admin",)),
        "system": _add_no_person_task(manager, "task_system"),
    }
    made = {
        "a": _objective_from(manager, tasks["a"], "Person A's Objective"),
        "b": _objective_from(manager, tasks["b"], "Person B's Objective"),
        "team": _objective_from(manager, tasks["system"], "The team's Objective"),
    }
    assert made["a"].owner_canonical_user_id == "usr_a"
    assert made["team"].owner_canonical_user_id is None
    return manager, tasks, made


def test_a_member_sees_their_own_and_the_teams_objectives_only(_people_and_objectives, caplog) -> None:
    manager, tasks, made = _people_and_objectives
    a, b, team = made["a"], made["b"], made["team"]
    _act_as(manager, tasks["a"])

    assert _listed(include_closed=True) == {a.objective_id, team.objective_id}
    assert json.loads(objectives(action="get", objective_id=team.objective_id))["objective_id"] == team.objective_id
    # Another person's Objective is refused as if it did not exist, however it is named.
    with caplog.at_level("INFO", logger="access_control.ownership"):
        by_id = json.loads(objectives(action="get", objective_id=b.objective_id))
    assert (by_id["objective"], by_id["error"]) == (None, _NO_OBJECTIVE)
    assert f"resource_id={b.objective_id}" in caplog.text and "event=private_access_refused" in caplog.text
    assert json.loads(objectives(action="get", plan_id=b.current_plan_id))["error"] == _NO_OBJECTIVE
    assert json.loads(objectives(action="get", task_id=tasks["b"].task_id))["error"] == _NO_OBJECTIVE

    refused = json.loads(
        objectives(action="update_progress", objective_id=b.objective_id, progress_summary="Changed by A.")
    )
    assert refused["error"] == _NO_OBJECTIVE
    assert manager.get_objective(b.objective_id).progress_summary != "Changed by A."
    assert json.loads(objectives(action="update_progress", objective_id=a.objective_id, progress_summary="Mine."))[
        "objective"
    ]["progress_summary"] == "Mine."
    assert json.loads(start_objective_task(work_brief="Continue B.", objective_id=b.objective_id))["error"] == _NO_OBJECTIVE
    heartbeat = json.loads(add_heartbeat_item(instruction="Check B.", objective_id=b.objective_id))
    assert "Unknown Objective" in heartbeat["error"]
    assert manager.get_objective(b.objective_id).heartbeat_links == []


def test_a_member_reads_but_does_not_change_the_teams_objectives(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    team = made["team"]
    _act_as(manager, tasks["a"])

    paused = json.loads(objectives(action="update_status", objective_id=team.objective_id, status="paused", summary="A pauses it."))
    assert paused["error"] == _NO_OBJECTIVE
    assert manager.get_objective(team.objective_id).status.value == "active"
    continued = json.loads(start_objective_task(work_brief="Continue the team's.", objective_id=team.objective_id))
    assert continued["error"] == _NO_OBJECTIVE


def test_an_administrator_and_system_work_change_the_teams_objectives(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    a, team = made["a"], made["team"]

    _act_as(manager, tasks["admin"])
    assert _listed() == {team.objective_id}  # administrators see no one else's
    paused = json.loads(objectives(action="update_status", objective_id=team.objective_id, status="paused", summary="Admin pauses it."))
    assert paused["objective"]["status"] == "paused"
    assert json.loads(objectives(action="get", objective_id=a.objective_id))["error"] == _NO_OBJECTIVE

    _act_as(manager, tasks["system"])
    assert _listed(include_closed=True) == {team.objective_id}
    progressed = json.loads(objectives(action="update_progress", objective_id=team.objective_id, progress_summary="System work."))
    assert progressed["objective"]["progress_summary"] == "System work."
    assert json.loads(objectives(action="get", objective_id=a.objective_id))["error"] == _NO_OBJECTIVE


def test_a_task_without_a_verified_identity_reaches_no_objective(_people_and_objectives, caplog) -> None:
    manager, _tasks, made = _people_and_objectives
    _act_as(manager, _add_no_person_task(manager, "task_unsigned", signed=False))

    with caplog.at_level("WARNING", logger="custom_tools.providers._runtime"):
        assert _listed(include_closed=True) == set()
        refused = json.loads(objectives(action="update_progress", objective_id=made["team"].objective_id, progress_summary="?"))
    assert refused["error"] == _NO_OBJECTIVE
    assert "event=task_identity_unverified" in caplog.text


def test_a_task_names_only_its_own_persons_tasks_as_a_source(_people_and_objectives) -> None:
    manager, tasks, made = _people_and_objectives
    a, b = made["a"], made["b"]
    _act_as(manager, tasks["a"])
    count_before = len(manager.objective_repository.list_objectives(limit=100, owner_agent_id=manager.durable_manager_scope_id()))

    # An Objective made from B's task would be B's; a continuation from it would work for B.
    created = json.loads(create_objective(title="Made from B", desired_outcome="x", why="y", task_id=tasks["b"].task_id))
    assert created["error"] == f"Task {tasks['b'].task_id} was not found among this person's tasks."
    continued = json.loads(
        start_objective_task(work_brief="Continue as B.", objective_id=a.objective_id, source_task_id=tasks["b"].task_id)
    )
    assert "was not found among this person's tasks" in continued["error"]
    noted = json.loads(
        objectives(action="update_progress", objective_id=a.objective_id, task_id=tasks["b"].task_id, progress_summary="x")
    )
    assert "was not found among this person's tasks" in noted["error"]
    assert len(manager.objective_repository.list_objectives(limit=100, owner_agent_id=manager.durable_manager_scope_id())) == count_before

    # Plans: another person's plan is out of view, and a named Objective never falls back to the task's plan.
    by_plan = json.loads(update_execution_plan_status("failed", "A fails B's plan.", plan_id=b.current_plan_id))
    assert by_plan["error"] == "No plan could be resolved from the supplied context."
    by_objective = json.loads(update_execution_plan_status("failed", "A fails B's plan.", objective_id=b.objective_id))
    assert by_objective["error"] == "No plan could be resolved from the supplied context."
    assert manager.get_plan(b.current_plan_id).status.value == "active"
    assert manager.get_plan(a.current_plan_id).status.value == "active"


def test_a_finished_tasks_plan_stays_its_persons_after_a_team_lead_restart(_people_and_objectives) -> None:
    """The plan's root task was archived by an earlier team lead process under another agent id."""

    from storage.sqlite_connection import connect_sqlite

    manager, tasks, _made = _people_and_objectives
    done = _add_person_task(manager, "task_a_done", "usr_a", conversation_id="conv_a_done", roles=("member",))
    assert manager.set_task_plan(done.task_id, "Finish the report.") is not None
    plan_id = manager.get_task_plan_id(done.task_id)
    # Archived while its plan stays open (a plan outlives its task).
    manager._save_completed_task(done.task_id, "completed")
    assert manager.get_task_info(done.task_id) is None
    assert manager.get_plan(plan_id).status.value == "active"
    with connect_sqlite(manager._db_path) as conn:
        conn.execute("UPDATE completed_tasks SET owner_agent_id = 'earlier_process' WHERE task_id = ?", (done.task_id,))
        conn.commit()

    _act_as(manager, tasks["b"])
    refused = json.loads(update_execution_plan_status("cancelled", "B cancels A's plan.", plan_id=plan_id))
    assert refused["error"] == "No plan could be resolved from the supplied context."

    _act_as(manager, tasks["a"])
    cancelled = json.loads(update_execution_plan_status("cancelled", "A cancels their own plan.", plan_id=plan_id))
    assert cancelled["plan"]["status"] == "cancelled"


def _break_objective_saves(monkeypatch: pytest.MonkeyPatch, manager: TaskManager) -> None:
    """Every Objective write fails, as when the database is locked or the disk is full."""

    def fail(objective) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(manager.objective_repository, "save_objective", fail)


def _not_saved(objective_id: str) -> str:
    return (
        f"The change to Objective {objective_id} was not saved. "
        "Try again later; if it keeps failing, tell the person."
    )


def test_objective_changes_that_were_not_saved_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    manager = _task_manager(tmp_path)
    current = _add_current_task(manager)
    set_agent_ref(_FakeAgent(manager))
    created = json.loads(create_objective(title="Write the report", desired_outcome="A report.", why="Asked."))
    objective_id, plan_id = created["objective_id"], created["plan_id"]
    _break_objective_saves(monkeypatch, manager)

    with caplog.at_level("ERROR", logger="test_objective_tools"):
        progressed = json.loads(
            objectives(action="update_progress", objective_id=objective_id, progress_summary="Half done.")
        )
    assert progressed == {"error": _not_saved(objective_id)}
    assert f"event=objective_save_failed objective_id={objective_id}" in caplog.text
    closed = json.loads(objectives(action="update_status", status="cancelled", objective_id=objective_id))
    assert closed == {"error": _not_saved(objective_id)}
    stored = manager.get_objective(objective_id)
    assert (stored.progress_summary, stored.status.value) == (None, "active")
    # The Plan closes only after its Objective's new status is saved.
    assert manager.get_plan(plan_id).status.value == "active"

    # A continuation its Objective cannot record stops before it runs.
    continued = json.loads(start_objective_task(objective_id=objective_id, work_brief="Keep going."))
    assert continued == {"error": _not_saved(objective_id)}
    still_open = [
        info.task.task_id
        for info in manager.task_state.tasks.values()
        if not manager.is_terminal_state(info.state)
    ]
    assert still_open == [current.task_id]


def test_a_schedule_or_heartbeat_item_its_objective_could_not_record_stays_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    created = json.loads(
        create_objective(title="Check daily", desired_outcome="Checked.", why="Asked.", horizon="recurring")
    )
    _break_objective_saves(monkeypatch, manager)

    scheduled = json.loads(create_scheduled_job(instruction="Check.", schedule_name="daily", interval_seconds=60))
    assert agent._schedule_store.get_scheduled_message(scheduled["schedule_id"]) is not None
    assert "could not record it" in scheduled["objective_link_warning"]
    heartbeat = json.loads(add_heartbeat_item(instruction="Check.", title="Check"))
    assert heartbeat["heartbeat_item_id"]
    assert "could not record it" in heartbeat["objective_link_warning"]
    stored = manager.get_objective(created["objective_id"])
    assert (stored.schedule_links, stored.heartbeat_links) == ([], [])


def test_a_task_ends_normally_when_its_objective_review_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    manager = _task_manager(tmp_path)
    task = _add_current_task(manager)
    set_agent_ref(_FakeAgent(manager))
    created = json.loads(create_objective(title="Try once", desired_outcome="Tried.", why="Asked."))
    _break_objective_saves(monkeypatch, manager)

    with caplog.at_level("WARNING", logger="test_objective_tools"):
        manager.fail_task(task.task_id, "The attempt failed.", "2026-10-16T10:00:00+00:00")

    assert manager.get_task_info(task.task_id) is None or manager.is_terminal_state(
        manager.get_task_info(task.task_id).state
    )
    assert f"event=objective_save_skipped objective_id={created['objective_id']} step=task_end_review:" in caplog.text


def _recurring_objective_with_schedule(manager: TaskManager, agent: _FakeAgent) -> tuple[str, str]:
    created = json.loads(
        create_objective(
            title="Check the feed",
            desired_outcome="The feed is checked every minute.",
            why="Recurring work belongs to one Objective.",
            horizon="recurring",
        )
    )
    scheduled = json.loads(
        create_scheduled_job(instruction="Check the feed.", schedule_name="feed", interval_seconds=60)
    )
    return created["objective_id"], scheduled["schedule_id"]


def test_the_status_tool_keeps_changes_stored_while_it_switches_links(tmp_path: Path) -> None:
    """A progress update or a schedule linked while the pause switches links survives.

    The tool used to save the copy it loaded before switching the links, which
    erased anything stored in between (a parallel tool call, or work the
    manager's event loop finished meanwhile).
    """
    manager = _task_manager(tmp_path)
    task = _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    objective_id, schedule_id = _recurring_objective_with_schedule(manager, agent)
    switch = agent._schedule_store.set_enabled

    def switch_while_others_write(*args, **kwargs):
        manager.update_objective_progress(objective_id, progress_summary="Half the feed checked.", task_id=task.task_id)
        manager.link_schedule_to_objective(objective_id, "sched_linked_meanwhile")
        return switch(*args, **kwargs)

    agent._schedule_store.set_enabled = switch_while_others_write

    paused = json.loads(objectives(action="update_status", status="paused", objective_id=objective_id))

    assert paused["objective"]["status"] == "paused"
    stored = manager.get_objective(objective_id)
    assert stored.progress_summary == "Half the feed checked."
    assert any(event.summary == "Updated objective progress." for event in stored.event_log)
    links = {link.schedule_id: link.disabled_by_objective_pause for link in stored.schedule_links}
    assert links == {schedule_id: True, "sched_linked_meanwhile": False}


@pytest.mark.parametrize("status", ["deleting", "deleted"])
def test_the_status_tool_refuses_deletion_statuses(tmp_path: Path, status: str) -> None:
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    objective_id, schedule_id = _recurring_objective_with_schedule(manager, agent)

    refused = json.loads(objectives(action="update_status", status=status, objective_id=objective_id))

    assert refused["error"].startswith("Only the person can delete an Objective")
    assert "deleted" not in refused["allowed_statuses"] and "deleting" not in refused["allowed_statuses"]
    assert manager.get_objective(objective_id).status.value == "active"
    assert agent._schedule_store.get_scheduled_message(schedule_id).enabled is True


def test_the_status_tool_leaves_an_objective_its_person_is_deleting(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    objective_id, _schedule_id = _recurring_objective_with_schedule(manager, agent)
    manager.update_objective_status(objective_id, "deleting", summary="Deletion requested.")

    refused = json.loads(objectives(action="update_status", status="active", objective_id=objective_id))

    assert refused["error"].endswith("is being deleted by its person, so it can no longer change.")
    assert manager.get_objective(objective_id).status.value == "deleting"


def test_the_status_tool_reports_a_linked_heartbeat_item_that_is_gone(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    objective_id, _schedule_id = _recurring_objective_with_schedule(manager, agent)
    heartbeat_item_id = json.loads(add_heartbeat_item(instruction="Check.", title="Check"))["heartbeat_item_id"]
    HeartbeatStore(db_path=manager._db_path, storage_settings=manager.storage_settings).remove_item(heartbeat_item_id)

    paused = json.loads(objectives(action="update_status", status="paused", objective_id=objective_id))

    assert paused["objective"]["status"] == "paused"
    assert paused["lifecycle_warnings"] == [
        f"The linked heartbeat {heartbeat_item_id} could not be turned on or off with Objective "
        f"{objective_id}; operator review is required."
    ]
    # Not recorded as paused by the Objective, so a resume does not claim it either.
    assert [link.disabled_by_objective_pause for link in manager.get_objective(objective_id).heartbeat_links] == [False]


def test_the_status_tool_reports_a_schedule_it_could_not_switch(tmp_path: Path, caplog) -> None:
    """An error while switching a link is reported; the saved status and the other links stand.

    The error used to escape after the status was saved: the tool answered
    with an error and the link stayed unmarked, so a resume would not undo it.
    """
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    objective_id, schedule_id = _recurring_objective_with_schedule(manager, agent)
    heartbeat_item_id = json.loads(add_heartbeat_item(instruction="Check.", title="Check"))["heartbeat_item_id"]
    store = agent._schedule_store

    def unavailable(*_args, **_kwargs):
        raise ValueError("ScheduleStore not initialized on this agent.")

    store.set_enabled = unavailable

    with caplog.at_level("WARNING", logger="custom_agents.objective_lifecycle"):
        paused = json.loads(objectives(action="update_status", status="paused", objective_id=objective_id))

    assert paused["objective"]["status"] == "paused"
    assert paused["lifecycle_warnings"] == [
        f"The linked schedule {schedule_id} could not be turned on or off with Objective "
        f"{objective_id}; operator review is required."
    ]
    assert (
        f"event=objective_control_link_switch_failed objective_id={objective_id} "
        f"kind=schedule item_id={schedule_id}"
    ) in caplog.text
    stored = manager.get_objective(objective_id)
    assert [link.disabled_by_objective_pause for link in stored.schedule_links] == [False]
    assert [link.disabled_by_objective_pause for link in stored.heartbeat_links] == [True]
    assert HeartbeatStore(db_path=manager._db_path, storage_settings=manager.storage_settings).get_item(
        heartbeat_item_id
    ).enabled is False


def test_the_status_tool_warns_when_switched_links_are_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _task_manager(tmp_path)
    _add_current_task(manager)
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    objective_id, schedule_id = _recurring_objective_with_schedule(manager, agent)
    save = manager.objective_repository.save_objective

    def fail_when_recording_links(record) -> None:
        if any(link.disabled_by_objective_pause for link in record.schedule_links):
            raise sqlite3.OperationalError("disk I/O error")
        save(record)

    monkeypatch.setattr(manager.objective_repository, "save_objective", fail_when_recording_links)

    paused = json.loads(objectives(action="update_status", status="paused", objective_id=objective_id))

    assert paused["objective"]["status"] == "paused"
    assert paused["lifecycle_warnings"] == [
        f"Objective {objective_id} could not record which schedules and heartbeat items it "
        "paused or resumed, so resuming it will not restart the ones it paused. Tell the person."
    ]
    assert agent._schedule_store.get_scheduled_message(schedule_id).enabled is False
    assert manager.get_objective(objective_id).schedule_links[0].disabled_by_objective_pause is False
