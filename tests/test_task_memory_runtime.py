from __future__ import annotations

import logging

from custom_agents.task_memory_delta_runtime import derive_task_turn_delta
from custom_agents.task_memory_snapshot_runtime import persist_task_memory_projection
from storage.config import resolve_storage_settings
from storage.memory_graph_repository import create_memory_graph_repository


def test_derive_task_turn_delta_extracts_structured_state():
    derived = derive_task_turn_delta(
        {
            "task_id": "task_123",
            "task_state": [["in_progress", "2026-04-13T10:00:00+00:00"], ["completed", "2026-04-13T10:10:00+00:00"]],
            "execution_digest": {
                "goal_summary": "Implement the memory graph.",
                "plan_summary": "Finish schema and runtime integration.",
                "current_focus": "Testing task-native projection.",
                "plan_items": [
                    {"title": "Write repository methods", "status": "completed"},
                    {"title": "Add tests", "status": "in_progress", "status_detail": "integration coverage"},
                ],
            },
            "execution_history_entries": [
                {"entry_type": "error_event", "summary": "Blocked waiting on migration detail."},
                {
                    "entry_type": "task_event",
                    "summary": "Delegated comparison research to ResearchComparisonAgent.",
                    "details": {"assignee_name": "ResearchComparisonAgent", "handoff_kind": "delegate"},
                },
                {"entry_type": "agent_note", "summary": "Lesson learned: avoid silent fallback logic."},
            ],
            "request_text": "Please continue implementing the memory graph.",
            "turn_summary": "Lesson learned: keep task-native state explicit.",
            "final_answer_text": "Memory graph implementation completed successfully.",
            "task_errors": [],
        }
    )

    assert derived.delta["status"] == "completed"
    assert derived.delta["blockers"]
    assert derived.delta["next_actions"]
    assert derived.delta["lessons"]
    assert derived.handoff is not None


def test_persist_task_memory_projection_writes_delta_snapshot_and_case_memory(tmp_path):
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}})
    repo = create_memory_graph_repository(settings, logger=logging.getLogger("test"))
    source_payload = {
        "task_id": "task_123",
        "conversation_id": "conversation_123",
        "turn_index": 2,
        "owner_named_agent_type": "TeamArchitect",
        "request_text": "Please continue implementing the memory graph.",
        "turn_summary": "Completed task-native projection.",
        "final_answer_text": "Done.",
        "task_answer": {"answer": "Done.", "tools_used": ["shell", "pytest"]},
    }
    derived = derive_task_turn_delta(
        {
            **source_payload,
            "task_state": [["completed", "2026-04-13T10:10:00+00:00"]],
            "execution_digest": {"plan_summary": "Ship tests."},
            "execution_history_entries": [{"entry_type": "agent_note", "summary": "Lesson learned: test the real path."}],
            "task_errors": [],
        }
    )

    projection = persist_task_memory_projection(
        repository=repo,
        source_payload=source_payload,
        derived_delta=derived,
    )

    deltas = repo.list_task_turn_deltas(task_id="task_123", limit=10)
    snapshot = repo.get_latest_task_state_snapshot(task_id="task_123")
    case_memories = repo.list_case_memories(task_id="task_123", limit=10)

    assert projection.delta_id
    assert snapshot is not None
    assert snapshot.current_summary
    assert deltas and deltas[0].task_id == "task_123"
    assert case_memories and case_memories[0].task_id == "task_123"
