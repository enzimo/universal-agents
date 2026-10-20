from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from access_control.identity import ContextSigner, ISSUER_SCHEDULER
from custom_agents.objective_work_config import load_objective_work_execution_config
from custom_agents.sdk.strands.turn import (
    _finalize_objective_work_invocation,
)
from custom_agents.manager_control_runtime import CommandPerson, _apply_objective_control
from custom_agents.scheduled_runtime import enqueue_fired_scheduled_task
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from config_loader import (
    BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH,
)
from custom_messages.contracts.routing_v1 import (
    MessageHeader,
    MessageMeta,
    GatewayContext,
)
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    ProgressState,
    ExecutionPlanItemStatus,
    MessageMetaType,
    ObjectiveEventType,
    ObjectiveHealthDisposition,
    ScheduleManagementClass,
    SystemScheduleKind,
    ObjectiveWorkSessionStatus,
    RequesterType,
)
from custom_messages.records.objectives import ObjectiveWorkBudget, ObjectiveWorkProfile
from custom_messages.enums import (
    ObjectiveWorkInstalledProfileStatus,
    ObjectiveWorkProfileProposalStatus,
    ObjectiveWorkProfileSource,
)
from storage.config import resolve_storage_settings
from custom_tools.providers.objective_work import (
    objective_work_profiles,
    objective_work_sessions,
    propose_objective_work_profile,
)
from custom_tools.providers.objectives import objectives
from custom_tools.runtime_context import set_agent_ref
from custom_tools.tool_stop import ToolStop
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.tool_result_support import sdk_tool_result, tool_json, tool_yaml


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Objective tools act for the person in the task's gateway-signed context."""

    use_signed_contexts(monkeypatch)


def _signed(person_id: str, *, team_id: str = "team"):
    return context_signer().issue(team_id=team_id, person_id=person_id, person_roles=("owner",))


def _write_policy(path: Path, **overrides: object) -> Path:
    data = {
        "schema_version": 2,
        "required_skills": ["long-horizon-plan-execution"],
        "default_profile": "daily",
        "profiles": {
            "daily": {
                "cadence_cron": "0 9 * * *",
                "cadence_timezone": "UTC",
                "budget": {
                    "active_seconds": 3600,
                    "checkpoint_reserve_seconds": 300,
                    "checkpoint_reserve_turns": 2,
                    "checkpoint_reserve_tokens": 10_000,
                    "checkpoint_reserve_tool_calls": 1,
                    "turns": 10,
                    "total_tokens": 50_000,
                    "tool_calls": 20,
                },
            }
        },
    }
    data.update(overrides)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _manager(
    tmp_path: Path,
    *,
    policy: bool,
    owner_id: str = "manager",
    focused_profile: bool = False,
) -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime.db"}},
        base_dir=str(tmp_path),
    )
    policy_path = (
        _write_policy(tmp_path / "OBJECTIVE_EXECUTION.yaml") if policy else None
    )
    if policy_path is not None and focused_profile:
        # A second profile with another cadence, to replace the schedule.
        written = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        written["profiles"]["focused"] = {**written["profiles"]["daily"], "cadence_cron": "0 */6 * * *"}
        policy_path.write_text(yaml.safe_dump(written), encoding="utf-8")
    manager = TaskManager(
        storage_settings=settings,
        db_path=settings.sqlite_path,
        objective_execution_paths=[str(policy_path)] if policy_path else [],
        logger=logging.getLogger("test.objective.work"),
    )
    manager.set_owner_identity(owner_id, "Manager", "team", "TeamArchitect")
    return manager


def _objective(manager: TaskManager, *, schedule_id: str | None = None):
    task = Task(
        task_id="root-task",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Deliver a long-running outcome.",
    )
    if schedule_id is None:
        task_context = _signed("objective-user", team_id=manager.identity.team_id)
    else:
        signer = ContextSigner.from_env(ISSUER_SCHEDULER)
        assert signer is not None
        task_context = signer.issue(
            team_id=manager.identity.team_id,
            person_id="objective-user",
            person_roles=("owner",),
            schedule_id=schedule_id,
        )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user"),
        MessageMeta(
            from_agent_name="user",
            conversation_id="conversation",
            requester_type=(
                RequesterType.USER_REQUESTER if schedule_id is None else RequesterType.TIMER_REQUESTER
            ),
            gateway_context=GatewayContext(
                canonical_user_id="objective-user",
                session_key="objective-session",
                task_context=task_context,
            ),
        ),
        owner_person_id="objective-user",  # what admission records from the signed context
    )
    objective = manager.create_objective(
        title="Long outcome",
        desired_outcome="Ship a verified multi-session result.",
        why="The result needs sustained work.",
        task_id=task.task_id,
        horizon="long_horizon",
    )
    assert objective is not None
    return task, objective


def _tool_agent(
    manager: TaskManager,
    task_id: str,
    monkeypatch: pytest.MonkeyPatch,
):
    manager.task_state.current_in_progress_task_id = task_id
    schedule_store = ScheduleStore(
        db_path=manager._db_path,
        storage_settings=manager.storage_settings,
        default_cron_timezone="UTC",
    )
    # The test database sits directly in the instance root.
    layout = InstanceLayout.at(Path(manager._db_path).parent)
    agent = SimpleNamespace(
        id="manager",
        name="Manager",
        team_id="team",
        named_agent_type="TeamArchitect",
        instance_root=str(layout.root),
        workspace_dir=str(layout.workspaces_dir),
        layout=layout,
        task_manager=manager,
        plugins=[object()],
        _strands_agent=object(),
        _schedule_store=schedule_store,
        _scheduler_wake=SimpleNamespace(set=lambda: None),
        _logger=logging.getLogger("test.objective.work.tool"),
        instance_timezone="UTC",
    )
    set_agent_ref(agent)
    monkeypatch.setattr(
        "custom_tools.providers._objective_work_skills.list_agent_skill_names",
        lambda _plugin, _sdk_agent: ["Long-Horizon-Plan-Execution"],
    )
    return agent, schedule_store


@pytest.mark.parametrize(
    ("estimated_sessions", "expected_sessions", "suggested"),
    [(None, 3, True), (0, 1, False), (-1, 1, False), (1, 1, False)],
)
def test_suggest_tool_preserves_explicit_session_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    estimated_sessions: int | None, expected_sessions: int, suggested: bool,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)
    try:
        result = tool_json(objective_work_sessions(
            action="suggest", objective_id=objective.objective_id, estimated_sessions=estimated_sessions,
        ))
        assert result["estimated_sessions"] == expected_sessions
        assert result["suggested"] is suggested
    finally:
        set_agent_ref(None)


def test_accepted_policy_gates_plan_and_resume_opens_one_window(tmp_path: Path) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    suggestion = manager.objective_work_coordinator.suggest(
        objective.objective_id, estimated_sessions=4
    )
    assert suggestion["suggested"] is True
    assert suggestion["requires_explicit_acceptance"] is True
    policy = manager.objective_work_coordinator.enable_policy(
        objective.objective_id,
        profile_name="daily",
        accepted_by_task_id=task.task_id,
        acceptance_summary="User explicitly chose daily bounded work sessions.",
    )
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": "Research", "objective": "Collect evidence."}],
        final_review=False,
    )
    assert plan is not None
    assert plan.plan_steps[0].current_step_task_id is None

    session = manager.objective_work_coordinator.start_session(
        objective.objective_id, task_id=task.task_id
    )
    assert session.required_skills == ["long-horizon-plan-execution"]
    assert manager.objective_work_coordinator.resume_existing_plan(session) is True

    resumed = manager.get_plan(plan.plan_id)
    assert resumed is not None
    step_task_id = resumed.plan_steps[0].current_step_task_id
    assert step_task_id is not None
    assert (
        manager.get_task_info(step_task_id).meta.objective_work_session_id
        == session.session_id
    )
    manager.objective_work_coordinator.record_skill_activation(
        step_task_id, "long-horizon-plan-execution"
    )
    activated = manager.objective_work_coordinator.session_for_meta(session.session_id)
    assert activated is not None
    assert activated.activated_skills == ["long-horizon-plan-execution"]
    assert policy.objective_id == objective.objective_id


@pytest.mark.parametrize("step_yielded", [True, False])
@pytest.mark.parametrize("notify, blocked, notify_on_failure", [
    (None, False, False), ("always", False, False),
    ("on_change", False, False), ("never", False, False),
    ("never", True, True), ("never", True, False),
])
def test_checkpoint_returns_plan_root_to_finish_session(
    tmp_path: Path, step_yielded: bool, notify: str | None,
    blocked: bool, notify_on_failure: bool,
) -> None:
    from custom_messages.records.task_results import TaskResultUpdate
    from test_turn_action_outbox import _make_agent
    from unittest.mock import AsyncMock

    manager = _manager(tmp_path, policy=True)
    manager.set_owner_identity("manager", "Manager", "test_team", "TeamArchitect")
    task, objective = _objective(manager)
    coordinator = manager.objective_work_coordinator
    coordinator.enable_policy(
        objective.objective_id, profile_name="daily",
        accepted_by_task_id=task.task_id, acceptance_summary="Daily sessions.",
    )
    session = coordinator.start_session(objective.objective_id, task_id=task.task_id)
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": name, "objective": "Collect evidence."} for name in ["First", "Later"]],
        final_review=False,
    )
    step_id = plan.plan_steps[0].current_step_task_id
    assert step_id is not None
    manager.update_main_task_state(task.task_id, ProgressState.WAITING, "2026-09-20T10:00:00+00:00")
    coordinator.checkpoint(
        session.session_id, observe="Session done", orient="More evidence needed",
        decide="Continue next session", act="Preserve plan",
        health=ObjectiveHealthDisposition.ON_TRACK,
        completed_subgoals=[] if step_yielded else ["First"],
        blockers=["Source unavailable"] if blocked else [],
    )
    manager.update_task_result(step_id, TaskResultUpdate(
        answer="Checkpoint saved.",
        errors=["objective_work_session_yield"] if step_yielded else [],
    ))
    manager.complete_task(step_id, "2026-09-20T10:01:00+00:00")
    assert task.task_id in manager.task_state.task_queue
    refreshed = manager.get_plan(plan.plan_id)
    assert refreshed.active_execution_task_id is None
    assert refreshed.plan_steps[1].status == ExecutionPlanItemStatus.PLANNED
    agent = _make_agent(tmp_path, task_manager=manager)
    agent._strands_agent = SimpleNamespace(invoke_async=AsyncMock())
    info = manager.get_task_info(task.task_id)
    if notify is not None:
        from custom_agents.objective_work_completion import prepare_objective_work_completion
        from custom_messages.contracts.schedule_v1 import ScheduleTaskMemory

        info.meta.requester_type = RequesterType.TIMER_REQUESTER
        agent._schedule_store = ScheduleStore(db_path=manager._db_path, default_cron_timezone="UTC")
        schedule = ScheduledMessage(
            schedule_id="objective-timer", payload=task,
            payload_meta=info.meta, objective_id=objective.objective_id,
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
            claim_team_id=agent.team_id, claim_named_agent_type=agent.named_agent_type,
            cron_expression="0 9 * * *", repeat_count=12,
            schedule_memory=ScheduleTaskMemory(policy={"notify": notify, "notify_on_failure": notify_on_failure}),
        )
        agent._schedule_store.add(schedule, remaining_count_override=7)
        coordinator.set_schedule(objective.objective_id, schedule_id=schedule.schedule_id)
        original_due = agent._schedule_store.get_scheduled_message(schedule.schedule_id).next_fire_at
        # Preparation can be repeated after a crash without counting another run.
        saved_session = coordinator.session_for_task(task.task_id)
        prepare_objective_work_completion(agent, info.task, info.meta, saved_session)
        prepare_objective_work_completion(agent, info.task, info.meta, saved_session)
    response = asyncio.run(agent.turn_executor.process_llm_interaction(
        info.task, info.header, info.meta,
    ))
    assert response.task_type == MessageMetaType.RESPONSE
    assert "checkpoint" in response.answer
    agent._strands_agent.invoke_async.assert_not_called()
    expected_notify = notify_on_failure if blocked else (
        notify is None or notify == "always" or (notify == "on_change" and not step_yielded)
    )
    assert agent.team_comms.send_message.await_count == int(expected_notify)
    assert manager.get_task_info(task.task_id) is None
    assert manager.get_objective(objective.objective_id).status.value == "active"
    assert coordinator.session_for_task(task.task_id).status == ObjectiveWorkSessionStatus.CHECKPOINTED
    if notify is not None:
        saved = agent._schedule_store.get_scheduled_message("objective-timer")
        assert saved.schedule_memory.ledger.total_runs == 1
        assert saved.schedule_memory.ledger.recent_runs[0]["outcome"] == (
            "inconclusive" if blocked else "no_change" if step_yielded else "changed"
        )
        assert saved.next_fire_at == original_due
        assert agent._schedule_store.get_all()[0]["remaining_count"] == 7


def test_replacement_manager_reconciles_complete_legacy_objective_work_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_owner = "manager_before_durable_scope"
    original = _manager(tmp_path, policy=True, owner_id=legacy_owner)
    monkeypatch.setattr(original, "durable_manager_scope_id", lambda: legacy_owner)
    original.set_owner_identity(
        legacy_owner,
        "Original Manager",
        "team",
        "TeamArchitect",
    )
    task, objective = _objective(original)
    _agent, schedule_store = _tool_agent(original, task.task_id, monkeypatch)
    enabled = tool_json(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the accepted daily work bounds.", profile_name="daily", confirmed=True)
    )
    assert "error" not in enabled, enabled
    schedule_id = enabled["schedule_id"]
    scheduled = schedule_store.get_scheduled_message(schedule_id)
    assert scheduled is not None
    assert scheduled.claim_agent_id is None
    assert scheduled.claim_team_id == "team"
    assert scheduled.claim_named_agent_type == "TeamArchitect"

    session = original.objective_work_coordinator.start_session(
        objective.objective_id,
        task_id=task.task_id,
        owner_canonical_user_id="objective-user",
    )
    checkpoint = original.objective_work_coordinator.checkpoint(
        session.session_id,
        observe="The legacy run persisted evidence.",
        orient="The next manager must resume the same policy.",
        decide="Continue after restart.",
        act="Checkpoint the handoff.",
        health=ObjectiveHealthDisposition.ON_TRACK,
        evidence_delta="One durable checkpoint was recorded.",
        completed_subgoals=["Persist the first checkpoint."],
        next_subgoal="Resume under the replacement manager.",
    )
    profile = ObjectiveWorkProfile(
        cadence_cron="0 */6 * * *",
        cadence_timezone="UTC",
        budget=ObjectiveWorkBudget(
            active_seconds=2_700,
            checkpoint_reserve_seconds=300,
            checkpoint_reserve_turns=2,
            checkpoint_reserve_tokens=50_000,
            checkpoint_reserve_tool_calls=1,
            turns=18,
            total_tokens=250_000,
            tool_calls=35,
        ),
    )
    proposal = original.objective_work_coordinator.propose_profile(
        owner_canonical_user_id="objective-user",
        source_task_id=task.task_id,
        scope="reusable",
        objective_id=None,
        profile_name="legacy-six-hour",
        profile=profile,
        rationale="Preserve the accepted reusable profile across replacement.",
        replace_existing=False,
    )
    installed = original.objective_work_coordinator.install_reusable_profile(
        proposal,
        accepted_by_task_id=task.task_id,
        acceptance_summary="Install the exact reusable profile.",
    )

    replacement = _manager(
        tmp_path,
        policy=True,
        owner_id="manager_after_restart",
    )
    durable_owner = replacement.durable_manager_scope_id()
    assert durable_owner == "manager_scope:team:TeamArchitect"
    restored_policy = replacement.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=durable_owner,
    )
    restored_session = replacement.objective_work_repository.get_session(
        session.session_id,
        owner_agent_id=durable_owner,
    )
    restored_checkpoints = replacement.objective_work_repository.list_checkpoints(
        objective.objective_id,
        owner_agent_id=durable_owner,
    )
    restored_proposal = replacement.objective_work_repository.get_profile_proposal(
        proposal.proposal_id,
        owner_agent_id=durable_owner,
        owner_canonical_user_id="objective-user",
    )
    restored_installed = replacement.objective_work_repository.get_installed_profile(
        installed.profile_name,
        owner_agent_id=durable_owner,
        owner_canonical_user_id="objective-user",
    )

    assert replacement.get_objective(objective.objective_id) is not None
    assert restored_policy is not None
    assert restored_policy.schedule_id == schedule_id
    assert restored_session is not None
    assert restored_checkpoints[0].checkpoint_id == checkpoint.checkpoint_id
    assert restored_proposal is not None
    assert restored_installed is not None
    assert (
        replacement.objective_work_repository.get_policy(
            objective.objective_id,
            owner_agent_id=legacy_owner,
        )
        is None
    )


def test_protected_schedule_fire_opens_session_and_resumes_plan(tmp_path: Path) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    policy = manager.objective_work_coordinator.enable_policy(
        objective.objective_id,
        profile_name="daily",
        accepted_by_task_id=task.task_id,
        acceptance_summary="Use a daily bounded work window.",
    )
    plan = manager.activate_plan_execution(
        task.task_id,
        steps=[{"title": "Research", "objective": "Collect evidence."}],
        final_review=False,
    )
    assert plan is not None
    assert plan.plan_steps[0].current_step_task_id is None

    schedule = ScheduledMessage(
        schedule_id="objwork_sched_test",
        schedule_name=f"objective_work_session:{objective.objective_id}",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        objective_id=objective.objective_id,
        payload=Task(
            task_id="task_work_window",
            parent_task_id=task.task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Resume this Objective for one bounded window.",
        ),
        payload_header=MessageHeader(from_agent_id="manager", agent_signature=""),
        payload_meta=MessageMeta(
            from_agent_name="Manager",
            conversation_id="conversation",
            objective_id=objective.objective_id,
        ),
        cron_expression=policy.cadence_cron,
        cron_timezone="UTC",
        repeat_count=None,
        next_fire_at="2026-08-22T00:00:00+00:00",
    )
    agent = SimpleNamespace(
        id="manager",
        name="Manager",
        team_id="team",
        task_manager=manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.objective.work.schedule"),
    )

    assert asyncio.run(
        enqueue_fired_scheduled_task(
            agent,
            schedule.schedule_id,
            schedule,
            from_runtime_mailbox=True,
        )
    )
    window_task = manager.get_task_info("task_work_window")
    assert window_task is not None
    assert window_task.meta.objective_work_session_id
    resumed = manager.get_plan(plan.plan_id)
    assert resumed is not None
    step_task_id = resumed.plan_steps[0].current_step_task_id
    assert step_task_id
    assert (
        manager.get_task_info(step_task_id).meta.objective_work_session_id
        == window_task.meta.objective_work_session_id
    )
    refreshed_policy = manager.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=manager.durable_manager_scope_id(),
    )
    assert refreshed_policy is not None
    assert refreshed_policy.next_run_at


def test_token_boundary_uses_reserved_checkpoint_invocation_and_accounts_split_usage(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    manager.objective_work_coordinator.enable_policy(
        objective.objective_id,
        profile_name="daily",
        accepted_by_task_id=task.task_id,
        acceptance_summary="Use bounded work windows.",
    )
    session = manager.objective_work_coordinator.start_session(
        objective.objective_id, task_id=task.task_id
    )
    manager.objective_work_coordinator.record_skill_activation(
        task.task_id, "long-horizon-plan-execution"
    )
    work_result = SimpleNamespace(
        stop_reason="limit_total_tokens",
        metrics=SimpleNamespace(
            latest_agent_invocation=SimpleNamespace(
                usage={
                    "inputTokens": 35_000,
                    "outputTokens": 5_000,
                    "totalTokens": 40_000,
                    "cacheReadInputTokens": 2_000,
                },
                cycles=[object()] * 8,
            )
        ),
    )

    async def _checkpoint_only(prompt: str, **kwargs):
        assert prompt.startswith("CHECKPOINT-ONLY BOUNDED OBJECTIVE PASS")
        assert kwargs["limits"] == {"turns": 2, "total_tokens": 10_000}
        manager.objective_work_coordinator.checkpoint(
            session.session_id,
            observe="The work allowance was consumed after evidence collection.",
            orient="The new evidence is durable and the next source remains pending.",
            decide="Resume with the next source in a later work window.",
            act="Yield with a complete continuation record.",
            health=ObjectiveHealthDisposition.ON_TRACK,
            evidence_delta=["source-a verified"],
            next_subgoal="Verify source B",
        )
        return SimpleNamespace(
            stop_reason="end_turn",
            metrics=SimpleNamespace(
                latest_agent_invocation=SimpleNamespace(
                    usage={
                        "inputTokens": 4_000,
                        "outputTokens": 1_000,
                        "totalTokens": 5_000,
                    },
                    cycles=[object(), object()],
                )
            ),
        )

    response = asyncio.run(
        _finalize_objective_work_invocation(
            model=SimpleNamespace(invoke_async=_checkpoint_only),
            logger=logging.getLogger("test.objective.work.checkpoint-reserve"),
            coordinator=manager.objective_work_coordinator,
            runtime=manager.operations_runtime,
            session=session,
            result=work_result,
            invocation_started=time.monotonic() - 0.5,
            invoke_kwargs={"invocation_state": {}},
            tool_call_limit_handler=None,
            msg_content=task,
            active_task_id=task.task_id,
            tool_stop=ToolStop(agent_id="agent", task_id=task.task_id),
        )
    )

    assert response is not None
    snapshot = manager.objective_work_coordinator.snapshot(objective.objective_id)
    persisted_session = snapshot["sessions"][0]
    assert persisted_session["status"] == ObjectiveWorkSessionStatus.CHECKPOINTED.value
    assert persisted_session["input_tokens_used"] == 39_000
    assert persisted_session["output_tokens_used"] == 6_000
    assert persisted_session["total_tokens_used"] == 45_000
    assert persisted_session["cache_read_input_tokens_used"] == 2_000
    assert snapshot["checkpoints"][0]["degraded"] is False
    assert snapshot["tuning_signals"]["limit_dimension"] == "total_tokens"
    assert snapshot["tuning_signals"]["work_tool_call_allowance"] == 19
    assert snapshot["tuning_signals"]["checkpoint_only_tool_call_reserve"] == 1
    assert (
        snapshot["tuning_signals"]["objective_profile_tool_budget_is_authoritative"]
        is True
    )


def test_policy_rejects_hidden_auto_activation_and_missing_required_skill(
    tmp_path: Path,
) -> None:
    auto_path = _write_policy(tmp_path / "auto.yaml", auto_activate=True)
    with pytest.raises(ValueError, match="auto_activate"):
        load_objective_work_execution_config([str(auto_path)])

    missing_path = _write_policy(tmp_path / "missing.yaml", required_skills=[])
    with pytest.raises(ValueError, match="long-horizon-plan-execution"):
        load_objective_work_execution_config([str(missing_path)])


@pytest.mark.parametrize("wake_fails", [False, True])
def test_enable_tool_requires_confirmation_and_installs_protected_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wake_fails: bool,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    manager.task_state.current_in_progress_task_id = task.task_id
    schedule_store = ScheduleStore(
        db_path=manager._db_path,
        storage_settings=manager.storage_settings,
        default_cron_timezone="UTC",
    )
    agent = SimpleNamespace(
        id="manager",
        name="Manager",
        team_id="team",
        named_agent_type="TeamArchitect",
        task_manager=manager,
        plugins=[object()],
        _strands_agent=object(),
        _schedule_store=schedule_store,
        _scheduler_wake=SimpleNamespace(set=lambda: None),
        _logger=logging.getLogger("test.objective.work.tool"),
        instance_timezone="UTC",
    )
    set_agent_ref(agent)
    monkeypatch.setattr(
        "custom_tools.providers._objective_work_skills.list_agent_skill_names",
        lambda _plugin, _sdk_agent: ["Long-Horizon-Plan-Execution"],
    )

    denied = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the daily bounded profile.", profile_name="daily", confirmed=False)
    )
    assert "Explicit user confirmation" in denied["error"]

    if wake_fails:
        def unavailable():
            raise RuntimeError("Event loop is closed")
        agent._scheduler_wake.set = unavailable
    result = asyncio.run(sdk_tool_result(objective_work_sessions, {
        "action": "enable", "objective_id": objective.objective_id,
        "confirmation_summary": "Use the daily bounded profile.", "profile_name": "daily", "confirmed": True,
    }))
    assert result["status"] == "success", result
    enabled = tool_yaml(result)
    schedule = schedule_store.get_scheduled_message(enabled["schedule_id"])
    assert schedule is not None
    assert schedule.management_class == ScheduleManagementClass.SYSTEM
    assert schedule.system_schedule_kind == SystemScheduleKind.OBJECTIVE_WORK_SESSION
    assert schedule.objective_id == objective.objective_id
    assert enabled["required_skills"] == ["long-horizon-plan-execution"]
    assert bool(enabled.get("warnings")) is wake_fails
    assert manager.objective_work_coordinator.snapshot(objective.objective_id)["policy"]["status"] == "active"


def test_agent_authored_profile_envelope_rejects_excessive_frequency_and_budget(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, policy=True)
    settings = manager.objective_work_config.custom_profiles

    with pytest.raises(ValueError, match="more frequently"):
        manager.objective_work_coordinator.propose_profile(
            owner_canonical_user_id="objective-user",
            source_task_id="task",
            scope="reusable",
            objective_id=None,
            profile_name="too-frequent",
            profile=ObjectiveWorkProfile(
                cadence_cron="*/30 * * * *",
                budget=ObjectiveWorkBudget(
                    active_seconds=600,
                    checkpoint_reserve_seconds=60,
                    checkpoint_reserve_turns=1,
                    checkpoint_reserve_tokens=10_000,
                    checkpoint_reserve_tool_calls=1,
                    turns=2,
                    total_tokens=25_000,
                    tool_calls=2,
                ),
            ),
            rationale="This should fail the operator cadence envelope.",
            replace_existing=False,
        )

    assert settings.budget_bounds.maximum.active_seconds == 28_800
    with pytest.raises(ValueError, match="active_seconds"):
        manager.objective_work_coordinator.propose_profile(
            owner_canonical_user_id="objective-user",
            source_task_id="task",
            scope="reusable",
            objective_id=None,
            profile_name="too-long",
            profile=ObjectiveWorkProfile(
                cadence_cron="0 */6 * * *",
                budget=ObjectiveWorkBudget(
                    active_seconds=28_801,
                    checkpoint_reserve_seconds=300,
                    checkpoint_reserve_turns=2,
                    checkpoint_reserve_tokens=25_000,
                    checkpoint_reserve_tool_calls=1,
                    turns=20,
                    total_tokens=250_000,
                    tool_calls=30,
                ),
            ),
            rationale="This should fail the operator budget envelope.",
            replace_existing=False,
        )


def test_reusable_profile_requires_exact_acceptance_survives_restart_and_reconfigures_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _agent, _schedule_store = _tool_agent(manager, task.task_id, monkeypatch)

    proposal = tool_yaml(
        propose_objective_work_profile(
            profile_name="six-hour-focus",
            cadence_cron="0 */6 * * *",
            active_seconds=2_700,
            checkpoint_reserve_seconds=300,
            checkpoint_reserve_turns=2,
            checkpoint_reserve_tokens=50_000,
            checkpoint_reserve_tool_calls=1,
            turns=18,
            total_tokens=250_000,
            tool_calls=35,
            rationale="Use four bounded runs per day while this program is active.",
            scope="reusable",
        )
    )
    assert "error" not in proposal, proposal
    assert proposal["status"] == ObjectiveWorkProfileProposalStatus.PENDING.value

    mismatch = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=proposal["proposal_id"], proposal_digest="0" * 64, confirmation_summary="Install the exact six-hour profile for future Objectives.", confirmed=True)
    )
    assert "does not match" in mismatch["error"]

    applied = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"], confirmation_summary="Install the exact six-hour profile for future Objectives.", confirmed=True)
    )
    assert applied["installed_profile"]["profile_name"] == "six-hour-focus"
    assert (
        applied["proposal"]["status"]
        == ObjectiveWorkProfileProposalStatus.APPLIED.value
    )

    replacement_proposal = tool_yaml(
        propose_objective_work_profile(
            profile_name="six-hour-focus",
            cadence_cron="0 */8 * * *",
            active_seconds=3_000,
            checkpoint_reserve_seconds=300,
            checkpoint_reserve_turns=2,
            checkpoint_reserve_tokens=50_000,
            checkpoint_reserve_tool_calls=1,
            turns=20,
            total_tokens=275_000,
            tool_calls=40,
            rationale="Replace the saved profile with the newly agreed eight-hour cadence.",
            scope="reusable",
            replace_existing=True,
        )
    )
    assert replacement_proposal["replace_existing"] is True
    assert (
        replacement_proposal["replaces_installed_profile_id"]
        == applied["installed_profile"]["installed_profile_id"]
    )
    replacement = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=replacement_proposal["proposal_id"], proposal_digest=replacement_proposal["proposal_digest"], confirmation_summary="Replace the saved profile with these exact updated bounds.", confirmed=True)
    )
    assert (
        replacement["installed_profile"]["installed_profile_id"]
        != applied["installed_profile"]["installed_profile_id"]
    )

    restored = _manager(tmp_path, policy=True)
    installed = restored.objective_work_repository.get_installed_profile(
        "six-hour-focus",
        owner_agent_id=restored.durable_manager_scope_id(),
        owner_canonical_user_id="objective-user",
    )
    assert installed is not None
    assert installed.status == ObjectiveWorkInstalledProfileStatus.ACTIVE
    assert installed.profile.cadence_cron == "0 */8 * * *"
    assert (
        restored.objective_work_repository.get_installed_profile(
            "six-hour-focus",
            owner_agent_id=restored.durable_manager_scope_id(),
            owner_canonical_user_id="another-user",
        )
        is None
    )
    _active_agent, restored_schedule_store = _tool_agent(
        manager,
        task.task_id,
        monkeypatch,
    )

    enabled = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use my installed six-hour profile.", profile_name="six-hour-focus", confirmed=True)
    )
    first_schedule_id = enabled["schedule_id"]
    assert enabled["profile_source"] == ObjectiveWorkProfileSource.INSTALLED.value
    assert enabled["installed_profile_id"] == installed.installed_profile_id
    assert enabled["profile_proposal_id"] == replacement_proposal["proposal_id"]
    assert enabled["cadence_cron"] == "0 */8 * * *"

    removed = tool_yaml(
        objective_work_profiles(action="remove", profile_name="six-hour-focus", confirmation_summary="Remove it from my catalog; existing policy snapshots must remain stable.", confirmed=True)
    )
    assert removed["status"] == ObjectiveWorkInstalledProfileStatus.REMOVED.value
    catalog = tool_yaml(objective_work_profiles(action="list", include_proposals=False))
    assert "six-hour-focus" not in {
        item["profile_name"] for item in catalog["profiles"]
    }
    snapshotted_policy = manager.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=manager.durable_manager_scope_id(),
    )
    assert snapshotted_policy.profile_source == ObjectiveWorkProfileSource.INSTALLED
    assert snapshotted_policy.profile_name == "six-hour-focus"

    reconfigured = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Return this Objective to the operator daily profile.", profile_name="daily", confirmed=True)
    )
    assert reconfigured["schedule_id"] == first_schedule_id
    assert reconfigured["profile_source"] == ObjectiveWorkProfileSource.CONFIGURED.value
    assert len(restored_schedule_store.get_all()) == 1
    assert (
        restored_schedule_store.get_scheduled_message(first_schedule_id).cron_expression
        == "0 9 * * *"
    )
    current_policy = manager.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=manager.durable_manager_scope_id(),
    )
    assert current_policy.profile_source == ObjectiveWorkProfileSource.CONFIGURED


def test_one_time_profile_applies_only_to_bound_objective_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _agent, schedule_store = _tool_agent(manager, task.task_id, monkeypatch)

    proposal = tool_yaml(
        propose_objective_work_profile(
            profile_name="launch-window",
            cadence_cron="0 */4 * * *",
            active_seconds=3_600,
            checkpoint_reserve_seconds=300,
            checkpoint_reserve_turns=2,
            checkpoint_reserve_tokens=50_000,
            checkpoint_reserve_tool_calls=1,
            turns=24,
            total_tokens=320_000,
            tool_calls=45,
            rationale="Use a temporary four-hour cadence through the launch Objective.",
            scope="objective",
            objective_id=objective.objective_id,
        )
    )
    assert "error" not in proposal, proposal
    denied = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"], confirmation_summary="Apply these exact bounds only to the launch Objective.", confirmed=False)
    )
    assert "confirmation" in denied["error"]

    applied = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"], confirmation_summary="Apply these exact bounds only to the launch Objective.", confirmed=True)
    )
    policy = applied["policy"]
    assert policy["profile_source"] == ObjectiveWorkProfileSource.ONE_TIME.value
    assert policy["profile_proposal_id"] == proposal["proposal_id"]
    assert policy["cadence_cron"] == "0 */4 * * *"
    assert len(schedule_store.get_all()) == 1

    repeated = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"], confirmation_summary="Apply these exact bounds only to the launch Objective.", confirmed=True)
    )
    assert repeated["already_applied"] is True
    assert (
        repeated["proposal"]["status"]
        == ObjectiveWorkProfileProposalStatus.APPLIED.value
    )
    assert repeated["proposal"]["applied_policy_id"] == policy["policy_id"]
    assert len(schedule_store.get_all()) == 1

    def fail_objective_schedule_link(*_args, **_kwargs):
        raise RuntimeError("simulated policy schedule-link failure")

    monkeypatch.setattr(
        manager,
        "link_schedule_to_objective",
        fail_objective_schedule_link,
    )
    failed_reconfiguration = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Try to replace the one-time profile with daily.", profile_name="daily", confirmed=True)
    )
    assert "error" in failed_reconfiguration, failed_reconfiguration
    assert "simulated policy schedule-link failure" in failed_reconfiguration["error"]
    restored_policy = manager.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=manager.durable_manager_scope_id(),
    )
    assert restored_policy.profile_source == ObjectiveWorkProfileSource.ONE_TIME
    assert restored_policy.cadence_cron == "0 */4 * * *"
    assert len(schedule_store.get_all()) == 1
    assert (
        schedule_store.get_scheduled_message(policy["schedule_id"]).cron_expression
        == "0 */4 * * *"
    )


@pytest.mark.parametrize("already_applied", [False, True])
def test_team_profile_application_rechecks_current_administrator_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_applied: bool,
) -> None:
    manager = _manager(tmp_path, policy=True)
    source, _private = _objective(manager)
    system = Task(task_id="team_root", task_type=MessageMetaType.REQUEST, problem_statement="Team work.")
    assert manager.add_task(
        system, MessageHeader(from_agent_id="system"),
        MessageMeta(from_agent_name="system", conversation_id="team_work", gateway_context=GatewayContext(
            task_context=context_signer().issue(team_id="team", person_id=None),
        )), owner_person_id=None,
    )
    team = manager.create_objective(task_id=system.task_id, title="Team outcome", desired_outcome="x", why="y")
    _agent, store = _tool_agent(manager, source.task_id, monkeypatch)
    proposal = tool_json(propose_objective_work_profile(
        profile_name="team-window", cadence_cron="0 */4 * * *", active_seconds=3600,
        checkpoint_reserve_seconds=300, checkpoint_reserve_turns=2,
        checkpoint_reserve_tokens=50_000, checkpoint_reserve_tool_calls=1,
        turns=24, total_tokens=320_000, tool_calls=45, rationale="Bounded team work.",
        scope="objective", objective_id=team.objective_id,
    ))
    assert "error" not in proposal
    acceptance = dict(
        action="apply", proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"],
        confirmation_summary="Apply these exact team bounds.", confirmed=True,
    )
    if already_applied:
        assert "policy" in tool_json(objective_work_profiles(**acceptance))
    before = manager.get_objective(team.objective_id).model_dump(mode="json")
    schedule_count = len(store.get_all())
    meta = manager.get_task_info(source.task_id).meta.model_copy(deep=True)
    meta.gateway_context.task_context = context_signer().issue(
        team_id="team", person_id="objective-user", person_roles=("member",),
    )
    member = Task(task_id="member_acceptance", task_type=MessageMetaType.REQUEST, problem_statement="Apply profile.")
    assert manager.add_task(member, manager.get_task_info(source.task_id).header, meta, owner_person_id="objective-user")
    manager.task_state.current_in_progress_task_id = member.task_id
    refused = tool_json(objective_work_profiles(**acceptance))
    assert refused == {"error": "No objective could be resolved from the supplied context."}
    assert manager.get_objective(team.objective_id).model_dump(mode="json") == before
    assert len(store.get_all()) == schedule_count


def test_one_time_profile_rejects_policy_state_drift_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _agent, schedule_store = _tool_agent(manager, task.task_id, monkeypatch)
    proposal = tool_yaml(
        propose_objective_work_profile(
            profile_name="drifted-window",
            cadence_cron="0 */4 * * *",
            active_seconds=3_600,
            checkpoint_reserve_seconds=300,
            checkpoint_reserve_turns=2,
            checkpoint_reserve_tokens=50_000,
            checkpoint_reserve_tool_calls=1,
            turns=24,
            total_tokens=320_000,
            tool_calls=45,
            rationale="Use this only if the Objective's current policy remains unchanged.",
            scope="objective",
            objective_id=objective.objective_id,
        )
    )
    assert "error" not in proposal, proposal
    enabled = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the daily operator profile instead.", profile_name="daily", confirmed=True)
    )
    assert enabled["profile_name"] == "daily"

    rejected = tool_yaml(
        objective_work_profiles(action="apply", proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"], confirmation_summary="Accept the stale proposal.", confirmed=True)
    )
    assert "changed after this profile was proposed" in rejected["error"]
    persisted = manager.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=manager.durable_manager_scope_id(),
    )
    assert persisted.profile_name == "daily"
    assert len(schedule_store.get_all()) == 1


def test_objective_work_policy_reconfiguration_is_canonical_user_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    _agent, schedule_store = _tool_agent(manager, task.task_id, monkeypatch)
    enabled = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the daily profile for this user's Objective.", profile_name="daily", confirmed=True)
    )
    assert enabled["owner_canonical_user_id"] == "objective-user"

    task_info = manager.get_task_info(task.task_id)
    task_info.meta.gateway_context.canonical_user_id = "another-user"
    task_info.meta.gateway_context.task_context = _signed("another-user")
    denied = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Attempt a cross-user reconfiguration.", profile_name="focused", confirmed=True)
    )
    # Another person's Objective is out of view: refused as if it did not exist.
    no_objective = "No objective could be resolved from the supplied context."
    assert denied["error"] == no_objective
    status_denied = tool_yaml(objective_work_sessions(action="status", objective_id=objective.objective_id))
    disable_denied = tool_yaml(
        objective_work_sessions(action="disable", objective_id=objective.objective_id, confirmed=True)
    )
    start_denied = tool_yaml(objective_work_sessions(action="start", objective_id=objective.objective_id))
    suggest_denied = tool_yaml(objective_work_sessions(action="suggest", objective_id=objective.objective_id))
    assert status_denied["error"] == no_objective
    assert disable_denied["error"] == no_objective
    assert start_denied["error"] == no_objective
    assert suggest_denied["error"] == no_objective
    persisted = manager.objective_work_repository.get_policy(
        objective.objective_id,
        owner_agent_id=manager.durable_manager_scope_id(),
    )
    assert persisted.profile_name == "daily"
    assert len(schedule_store.get_all()) == 1


def test_objective_work_schedule_replacement_uses_a_held_access_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The access hook allowed the tool for the agent and the person, so trusted
    code records that as held access, not as a gateway user's command."""

    manager = _manager(tmp_path, policy=True, focused_profile=True)
    task, objective = _objective(manager)
    _agent, schedule_store = _tool_agent(manager, task.task_id, monkeypatch)
    first = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the daily profile.", profile_name="daily", confirmed=True)
    )
    assert "error" not in first, first

    with caplog.at_level(logging.INFO, logger="custom_agents.schedule_management"):
        replaced = tool_yaml(
            objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the focused profile.", profile_name="focused", confirmed=True)
        )

    assert "error" not in replaced, replaced
    assert "event=system_schedule_change_permitted" in caplog.text
    assert "authority_mode=held_access" in caplog.text
    assert "authority_mode=authenticated_gateway_user" not in caplog.text
    assert len(schedule_store.get_all()) == 1


def test_profile_yaml_export_is_inert_and_round_trips_as_a_policy_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path, policy=True)
    task, _objective_record = _objective(manager)
    _tool_agent(manager, task.task_id, monkeypatch)

    rendered = tool_json(
        objective_work_profiles(action="export", profile_name="daily", save=True, confirmed=True)
    )

    assert rendered["saved"] is True
    assert rendered["activation_required"] is True
    export_path = Path(rendered["saved_path"])
    assert export_path.is_file()
    # Exported layers are team control files written only by the trusted export.
    assert export_path.parent == InstanceLayout.at(tmp_path).objective_profiles_dir
    exported = yaml.safe_load(export_path.read_text(encoding="utf-8"))
    assert exported["schema_version"] == 2
    assert exported["profiles"]["daily"]["budget"]["total_tokens"] == 50_000
    reloaded = load_objective_work_execution_config(
        [str(tmp_path / "OBJECTIVE_EXECUTION.yaml"), str(export_path)]
    )
    assert reloaded is not None
    assert reloaded.profiles["daily"].budget.total_tokens == 50_000


def test_builtin_policy_has_live_calibrated_token_headroom() -> None:
    policy = load_objective_work_execution_config(
        [str(BUILTIN_OBJECTIVE_EXECUTION_POLICY_PATH)]
    )

    assert policy is not None
    assert policy.default_profile == "nightly"
    assert set(policy.profiles) == {"nightly", "deep"}
    # Night-time in the owner's own time zone.
    assert policy.profiles["nightly"].cadence_cron == "0 3 * * *"
    assert policy.profiles["deep"].cadence_cron == "0 4 * * *"
    assert policy.profiles["nightly"].cadence_timezone == policy.profiles["deep"].cadence_timezone == "owner"
    assert policy.profiles["nightly"].budget.total_tokens == 1_500_000
    assert policy.profiles["deep"].budget.total_tokens == 3_000_000
    assert policy.profiles["nightly"].budget.checkpoint_reserve_tokens == 150_000
    assert policy.profiles["deep"].budget.checkpoint_reserve_tokens == 250_000
    assert policy.profiles["nightly"].budget.active_seconds == 1_800
    assert policy.profiles["nightly"].budget.checkpoint_reserve_seconds == 300
    assert policy.profiles["deep"].budget.active_seconds == 5_400
    assert policy.profiles["deep"].budget.checkpoint_reserve_seconds == 300
    assert policy.profiles["nightly"].budget.turns == 30
    assert policy.profiles["nightly"].budget.checkpoint_reserve_turns == 2
    assert policy.profiles["deep"].budget.turns == 50
    assert policy.profiles["deep"].budget.checkpoint_reserve_turns == 3
    assert policy.profiles["nightly"].budget.tool_calls == 150
    assert policy.profiles["deep"].budget.tool_calls == 300
    assert policy.profiles["nightly"].budget.checkpoint_reserve_tool_calls == 5
    assert policy.profiles["deep"].budget.checkpoint_reserve_tool_calls == 5
    assert policy.custom_profiles.enabled is True
    assert policy.custom_profiles.allow_reusable_installation is True
    assert policy.custom_profiles.proposal_ttl_seconds == 86_400
    assert policy.custom_profiles.minimum_cadence_interval_seconds == 3_600
    assert policy.custom_profiles.max_installed_profiles_per_owner == 50
    assert policy.custom_profiles.budget_bounds.minimum.active_seconds == 300
    assert policy.custom_profiles.budget_bounds.maximum.active_seconds == 28_800
    assert policy.custom_profiles.budget_bounds.maximum.total_tokens == 3_000_000


def test_a_failed_objective_work_schedule_replacement_restores_controls_and_keeps_new_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install and rollback change definitions; notes written meanwhile stay current."""

    manager = _manager(tmp_path, policy=True, focused_profile=True)
    task, objective = _objective(manager)
    _agent, schedule_store = _tool_agent(manager, task.task_id, monkeypatch)
    first = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the daily profile.", profile_name="daily", confirmed=True)
    )
    assert "error" not in first, first
    [daily_row] = schedule_store.get_all()

    def prior_note(memory):
        memory.state.summary = "Notes from the prior run"

    schedule_store.edit_schedule_memory(daily_row["schedule_id"], prior_note)
    daily = schedule_store.get_scheduled_message(daily_row["schedule_id"])

    def fail_link(*_args, **_kwargs):
        installed = schedule_store.get_scheduled_message(daily.schedule_id)
        assert installed.schedule_memory.state.summary == "Notes from the prior run"

        def new_note(memory):
            memory.state.summary = "Notes written during reconfiguration"

        schedule_store.edit_schedule_memory(daily.schedule_id, new_note)
        raise RuntimeError("Injected Objective link failure")

    monkeypatch.setattr(manager, "link_schedule_to_objective", fail_link)
    replaced = tool_yaml(
        objective_work_sessions(action="enable", objective_id=objective.objective_id, confirmation_summary="Use the focused profile.", profile_name="focused", confirmed=True)
    )

    assert "error" in json.dumps(replaced)
    restored = schedule_store.get_scheduled_message(daily.schedule_id)
    assert (restored.cron_expression, restored.interval_ms) == (daily.cron_expression, daily.interval_ms)
    assert restored.payload.problem_statement == daily.payload.problem_statement
    assert restored.schedule_memory.state.summary == "Notes written during reconfiguration"


@pytest.mark.parametrize("first_install", [False, True])
def test_failed_objective_schedule_install_keeps_a_concurrent_operator_stop(tmp_path, monkeypatch, first_install):
    from custom_agents.schedule_management import (
        ScheduleMutationAuthorityMode,
        ScheduleMutationOperation,
        build_schedule_mutation_permit,
    )

    manager = _manager(tmp_path, policy=True, focused_profile=True)
    task, objective = _objective(manager)
    _agent, store = _tool_agent(manager, task.task_id, monkeypatch)
    if not first_install:
        first = tool_yaml(
            objective_work_sessions(
                action="enable", objective_id=objective.objective_id, confirmation_summary="Use daily sessions.",
                profile_name="daily", confirmed=True,
            )
        )
        assert "error" not in first, first
    stopped = []

    def stop_then_fail(*_args, **_kwargs):
        [row] = store.get_all()
        installed = store.get_scheduled_message(row["schedule_id"])
        permit = build_schedule_mutation_permit(
            schedule=installed,
            operation=ScheduleMutationOperation.DISABLE,
            authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
            actor_principal_id="operator",
            owner_canonical_user_id=installed.payload_meta.gateway_context.canonical_user_id,
            team_id=installed.claim_team_id,
            candidate=installed.model_copy(update={"enabled": False}),
        )
        assert store.set_enabled(installed.schedule_id, False, system_mutation_permit=permit)
        stopped.append(store.get_scheduled_message(installed.schedule_id))
        raise RuntimeError("Injected Objective link failure after an operator stop")

    monkeypatch.setattr(manager, "link_schedule_to_objective", stop_then_fail)
    result = tool_yaml(
        objective_work_sessions(
            action="enable", objective_id=objective.objective_id, confirmation_summary="Use focused sessions.",
            profile_name="focused", confirmed=True,
        )
    )

    assert "error" in json.dumps(result)
    assert len(stopped) == 1
    assert store.get_scheduled_message(stopped[0].schedule_id) == stopped[0]


# -- Objective lifecycle changes and the work policy ----------------------------------


def _policy_objective(tmp_path: Path):
    manager = _manager(tmp_path, policy=True)
    task, objective = _objective(manager)
    manager.objective_work_coordinator.enable_policy(
        objective.objective_id,
        profile_name="daily",
        accepted_by_task_id=task.task_id,
        acceptance_summary="User explicitly chose daily bounded work sessions.",
        owner_canonical_user_id="objective-user",
    )
    return manager, task, objective


def _event_types(manager: TaskManager, objective_id: str) -> list[ObjectiveEventType]:
    return [event.event_type for event in manager.get_objective(objective_id).event_log]


def _policy(manager: TaskManager, objective_id: str):
    return manager.objective_work_repository.get_policy(
        objective_id, owner_agent_id=manager.durable_manager_scope_id()
    )


def _control(manager: TaskManager, objective_id: str, action: str) -> ManagerRuntimeWorkCommand:
    agent = SimpleNamespace(
        task_manager=manager,
        _logger=logging.getLogger("test.objective.work.control"),
        _ensure_schedule_store=lambda: ScheduleStore(
            db_path=manager._db_path, storage_settings=manager.storage_settings
        ),
        _scheduler_wake=SimpleNamespace(set=lambda: None),
    )
    command = ManagerRuntimeWorkCommand(
        command_id=f"cmd_{action}", action=action, scope="objective", target_objective_id=objective_id
    )
    response = command.model_copy(deep=True)
    asyncio.run(
        _apply_objective_control(
            agent,
            command=command,
            person=CommandPerson(canonical_user_id="objective-user", team_id="team", roles=("owner",)),
            response=response,
        )
    )
    return response


def test_a_persons_pause_and_resume_keep_the_work_policy_events(tmp_path: Path) -> None:
    """The policy's own Objective event survives the control that caused it.

    The control used to save the Objective copy it held from before the
    policy changed, erasing the policy event every time.
    """
    manager, _task, objective = _policy_objective(tmp_path)

    paused = _control(manager, objective.objective_id, "pause_objective")

    assert (paused.success, paused.message) == (True, "objective_paused")
    policy = _policy(manager, objective.objective_id)
    assert (policy.status.value, policy.disabled_by_objective_control) == ("disabled", True)
    assert _event_types(manager, objective.objective_id)[-2:] == [
        ObjectiveEventType.STATUS_CHANGED,
        ObjectiveEventType.WORK_SESSION_POLICY_DISABLED,
    ]

    # Stopping a paused Objective (to end its current work) records nothing new.
    events_before = _event_types(manager, objective.objective_id)
    stopped = _control(manager, objective.objective_id, "stop_objective")
    assert (stopped.success, stopped.message) == (True, "objective_paused")
    assert _event_types(manager, objective.objective_id) == events_before

    resumed = _control(manager, objective.objective_id, "resume_objective")

    assert (resumed.success, resumed.message) == (True, "objective_active")
    policy = _policy(manager, objective.objective_id)
    assert (policy.status.value, policy.disabled_by_objective_control) == ("active", False)
    stored = manager.get_objective(objective.objective_id)
    assert [event.event_type for event in stored.event_log][-2:] == [
        ObjectiveEventType.STATUS_CHANGED,
        ObjectiveEventType.WORK_SESSION_POLICY_ENABLED,
    ]
    assert stored.event_log[-1].summary == "Resumed bounded Objective work sessions after Objective control."


def test_a_resume_leaves_a_policy_the_person_turned_off(tmp_path: Path) -> None:
    manager, _task, objective = _policy_objective(tmp_path)
    manager.objective_work_coordinator.disable_policy(
        objective.objective_id, owner_canonical_user_id="objective-user"
    )
    _control(manager, objective.objective_id, "pause_objective")
    _control(manager, objective.objective_id, "resume_objective")

    policy = _policy(manager, objective.objective_id)
    assert (policy.status.value, policy.disabled_by_objective_control) == ("disabled", False)
    assert _event_types(manager, objective.objective_id).count(ObjectiveEventType.WORK_SESSION_POLICY_DISABLED) == 1


def test_the_team_leads_pause_and_resume_follow_the_work_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool's pause and resume use the same lifecycle change as the person's controls.

    Before, the tool left the policy alone: after a person paused in Runtime
    controls and asked the team lead to resume, work sessions never ran again.
    """
    manager, task, objective = _policy_objective(tmp_path)
    _tool_agent(manager, task.task_id, monkeypatch)

    paused = tool_json(objectives(action="update_status", status="paused", objective_id=objective.objective_id))

    assert paused["objective"]["status"] == "paused"
    assert "lifecycle_warnings" not in paused
    assert _policy(manager, objective.objective_id).disabled_by_objective_control is True
    assert _event_types(manager, objective.objective_id)[-1] == ObjectiveEventType.WORK_SESSION_POLICY_DISABLED

    resumed = tool_json(objectives(action="update_status", status="active", objective_id=objective.objective_id))

    assert resumed["objective"]["status"] == "active"
    policy = _policy(manager, objective.objective_id)
    assert (policy.status.value, policy.disabled_by_objective_control) == ("active", False)
    assert _event_types(manager, objective.objective_id)[-1] == ObjectiveEventType.WORK_SESSION_POLICY_ENABLED


def test_a_fired_work_session_opens_inside_one_task_operation(tmp_path: Path) -> None:
    """Opening the session runs as a task operation, like every Objective change.

    It ran on the event loop outside the lane, so it could write between
    another operation's load and save of the Objective, and that save erased
    the WORK_SESSION_STARTED event; it also changed the task and its queue
    outside the lane.
    """
    manager, task, objective = _policy_objective(tmp_path)
    schedule = ScheduledMessage(
        schedule_id="objwork_sched_lane",
        schedule_name=f"objective_work_session:{objective.objective_id}",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        objective_id=objective.objective_id,
        payload=Task(
            task_id="task_work_window_lane",
            parent_task_id=task.task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Resume this Objective for one bounded window.",
        ),
        payload_header=MessageHeader(from_agent_id="manager", agent_signature=""),
        payload_meta=MessageMeta(
            from_agent_name="Manager", conversation_id="conversation", objective_id=objective.objective_id
        ),
        cron_expression="0 9 * * *",
        cron_timezone="UTC",
        repeat_count=None,
        next_fire_at="2026-08-22T00:00:00+00:00",
    )
    agent = SimpleNamespace(
        id="manager",
        name="Manager",
        team_id="team",
        task_manager=manager,
        paused=False,
        is_executing=True,
        # The run's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the run stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.objective.work.schedule"),
    )
    inside: list[bool] = []
    start_session = manager.objective_work_coordinator.start_session

    def start_and_note(*args, **kwargs):
        inside.append(manager.operations_runtime.inside_operation())
        return start_session(*args, **kwargs)

    manager.objective_work_coordinator.start_session = start_and_note

    assert asyncio.run(
        enqueue_fired_scheduled_task(agent, schedule.schedule_id, schedule, from_runtime_mailbox=True)
    )
    assert inside == [True]
    stored = manager.get_objective(objective.objective_id)
    assert ObjectiveEventType.WORK_SESSION_STARTED in [event.event_type for event in stored.event_log]
    assert manager.get_task_info("task_work_window_lane").meta.objective_work_session_id
