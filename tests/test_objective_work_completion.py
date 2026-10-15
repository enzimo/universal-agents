"""Complete real Objective schedule payloads without relying on delivery metadata."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import task_message_runtime
from custom_agents.schedule_store import ScheduleStore
from custom_messages.enums import (
    ObjectiveHealthDisposition,
    MessageMetaType,
    ProgressState,
    SystemScheduleKind,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_tools.providers.objective_work import _build_objective_work_schedule
from test_objective_work_sessions import _manager, _objective
from test_turn_action_outbox import _make_agent


def _checkpointed_timer(tmp_path, *, schedule_changes=None):
    manager = _manager(tmp_path, policy=True)
    manager.set_owner_identity("manager", "Manager", "test_team", "TeamArchitect")
    task, objective = _objective(manager)
    coordinator = manager.objective_work_coordinator
    policy = coordinator.enable_policy(
        objective.objective_id,
        profile_name="daily",
        accepted_by_task_id=task.task_id,
        acceptance_summary="Daily work",
        owner_canonical_user_id="objective-user",
    )
    agent = _make_agent(tmp_path, task_manager=manager)
    agent._schedule_store = ScheduleStore(db_path=manager._db_path)
    info = manager.get_task_info(task.task_id)
    schedule = _build_objective_work_schedule(
        agent,
        current=info,
        policy=policy,
        schedule_id="objective-timer",
    )
    if schedule_changes:
        schedule = schedule.model_copy(update=schedule_changes)
    agent._schedule_store.add(schedule)
    coordinator.set_schedule(objective.objective_id, schedule_id=schedule.schedule_id)
    session = coordinator.start_session(objective.objective_id, task_id=task.task_id)
    checkpoint = coordinator.checkpoint(
        session.session_id,
        observe="Saved work",
        orient="More evidence needed",
        decide="Continue next session",
        act="Preserve plan",
        health=ObjectiveHealthDisposition.ON_TRACK,
    )
    info.meta = schedule.payload_meta.model_copy(deep=True)
    info.meta.objective_work_session_id = session.session_id
    return agent, info, coordinator.session_for_task(task.task_id), checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "schedule_missing",
        "owner_mismatch",
        "team_mismatch",
        "policy_mismatch",
        "checkpoint_mismatch",
        "objective_mismatch",
        "schedule_kind_mismatch",
        "policy_owner_mismatch",
    ],
)
async def test_completion_finishes_or_fails_cleanly_and_frees_queue_capacity(
    tmp_path, caplog, failure
):
    changes = {}
    if failure == "objective_mismatch":
        changes["objective_id"] = "another-objective"
    elif failure == "schedule_kind_mismatch":
        changes["system_schedule_kind"] = SystemScheduleKind.HEARTBEAT_LAUNCHER
    agent, info, session, checkpoint = _checkpointed_timer(
        tmp_path, schedule_changes=changes
    )
    manager = agent.task_manager
    coordinator = manager.objective_work_coordinator
    before = agent._schedule_store.get_scheduled_message("objective-timer").model_dump(
        mode="json"
    )
    # The person the message names is never used; the recorded owner decides.
    info.meta.gateway_context.canonical_user_id = "another-owner"
    if failure == "schedule_missing":
        coordinator.set_schedule(session.objective_id, schedule_id="missing")
    elif failure == "owner_mismatch":
        info.owner_person_id = "another-owner"
    elif failure == "team_mismatch":
        agent.team_id = "another-team"
    elif failure == "policy_mismatch":
        session.policy_id = "another-policy"
        manager.objective_work_repository.save_session(session)
    elif failure == "policy_owner_mismatch":
        policy = manager.objective_work_repository.get_policy(
            session.objective_id, owner_agent_id=session.owner_agent_id
        )
        policy.owner_canonical_user_id = "another-owner"
        manager.objective_work_repository.save_policy(policy)
    elif failure == "checkpoint_mismatch":
        session.checkpoint_id = "missing-checkpoint"
        manager.objective_work_repository.save_session(session)
    agent._strands_agent = SimpleNamespace(invoke_async=AsyncMock(), messages=[])
    agent._conversation_store = None
    object.__setattr__(agent, "_refresh_self_agent_state", AsyncMock())
    object.__setattr__(agent, "_notify_context_after_turn", AsyncMock())
    assert manager.add_task(
        Task(
            task_id="next-task",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Continue work",
        ),
        MessageHeader(),
        MessageMeta(),
        owner_person_id=None,
    )
    assert await task_message_runtime._check_and_process_next_task(agent)
    agent._strands_agent.invoke_async.assert_not_awaited()
    assert manager.get_task_info(info.task.task_id) is None
    saved = manager.get_task_from_db(info.task.task_id, owner_agent_id=agent.id)
    assert saved["main_task"].task_state[-1][0] == (
        ProgressState.FAILED if failure else ProgressState.COMPLETED
    )
    assert manager.can_start_new_task()
    assert manager.get_next_task_to_process() == "next-task"
    assert manager.get_objective(session.objective_id).status.value == (
        "blocked" if failure else "active"
    )
    assert checkpoint in manager.objective_work_repository.list_checkpoints(
        session.objective_id,
        owner_agent_id=session.owner_agent_id,
    )
    assert not any(record.exc_info for record in caplog.records)
    if failure:
        warnings = [
            record
            for record in caplog.records
            if "objective_work_completion_rejected" in record.message
        ]
        assert len(warnings) == 1
        assert f"reason={failure}" in warnings[0].message
        response = agent.team_comms.send_message.call_args.kwargs["message"].msg_content
        assert response.task_state[-1][0] == ProgressState.FAILED
        assert "Saved checkpoints were kept" in response.answer
        assert (
            agent._schedule_store.get_scheduled_message("objective-timer").model_dump(
                mode="json"
            )
            == before
        )
    else:
        assert (
            agent._schedule_store.get_scheduled_message(
                "objective-timer"
            ).schedule_memory.ledger.total_runs
            == 1
        )
