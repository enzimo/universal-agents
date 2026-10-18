from __future__ import annotations

import logging
from pathlib import Path

import pytest

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ExecutionPlanItemStatus, MessageMetaType, ObjectiveCompletionPolicy, ObjectiveCriterionStatus, ObjectiveEventType, ObjectiveHorizon, ObjectiveStatus, PlanStatus
from custom_messages.records.plans import PlanRecord
from storage.config import resolve_storage_settings
from storage.objective_repository import ObjectiveView
from storage.sqlite_connection import connect_sqlite


def _task_manager(tmp_path: Path, manager_type: type[TaskManager] = TaskManager) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = manager_type(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_objective_primitive"),
        storage_settings=settings,
    )
    manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    return manager


def _add_root_task(manager: TaskManager, task_id: str = "task_root") -> Task:
    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Track a demand that may outlive a single task.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    return task


def test_create_objective_promotes_task_and_creates_initial_plan(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)

    objective = manager.create_objective(
        task_id=task.task_id,
        title="Maintain weekly research coverage",
        desired_outcome="A durable stream of research updates is maintained.",
        why="The demand is recurring and cannot be represented by one task result.",
        horizon=ObjectiveHorizon.RECURRING,
        success_criteria=["The recurring review stays active."],
    )

    assert objective is not None
    assert objective.current_plan_id is not None
    assert manager.get_task_plan_id(task.task_id) == objective.current_plan_id
    assert manager.get_task_objective_id(task.task_id) == objective.objective_id
    assert manager.get_objective(objective.objective_id).related_task_ids == [task.task_id]
    assert task.execution_history[-1].objective_id == objective.objective_id
    plan = manager.get_plan(objective.current_plan_id)
    assert plan is not None
    assert plan.objective_id == objective.objective_id
    assert plan.root_task_id == task.task_id
    # The task works for no person, so this is a team Objective.
    assert manager.list_objectives(view=ObjectiveView(person_id=None))[0].objective_id == objective.objective_id


def test_replacement_manager_adopts_durable_objective_and_plan_scope(
    tmp_path: Path,
) -> None:
    original = _task_manager(tmp_path)
    task = _add_root_task(original)
    objective = original.create_objective(
        task_id=task.task_id,
        title="Survive manager replacement",
        desired_outcome="The replacement manager continues the durable work.",
        why="Process ids are not durable ownership identities.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
    )
    assert objective is not None
    assert objective.current_plan_id is not None

    settings = original.storage_settings
    replacement = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_objective_replacement_manager"),
        storage_settings=settings,
    )
    replacement.set_owner_identity(
        agent_id="manager_replacement",
        agent_name="Replacement Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )

    restored_objective = replacement.get_objective(objective.objective_id)
    restored_plan = replacement.get_plan(objective.current_plan_id)
    assert replacement.durable_manager_scope_id() == (
        "manager_scope:team_1:TeamArchitect"
    )
    assert restored_objective is not None
    assert restored_objective.owner_agent_id == replacement.durable_manager_scope_id()
    assert restored_plan is not None
    assert restored_plan.owner_agent_id == replacement.durable_manager_scope_id()


def test_replacement_manager_reconciles_runtime_owned_objective_and_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_owner = "manager_before_durable_scope"

    class LegacyTaskManager(TaskManager):
        legacy_ownership = False

        def durable_manager_scope_id(self) -> str:
            return legacy_owner if self.legacy_ownership else super().durable_manager_scope_id()

    original = _task_manager(tmp_path, LegacyTaskManager)
    original.set_owner_identity(
        agent_id=legacy_owner,
        agent_name="Original Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    original.legacy_ownership = True
    task = _add_root_task(original)
    objective = original.create_objective(
        task_id=task.task_id,
        title="Repair runtime-owned durable work",
        desired_outcome="A replacement manager resumes the same Objective.",
        why="The original row predates durable manager-role ownership.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
    )
    assert objective is not None
    assert objective.current_plan_id is not None
    assert objective.owner_agent_id == legacy_owner

    settings = original.storage_settings
    other_team = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_objective_other_team"),
        storage_settings=settings,
    )
    other_team.set_owner_identity(
        agent_id="other_team_manager",
        agent_name="Other Team Manager",
        team_id="team_2",
        named_agent_type="TeamArchitect",
    )
    assert other_team.get_objective(objective.objective_id) is None

    replacement = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_objective_legacy_replacement"),
        storage_settings=settings,
    )
    replacement.set_owner_identity(
        agent_id="manager_after_restart",
        agent_name="Replacement Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )

    restored_objective = replacement.get_objective(objective.objective_id)
    restored_plan = replacement.get_plan(objective.current_plan_id)
    assert restored_objective is not None
    assert restored_objective.owner_agent_id == "manager_scope:team_1:TeamArchitect"
    assert restored_plan is not None
    assert restored_plan.owner_agent_id == "manager_scope:team_1:TeamArchitect"
    assert original.objective_repository.get_objective(
        objective.objective_id,
        owner_agent_id=legacy_owner,
    ) is None

    second_replacement = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_objective_legacy_second_replacement"),
        storage_settings=settings,
    )
    second_replacement.set_owner_identity(
        agent_id="manager_after_second_restart",
        agent_name="Second Replacement Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    second_restore = second_replacement.get_objective(objective.objective_id)
    assert second_restore is not None
    assert second_restore.owner_agent_id == "manager_scope:team_1:TeamArchitect"


def test_replacement_manager_archives_duplicate_legacy_plan_snapshot(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = _task_manager(tmp_path)
    plan_id = "plan_id_duplicate_legacy"
    shared_updated_at = "2026-06-10T00:02:37.660873+00:00"
    older = PlanRecord(
        plan_id=plan_id,
        root_task_id="task_duplicate_legacy",
        owner_agent_id="agent_id_legacy_older",
        owner_agent_name="Older Manager",
        team_id="team_1",
        owner_named_agent_type="TeamArchitect",
        status=PlanStatus.ACTIVE,
        created_at="2026-06-10T00:02:37.668792+00:00",
        updated_at=shared_updated_at,
    )
    newer = older.model_copy(deep=True)
    newer.owner_agent_id = "agent_id_legacy_newer"
    newer.owner_agent_name = "Newer Manager"
    newer.status = PlanStatus.FAILED
    newer.created_at = "2026-06-10T00:13:31.978202+00:00"
    original.plan_repository.save_plan(older)
    original.plan_repository.save_plan(newer)

    replacement = TaskManager(
        db_path=original.storage_settings.sqlite_path,
        logger=logging.getLogger("test_duplicate_legacy_plan"),
        storage_settings=original.storage_settings,
    )
    with caplog.at_level(logging.WARNING):
        replacement.set_owner_identity(
            agent_id="manager_after_duplicate",
            agent_name="Replacement Manager",
            team_id="team_1",
            named_agent_type="TeamArchitect",
        )

    durable_owner = "manager_scope:team_1:TeamArchitect"
    restored = replacement.get_plan(plan_id)
    assert restored is not None
    assert restored.owner_agent_id == durable_owner
    assert restored.status == PlanStatus.FAILED
    assert restored.created_at == newer.created_at
    assert "event=durable_manager_scope_collision_archived" in caplog.text

    with connect_sqlite(original.storage_settings.sqlite_path or "") as conn:
        plan_rows = conn.execute(
            "SELECT plan_record_id, owner_agent_id FROM plans WHERE plan_id=?",
            (plan_id,),
        ).fetchall()
        archived = conn.execute(
            """
            SELECT source_record_id, target_record_id, selected_record_id,
                   reason, source_data
            FROM durable_owner_scope_reconciliation_archive
            WHERE source_table='plans' AND target_record_id=?
            """,
            (f"{durable_owner}::{plan_id}",),
        ).fetchall()
    assert plan_rows == [(f"{durable_owner}::{plan_id}", durable_owner)]
    assert len(archived) == 1
    assert archived[0][:4] == (
        f"{older.owner_agent_id}::{plan_id}",
        f"{durable_owner}::{plan_id}",
        f"{newer.owner_agent_id}::{plan_id}",
        "superseded_legacy_snapshot",
    )
    assert PlanRecord.model_validate_json(archived[0][4]).status == PlanStatus.ACTIVE

    second_replacement = TaskManager(
        db_path=original.storage_settings.sqlite_path,
        logger=logging.getLogger("test_duplicate_legacy_plan_second_restart"),
        storage_settings=original.storage_settings,
    )
    second_replacement.set_owner_identity(
        agent_id="manager_after_duplicate_second_restart",
        agent_name="Second Replacement Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    with connect_sqlite(original.storage_settings.sqlite_path or "") as conn:
        archive_count = conn.execute(
            "SELECT COUNT(*) FROM durable_owner_scope_reconciliation_archive"
        ).fetchone()[0]
    assert archive_count == 1


def test_replacement_manager_rejects_ambiguous_duplicate_legacy_plans_atomically(
    tmp_path: Path,
) -> None:
    original = _task_manager(tmp_path)
    plan_id = "plan_id_ambiguous_legacy"
    first = PlanRecord(
        plan_id=plan_id,
        root_task_id="task_ambiguous_legacy",
        owner_agent_id="agent_id_ambiguous_a",
        owner_agent_name="Manager A",
        team_id="team_1",
        owner_named_agent_type="TeamArchitect",
        status=PlanStatus.ACTIVE,
        created_at="2026-06-10T00:00:00+00:00",
        updated_at="2026-06-10T00:00:00+00:00",
    )
    second = first.model_copy(deep=True)
    second.owner_agent_id = "agent_id_ambiguous_b"
    second.owner_agent_name = "Manager B"
    second.status = PlanStatus.FAILED
    original.plan_repository.save_plan(first)
    original.plan_repository.save_plan(second)

    replacement = TaskManager(
        db_path=original.storage_settings.sqlite_path,
        logger=logging.getLogger("test_ambiguous_legacy_plan"),
        storage_settings=original.storage_settings,
    )
    with pytest.raises(RuntimeError, match="ambiguous legacy snapshots"):
        replacement.set_owner_identity(
            agent_id="manager_after_ambiguous",
            agent_name="Replacement Manager",
            team_id="team_1",
            named_agent_type="TeamArchitect",
        )

    with connect_sqlite(original.storage_settings.sqlite_path or "") as conn:
        rows = conn.execute(
            "SELECT owner_agent_id, status FROM plans WHERE plan_id=? ORDER BY owner_agent_id",
            (plan_id,),
        ).fetchall()
        archive_table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table'
              AND name='durable_owner_scope_reconciliation_archive'
            """
        ).fetchone()
    assert rows == [
        ("agent_id_ambiguous_a", PlanStatus.ACTIVE.value),
        ("agent_id_ambiguous_b", PlanStatus.FAILED.value),
    ]
    assert archive_table is None


def test_create_objective_spawn_mode_creates_separate_root_task_and_plan(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    source_task = _add_root_task(manager)

    objective = manager.create_objective(
        task_id=source_task.task_id,
        creation_mode="spawn_task",
        title="Track separate research stream",
        desired_outcome="A separate objective root task owns the research stream.",
        why="The source task should only create the durable workstream.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
    )

    assert objective is not None
    assert objective.created_by_task_id == source_task.task_id
    assert objective.promoted_from_task_id is None
    assert len(objective.related_task_ids) == 1
    objective_task_id = objective.related_task_ids[0]
    assert objective_task_id != source_task.task_id
    assert manager.get_task_objective_id(objective_task_id) == objective.objective_id
    assert source_task.task_id not in objective.related_task_ids
    assert objective.current_plan_id is not None
    assert manager.get_task_plan_id(objective_task_id) == objective.current_plan_id
    plan = manager.get_plan(objective.current_plan_id)
    assert plan is not None
    assert plan.root_task_id == objective_task_id
    assert plan.objective_id == objective.objective_id
    objective_task_info = manager.get_task_info(objective_task_id)
    assert objective_task_info is not None
    assert objective_task_info.meta.objective_id == objective.objective_id

    manager.fail_task(
        source_task.task_id,
        "The creator Task failed after the Objective was created.",
        "2026-05-23T10:00:00+00:00",
    )
    surviving_objective = manager.get_objective(objective.objective_id)
    surviving_plan = manager.get_plan(objective.current_plan_id)
    assert surviving_objective is not None
    assert surviving_objective.status == ObjectiveStatus.ACTIVE
    assert surviving_plan is not None
    assert surviving_plan.status == PlanStatus.ACTIVE


def test_objective_sits_above_existing_plan_and_inherited_subtasks(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    manager.set_task_plan(
        task.task_id,
        "Research, implement, verify.",
        goal_summary="Ship an objective primitive.",
        current_focus="Runtime linkage",
    )
    plan_id = manager.get_task_plan_id(task.task_id)
    assert plan_id is not None

    objective = manager.create_objective(
        task_id=task.task_id,
        title="Ship objective primitive",
        desired_outcome="Objective becomes the durable root of complex workflows.",
        why="The work spans tasks and plans.",
        horizon="long_horizon",
    )

    assert objective is not None
    assert objective.current_plan_id == plan_id
    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert plan.objective_id == objective.objective_id
    assert plan.objective_title == objective.title

    subtask = Task(
        task_id="task_child",
        parent_task_id=task.task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Verify objective inheritance.",
    )
    assert manager.add_subtask(
        task.task_id,
        subtask,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(to_named_agent_type="ResearchComparisonAgent", conversation_id="conversation_1"),
    )

    objective = manager.get_objective(objective.objective_id)
    assert objective is not None
    assert manager.get_task_objective_id(subtask.task_id) == objective.objective_id
    assert set(objective.related_task_ids) == {"task_root", "task_child"}
    assert manager.get_plan_objective_id(plan_id) == objective.objective_id


def test_objective_completion_policies_are_enforced(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Confirm before closing",
        desired_outcome="The user confirms the objective is complete.",
        why="The manager cannot know the final acceptance condition alone.",
        horizon=ObjectiveHorizon.FINITE,
        completion_policy=ObjectiveCompletionPolicy.USER_CONFIRMS,
        success_criteria=[{"criterion_id": "criterion_1", "description": "User confirms acceptance."}],
    )
    assert objective is not None

    with pytest.raises(ValueError):
        manager.update_objective_status(objective.objective_id, ObjectiveStatus.SATISFIED)

    objective = manager.update_objective_progress(
        objective.objective_id,
        criterion_updates=[
            {
                "criterion_id": "criterion_1",
                "status": ObjectiveCriterionStatus.MET,
                "evidence_summary": "The user accepted the result.",
            }
        ],
    )
    assert objective is not None
    assert objective.success_criteria[0].status == ObjectiveCriterionStatus.MET

    objective = manager.update_objective_status(
        objective.objective_id,
        ObjectiveStatus.SATISFIED,
        user_confirmation_summary="User confirmed the objective is met.",
        satisfied_by_task_id=task.task_id,
    )
    assert objective is not None
    assert objective.status == ObjectiveStatus.SATISFIED
    assert objective.satisfied_by_task_id == task.task_id
    assert objective.user_confirmation_summary == "User confirmed the objective is met."
    plan = manager.get_plan(objective.current_plan_id)
    assert plan is not None
    assert plan.status == PlanStatus.COMPLETED


def test_completed_linked_task_leaves_objective_owned_plan_active(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Finish finite objective",
        desired_outcome="The linked task result satisfies the objective.",
        why="A finite objective can close when no linked work remains.",
        horizon=ObjectiveHorizon.FINITE,
        completion_policy=ObjectiveCompletionPolicy.MANAGER_DECIDES,
    )
    assert objective is not None

    manager.complete_task(task.task_id, "2026-05-23T10:00:00+00:00")

    refreshed = manager.get_objective(objective.objective_id)
    assert refreshed is not None
    assert refreshed.status == ObjectiveStatus.ACTIVE
    assert refreshed.satisfied_by_task_id is None
    plan = manager.get_plan(objective.current_plan_id)
    assert plan is not None
    assert plan.status == PlanStatus.ACTIVE
    assert any(event.event_type == ObjectiveEventType.REVIEW_REQUESTED for event in refreshed.event_log)
    review_events = [event for event in refreshed.event_log if event.event_type == ObjectiveEventType.REVIEW_COMPLETED]
    assert review_events
    assert review_events[-1].details["conclusion"] == "plan_work_remains"


@pytest.mark.parametrize(
    ("held_status", "conclusion"),
    [(ObjectiveStatus.PAUSED, "objective_paused"), (ObjectiveStatus.DELETING, "objective_deletion_pending")],
)
@pytest.mark.parametrize("task_ends", ["completed", "failed"])
def test_a_task_ending_keeps_a_persons_pause_or_pending_deletion(
    tmp_path: Path, held_status: ObjectiveStatus, conclusion: str, task_ends: str
) -> None:
    """A task that was running when the person paused (or started deleting) its Objective ends.

    The review used to set the Objective active (task completed) or blocked
    (task failed) because its active Plan was checked first, leaving its
    schedules and work policy off and Resume no longer offered.
    """
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Keep the person's control",
        desired_outcome="A pause or deletion outlasts the task that was running.",
        why="The person decides when the Objective runs again.",
        horizon=ObjectiveHorizon.FINITE,
    )
    assert objective is not None
    manager.update_objective_status(objective.objective_id, held_status, summary="Set by the person.")
    events_before = len(manager.get_objective(objective.objective_id).event_log)

    if task_ends == "completed":
        manager.complete_task(task.task_id, "2026-05-23T10:00:00+00:00")
    else:
        manager.fail_task(task.task_id, "verification failed", "2026-05-23T10:00:00+00:00")

    refreshed = manager.get_objective(objective.objective_id)
    assert refreshed.status == held_status
    assert manager.get_plan(objective.current_plan_id).status == PlanStatus.ACTIVE
    new_events = refreshed.event_log[events_before:]
    assert [event.event_type for event in new_events] == [
        ObjectiveEventType.REVIEW_REQUESTED,
        ObjectiveEventType.REVIEW_COMPLETED,
    ]
    assert new_events[-1].details["conclusion"] == conclusion
    assert new_events[-1].details["active_plan_ids"] == [objective.current_plan_id]


def test_a_repeated_status_change_records_nothing(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id, title="Pause twice", desired_outcome="One record.", why="Retries happen."
    )
    paused = manager.update_objective_status(objective.objective_id, ObjectiveStatus.PAUSED, summary="Paused.")
    again = manager.update_objective_status(objective.objective_id, ObjectiveStatus.PAUSED, summary="Paused.")

    assert again.event_log == paused.event_log
    assert again.updated_at == paused.updated_at
    status_events = [event for event in again.event_log if event.event_type == ObjectiveEventType.STATUS_CHANGED]
    assert [event.details["new_status"] for event in status_events] == ["paused"]


def test_terminal_linked_task_blocks_objective_waiting_for_user_confirmation(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Wait for user acceptance",
        desired_outcome="The user confirms the work is acceptable.",
        why="The task result alone is not enough evidence.",
        horizon=ObjectiveHorizon.FINITE,
        completion_policy=ObjectiveCompletionPolicy.USER_CONFIRMS,
    )
    assert objective is not None
    assert objective.current_plan_id is not None
    assert manager.plan_coordinator.update_plan_status(
        objective.current_plan_id,
        PlanStatus.COMPLETED,
        summary="The execution Plan is complete and awaits user acceptance.",
        task_id=task.task_id,
    ) is not None

    manager.complete_task(task.task_id, "2026-05-23T10:00:00+00:00")

    refreshed = manager.get_objective(objective.objective_id)
    assert refreshed is not None
    assert refreshed.status == ObjectiveStatus.BLOCKED
    assert refreshed.satisfied_by_task_id is None
    review_events = [event for event in refreshed.event_log if event.event_type == ObjectiveEventType.REVIEW_COMPLETED]
    assert review_events[-1].details["conclusion"] == "awaiting_user_confirmation"


def test_failed_linked_task_blocks_objective_for_follow_up(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Recover failed linked work",
        desired_outcome="The objective receives follow-up after task failure.",
        why="A failed task should not silently close a finite objective.",
        horizon=ObjectiveHorizon.FINITE,
    )
    assert objective is not None

    manager.fail_task(task.task_id, "verification failed", "2026-05-23T10:00:00+00:00")

    refreshed = manager.get_objective(objective.objective_id)
    assert refreshed is not None
    assert refreshed.status == ObjectiveStatus.BLOCKED
    assert refreshed.satisfied_by_task_id is None
    plan = manager.get_plan(objective.current_plan_id)
    assert plan is not None
    assert plan.status == PlanStatus.ACTIVE
    review_events = [event for event in refreshed.event_log if event.event_type == ObjectiveEventType.REVIEW_COMPLETED]
    assert review_events[-1].details["conclusion"] == "task_failed_plan_remains_active"


def test_terminal_linked_task_keeps_recurring_objective_active(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Maintain recurring coverage",
        desired_outcome="Coverage continues after each individual run.",
        why="The objective represents ongoing responsibility.",
        horizon=ObjectiveHorizon.RECURRING,
        completion_policy=ObjectiveCompletionPolicy.RECURRING_INDEFINITE,
    )
    assert objective is not None
    assert objective.current_plan_id is not None
    assert manager.plan_coordinator.update_plan_status(
        objective.current_plan_id,
        PlanStatus.COMPLETED,
        summary="This recurring execution cycle is complete.",
        task_id=task.task_id,
    ) is not None

    manager.complete_task(task.task_id, "2026-05-23T10:00:00+00:00")

    refreshed = manager.get_objective(objective.objective_id)
    assert refreshed is not None
    assert refreshed.status == ObjectiveStatus.ACTIVE
    assert refreshed.satisfied_by_task_id is None
    review_events = [event for event in refreshed.event_log if event.event_type == ObjectiveEventType.REVIEW_COMPLETED]
    assert review_events[-1].details["conclusion"] == "standing_or_recurring_objective_remains_active"


def test_terminal_linked_task_does_not_close_objective_with_active_linked_work(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    root_task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=root_task.task_id,
        title="Coordinate multiple linked tasks",
        desired_outcome="All linked work completes before the objective closes.",
        why="One task result is not enough while another linked task is active.",
        horizon=ObjectiveHorizon.FINITE,
    )
    assert objective is not None
    followup_task = Task(
        task_id="task_followup",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Continue objective-linked work.",
    )
    assert manager.add_task(
        followup_task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(
            to_named_agent_type="ResearchComparisonAgent",
            conversation_id="conversation_1",
            objective_id=objective.objective_id,
        ),
        owner_person_id=None,
    )

    manager.complete_task(root_task.task_id, "2026-05-23T10:00:00+00:00")

    refreshed = manager.get_objective(objective.objective_id)
    assert refreshed is not None
    assert refreshed.status == ObjectiveStatus.ACTIVE
    assert refreshed.satisfied_by_task_id is None
    review_events = [event for event in refreshed.event_log if event.event_type == ObjectiveEventType.REVIEW_COMPLETED]
    assert review_events[-1].details["conclusion"] == "active_work_remains"
    assert review_events[-1].details["active_task_ids"] == ["task_followup"]


def test_failed_first_task_can_be_replaced_without_losing_objective_plan(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    first_task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=first_task.task_id,
        title="Deliver a durable outcome",
        desired_outcome="Work continues across replaceable execution Tasks.",
        why="The Objective and Plan must survive individual Task failures.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
    )
    assert objective is not None
    assert objective.current_plan_id is not None
    plan_id = objective.current_plan_id
    plan_item = manager.upsert_execution_plan_item(
        first_task.task_id,
        title="Verify durable continuation",
        rationale="A replacement Task must inherit this unfinished Plan item.",
        status=ExecutionPlanItemStatus.PLANNED,
    )
    assert plan_item is not None

    manager.fail_task(
        first_task.task_id,
        "The first execution attempt failed.",
        "2026-05-23T10:00:00+00:00",
    )

    blocked = manager.get_objective(objective.objective_id)
    surviving_plan = manager.get_plan(plan_id)
    assert blocked is not None
    assert blocked.status == ObjectiveStatus.BLOCKED
    assert surviving_plan is not None
    assert surviving_plan.status == PlanStatus.ACTIVE
    assert surviving_plan.plan_items[0].step_id == plan_item.step_id

    controller = _add_root_task(manager, task_id="task_controller")
    continuation = manager.objective_coordinator.start_objective_task(
        objective.objective_id,
        source_task_id=controller.task_id,
        work_brief="Resume the unfinished verification work.",
        current_focus="Verify replacement execution",
    )

    assert continuation is not None
    continuation_task_id = continuation.task.task_id
    assert manager.get_task_plan_id(continuation_task_id) == plan_id
    digest = manager.get_execution_digest(continuation_task_id)
    assert digest is not None
    assert digest.plan_items[0].step_id == plan_item.step_id
    assert manager.get_objective(objective.objective_id).status == ObjectiveStatus.ACTIVE
    assert manager.begin_final_review_if_needed(continuation_task_id) is None

    repository_only_manager = _task_manager(tmp_path)
    assert repository_only_manager.get_task_plan_id(continuation_task_id) == plan_id

    manager.upsert_execution_plan_item(
        continuation_task_id,
        title=plan_item.title,
        rationale=plan_item.rationale,
        step_id=plan_item.step_id,
        status=ExecutionPlanItemStatus.COMPLETED,
        resolution_summary="Replacement execution verified the durable Plan.",
    )
    assert manager.plan_coordinator.update_plan_status(
        plan_id,
        PlanStatus.COMPLETED,
        summary="All Objective Plan work is complete.",
        task_id=continuation_task_id,
    ) is not None
    manager.complete_task(continuation_task_id, "2026-05-23T10:05:00+00:00")

    completed = manager.get_objective(objective.objective_id)
    assert completed is not None
    assert completed.status == ObjectiveStatus.SATISFIED
    assert completed.satisfied_by_task_id == continuation_task_id


def test_objective_runtime_links_restore_from_snapshot(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = _add_root_task(manager)
    objective = manager.create_objective(
        task_id=task.task_id,
        title="Restore objective link",
        desired_outcome="The active task remains linked after restart.",
        why="Runtime ownership can move between worker processes.",
        horizon=ObjectiveHorizon.LONG_HORIZON,
    )
    assert objective is not None

    restored = _task_manager(tmp_path)
    restored_result = restored.restore_runtime_state()

    assert restored_result is not None
    assert restored.get_task_objective_id(task.task_id) == objective.objective_id
    restored_objective = restored.get_objective(objective.objective_id)
    assert restored_objective is not None
    assert restored_objective.title == "Restore objective link"
