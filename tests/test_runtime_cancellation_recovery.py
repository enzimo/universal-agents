from __future__ import annotations

import logging
from pathlib import Path

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.runtime_snapshot_v3 import PlanStepTaskLink, RecoveryTaskLink
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from storage.config import resolve_storage_settings


def _manager(tmp_path: Path, *, agent_id: str) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = TaskManager(
        storage_settings=settings,
        logger=logging.getLogger(f"test.runtime_cancel.{agent_id}"),
    )
    manager.set_owner_identity(
        agent_id,
        agent_id,
        "team_1",
        "TeamArchitect",
    )
    return manager


def test_cancelled_execution_graph_cannot_resurrect_during_role_takeover(
    tmp_path: Path,
) -> None:
    original = _manager(tmp_path, agent_id="manager_original")
    task = Task(
        task_id="task_cancelled_before_restart",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Do not resurrect this task.",
    )
    assert original.add_task(
        task,
        MessageHeader(from_agent_id="gateway", response_routing_key="gateway"),
        MessageMeta(
            conversation_id="conversation_1",
            gateway_context=GatewayContext(
                canonical_user_id="user_1",
                session_key="session_1",
            ),
        ),
        owner_person_id="user_1",
    )
    plan_step = Task(
        task_id="task_plan_step",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Internal plan step.",
    )
    recovery = Task(
        task_id="task_recovery",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Isolated recovery branch.",
    )
    for attached in (plan_step, recovery):
        assert original.add_task(
            attached,
            MessageHeader(from_agent_id="manager_original"),
            MessageMeta(),
            owner_person_id=None,
        )
    original.plan_state.plan_step_task_links[plan_step.task_id] = PlanStepTaskLink(
        root_task_id=task.task_id,
        plan_id="plan_1",
        step_id="step_1",
        step_task_id=plan_step.task_id,
    )
    original.plan_state.recovery_links[recovery.task_id] = RecoveryTaskLink(
        original_task_id=plan_step.task_id,
        original_plan_id="plan_1",
        recovery_task_id=recovery.task_id,
        branch_id="recovery_branch_1",
        failure_type="worker_timeout",
        failure_summary="The delegated worker did not respond.",
    )
    original._persist_runtime_state()
    assert {
        info.task.task_id
        for info in original.get_execution_graph_task_infos(recovery.task_id)
    } == {task.task_id, plan_step.task_id, recovery.task_id}
    original.record_execution_graph_cancelled(
        root_task_id=task.task_id,
        task_ids=[task.task_id, plan_step.task_id, recovery.task_id],
        canonical_user_id="user_1",
        command_id="runtime_work_command_1",
        reason="Operator stop before restart.",
        cancelled_at="2026-08-23T12:00:00+00:00",
    )

    replacement = _manager(tmp_path, agent_id="manager_replacement")
    restored = replacement.restore_runtime_state_for_owner_ids(
        ["manager_original"]
    )

    assert restored is not None
    assert restored["task_count"] == 0
    assert replacement.get_task_info(task.task_id) is None
    assert replacement.get_task_info(plan_step.task_id) is None
    assert replacement.get_task_info(recovery.task_id) is None
    assert replacement.is_execution_cancelled(task.task_id)
    assert replacement.has_queued_top_level_tasks() is False


def test_cancelled_task_is_rejected_at_new_runtime_admission(tmp_path: Path) -> None:
    manager = _manager(tmp_path, agent_id="manager")
    manager.record_execution_graph_cancelled(
        root_task_id="task_cancelled",
        task_ids=["task_cancelled"],
        canonical_user_id="user_1",
        command_id="runtime_work_command_2",
        reason="Operator stop.",
        cancelled_at="2026-08-23T12:00:00+00:00",
    )

    admitted = manager.add_task(
        Task(
            task_id="task_cancelled",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Stale broker redelivery.",
        ),
        MessageHeader(from_agent_id="gateway"),
        MessageMeta(),
        owner_person_id=None,
    )

    assert admitted is False
    assert manager.get_task_info("task_cancelled") is None
