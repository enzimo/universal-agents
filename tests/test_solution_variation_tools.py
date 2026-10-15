from __future__ import annotations

import json
import logging
from pathlib import Path

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from custom_tools.providers.solution_variations import (
    close_solution_variation,
    inspect_solution_variation,
    record_solution_candidate,
    record_solution_evaluation,
    select_solution_candidate,
    start_solution_variation,
)
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings


class _FakeAgent:
    def __init__(
        self,
        task_manager: TaskManager,
        *,
        named_agent_type: str = "TeamArchitect",
    ) -> None:
        self.id = "manager-runtime-id"
        self.name = "Manager"
        self.named_agent_type = named_agent_type
        self.team_id = "team-1"
        self.task_manager = task_manager


def _manager(tmp_path: Path, *, named_agent_type: str = "TeamArchitect") -> TaskManager:
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )
    manager = TaskManager(
        db_path=settings.sqlite_path,
        logger=logging.getLogger("test_solution_variation_tools"),
        storage_settings=settings,
    )
    manager.set_owner_identity(
        agent_id="manager-runtime-id",
        agent_name="Manager",
        team_id="team-1",
        named_agent_type=named_agent_type,
    )
    task = Task(
        task_id="task-root",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Compare viable implementations when useful.",
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(
            from_agent_name="user",
            conversation_id="conversation-1",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session-1",
                canonical_user_id="user-1",
            ),
        ),
        owner_person_id="user-1",
    )
    manager.task_state.current_in_progress_task_id = task.task_id
    return manager


def test_solution_variation_tools_record_evaluate_select_and_close(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    set_agent_ref(_FakeAgent(manager))

    started = json.loads(
        start_solution_variation(
            purpose="Compare a direct sidecar with a new workflow primitive.",
            evaluation_criteria_json=json.dumps(
                ["preserves Task semantics", "supports durable lineage"]
            ),
            constraints_json=json.dumps(["must remain optional"]),
            candidate_limit=2,
            resource_budget="Two candidates and targeted tests.",
            stop_condition="Stop when one route passes the common criteria.",
        )
    )
    assert started["created"] is True
    assert started["required"] is False
    assert started["workspace_isolation_created"] is False

    first = json.loads(
        record_solution_candidate(
            candidate_key="sidecar",
            title="Manager-owned sidecar",
            hypothesis="A sidecar preserves the executable Task primitive.",
            approach="Persist bounded candidates and evaluations outside Plan.",
        )
    )
    assert first["created"] is True
    second = json.loads(
        record_solution_candidate(
            candidate_key="primitive",
            title="New workflow primitive",
            hypothesis="A new primitive could encode branching directly.",
            approach="Add a new transport and runtime lifecycle.",
        )
    )
    assert second["created"] is True

    evaluated = json.loads(
        record_solution_evaluation(
            candidate_key="sidecar",
            evaluation_key="architecture-check",
            outcome="supports",
            summary="The sidecar preserves Task transport and keeps selection advisory.",
            hard_constraints_passed=True,
            metrics_json=json.dumps(
                [
                    {
                        "criterion": "runtime primitives added",
                        "result": "none",
                        "score": 0,
                        "direction": "minimize",
                    }
                ]
            ),
            evidence_refs_json=json.dumps(["tests:test_solution_variation_tools"]),
        )
    )
    assert evaluated["created"] is True
    assert evaluated["evaluation_authority"] == "agent_authored_advisory"

    selected = json.loads(
        select_solution_candidate(
            candidate_key="sidecar",
            evaluation_key="architecture-check",
            rationale="It adds comparison lineage without changing execution semantics.",
        )
    )
    assert selected["selection_recorded"] is True
    assert selected["artifact_applied"] is False
    assert selected["plan_status_changed"] is False
    assert selected["objective_status_changed"] is False
    assert selected["authority_granted"] is False

    task_before_close = manager.get_task_info("task-root")
    assert task_before_close is not None
    state_before_close = task_before_close.state
    closed = json.loads(
        close_solution_variation(
            outcome="completed",
            summary="Selected route was applied through normal tools and re-verified.",
        )
    )
    assert closed["closed"] is True
    assert closed["run"]["status"] == "completed"
    assert manager.get_task_info("task-root").state == state_before_close

    inspected = json.loads(inspect_solution_variation(include_closed=True))
    assert inspected["run"]["selected_candidate_key"] == "sidecar"
    assert len(inspected["candidates"]) == 2
    assert len(inspected["evaluations"]) == 1


def test_solution_variation_promotes_from_task_to_first_plan_and_continues(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    set_agent_ref(_FakeAgent(manager))

    started = json.loads(
        start_solution_variation(
            purpose="Compare implementations before the execution plan is written.",
            evaluation_criteria_json=json.dumps(["correctness"]),
            candidate_limit=2,
        )
    )
    run_id = started["run"]["run_id"]
    assert started["run"]["scope_key"] == "task:task-root"
    assert started["run"]["plan_id"] is None

    json.loads(
        record_solution_candidate(
            candidate_key="before-plan",
            title="Candidate recorded before planning",
            hypothesis="The candidate remains attached when the first Plan appears.",
            approach="Record it in the Task-scoped run.",
        )
    )
    manager.set_task_plan(
        "task-root",
        "Evaluate the existing candidates and select one.",
        goal_summary="Choose the supported implementation.",
    )
    plan_id = manager.get_task_plan_id("task-root")
    assert plan_id is not None

    after_plan = json.loads(
        record_solution_candidate(
            candidate_key="after-plan",
            title="Candidate recorded after planning",
            hypothesis="All model-facing operations resolve the promoted run.",
            approach="Resolve the Plan scope before appending the candidate.",
        )
    )
    assert after_plan["created"] is True

    evaluated = json.loads(
        record_solution_evaluation(
            candidate_key="before-plan",
            evaluation_key="promotion-regression",
            outcome="supports",
            summary="The candidate and run identity survived Task-to-Plan promotion.",
            hard_constraints_passed=True,
        )
    )
    assert evaluated["created"] is True
    selected = json.loads(
        select_solution_candidate(
            candidate_key="before-plan",
            evaluation_key="promotion-regression",
            rationale="The promoted lineage stayed intact.",
        )
    )
    assert selected["selection_recorded"] is True
    assert selected["run"]["run_id"] == run_id
    assert selected["run"]["scope_key"] == f"plan:{plan_id}"
    assert selected["run"]["plan_id"] == plan_id

    closed = json.loads(
        close_solution_variation(
            outcome="completed",
            summary="The promoted candidate passed the focused regression.",
        )
    )
    assert closed["closed"] is True
    assert closed["run"]["run_id"] == run_id
    assert closed["run"]["status"] == "completed"
    assert {item["candidate_key"] for item in closed["candidates"]} == {
        "before-plan",
        "after-plan",
    }
    assert [item["evaluation_key"] for item in closed["evaluations"]] == [
        "promotion-regression"
    ]


def test_solution_variation_closed_task_scope_remains_inspectable_after_plan(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    set_agent_ref(_FakeAgent(manager))
    started = json.loads(
        start_solution_variation(
            purpose="Preserve a closed Task-scoped comparison.",
            evaluation_criteria_json=json.dumps(["durability"]),
        )
    )
    closed = json.loads(
        close_solution_variation(
            outcome="no_selection",
            summary="Planning started after this comparison had already closed.",
        )
    )
    closed_revision = closed["run"]["revision"]

    manager.set_task_plan("task-root", "Continue through the first durable Plan.")
    plan_id = manager.get_task_plan_id("task-root")
    assert plan_id is not None
    continuation = Task(
        task_id="task-continuation",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Inspect the earlier comparison from a Plan continuation.",
    )
    assert manager.add_task(
        continuation,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(
            from_agent_name="user",
            conversation_id="conversation-2",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session-1",
                canonical_user_id="user-1",
            ),
        ),
        owner_person_id="user-1",
    )
    assert (
        manager.plan_coordinator.link_task_to_plan(continuation.task_id, plan_id)
        is not None
    )
    manager.task_state.current_in_progress_task_id = continuation.task_id
    inspected = json.loads(inspect_solution_variation(include_closed=True))

    assert inspected["run"]["run_id"] == started["run"]["run_id"]
    assert inspected["run"]["scope_key"] == "task:task-root"
    assert inspected["run"]["plan_id"] is None
    assert inspected["run"]["revision"] == closed_revision


def test_solution_variation_rejects_conflicting_replay_and_unsupported_selection(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    set_agent_ref(_FakeAgent(manager))
    json.loads(
        start_solution_variation(
            purpose="Compare approaches.",
            evaluation_criteria_json=json.dumps(["correctness"]),
            candidate_limit=2,
        )
    )
    original = json.loads(
        record_solution_candidate(
            candidate_key="one",
            title="One",
            hypothesis="First hypothesis.",
            approach="First approach.",
        )
    )
    assert original["created"] is True
    replay = json.loads(
        record_solution_candidate(
            candidate_key="one",
            title="One",
            hypothesis="First hypothesis.",
            approach="First approach.",
        )
    )
    assert replay["created"] is False
    conflict = json.loads(
        record_solution_candidate(
            candidate_key="one",
            title="Changed",
            hypothesis="Different hypothesis.",
            approach="Different approach.",
        )
    )
    assert "different content" in conflict["error"]

    json.loads(
        record_solution_evaluation(
            candidate_key="one",
            evaluation_key="failed-check",
            outcome="rejects",
            summary="The candidate violates a constraint.",
            hard_constraints_passed=False,
        )
    )
    rejected = json.loads(
        select_solution_candidate(
            candidate_key="one",
            evaluation_key="failed-check",
            rationale="Attempt an invalid selection.",
        )
    )
    assert "failed hard constraints" in rejected["error"]


def test_solution_variation_tools_are_manager_only(tmp_path: Path) -> None:
    manager = _manager(tmp_path, named_agent_type="CodeWritingAgent")
    set_agent_ref(_FakeAgent(manager, named_agent_type="CodeWritingAgent"))

    result = json.loads(
        start_solution_variation(
            purpose="Should not start.",
            evaluation_criteria_json=json.dumps(["correctness"]),
        )
    )
    assert result["manager_only"] is True
