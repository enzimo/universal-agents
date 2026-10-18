from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_agents.task_manager import TaskManager
from custom_agents.task_records import ReportedWorkIdentity
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import AfterActionReport, Task
from custom_messages.enums import AfterActionReportReviewStatus, ExecutionPlanItemStatus, MessageMetaType, ProgressState, TaskHistoryImportance, TaskHistoryType
from custom_tools.providers.agent_management import get_runtime_pressure
from custom_tools.providers.context_memory import (
    list_pending_after_action_reports,
    resolve_after_action_report_review,
)
from custom_tools.providers.diagnostics import (
    get_execution_digest,
    get_recovery_context,
)
from custom_tools.providers.objectives import (
    activate_plan_execution,
    record_plan_action,
    set_execution_plan,
    start_isolated_recovery,
    update_execution_plan_step,
    update_execution_unknown,
)
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

# These tasks carry no signed context, so the reports describe work for nobody
# and the fake agent's tools (no verified person) see exactly those.
NOBODY = ReportedWorkIdentity(person_id=None)


class _FakeAgent:
    def __init__(
        self,
        task_manager: TaskManager,
        *,
        agent_id: str = "agent_test",
        name: str = "TestAgent",
        named_agent_type: str = "CodeWritingAgent",
        context_window_tokens: int | None = None,
        max_tool_calls_per_task: int | None = None,
        strands_messages: list | None = None,
        schedule_store: ScheduleStore | None = None,
    ):
        self.id = agent_id
        self.name = name
        self.named_agent_type = named_agent_type
        self.task_manager = task_manager
        self.context_window_tokens = context_window_tokens
        self.max_tool_calls_per_task = max_tool_calls_per_task
        self.inner_loop_context = {"max_total_tokens": 400}
        self.model_id = "fake-test-model"
        self._strands_model = _FakeTokenCountingModel()
        self._strands_agent = type(
            "FakeStrandsRuntime",
            (),
            {"messages": strands_messages or [], "model": self._strands_model},
        )()
        self._schedule_store = schedule_store
        self._scheduler_wake = type("FakeWake", (), {"set": lambda self: None})()

    def _build_team_routing_prompt_context(self):
        return ""


class _FakeTokenCountingModel:
    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
        return max(1, len(json.dumps(messages, ensure_ascii=False, default=str)) // 4)


def test_execution_digest_tools_update_current_task():
    task_manager = TaskManager()
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Track a durable execution digest",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager))

    plan_result = json.loads(
        set_execution_plan(
            "1. Add structured digest state. 2. Render it into prompts.",
            goal_summary="Keep the manager's living plan durable.",
            current_focus="Tool-driven updates",
            rationale="Long-running work needs a pinned summary that survives compaction.",
        )
    )
    assert plan_result["digest"]["plan_summary"].startswith("1. Add structured digest state.")

    step_result = json.loads(
        update_execution_plan_step(
            title="Add durable plan-item state",
            rationale="Each TODO item must carry rationale so later summaries stay intelligible.",
            step_id="plan_step_state",
            status="in_progress",
        )
    )
    assert step_result["step"]["step_id"] == "plan_step_state"
    assert step_result["step"]["status"] == "in_progress"

    unknown_result = json.loads(
        update_execution_unknown(
            summary="How much of the digest should be rendered in prompts?",
            unknown_id="unknown_prompt_budget",
            why_it_matters="Prompt duplication can waste context budget.",
            resolution_plan="Keep the digest pinned and concise.",
        )
    )
    assert unknown_result["unknown"]["unknown_id"] == "unknown_prompt_budget"

    digest_result = json.loads(get_execution_digest())
    assert digest_result["task_id"] == task.task_id
    assert digest_result["plan_id"] is None
    assert digest_result["active"] is True
    assert digest_result["digest"]["plan_items"][0]["rationale"].startswith("Each TODO item must carry rationale")
    assert "## Execution Digest" in digest_result["markdown"]


def test_execution_digest_tools_update_delegated_specialist_task():
    task_manager = TaskManager()
    task = Task(
        task_id="subtask_research_001",
        parent_task_id="manager_task_001",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Research current LLM rankings.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="manager", to_named_agent_type="WebResearchAgent"),
        owner_person_id=None,
    )
    assert task_manager.update_main_task_state(
        task.task_id,
        ProgressState.IN_PROGRESS,
        "2026-04-25T04:14:41+00:00",
    )
    set_agent_ref(_FakeAgent(task_manager))

    plan_result = json.loads(
        set_execution_plan(
            "Research and compare top LLMs.",
            goal_summary="Produce a sourced top-five LLM comparison.",
            current_focus="Gather current external references.",
            rationale="Delegated specialist work needs its own local execution trajectory.",
        )
    )
    assert plan_result["task_id"] == task.task_id

    step_result = json.loads(
        update_execution_plan_step(
            title="Gather current references",
            rationale="The specialist needs current sources before synthesizing rankings.",
            step_id="research_sources",
            status="in_progress",
        )
    )
    assert step_result["step"]["step_id"] == "research_sources"

    digest_result = json.loads(get_execution_digest())
    assert digest_result["task_id"] == task.task_id
    assert digest_result["plan_id"] is None
    assert digest_result["active"] is True
    assert digest_result["digest"]["goal_summary"] == "Produce a sourced top-five LLM comparison."


def test_execution_plan_step_tool_requires_rationale():
    task_manager = TaskManager()
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Reject rationale-free steps",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager))

    result = json.loads(
        update_execution_plan_step(
            title="Add a step with no rationale",
            rationale="",
        )
    )
    assert result["error"] == "Execution plan items require a rationale."


def test_record_plan_action_tool_records_manager_action(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Recover from a tool budget warning.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
        )
    )

    result = json.loads(
        record_plan_action(
            "start_inline_recovery",
            "Retry with a narrower tool call.",
            reason="The tool result budget hit a warning threshold.",
            related_task_id="subtask_1",
            plan_step_id="verify_step",
            user_visible=True,
            details_json='{"failure_type": "tool_result_budget_pressure"}',
        )
    )

    assert result["plan_id"]
    assert result["plan_action"]["action_type"] == "start_inline_recovery"
    assert result["plan_action"]["details"]["failure_type"] == "tool_result_budget_pressure"

    digest_result = json.loads(get_execution_digest())
    assert digest_result["recent_plan_actions"][0]["action_id"] == result["plan_action"]["action_id"]
    assert "start_inline_recovery" in digest_result["markdown"]


def test_record_plan_action_tool_is_manager_only(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="specialist_agent",
        agent_name="Specialist",
        team_id="team_1",
        named_agent_type="CodeWritingAgent",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Specialists use the normal task path.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="manager", to_named_agent_type="CodeWritingAgent"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager, agent_id="specialist_agent", name="Specialist"))

    result = json.loads(record_plan_action("checkpoint", "Specialists cannot record manager plan actions."))

    assert result["manager_only"] is True


def test_after_action_report_review_tools_list_and_resolve(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    parent = Task(
        task_id="task_parent_aar",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Coordinate delegated research.",
    )
    task_manager.add_task(
        parent,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = parent.task_id
    report = AfterActionReport(
        report_id="aar_tool_review",
        source_task_id="task_child_aar",
        parent_task_id=parent.task_id,
        source_agent_id="specialist_agent",
        source_agent_name="Researcher",
        source_named_agent_type="WebResearchAgent",
        summary="Aggregator data was stale for this research class.",
        durable_context_candidates=["Prefer primary-source release notes for benchmark freshness."],
        risks_or_followups=["Check source publication dates before citing benchmark tables."],
        evidence_refs=["task_child_aar#benchmark_source"],
        recommended_importance=TaskHistoryImportance.HIGH,
    )
    recorded = task_manager.record_after_action_report(
        report, identity=NOBODY, agent_name="Researcher", agent_id="specialist_agent"
    )
    assert recorded is not None
    assert recorded.history_entry is not None
    assert recorded.history_entry.details["review_state"] == "pending_manager_review"

    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
        )
    )

    pending = json.loads(list_pending_after_action_reports(limit=5))

    assert pending["returned"] == 1
    review_id = pending["reviews"][0]["review_id"]
    assert pending["reviews"][0]["summary"] == "Aggregator data was stale for this research class."

    resolved = json.loads(
        resolve_after_action_report_review(
            review_id,
            AfterActionReportReviewStatus.SUMMARIZED_TO_TASK_HISTORY.value,
            "Keep this as a task-history lesson for future source triage.",
            review_rationale="The lesson is useful but not broad enough for pinned memory.",
        )
    )

    assert resolved["review"]["status"] == "summarized_to_task_history"
    assert resolved["history_entry_id"]
    assert any(
        history_entry.details.get("review_status") == "summarized_to_task_history"
        for history_entry in task_manager.get_task_info(parent.task_id).task.execution_history
    )
    assert json.loads(list_pending_after_action_reports(limit=5))["returned"] == 0


def test_late_after_action_reports_batch_and_schedule_review_task(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    parent = Task(
        task_id="task_parent_late_aar",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Coordinate delegated research.",
    )
    task_manager.add_task(
        parent,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.update_main_task_state(
        parent.task_id,
        ProgressState.IN_PROGRESS,
        "2026-03-18T00:00:00+00:00",
    )
    task_manager.complete_task(parent.task_id, "2026-03-18T00:01:00+00:00")

    opened_at = datetime(2026, 3, 18, 0, 1, 5, tzinfo=timezone.utc)
    first = AfterActionReport(
        report_id="aar_late_first",
        source_task_id="task_child_first",
        parent_task_id=parent.task_id,
        source_agent_id="specialist_1",
        source_agent_name="Researcher 1",
        summary="First late lesson.",
        durable_context_candidates=["Use primary sources first."],
        recommended_importance=TaskHistoryImportance.HIGH,
    )
    second = AfterActionReport(
        report_id="aar_late_second",
        source_task_id="task_child_second",
        parent_task_id=parent.task_id,
        source_agent_id="specialist_2",
        source_agent_name="Researcher 2",
        summary="Second late lesson.",
        risks_or_followups=["Verify dates on roundups."],
    )
    stale = AfterActionReport(
        report_id="aar_late_stale",
        source_task_id="task_child_stale",
        parent_task_id=parent.task_id,
        source_agent_id="specialist_3",
        source_agent_name="Researcher 3",
        summary="Too-late lesson.",
    )

    first_recorded = task_manager.record_after_action_report(
        first,
        identity=NOBODY,
        agent_name="Researcher 1",
        agent_id="specialist_1",
        timestamp=opened_at.isoformat(),
    )
    second_recorded = task_manager.record_after_action_report(
        second,
        identity=NOBODY,
        agent_name="Researcher 2",
        agent_id="specialist_2",
        timestamp=(opened_at + timedelta(seconds=5)).isoformat(),
    )
    stale_recorded = task_manager.record_after_action_report(
        stale,
        identity=NOBODY,
        agent_name="Researcher 3",
        agent_id="specialist_3",
        timestamp=(opened_at + timedelta(seconds=31)).isoformat(),
    )

    assert first_recorded is not None
    assert first_recorded.history_entry is None
    assert first_recorded.accepted is True
    assert first_recorded.schedule_required is True
    assert first_recorded.batch is not None
    assert second_recorded is not None
    assert second_recorded.batch.batch_id == first_recorded.batch.batch_id
    assert second_recorded.schedule_required is False
    assert stale_recorded is not None
    assert stale_recorded.accepted is False
    assert stale_recorded.stale is True
    assert stale_recorded.review.status == AfterActionReportReviewStatus.IGNORED

    archived_before = task_manager.get_task_from_db(parent.task_id)
    assert not [
        entry
        for entry in archived_before["main_task"].execution_history
        if (entry.details or {}).get("report_id") in {"aar_late_first", "aar_late_second"}
    ]

    review_task = task_manager.build_after_action_report_batch_review_task(
        first_recorded.batch.batch_id,
        task_id="task_aar_review_test",
    )

    assert review_task is not None
    assert "report_id: aar_late_first" in review_task.problem_statement
    assert "report_id: aar_late_second" in review_task.problem_statement
    assert "aar_late_stale" not in review_task.problem_statement

    closed_batch = task_manager.close_after_action_report_batch_collection_for_review(
        first_recorded.batch.batch_id,
        scheduled_review_task_id=review_task.task_id,
        timestamp=(opened_at + timedelta(seconds=35)).isoformat(),
    )
    assert closed_batch is not None
    assert closed_batch.report_count == 2
    assert closed_batch.stale_report_count == 1

    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
        )
    )
    resolved = json.loads(
        resolve_after_action_report_review(
            status=AfterActionReportReviewStatus.SUMMARIZED_TO_TASK_HISTORY.value,
            decision_summary="Keep the primary-source lesson in task history.",
            report_id="aar_late_first",
        )
    )
    assert resolved["review"]["report_id"] == "aar_late_first"
    assert resolved["history_entry_id"]


def test_active_after_action_reports_join_batch_scheduled_on_completion(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    parent = Task(
        task_id="task_parent_active_aar",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Coordinate active work.",
    )
    task_manager.add_task(
        parent,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_active_aar"),
        owner_person_id=None,
    )
    report = AfterActionReport(
        report_id="aar_active_first",
        source_task_id="task_child_active",
        parent_task_id=parent.task_id,
        source_agent_id="specialist_active",
        source_agent_name="Active Specialist",
        summary="Active AAR should be reviewed after parent completion.",
    )

    recorded = task_manager.record_after_action_report(
        report,
        identity=NOBODY,
        agent_name="Active Specialist",
        agent_id="specialist_active",
        timestamp="2026-03-18T00:00:10+00:00",
    )

    assert recorded is not None
    assert recorded.accepted is True
    assert recorded.schedule_required is False
    assert recorded.batch is not None
    assert recorded.review.batch_id == recorded.batch.batch_id
    assert recorded.history_entry.details["batch_id"] == recorded.batch.batch_id

    [scheduled_batch] = task_manager.complete_task(parent.task_id, "2026-03-18T00:01:00+00:00")

    assert scheduled_batch.batch_id == recorded.batch.batch_id
    assert scheduled_batch.accept_until == "2026-03-18T00:01:30+00:00"
    assert scheduled_batch.scheduled_review_at == "2026-03-18T00:01:35+00:00"
    review_task = task_manager.build_after_action_report_batch_review_task(
        scheduled_batch.batch_id,
        task_id="task_active_aar_review",
    )
    assert review_task is not None
    assert "report_id: aar_active_first" in review_task.problem_statement


def test_final_review_loop_suppresses_once_for_unresolved_plan_items(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_id="task_final_review",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Finish only after unresolved plan state is reviewed.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.update_main_task_state(task.task_id, ProgressState.IN_PROGRESS, "2026-04-29T00:00:00+00:00")
    task_manager.upsert_execution_plan_item(
        task.task_id,
        title="Validate evidence",
        rationale="The final answer depends on the validation result.",
        status=ExecutionPlanItemStatus.PLANNED,
    )

    review = task_manager.begin_final_review_if_needed(
        task.task_id,
        attempted_answer="Premature final answer.",
        agent_name="Manager",
        agent_id="manager_agent",
        timestamp="2026-04-29T00:01:00+00:00",
    )

    assert review is not None
    assert review["attempt"] == 1
    assert review["open_plan_items"][0]["title"] == "Validate evidence"
    assert task_manager.is_final_review_pending(task.task_id) is True
    assert task_manager.get_task_info(task.task_id).state == ProgressState.PARTIAL
    assert task_manager.get_next_task_to_process() == task.task_id
    task_manager.update_main_task_state(task.task_id, ProgressState.IN_PROGRESS, "2026-04-29T00:02:00+00:00")
    assert task_manager.is_final_review_pending(task.task_id) is False

    second_review = task_manager.begin_final_review_if_needed(
        task.task_id,
        attempted_answer="Complete with caveats.",
        agent_name="Manager",
        agent_id="manager_agent",
        timestamp="2026-04-29T00:03:00+00:00",
    )

    assert second_review is None
    history_summaries = [entry.summary for entry in task_manager.get_task_info(task.task_id).task.execution_history]
    assert any("Final review required before completion" in summary for summary in history_summaries)


def test_runtime_batch_projection_preserves_pending_final_review(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_id="task_final_review_projection",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Do not complete before final review.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.update_main_task_state(task.task_id, ProgressState.IN_PROGRESS, "2026-04-29T00:00:00+00:00")
    task_manager.upsert_execution_plan_item(
        task.task_id,
        title="Review evidence",
        rationale="The completion needs a final review pass.",
        status=ExecutionPlanItemStatus.PLANNED,
    )

    review = task_manager.begin_final_review_if_needed(
        task.task_id,
        attempted_answer="Premature final answer.",
        agent_name="Manager",
        agent_id="manager_agent",
        timestamp="2026-04-29T00:01:00+00:00",
    )
    assert review is not None

    projected = task_manager.project_task_state_after_batch(
        task.task_id,
        batch_id="turn_batch_final_review",
        reason="Suppressed final answer batch completed.",
        timestamp="2026-04-29T00:01:01+00:00",
    )

    assert projected == ProgressState.PARTIAL
    assert task_manager.is_final_review_pending(task.task_id) is True
    assert task_manager.get_task_info(task.task_id).state == ProgressState.PARTIAL
    assert task_manager.get_next_task_to_process() == task.task_id


def test_runtime_batch_projection_leaves_unblocked_root_runnable_without_claiming_slot(
    tmp_path,
):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_id="task_unblocked_projection",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Continue after the runtime batch commits.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    assert task_manager.get_next_task_to_process() == task.task_id
    task_manager.update_main_task_state(
        task.task_id,
        ProgressState.IN_PROGRESS,
        "2026-04-29T00:00:00+00:00",
    )

    projected = task_manager.project_task_state_after_batch(
        task.task_id,
        batch_id="turn_batch_unblocked",
        reason="The runtime batch committed without an external wait.",
        timestamp="2026-04-29T00:00:01+00:00",
    )

    assert projected == ProgressState.PARTIAL
    assert task_manager.get_task_info(task.task_id).state == ProgressState.PARTIAL
    assert task_manager.current_task_id is None
    assert task_manager.enqueue_task(task.task_id, front=True) is True
    assert task_manager.get_next_task_to_process() == task.task_id


def test_activate_plan_execution_tool_hides_runtime_ids(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Run a complex task through plan-step execution.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", response_routing_key="user_queue", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
        )
    )

    result = json.loads(
        activate_plan_execution(
            json.dumps(
                [
                    {
                        "title": "Inspect",
                        "objective": "Inspect the relevant runtime behavior.",
                        "step_id": "model_must_not_control_this",
                    }
                ]
            ),
            final_review=True,
        )
    )

    assert result["plan_execution_started"] is True
    assert result["metadata_hidden"] is True
    assert result["ids_available_to_model"] is False
    assert "plan_id" not in result
    assert "task_id" not in result
    plan_id = task_manager.get_task_plan_id(task.task_id)
    assert plan_id is not None
    plan = task_manager.get_plan(plan_id)
    assert plan is not None
    assert len(plan.plan_steps) == 2
    assert "model_must_not_control_this" not in {step.step_id for step in plan.plan_steps}


def test_activate_plan_execution_tool_is_manager_only(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="specialist_agent",
        agent_name="Specialist",
        team_id="team_1",
        named_agent_type="CodeWritingAgent",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Specialists should not activate manager plan execution.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="manager", to_named_agent_type="CodeWritingAgent"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager, agent_id="specialist_agent", name="Specialist"))

    result = json.loads(
        activate_plan_execution(
            '[{"title":"Inspect","objective":"Inspect behavior."}]',
            final_review=False,
        )
    )

    assert result["manager_only"] is True


def test_get_runtime_pressure_reports_current_manager_task_pressure(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_id="task_pressure",
        task_type=MessageMetaType.REQUEST,
        problem_statement=" ".join(["large-context"] * 120),
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="conversation_1"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    task_manager.log_agent_note(
        task.task_id,
        note="Verifier timeout after repeated attempts.",
        agent_name="Manager",
        agent_id="manager_agent",
    )
    fake_messages = [
        {
            "role": "assistant",
            "content": [
                {"toolUse": {"name": "get_execution_digest", "input": {}}},
                {"toolUse": {"name": "log_task_history_entry", "input": {"note": "timeout"}}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"content": [{"text": "x" * 240}]}}
            ],
        },
    ]
    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
            context_window_tokens=1024,
            max_tool_calls_per_task=4,
            strands_messages=fake_messages,
        )
    )

    result = json.loads(get_runtime_pressure())

    assert result["current_task_id"] == task.task_id
    assert result["context_window_tokens"] == 1024
    assert result["estimated_context_tokens"] > 0
    assert result["context_fill_ratio"] is not None
    assert result["context_pressure_level"] in {"low", "moderate", "high", "critical"}
    assert result["tool_call_budget_used"] == 2
    assert result["tool_call_budget_limit"] == 4
    assert result["tool_call_budget_fill_ratio"] == 0.5
    assert result["tool_result_tokens_observed"] > 0
    assert result["inner_loop_context_limit_tokens"] == 400
    assert result["pending_subtask_count"] == 0
    assert result["recent_failure_count"] >= 1
    assert result["context_estimate"]["method"] == "strands_model_count_tokens"
    assert {source["source"] for source in result["context_estimate"]["sources"]}.issuperset(
        {"incoming_message", "task_history_summary", "strands_message_buffer"}
    )


def test_get_runtime_pressure_is_manager_only(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="specialist_agent",
        agent_name="Specialist",
        team_id="team_1",
        named_agent_type="CodeWritingAgent",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Specialists should not receive manager pressure tools.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="manager", to_named_agent_type="CodeWritingAgent"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager, agent_id="specialist_agent", name="Specialist"))

    result = json.loads(get_runtime_pressure())

    assert result["manager_only"] is True


def test_start_isolated_recovery_tool_queues_fresh_manager_task(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task context that should not be replayed into recovery.",
    )
    task_manager.add_task(
        task,
        MessageHeader(
            from_agent_id="live-test-user",
            response_routing_key="user_queue",
            agent_signature="",
        ),
        MessageMeta(
            from_agent_name="live-test-user",
            requester_type=None,
            is_response_requested=True,
            msg_correlation_id="corr_original",
            conversation_id="conversation_original",
        ),
        owner_person_id=None,
    )
    task_manager.set_task_plan(
        task.task_id,
        "Diagnose the verifier timeout and choose a smaller retry.",
        goal_summary="Recover from delegated verification failure.",
        current_focus="Isolated recovery.",
    )
    task_manager.append_history_entry(
        task.task_id,
        "Verifier subtask task_verify failed with delegated_specialist_timeout.",
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
        related_task_id="task_verify",
        details={
            "task_id": task.task_id,
            "affected_subtask_id": "task_verify",
            "conversation_id": "conversation_original",
            "error": "Verifier did not return before timeout.",
        },
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    schedule_store = ScheduleStore(db_path=str(tmp_path / "schedules.db"))
    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
            schedule_store=schedule_store,
        )
    )

    result = json.loads(
        start_isolated_recovery(
            failure_type="delegated_specialist_timeout",
            failure_summary="Verifier did not return before the manager timeout.",
            recovery_brief="Inspect the verifier timeout and propose a smaller retry.",
            affected_step_id="plan_step_verify",
            affected_subtask_id="task_verify",
            notify_user=True,
            details_json='{"retry_budget": 1}',
            checkpoint_delay_seconds=300,
        )
    )

    assert result["queued"] is True
    assert result["recovery_started"] is True
    assert result["metadata_hidden"] is True
    assert "original_task_id" not in result
    assert "recovery_task_id" not in result
    assert "plan_id" not in result
    assert result["recovery_task_payload_has_plan_id"] is False
    assert result["user_notice_recorded"] is True
    assert result["checkpoint_schedule"] == {"scheduled": True}

    recovery_links = [
        link
        for link in task_manager.plan_state.recovery_links.values()
        if link.original_task_id == task.task_id
    ]
    assert len(recovery_links) == 1
    recovery_task_id = recovery_links[0].recovery_task_id
    assert recovery_links[0].original_plan_id == task_manager.get_task_plan_id(task.task_id)
    assert recovery_links[0].checkpoint_schedule_id is not None
    checkpoint = schedule_store.get_scheduled_message(recovery_links[0].checkpoint_schedule_id)
    assert checkpoint is not None
    assert checkpoint.claim_agent_id == "manager_agent"
    claim_time = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert schedule_store._repository.claim_next_due(
        now_iso=claim_time.isoformat(), claim_expires_at=(claim_time + timedelta(minutes=2)).isoformat(),
        claim_token="sibling_claim",
        claimer_agent_id="sibling_manager", claimer_named_agent_type="TeamArchitect", claimer_team_id="team_1",
    ) is None

    recovery_info = task_manager.get_task_info(recovery_task_id)
    assert recovery_info is not None
    assert recovery_info.task.parent_task_id is None
    assert recovery_info.meta.followup_to_task_id is None
    assert recovery_info.meta.conversation_id != "conversation_original"
    assert recovery_info.meta.msg_correlation_id.startswith("recovery_corr_")
    assert recovery_info.header.to_routing_path == "manager_agent"
    assert recovery_info.task.problem_statement is not None
    assert "Initial sanitized recovery context" in recovery_info.task.problem_statement
    assert "Inspect the verifier timeout" in recovery_info.task.problem_statement
    assert "Recover from delegated verification failure" in recovery_info.task.problem_statement
    assert "Verifier did not return before timeout" in recovery_info.task.problem_statement
    assert "Original task context that should not be replayed" not in recovery_info.task.problem_statement
    assert task.task_id not in recovery_info.task.problem_statement
    assert recovery_task_id not in recovery_info.task.problem_statement
    assert "task_verify" not in recovery_info.task.problem_statement
    assert "conversation_original" not in recovery_info.task.problem_statement
    assert "plan_id" not in recovery_info.task.model_dump()

    plan = task_manager.get_plan(task_manager.get_task_plan_id(task.task_id))
    assert plan is not None
    assert recovery_task_id in plan.related_task_ids
    assert recovery_task_id in plan.recovery_task_ids
    recovery_actions = [
        action for action in plan.plan_actions if action.action_type.value == "start_isolated_recovery"
    ]
    assert recovery_actions[-1].details["retry_budget"] == 1
    assert any(action.action_type.value == "user_notice" for action in plan.plan_actions)


def test_start_isolated_recovery_tool_is_manager_only(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="specialist_agent",
        agent_name="Specialist",
        team_id="team_1",
        named_agent_type="CodeWritingAgent",
    )
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Specialists must stay on the normal task path.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="manager", to_named_agent_type="CodeWritingAgent"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager, agent_id="specialist_agent", name="Specialist"))

    result = json.loads(
        start_isolated_recovery(
            failure_type="context_pressure",
            failure_summary="The specialist should not create manager recovery lanes.",
            recovery_brief="This should be rejected.",
        )
    )

    assert result["manager_only"] is True


def test_get_recovery_context_returns_sanitized_original_context(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    original_task = Task(
        task_id="task_original_secret",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Original task details: retry the failing verifier without replaying every prior step.",
        errors=["2026-04-28T12:00:00+00:00: verifier task_verify_secret timed out"],
    )
    task_manager.add_task(
        original_task,
        MessageHeader(
            from_agent_id="user_agent",
            response_routing_key="user_queue_secret",
            agent_signature="",
        ),
        MessageMeta(
            from_agent_name="user",
            conversation_id="conversation_secret",
            msg_correlation_id="corr_secret",
        ),
        owner_person_id=None,
    )
    task_manager.set_task_plan(
        original_task.task_id,
        "Investigate the timeout and retry with a smaller verification task.",
        goal_summary="Complete the original user request after a specialist timeout.",
        current_focus="Recovery",
    )
    task_manager.append_history_entry(
        original_task.task_id,
        "Subtask task_verify_secret failed after waiting for response on conversation_secret.",
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
        related_task_id="task_verify_secret",
        details={
            "task_id": "task_original_secret",
            "subtask_id": "task_verify_secret",
            "conversation_id": "conversation_secret",
            "error": "Verifier timed out.",
        },
    )
    recovery_info = task_manager.start_isolated_recovery_task(
        original_task.task_id,
        failure_type="delegated_specialist_timeout",
        failure_summary="Verifier timed out while checking the implementation.",
        recovery_brief="Determine whether to retry, split, or bypass verification.",
        affected_subtask_id="task_verify_secret",
        recovery_task_id="recovery_task_secret",
        branch_id="recovery_branch_secret",
        header=MessageHeader(from_agent_id="user_agent", response_routing_key="user_queue_secret", agent_signature=""),
        meta=MessageMeta(from_agent_name="user", conversation_id="recovery_conversation_secret"),
    )
    assert recovery_info is not None
    task_manager.task_state.current_in_progress_task_id = "recovery_task_secret"
    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
        )
    )

    result = json.loads(get_recovery_context(detail_level="all", max_entries=10))
    raw_result = json.dumps(result)

    assert result["recovery_context"] is True
    assert result["metadata_hidden"] is True
    assert result["summary"]["failure"]["type"] == "delegated_specialist_timeout"
    assert "Original task details" in result["summary"]["original_task"]["problem_statement"]
    assert result["plan_state"]["goal_summary"].startswith("Complete the original user request")
    assert result["failure_window"]["returned"] >= 1
    assert "Verifier timed out" in raw_result
    assert "task_original_secret" not in raw_result
    assert "recovery_task_secret" not in raw_result
    assert "task_verify_secret" not in raw_result
    assert "conversation_secret" not in raw_result
    assert "corr_secret" not in raw_result
    assert "user_queue_secret" not in raw_result


def test_get_recovery_context_requires_current_recovery_task(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="manager_agent",
        agent_name="Manager",
        team_id="team_1",
        named_agent_type="TeamArchitect",
    )
    task = Task(task_type=MessageMetaType.REQUEST, problem_statement="Normal manager task.")
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(
        _FakeAgent(
            task_manager,
            agent_id="manager_agent",
            name="Manager",
            named_agent_type="TeamArchitect",
        )
    )

    result = json.loads(get_recovery_context())

    assert result["recovery_task_only"] is True


def test_get_recovery_context_is_manager_only(tmp_path):
    task_manager = TaskManager(db_path=str(tmp_path / "task_history.db"))
    task_manager.set_owner_identity(
        agent_id="specialist_agent",
        agent_name="Specialist",
        team_id="team_1",
        named_agent_type="CodeWritingAgent",
    )
    task = Task(task_type=MessageMetaType.REQUEST, problem_statement="Specialist task.")
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="manager_agent", agent_signature=""),
        MessageMeta(from_agent_name="Manager"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager, agent_id="specialist_agent", name="Specialist"))

    result = json.loads(get_recovery_context())

    assert result["manager_only"] is True


def test_get_execution_digest_reports_inactive_for_plan_only_task():
    task_manager = TaskManager()
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Carry only a simple plan string",
        task_plan="Do the obvious thing and finish quickly.",
    )
    task_manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user"),
        owner_person_id=None,
    )
    task_manager.task_state.current_in_progress_task_id = task.task_id
    set_agent_ref(_FakeAgent(task_manager))

    digest_result = json.loads(get_execution_digest())

    assert digest_result["task_id"] == task.task_id
    assert digest_result["plan_id"] is None
    assert digest_result["active"] is False
    assert digest_result["digest"]["plan_summary"] == "Do the obvious thing and finish quickly."
    assert "## Execution Digest" in digest_result["markdown"]


def _signed_task(manager: TaskManager, task_id: str, person_id: str | None, *, signed: bool = True) -> Task:
    """A task working for ``person_id`` (None: for no person), with a gateway-signed context."""

    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Work of {task_id}.")
    gateway_context = (
        GatewayContext(
            session_key=f"session_{task_id}",
            canonical_user_id=person_id,
            task_context=context_signer().issue(
                team_id="team_1", person_id=person_id, person_roles=("member",) if person_id else ()
            ),
        )
        if signed
        else None
    )
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id=f"conv_{task_id}", gateway_context=gateway_context),
        owner_person_id=person_id,
    )
    return task


@pytest.fixture
def _people_tasks(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Tasks of two people and of system work, each with a digest of its own; one of B's is archived."""

    use_signed_contexts(monkeypatch)
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}}, base_dir=str(tmp_path)
    )
    manager = TaskManager(db_path=settings.sqlite_path, storage_settings=settings)
    manager.set_owner_identity(agent_id="manager_agent", agent_name="Manager", team_id="team_1")
    agent = _FakeAgent(manager, agent_id="manager_agent", name="Manager", named_agent_type="TeamArchitect")
    agent.team_id = "team_1"
    agent.authorization_config = {"enabled": False}
    set_agent_ref(agent)
    tasks = {
        "a": _signed_task(manager, "task_a", "usr_a"),
        "a_other": _signed_task(manager, "task_a_other", "usr_a"),
        "b": _signed_task(manager, "task_b", "usr_b"),
        "b_done": _signed_task(manager, "task_b_done", "usr_b"),
        "system": _signed_task(manager, "task_system", None),
        "system_other": _signed_task(manager, "task_system_other", None),
        "unsigned": _signed_task(manager, "task_unsigned", None, signed=False),
    }
    for name, task in tasks.items():
        assert manager.set_task_plan(task.task_id, f"The private plan of {name}.") is not None
    manager._save_completed_task(tasks["b_done"].task_id, "completed")
    assert manager.get_task_info(tasks["b_done"].task_id) is None
    return manager, tasks


def _digest_as(manager: TaskManager, acting: Task, task: Task) -> dict:
    manager.task_state.current_in_progress_task_id = acting.task_id
    return json.loads(get_execution_digest(task_id=task.task_id))


def test_a_persons_task_reads_only_that_persons_task_digests(_people_tasks, caplog) -> None:
    manager, tasks = _people_tasks

    own = _digest_as(manager, tasks["a"], tasks["a_other"])
    assert own["digest"]["plan_summary"] == "The private plan of a_other."
    for other in ("b", "b_done", "system"):
        with caplog.at_level("INFO", logger="access_control.ownership"):
            refused = _digest_as(manager, tasks["a"], tasks[other])
        # Live or archived, another person's task reads as one that does not exist.
        assert refused == {"error": f"Task {tasks[other].task_id} was not found among this person's tasks."}
        assert f"resource_id={tasks[other].task_id} action=private.read" in caplog.text
    assert _digest_as(manager, tasks["b"], tasks["b_done"])["digest"]["plan_summary"] == "The private plan of b_done."


def test_work_for_no_person_reads_only_digests_of_work_for_no_person(_people_tasks, caplog) -> None:
    manager, tasks = _people_tasks

    assert _digest_as(manager, tasks["system"], tasks["system_other"])["digest"] is not None
    assert "error" in _digest_as(manager, tasks["system"], tasks["a"])
    # A task without a verified identity reads its own digest, and no other.
    assert _digest_as(manager, tasks["unsigned"], tasks["unsigned"])["digest"] is not None
    with caplog.at_level("WARNING", logger="custom_tools.providers._runtime"):
        refused = _digest_as(manager, tasks["unsigned"], tasks["system"])
    assert "error" in refused and "digest" not in refused
    assert "event=task_identity_unverified" in caplog.text
