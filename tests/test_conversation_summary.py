from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
from pathlib import Path
import re
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from strands.models import Model

from custom_agents.conversation_compaction.base import ConversationCompactionConfig
from custom_agents.conversation_compaction.llm_compactor import LLMSummarizingConversationCompactor
from custom_agents.conversation_store import ConversationStore
from custom_agents.conversation_summary import (
    ConversationSummaryConfig,
    LLMTurnSummaryGenerator,
    TurnSummaryRequest,
    strip_redundant_answer_from_context_summary,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.runtime_context import set_agent_ref, task_work


class _RecordingSummaryModel(Model):
    """Real SDK calls, with offline responses derived from everything the model saw.

    Echoing all thread markers into the result also exposes a contaminated
    prompt in the saved compaction epoch, instead of hiding it with fixed output.
    """

    def __init__(self, output=None, *, barrier=None, fail=False):
        self.calls = []
        self.output = output
        self.barrier = barrier
        self.fail = fail
        self.lock = Lock()

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "offline-summary"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("The SDK must use its structured-output tool.")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.fail:
            raise RuntimeError("summary model unavailable")
        with self.lock:
            self.calls.append(deepcopy(messages))
            call_id = f"summary-{len(self.calls)}"
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        tool_name = next(
            spec["name"] for spec in tool_specs
            if spec["name"] in {"StructuredTurnSummary", "StructuredConversationSynopsis"}
        )
        markers = " ".join(sorted(set(re.findall(r"THREAD_[AB]_(?:PRIVATE|NEXT)_SENTINEL", json.dumps(messages)))))
        if self.output is not None:
            output = self.output
        elif tool_name == "StructuredTurnSummary":
            output = {
                "user_request_summary": markers,
                "assistant_response_summary": markers,
                "context_summary_text": markers,
                "artifact_summary": [],
            }
        else:
            output = {"summary_markdown": markers, "important_facts": [markers]}
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {"name": tool_name, "toolUseId": call_id}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(output)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_llm_turn_summary_generator_uses_structured_output() -> None:
    model = _RecordingSummaryModel(
        dict(
            user_request_summary="User asked for a migration plan.",
            assistant_response_summary="Assistant produced a phased plan.",
            context_summary_text="Keep the plan durable and preserve retrieval hooks.",
            artifact_summary=[
                {
                    "artifact_type": "plan",
                    "storage_path": "plans/context.md",
                    "summary_text": "Context plan.",
                }
            ],
        )
    )
    generator = LLMTurnSummaryGenerator(
        config=ConversationSummaryConfig(default_model_provider="openai", default_model_id="gpt-test"),
        logger=logging.getLogger("test"),
        summarization_model=model,
    )

    result = generator.summarize_turn(
        TurnSummaryRequest(
            task=Task(
                task_id="task_summary",
                task_type=MessageMetaType.REQUEST,
                problem_statement="Please create a migration plan.",
                answer="Here is the phased plan.",
            ),
            final_answer_text="Here is the phased plan.",
            artifact_manifest=[],
        )
    )

    assert len(model.calls) == 1
    assert "Please create a migration plan." in json.dumps(model.calls[0])
    assert result.user_request_summary == "User asked for a migration plan."
    assert result.assistant_response_summary == "Assistant produced a phased plan."
    assert result.context_summary_text == "Keep the plan durable and preserve retrieval hooks."
    assert result.artifact_summary[0]["storage_path"] == "plans/context.md"
    assert result.summary_generator == "llm_summarizing"


def test_llm_turn_summary_generator_falls_back_to_heuristic_on_failure() -> None:
    generator = LLMTurnSummaryGenerator(
        logger=logging.getLogger("test"),
        summarization_model=_RecordingSummaryModel(fail=True),
    )

    result = generator.summarize_turn(
        TurnSummaryRequest(
            task=Task(
                task_id="task_fallback",
                task_type=MessageMetaType.REQUEST,
                problem_statement="Summarize this even if the model fails.",
                answer="Fallback answer.",
            ),
            final_answer_text="Fallback answer.",
        )
    )

    assert result.user_request_summary == "Summarize this even if the model fails."
    assert result.assistant_response_summary == "Fallback answer."
    assert result.context_summary_text == ""
    assert result.summary_generator == "heuristic"


def _seed_thread(store: ConversationStore, name: str, *, next_marker: str | None = None) -> None:
    conversation_id = f"conversation_{name}"
    store.ensure_thread(
        conversation_id=conversation_id,
        team_id="team_example",
        owner_canonical_user_id="person_example",
        owner_agent_id="lead",
        owner_agent_name="Lead",
        requester_name="exampleuser",
        requester_id="person_example",
        root_task_id=f"task_{name}_1",
    )
    for index in (1, 2):
        marker = next_marker if index == 2 and next_marker else f"THREAD_{name}_PRIVATE_SENTINEL"
        _append_thread_turn(store, name, index, marker)


def _append_thread_turn(store: ConversationStore, name: str, index: int, marker: str) -> None:
    task_id = f"task_{name}_{index}"
    store.append_turn(
        conversation_id=f"conversation_{name}",
        task_id=task_id,
        followup_to_task_id=None,
        owner_agent_id="lead",
        requester_name="exampleuser",
        requester_id="person_example",
        correlation_id=f"correlation_{task_id}",
        problem_text=marker,
        task_status="completed",
        requester_type="user_req",
        delivery_state="not_applicable",
    )
    store.update_turn_completion(
        task_id=task_id,
        task_status="completed",
        final_answer_text=marker,
        turn_summary_text=marker,
    )


@pytest.mark.parametrize("operation", ["summary", "compaction"])
@pytest.mark.parametrize("own_model", [False, True], ids=["team_model", "same_person_model"])
@pytest.mark.parametrize("overlap", [False, True], ids=["repeated", "overlapping"])
def test_independent_summary_operations_keep_only_their_thread(
    tmp_path: Path, caplog, operation: str, own_model: bool, overlap: bool,
) -> None:
    """One service and selected model can serve both threads without sharing SDK state."""

    team_model = _RecordingSummaryModel()
    person_model = _RecordingSummaryModel()
    selected_model = person_model if own_model else team_model
    selected_model.barrier = Barrier(2) if overlap else None
    store = ConversationStore(str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "artifacts")
    if operation == "summary":
        service = LLMTurnSummaryGenerator(summarization_model=team_model)
    else:
        service = LLMSummarizingConversationCompactor(
            ConversationCompactionConfig(
                context_window_tokens=100_000, trigger_turn_count=1,
                min_recent_turns=1, max_recent_turns=1,
            ),
            summarization_model=team_model,
        )
        for name in ("A", "B"):
            _seed_thread(store, name)

    def run(name: str) -> str:
        with task_work(f"task_{name}") as work:
            if own_model:
                # The same authorized person model for both threads, as cached
                # in PersonModelState and bound by select_turn_model.
                set_agent_ref(SimpleNamespace())
                work.person_model = SimpleNamespace(person_id="person_example", model=person_model)
            try:
                if operation == "summary":
                    result = service.summarize_turn(TurnSummaryRequest(task=Task(
                        task_id=f"task_{name}", task_type=MessageMetaType.REQUEST,
                        problem_statement=f"THREAD_{name}_PRIVATE_SENTINEL",
                    )))
                    assert result.summary_generator == "llm_summarizing"
                    return result.context_summary_text
                result = service.compact_if_needed(store=store, conversation_id=f"conversation_{name}")
                assert result.compacted and result.context_epoch_id
                epoch = store.get_context_epoch(result.context_epoch_id)
                assert epoch["compactor_type"] == "llm_summarizing"
                return epoch["summary_markdown"]
            finally:
                set_agent_ref(None)

    if overlap:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ("A", "B")))
    else:
        results = [run(name) for name in ("A", "B")]

    assert results == ["THREAD_A_PRIVATE_SENTINEL", "THREAD_B_PRIVATE_SENTINEL"]
    assert len(selected_model.calls) == 2
    for messages in selected_model.calls:
        assert len(messages) == 1, "Each operation starts with only its own prompt."
        rendered = json.dumps(messages)
        assert ("THREAD_A_PRIVATE_SENTINEL" in rendered) != ("THREAD_B_PRIVATE_SENTINEL" in rendered)
    unused_model = team_model if own_model else person_model
    assert not unused_model.calls
    assert "falling back" not in caplog.text


def test_compaction_preserves_saved_synopsis_after_summarizing_another_thread(tmp_path: Path) -> None:
    model = _RecordingSummaryModel()
    store = ConversationStore(str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "artifacts")
    compactor = LLMSummarizingConversationCompactor(
        ConversationCompactionConfig(
            context_window_tokens=100_000, trigger_turn_count=1,
            min_recent_turns=1, max_recent_turns=1,
        ),
        summarization_model=model,
    )
    for name in ("A", "B"):
        _seed_thread(store, name, next_marker="THREAD_A_NEXT_SENTINEL" if name == "A" else None)
        result = compactor.compact_if_needed(store=store, conversation_id=f"conversation_{name}")
        assert result.compacted and result.context_epoch_id

    saved_synopsis = store.get_thread("conversation_A")["synopsis_json"]
    # The next rollup must preserve A's saved synopsis even when its next turn
    # carries different facts and the service has processed B in between.
    _append_thread_turn(store, "A", 3, "Recent turn stays uncompressed.")
    result = compactor.compact_if_needed(store=store, conversation_id="conversation_A")

    assert result.compacted and result.context_epoch_id
    assert len(model.calls) == 3
    assert len(model.calls[-1]) == 1
    prompt = model.calls[-1][0]["content"][0]["text"]
    existing, turns = prompt.split("Existing synopsis JSON:\n", 1)[1].split("\n\nNew turn block JSON:\n")
    assert json.loads(existing) == saved_synopsis
    assert [turn["task_id"] for turn in json.loads(turns)] == ["task_A_2"]
    assert "THREAD_A_PRIVATE_SENTINEL" not in turns
    assert "THREAD_B_PRIVATE_SENTINEL" not in prompt
    epoch = store.get_context_epoch(result.context_epoch_id)
    assert epoch["summary_markdown"] == "THREAD_A_NEXT_SENTINEL THREAD_A_PRIVATE_SENTINEL"
    assert result.through_turn_index == 2
    assert "Recent turn stays uncompressed." in result.prompt_context


def test_legacy_answer_block_can_be_removed_without_losing_reasoning_notes() -> None:
    answer = "The full answer."
    legacy = (
        "Answer:\nThe full answer.\n\n"
        "Decision rationale:\n- Chose the stable path because it preserves compatibility."
    )

    assert strip_redundant_answer_from_context_summary(legacy, answer) == (
        "Decision rationale:\n- Chose the stable path because it preserves compatibility."
    )
