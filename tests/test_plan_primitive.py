from __future__ import annotations

import logging
from pathlib import Path

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import ExecutionPlanItemStatus, MessageMetaType, PlanActionStatus, PlanActionType, PlanStepFailurePolicy, PlanStatus, ProgressState
from storage.config import resolve_storage_settings


def _task_manager(tmp_path: Path) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_plan_primitive"),
        storage_settings=settings,
    )
    manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    return manager


def _specialist_task_manager(tmp_path: Path) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_plan_primitive_specialist"),
        storage_settings=settings,
    )
    manager.set_owner_identity(
        agent_id="specialist_agent",
        agent_name="Specialist",
        team_id="team_1",
        named_agent_type="CodeWritingAgent",
    )
    return manager


def _seed_manager_plan(manager: TaskManager, task_id: str) -> str:
    manager.set_task_plan(
        task_id,
        "Seeded durable manager plan.",
        goal_summary="Seeded manager objective summary.",
        current_focus="Plan primitive test",
    )
    plan_id = manager.get_task_plan_id(task_id)
    assert plan_id is not None
    return plan_id


def test_task_manager_does_not_assign_plan_for_plain_root_task(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Create a durable plan record.",
    )

    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )

    plan_id = manager.get_task_plan_id(task.task_id)
    assert plan_id is None
    assert {entry.plan_id for entry in task.execution_history} == {None}
    assert "plan_id" not in task.model_dump()

    plan_id = _seed_manager_plan(manager, task.task_id)
    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert plan.root_task_id == "task_root"
    assert plan.root_conversation_id == "conversation_1"
    assert plan.owner_agent_id == "manager_scope:team_1:TeamArchitect"
    assert plan.status == PlanStatus.ACTIVE
    assert plan.related_task_ids == ["task_root"]


def test_plan_record_tracks_digest_updates_and_subtask_lineage(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Track plan updates across a delegated subtask.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )

    manager.set_task_plan(
        task.task_id,
        "Investigate, implement, verify.",
        goal_summary="Make Plan durable without changing the task protocol.",
        current_focus="Phase 1 implementation",
    )
    plan_id = manager.get_task_plan_id(task.task_id)
    assert plan_id is not None
    manager.upsert_execution_plan_item(
        task.task_id,
        title="Add repository",
        rationale="The plan must be inspectable outside the task payload.",
        step_id="plan_step_repository",
        status=ExecutionPlanItemStatus.IN_PROGRESS,
    )

    subtask = Task(
        task_id="task_child",
        parent_task_id=task.task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Verify the repository behavior.",
    )
    assert manager.add_subtask(
        task.task_id,
        subtask,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(to_named_agent_type="ResearchComparisonAgent", conversation_id="conversation_1"),
    )

    assert {entry.plan_id for entry in subtask.execution_history} == {plan_id}
    assert "plan_id" not in subtask.model_dump()

    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert plan.plan_summary == "Investigate, implement, verify."
    assert plan.goal_summary == "Make Plan durable without changing the task protocol."
    assert [item.step_id for item in plan.plan_items] == ["plan_step_repository"]
    assert set(plan.related_task_ids) == {"task_root", "task_child"}


def test_plan_status_and_archive_retain_plan_id_when_root_task_completes(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Complete a planned task.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    plan_id = _seed_manager_plan(manager, task.task_id)

    manager.complete_task(task.task_id, "2026-04-27T12:00:00+00:00")

    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert plan.status == PlanStatus.COMPLETED

    archived = manager.get_task_from_db(task.task_id)
    assert archived is not None
    assert "plan_id" not in archived["main_task"].model_dump()
    assert archived["main_task"].execution_history[-1].plan_id == plan_id


def test_manager_records_recovery_aware_plan_actions(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Recover from a failing subtask.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    plan_id = _seed_manager_plan(manager, task.task_id)

    action = manager.record_plan_action(
        task.task_id,
        PlanActionType.START_INLINE_RECOVERY,
        "Retry the failing branch with a smaller prompt.",
        status=PlanActionStatus.ACTIVE,
        reason="The delegated specialist timed out.",
        related_task_id="task_child",
        plan_step_id="plan_step_verify",
        branch_id="recovery_branch_1",
        user_visible=True,
        details={"failure_type": "delegated_specialist_timeout"},
        agent_name="Manager",
        agent_id="manager_agent",
    )

    assert action is not None
    assert action.user_visible is True
    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert [row.action_id for row in plan.plan_actions] == [action.action_id]
    assert plan.plan_actions[0].details["failure_type"] == "delegated_specialist_timeout"
    assert manager.get_plan_actions(task.task_id)[0].action_type == PlanActionType.START_INLINE_RECOVERY
    assert "start_inline_recovery" in manager.render_execution_digest_markdown(task.task_id, only_if_active=False)
    assert task.execution_history[-1].entry_type.value == "plan_event"
    assert task.execution_history[-1].plan_id == plan_id
    assert task.execution_history[-1].details["plan_action"]["action_id"] == action.action_id


def test_manager_starts_isolated_recovery_task_on_same_plan(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task with growing context.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    plan_id = _seed_manager_plan(manager, task.task_id)

    recovery_info = manager.start_isolated_recovery_task(
        task.task_id,
        failure_type="critical_context_pressure",
        failure_summary="The original task context is too large for reliable troubleshooting.",
        recovery_brief="Inspect only the latest failure summary and propose a compact recovery path.",
        affected_step_id="plan_step_verify",
        recovery_task_id="recovery_task_1",
        branch_id="recovery_branch_1",
        header=MessageHeader(
            from_agent_id="user",
            to_routing_path="manager_agent",
            response_routing_key="user_queue",
            agent_signature="",
        ),
        meta=MessageMeta(
            from_agent_name="user",
            conversation_id="conversation_recovery",
            msg_correlation_id="recovery_corr_1",
        ),
        agent_name="Manager",
        agent_id="manager_agent",
    )

    assert recovery_info is not None
    assert recovery_info.task.task_id == "recovery_task_1"
    assert recovery_info.task.task_plan is None
    assert recovery_info.task.execution_digest is None
    assert "plan_id" not in recovery_info.task.model_dump()
    assert manager.get_task_plan_id("recovery_task_1") == plan_id
    link = manager.get_recovery_link("recovery_task_1")
    assert link is not None
    assert link.original_task_id == "task_root"
    assert link.original_plan_id == plan_id
    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert {"task_root", "recovery_task_1"}.issubset(set(plan.related_task_ids))
    assert plan.recovery_task_ids == ["recovery_task_1"]
    recovery_actions = [
        action for action in plan.plan_actions if action.action_type == PlanActionType.START_ISOLATED_RECOVERY
    ]
    assert recovery_actions[-1].recovery_task_id == "recovery_task_1"
    assert any(action.action_type == PlanActionType.USER_NOTICE for action in plan.plan_actions)
    assert "full original task context" in recovery_info.task.problem_statement
    assert "task_root" not in recovery_info.task.problem_statement
    assert "recovery_task_1" not in recovery_info.task.problem_statement


def test_recovery_completion_merges_result_and_requeues_waiting_original(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task waiting for recovery guidance.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    plan_id = _seed_manager_plan(manager, task.task_id)

    recovery_info = manager.start_isolated_recovery_task(
        task.task_id,
        failure_type="delegated_specialist_timeout",
        failure_summary="The specialist timed out.",
        recovery_brief="Find a smaller retry path.",
        recovery_task_id="recovery_task_1",
        branch_id="recovery_branch_1",
        header=MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        meta=MessageMeta(from_agent_name="user", conversation_id="conversation_recovery"),
        agent_name="Manager",
        agent_id="manager_agent",
    )
    assert recovery_info is not None
    assert manager.update_main_task_state(task.task_id, ProgressState.WAITING, "2026-04-28T12:00:00+00:00")
    recovery_info.task.answer = "Split the remaining verification into two smaller specialist tasks."

    manager.complete_task("recovery_task_1", "2026-04-28T12:05:00+00:00")

    original_info = manager.get_task_info(task.task_id)
    assert original_info is not None
    assert original_info.state == ProgressState.PARTIAL
    assert manager.get_next_task_to_process() == task.task_id
    assert any(
        "Split the remaining verification into two smaller specialist tasks" in entry.summary
        for entry in original_info.task.execution_history
    )
    link = manager.get_recovery_link("recovery_task_1")
    assert link is not None
    assert link.merge_status == "resumed"
    plan = manager.get_plan(plan_id)
    assert plan is not None
    assert any(action.action_type == PlanActionType.RECOVERY_RESULT_READY for action in plan.plan_actions)
    assert any(action.action_type == PlanActionType.RESUME_TASK for action in plan.plan_actions)
    assert "recovery_task_1" not in manager.render_execution_digest_markdown(task.task_id, only_if_active=False)


def test_recovery_completion_does_not_resume_original_waiting_on_user(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task waiting on the user.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    _seed_manager_plan(manager, task.task_id)
    recovery_info = manager.start_isolated_recovery_task(
        task.task_id,
        failure_type="context_pressure",
        failure_summary="The task needs help.",
        recovery_brief="Investigate a compact path.",
        recovery_task_id="recovery_task_1",
        branch_id="recovery_branch_1",
        header=MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        meta=MessageMeta(from_agent_name="user", conversation_id="conversation_recovery"),
    )
    assert recovery_info is not None
    manager.add_task_query(
        task.task_id,
        TaskQuery(query_type=MessageMetaType.REQUEST, parent_task_id=task.task_id, message="Need approval."),
    )
    assert manager.update_main_task_state(task.task_id, ProgressState.WAITING, "2026-04-28T12:00:00+00:00")
    recovery_info.task.answer = "Recovery found a path, but user approval is still required."

    manager.complete_task("recovery_task_1", "2026-04-28T12:05:00+00:00")

    original_info = manager.get_task_info(task.task_id)
    assert original_info is not None
    assert original_info.state == ProgressState.WAITING
    assert manager.get_next_task_to_process() is None
    link = manager.get_recovery_link("recovery_task_1")
    assert link is not None
    assert link.merge_status == "merged_waiting_on_user"


def test_recovery_link_restores_from_runtime_snapshot_and_resumes_original(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task should resume after restart.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    _seed_manager_plan(manager, task.task_id)
    recovery_info = manager.start_isolated_recovery_task(
        task.task_id,
        failure_type="runtime_restart",
        failure_summary="Recovery link must survive restart.",
        recovery_brief="Return a compact post-restart continuation path.",
        recovery_task_id="recovery_task_1",
        branch_id="recovery_branch_1",
        header=MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        meta=MessageMeta(from_agent_name="user", conversation_id="conversation_recovery"),
    )
    assert recovery_info is not None
    assert manager.update_main_task_state(task.task_id, ProgressState.WAITING, "2026-04-28T12:00:00+00:00")

    restored = _task_manager(tmp_path)
    restored_result = restored.restore_runtime_state()
    assert restored_result is not None
    restored_link = restored.get_recovery_link("recovery_task_1")
    assert restored_link is not None
    assert restored_link.original_task_id == "task_root"
    assert restored.get_task_plan_id("recovery_task_1") == restored_link.original_plan_id

    restored_recovery = restored.get_task_info("recovery_task_1")
    assert restored_recovery is not None
    restored_recovery.task.answer = "Continue with a smaller restored recovery path."
    restored.complete_task("recovery_task_1", "2026-04-28T12:05:00+00:00")

    original_info = restored.get_task_info("task_root")
    assert original_info is not None
    assert original_info.state == ProgressState.PARTIAL
    assert restored.get_next_task_to_process() == "task_root"
    assert restored.get_recovery_link("recovery_task_1").merge_status == "resumed"


def test_activate_plan_execution_creates_runtime_owned_steps_and_first_internal_task(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Research and implement a complex feature.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )

    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[
            {
                "title": "Research current behavior",
                "objective": "Inspect the code and summarize the relevant runtime path.",
                "step_id": "llm_supplied_id_must_be_ignored",
            },
            {
                "title": "Implement change",
                "objective": "Apply the smallest runtime change that satisfies the design.",
            },
        ],
        final_review=True,
        agent_name="Manager",
        agent_id="manager_agent",
    )

    assert plan is not None
    assert len(plan.plan_steps) == 3
    assert [step.title for step in plan.plan_steps] == [
        "Research current behavior",
        "Implement change",
        "Final review and response",
    ]
    assert all(step.step_id.startswith("plan_step_") for step in plan.plan_steps)
    assert "llm_supplied_id_must_be_ignored" not in {step.step_id for step in plan.plan_steps}
    assert plan.plan_steps[0].status == ExecutionPlanItemStatus.IN_PROGRESS
    assert plan.plan_steps[1].depends_on_step_ids == [plan.plan_steps[0].step_id]

    step_task_id = plan.plan_steps[0].current_step_task_id
    assert step_task_id is not None
    assert manager.is_plan_step_task(step_task_id)
    link = manager.get_plan_step_task_link(step_task_id)
    assert link is not None
    assert link.root_task_id == task.task_id
    assert link.plan_id == plan.plan_id
    assert link.step_id == plan.plan_steps[0].step_id

    step_info = manager.get_task_info(step_task_id)
    assert step_info is not None
    assert step_info.header.from_agent_id == "manager_agent"
    assert step_info.header.to_routing_path == "manager_agent"
    assert step_info.header.response_routing_key == "user_queue"
    assert step_info.meta.conversation_id != "conversation_original"
    assert "llm_supplied_id_must_be_ignored" not in step_info.task.problem_statement
    assert task.task_id not in step_info.task.problem_statement
    latest_plan = manager.get_plan(plan.plan_id)
    assert latest_plan is not None
    user_notices = [action for action in latest_plan.plan_actions if action.action_type == PlanActionType.USER_NOTICE]
    assert any("Created a runtime plan with 3 steps" in action.summary for action in user_notices)


def test_plan_step_uses_root_sender_when_explicit_response_route_is_missing(
    tmp_path: Path,
) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Execute one internal plan step.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="requester_route", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )

    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": "Inspect", "objective": "Inspect the current state."}],
        final_review=False,
    )

    assert plan is not None
    step_task_id = plan.plan_steps[0].current_step_task_id
    assert step_task_id is not None
    step_info = manager.get_task_info(step_task_id)
    assert step_info is not None
    assert step_info.header.from_agent_id == "manager_agent"
    assert step_info.header.to_routing_path == "manager_agent"
    assert step_info.header.response_routing_key == "requester_route"


def test_plan_step_completion_merges_result_and_queues_next_step_then_root(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Execute a two-step plan.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[
            {"title": "First step", "objective": "Produce the first result."},
            {"title": "Second step", "objective": "Produce the second result."},
        ],
        final_review=False,
    )
    assert plan is not None
    first_task_id = plan.plan_steps[0].current_step_task_id
    assert first_task_id is not None
    first_info = manager.get_task_info(first_task_id)
    assert first_info is not None
    first_info.task.answer = "First step result."

    manager.complete_task(first_task_id, "2026-04-28T12:01:00+00:00")

    plan_after_first = manager.get_plan(plan.plan_id)
    assert plan_after_first is not None
    assert plan_after_first.plan_steps[0].status == ExecutionPlanItemStatus.COMPLETED
    assert plan_after_first.plan_steps[0].result_summary == "First step result."
    second_task_id = plan_after_first.plan_steps[1].current_step_task_id
    assert second_task_id is not None
    assert manager.is_plan_step_task(second_task_id)
    assert manager.get_task_info(task.task_id).state == ProgressState.ACCEPTED

    second_info = manager.get_task_info(second_task_id)
    assert second_info is not None
    second_info.task.answer = "Second step result."
    manager.complete_task(second_task_id, "2026-04-28T12:02:00+00:00")

    root_info = manager.get_task_info(task.task_id)
    assert root_info is not None
    assert root_info.state == ProgressState.PARTIAL
    assert manager.get_next_task_to_process() == task.task_id
    plan_after_second = manager.get_plan(plan.plan_id)
    assert plan_after_second is not None
    assert [step.status for step in plan_after_second.plan_steps] == [
        ExecutionPlanItemStatus.COMPLETED,
        ExecutionPlanItemStatus.COMPLETED,
    ]
    assert any("Second step result" in entry.summary for entry in root_info.task.execution_history)
    user_notices = [action for action in plan_after_second.plan_actions if action.action_type == PlanActionType.USER_NOTICE]
    assert any("Plan milestone completed: First step" in action.summary for action in user_notices)
    assert any("All runtime plan milestones are complete" in action.summary for action in user_notices)


def test_plan_step_failure_retries_once_then_resumes_root_for_replan(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Execute a step that may need one retry.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": "Fragile step", "objective": "Complete despite one transient failure."}],
        final_review=False,
    )
    assert plan is not None
    first_task_id = plan.plan_steps[0].current_step_task_id
    assert first_task_id is not None

    manager.fail_task(first_task_id, "temporary provider error", "2026-04-28T12:01:00+00:00")

    plan_after_retry = manager.get_plan(plan.plan_id)
    assert plan_after_retry is not None
    retry_step = plan_after_retry.plan_steps[0]
    assert retry_step.status == ExecutionPlanItemStatus.IN_PROGRESS
    assert retry_step.attempt_count == 2
    assert retry_step.last_failure_policy == PlanStepFailurePolicy.RETRY_STEP
    retry_task_id = retry_step.current_step_task_id
    assert retry_task_id is not None
    assert retry_task_id != first_task_id
    assert manager.get_task_info(task.task_id).state == ProgressState.WAITING

    manager.fail_task(retry_task_id, "temporary provider error again", "2026-04-28T12:02:00+00:00")

    plan_after_cap = manager.get_plan(plan.plan_id)
    assert plan_after_cap is not None
    blocked_step = plan_after_cap.plan_steps[0]
    assert blocked_step.status == ExecutionPlanItemStatus.BLOCKED
    assert blocked_step.attempt_count == 2
    assert blocked_step.last_failure_policy == PlanStepFailurePolicy.RESUME_ROOT_FOR_REPLAN
    assert manager.get_task_info(task.task_id).state == ProgressState.PARTIAL
    assert manager.get_next_task_to_process() == task.task_id
    user_notices = [action for action in plan_after_cap.plan_actions if action.action_type == PlanActionType.USER_NOTICE]
    assert any("Next action: retry_step" in action.summary for action in user_notices)
    assert any("Next action: resume_root_for_replan" in action.summary for action in user_notices)


def test_plan_step_context_pressure_starts_isolated_recovery(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Investigate a long context-heavy task.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": "Long investigation", "objective": "Finish without exhausting context."}],
        final_review=False,
    )
    assert plan is not None
    step_task_id = plan.plan_steps[0].current_step_task_id
    assert step_task_id is not None

    manager.fail_task(
        step_task_id,
        "context window exceeded while processing tool results",
        "2026-04-28T12:01:00+00:00",
    )

    plan_after_failure = manager.get_plan(plan.plan_id)
    assert plan_after_failure is not None
    failed_step = plan_after_failure.plan_steps[0]
    assert failed_step.status == ExecutionPlanItemStatus.BLOCKED
    assert failed_step.failure_type == "context_pressure"
    assert failed_step.last_failure_policy == PlanStepFailurePolicy.START_ISOLATED_RECOVERY
    assert manager.get_task_info(task.task_id).state == ProgressState.WAITING
    assert len(plan_after_failure.recovery_task_ids) == 1

    recovery_task_id = plan_after_failure.recovery_task_ids[0]
    recovery_info = manager.get_task_info(recovery_task_id)
    assert recovery_info is not None
    assert "Failed plan step:" in recovery_info.task.problem_statement
    assert "Failure type: context_pressure" in recovery_info.task.problem_statement
    assert manager.get_recovery_link(recovery_task_id).affected_step_id == failed_step.step_id


def test_plan_step_links_restore_from_runtime_snapshot(tmp_path: Path) -> None:
    manager = _task_manager(tmp_path)
    task = Task(
        task_id="task_root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Restore active plan-step execution.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_original"),
        owner_person_id=None,
    )
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": "Restored step", "objective": "Complete after restore."}],
        final_review=False,
    )
    assert plan is not None
    step_task_id = plan.plan_steps[0].current_step_task_id
    assert step_task_id is not None

    restored = _task_manager(tmp_path)
    restored_result = restored.restore_runtime_state()
    assert restored_result is not None
    restored_link = restored.get_plan_step_task_link(step_task_id)
    assert restored_link is not None
    assert restored_link.root_task_id == "task_root"
    restored_step_info = restored.get_task_info(step_task_id)
    assert restored_step_info is not None
    restored_step_info.task.answer = "Restored step result."

    restored.complete_task(step_task_id, "2026-04-28T12:03:00+00:00")

    root_info = restored.get_task_info("task_root")
    assert root_info is not None
    assert root_info.state == ProgressState.PARTIAL
    assert restored.get_next_task_to_process() == "task_root"


def test_specialist_task_manager_does_not_create_first_class_plan(tmp_path: Path) -> None:
    manager = _specialist_task_manager(tmp_path)
    task = Task(
        task_id="task_specialist",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Execute a specialist task without manager plan state.",
    )

    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="Manager", to_named_agent_type="CodeWritingAgent"),
        owner_person_id=None,
    )
    manager.set_task_plan(
        task.task_id,
        "Specialist-local execution digest should not become a first-class Plan.",
    )

    assert manager.get_task_plan_id(task.task_id) is None
    assert {entry.plan_id for entry in task.execution_history} == {None}
    manager.plan_state.task_to_plan_id[task.task_id] = "legacy_specialist_plan_id"
    assert manager._export_runtime_state()["task_to_plan_id"] == {}
    action = manager.record_plan_action(
        task.task_id,
        PlanActionType.START_INLINE_RECOVERY,
        "Specialists cannot create manager plan actions.",
    )
    assert action is None
    assert manager.plan_repository.list_plans(limit=10, owner_agent_id="specialist_agent") == []
