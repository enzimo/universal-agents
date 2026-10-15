import logging

from custom_agents.conversation_summary import (
    ConversationSummaryConfig,
    LLMTurnSummaryGenerator,
    StructuredTurnSummary,
    TurnSummaryRequest,
    strip_redundant_answer_from_context_summary,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType


class _StructuredResult:
    def __init__(self, structured_output):
        self.structured_output = structured_output


class _FakeSummaryAgent:
    def __init__(self, structured_output):
        self.calls = []
        self.structured_output = structured_output

    def __call__(self, prompt, *, structured_output_model):
        self.calls.append((prompt, structured_output_model))
        return _StructuredResult(self.structured_output)


def test_llm_turn_summary_generator_uses_structured_output() -> None:
    fake_agent = _FakeSummaryAgent(
        StructuredTurnSummary(
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
        summarization_agent=fake_agent,
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

    assert fake_agent.calls
    assert fake_agent.calls[0][1] is StructuredTurnSummary
    assert "Please create a migration plan." in fake_agent.calls[0][0]
    assert result.user_request_summary == "User asked for a migration plan."
    assert result.assistant_response_summary == "Assistant produced a phased plan."
    assert result.context_summary_text == "Keep the plan durable and preserve retrieval hooks."
    assert result.artifact_summary[0]["storage_path"] == "plans/context.md"
    assert result.summary_generator == "llm_summarizing"


def test_llm_turn_summary_generator_falls_back_to_heuristic_on_failure() -> None:
    class _FailingSummaryAgent:
        def __call__(self, prompt, *, structured_output_model):
            raise RuntimeError("summary model unavailable")

    generator = LLMTurnSummaryGenerator(
        logger=logging.getLogger("test"),
        summarization_agent=_FailingSummaryAgent(),
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


def test_legacy_answer_block_can_be_removed_without_losing_reasoning_notes() -> None:
    answer = "The full answer."
    legacy = (
        "Answer:\nThe full answer.\n\n"
        "Decision rationale:\n- Chose the stable path because it preserves compatibility."
    )

    assert strip_redundant_answer_from_context_summary(legacy, answer) == (
        "Decision rationale:\n- Chose the stable path because it preserves compatibility."
    )
