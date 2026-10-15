from __future__ import annotations

import json
from pathlib import Path

from custom_agents.context_engine.thread_renderer import (
    BudgetedThreadContextRenderer,
)
from custom_agents.conversation_compaction import RecursiveLargeTextArtifactOffloader
from custom_agents.conversation_compaction.base import ConversationCompactionConfig
from custom_agents.conversation_compaction.heuristic_compactor import HeuristicSlidingWindowCompactor
from custom_agents.conversation_store import ConversationStore


def _store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")


def _seed_thread(store: ConversationStore, *, conversation_id: str = "conversation_epoch") -> None:
    store.ensure_thread(
        conversation_id=conversation_id,
        team_id="team_1",
        owner_agent_id="agent_1",
        owner_agent_name="manager",
        requester_name="user",
        requester_id="user_1",
        root_task_id="task_1",
        subject_preview="subject",
    )
    for index in range(1, 5):
        task_id = f"task_{index}"
        store.append_turn(
            conversation_id=conversation_id,
            task_id=task_id,
            followup_to_task_id=f"task_{index - 1}" if index > 1 else None,
            owner_agent_id="agent_1",
            requester_name="user",
            requester_id="user_1",
            correlation_id=f"corr_{index}",
            problem_text=f"Request {index}",
            requester_type="user_req",
            delivery_state="not_applicable",
            task_status="completed",
        )
        store.update_turn_completion(
            task_id=task_id,
            task_status="completed",
            final_answer_text=f"Answer {index}",
            turn_summary_text=f"Summary {index}",
            user_request_summary=f"Request summary {index}",
            assistant_response_summary=f"Assistant summary {index}",
            context_summary_text=f"Context summary {index}",
            artifact_summary=[],
            tool_artifact_manifest=[],
            summary_generator="test",
        )


def test_renderer_uses_epoch_boundary_without_replaying_pre_epoch_raw_turns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_thread(store)
    epoch = store.create_context_epoch(
        conversation_id="conversation_epoch",
        trigger="manual",
        through_turn_index=2,
        preserved_suffix_start_turn=3,
        summary_json={"summary_markdown": "Epoch captured earlier requests."},
        summary_markdown="Epoch captured earlier requests.",
        rehydration_manifest={"source_turn_range": [1, 2]},
    )

    rendered = BudgetedThreadContextRenderer().render(
        store=store,
        conversation_id="conversation_epoch",
        exclude_task_id="task_4",
        available_tokens=10_000,
    )

    assert epoch["context_epoch_id"] in rendered.text
    assert "Epoch captured earlier requests." in rendered.text
    assert "Request 1" not in rendered.text
    assert "Answer 1" not in rendered.text
    assert "Request 3" in rendered.text
    assert rendered.metadata["context_epoch_id"] == epoch["context_epoch_id"]
    assert rendered.metadata["epoch_through_turn_index"] == 2


def test_heuristic_compactor_writes_epoch_without_overwriting_turn_summaries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_thread(store)
    compactor = HeuristicSlidingWindowCompactor(
        ConversationCompactionConfig(
            context_window_tokens=200,
            trigger_ratio=0.01,
            target_ratio=0.01,
            min_recent_turns=1,
            max_recent_turns=1,
        )
    )

    result = compactor.compact_if_needed(
        store=store,
        conversation_id="conversation_epoch",
        latest_request_text="next",
    )

    assert result.compacted is True
    assert result.context_epoch_id
    epochs = store.list_context_epochs("conversation_epoch", limit=10)
    assert len(epochs) >= 2
    assert any(epoch["rehydration_manifest"].get("source_epoch_ids") for epoch in epochs[:-1])
    epoch = store.get_context_epoch(result.context_epoch_id)
    assert epoch["rehydration_manifest"]["summary_layering"].startswith("epoch_rollup")
    assert "context_summary_text" in epoch["rehydration_manifest"]["source_summary_fields"]
    assert store.get_turn_by_task_id("task_1")["context_summary_text"] == "Context summary 1"


class _ArtifactStore:
    def __init__(self) -> None:
        self.offloaded = []

    def offload_large_text(self, **kwargs):
        self.offloaded.append(kwargs)
        return {
            "artifact_id": "artifact_raw",
            "conversation_id": kwargs["conversation_id"],
            "task_id": kwargs["task_id"],
            "artifact_type": kwargs["artifact_type"],
            "storage_path": "/tmp/raw.txt",
            "summary_text": kwargs["summary_text"],
        }


def test_offloaded_tool_result_marker_is_preserved_as_manifest_reference() -> None:
    marker = {
        "offloaded_tool_result": True,
        "tool_name": "big_tool",
        "tool_use_id": "tooluse_1",
        "artifact_manifest_path": "/workspace/artifacts/tool_results/task/manifest.json",
        "original_tokens": 12345,
        "original_chars": 90000,
    }
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "tooluse_1",
                        "content": [{"text": json.dumps(marker)}],
                    }
                }
            ],
        }
    ]
    store = _ArtifactStore()

    artifacts = RecursiveLargeTextArtifactOffloader(min_chars=10).extract_large_text_artifacts(
        store=store,
        conversation_id="conversation_epoch",
        task_id="task_1",
        messages=messages,
    )

    assert store.offloaded == []
    assert artifacts[0]["artifact_type"] == "tool_result_manifest"
    assert artifacts[0]["storage_path"] == marker["artifact_manifest_path"]
    assert artifacts[0]["source"] == "offloaded_tool_result"
