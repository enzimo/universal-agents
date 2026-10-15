import logging
from pathlib import Path

from custom_messages.contracts.task_v1 import ExecutionPlanItem
from custom_messages.enums import PlanActionType, PlanStatus
from custom_messages.records.plans import PlanActionRecord, PlanRecord
from storage.config import resolve_storage_settings
from storage.plan_repository import create_plan_repository
from tests.postgres_test_helpers import patch_fake_postgres


def test_plan_repository_saves_and_loads_sqlite_plan(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_plan_repository(settings, logger=logging.getLogger("test"))

    plan = PlanRecord(
        plan_id="plan_id_test",
        root_task_id="task_root",
        root_conversation_id="conversation_1",
        owner_agent_id="agent_a",
        owner_agent_name="Agent A",
        team_id="team_1",
        owner_named_agent_type="TeamArchitect",
        status=PlanStatus.ACTIVE,
        active_execution_task_id="task_continuation",
        objective_id="objective_id_test",
        objective_title="Durable objective",
        goal_summary="Solve the user request.",
        plan_summary="Work the plan.",
        current_focus="Repository test",
        plan_items=[
            ExecutionPlanItem(
                step_id="plan_step_1",
                title="Persist plan",
                rationale="The plan must be reloadable independently from the task.",
            )
        ],
        related_task_ids=["task_root", "task_continuation"],
        plan_actions=[
            PlanActionRecord(
                action_type=PlanActionType.CHECKPOINT,
                summary="Repository action checkpoint.",
            )
        ],
    )

    repo.save_plan(plan)

    loaded = repo.get_plan("plan_id_test", owner_agent_id="agent_a")
    assert loaded is not None
    assert loaded.plan_summary == "Work the plan."
    assert loaded.objective_id == "objective_id_test"
    assert loaded.plan_items[0].rationale.startswith("The plan must be reloadable")
    assert loaded.plan_actions[0].summary == "Repository action checkpoint."
    assert loaded.active_execution_task_id == "task_continuation"

    by_root = repo.get_plan_by_root_task("task_root", owner_agent_id="agent_a")
    assert by_root is not None
    assert by_root.plan_id == "plan_id_test"

    by_continuation = repo.get_plan_by_task(
        "task_continuation",
        owner_agent_id="agent_a",
    )
    assert by_continuation is not None
    assert by_continuation.plan_id == "plan_id_test"

    listed = repo.list_plans(limit=10, owner_agent_id="agent_a")
    assert [row.plan_id for row in listed] == ["plan_id_test"]
    assert listed[0].objective_id == "objective_id_test"


def test_plan_repository_scopes_same_plan_id_by_owner(tmp_path: Path) -> None:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    repo = create_plan_repository(settings, logger=logging.getLogger("test"))

    repo.save_plan(
        PlanRecord(
            plan_id="plan_id_shared",
            owner_agent_id="manager_agent",
            root_task_id="task_parent",
            plan_summary="Manager plan",
        )
    )
    repo.save_plan(
        PlanRecord(
            plan_id="plan_id_shared",
            owner_agent_id="manager_agent_b",
            root_task_id="task_child",
            plan_summary="Second manager plan",
        )
    )

    manager_plan = repo.get_plan(
        "plan_id_shared",
        owner_agent_id="manager_agent",
    )
    second_manager_plan = repo.get_plan(
        "plan_id_shared",
        owner_agent_id="manager_agent_b",
    )

    assert manager_plan is not None
    assert second_manager_plan is not None
    assert manager_plan.root_task_id == "task_parent"
    assert second_manager_plan.root_task_id == "task_child"


def test_plan_repository_accepts_postgres_when_driver_is_available(tmp_path: Path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_plan_repository(settings, logger=logging.getLogger("test"))
    assert repo is not None
