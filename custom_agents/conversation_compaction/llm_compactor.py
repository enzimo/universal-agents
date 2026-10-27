import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from custom_agents.conversation_compaction.base import ConversationCompactionConfig
from custom_agents.conversation_compaction.heuristic_compactor import HeuristicSlidingWindowCompactor
from custom_agents.conversation_summary import strip_redundant_answer_from_context_summary
from custom_agents.output_retention import with_deleted_outputs_marked
from custom_agents.person_model_runtime import new_summary_agent
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model


LLM_COMPACTION_SYSTEM_PROMPT = """You compact durable task-oriented conversation history.

Return only structured memory that preserves information needed to continue the
conversation correctly.

Rules:
- Preserve user goals, decisions, constraints, important facts, tool results, artifact references, and open questions.
- Preserve distilled reasoning state such as decision rationales, active assumptions, rejected approaches, open risks, and next-step intent.
- Remove filler, repetition, and conversational noise.
- Keep the summary faithful to the source turns.
- Prefer concrete facts over generic wording.
- `summary_markdown` must be concise and operator-readable.
"""


class StructuredConversationSynopsis(BaseModel):
    goals: List[str] = Field(default_factory=list)
    decisions: List[str] = Field(default_factory=list)
    rationales: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    active_assumptions: List[str] = Field(default_factory=list)
    important_facts: List[str] = Field(default_factory=list)
    tool_results: List[str] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)
    rejected_approaches: List[str] = Field(default_factory=list)
    open_risks: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    current_state: str = ""
    next_step_intent: str = ""
    summary_markdown: str = ""


class LLMSummarizingConversationCompactor(HeuristicSlidingWindowCompactor):
    compactor_type = "llm_summarizing"

    def __init__(
        self,
        config: ConversationCompactionConfig,
        logger: Optional[logging.Logger] = None,
        summarization_model: Any = None,
    ) -> None:
        super().__init__(config=config, logger=logger)
        self._summarization_model = summarization_model
        if self._summarization_model is not None:
            return
        try:
            self._summarization_model = self._build_model()
        except Exception as exc:
            self.logger.warning(
                "Failed to initialize llm_summarizing compactor model; falling back to heuristic summaries: %s",
                exc,
            )

    def _build_model(self) -> Any:
        provider = self.config.compaction_model_provider or self.config.default_model_provider
        model_id = self.config.compaction_model_id or self.config.default_model_id
        return build_strands_model(
            StrandsModelFactoryConfig(
                provider=provider,
                model_id=model_id,
                temperature=self.config.temperature if str(provider).strip().lower() == "litellm" else None,
                context_window_tokens=self.config.context_window_tokens,
                litellm_api_base=self.config.litellm_api_base,
                litellm_api_mode=self.config.litellm_api_mode,
                openai_codex=self.config.openai_codex,
                xai_oauth=self.config.xai_oauth,
                instance_root=self.config.instance_root,
            )
        )

    def _turn_payload(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        final_answer_text = str(turn.get("final_answer_text") or "")
        context_summary_text = strip_redundant_answer_from_context_summary(
            turn.get("context_summary_text") or turn.get("turn_summary_text") or "",
            final_answer_text,
        )
        return {
            "turn_index": turn.get("turn_index"),
            "task_id": turn.get("task_id"),
            "correlation_id": turn.get("correlation_id"),
            "task_status": turn.get("task_status"),
            "problem_text": turn.get("problem_text"),
            "clarification_summary_text": turn.get("clarification_summary_text"),
            "final_answer_text": final_answer_text,
            "execution_history_text": turn.get("execution_history_text"),
            "turn_summary_text": context_summary_text,
            "tool_artifact_manifest": [
                {
                    "artifact_type": artifact.get("artifact_type"),
                    "summary_text": artifact.get("summary_text"),
                    "storage_path": artifact.get("storage_path"),
                }
                # A saved output deleted under the retention policy says so instead of naming its file.
                for artifact in with_deleted_outputs_marked(turn.get("tool_artifact_manifest"))
            ],
        }

    def _build_markdown_from_output(self, synopsis: Dict[str, Any]) -> str:
        sections = []
        for title, key, limit in [
            ("Goals", "goals", 8),
            ("Decisions", "decisions", 8),
            ("Decision Rationales", "rationales", 8),
            ("Constraints", "constraints", 8),
            ("Active Assumptions", "active_assumptions", 8),
            ("Important Facts", "important_facts", 12),
            ("Tool Results", "tool_results", 8),
            ("Artifacts", "artifacts", 8),
            ("Rejected Approaches", "rejected_approaches", 8),
            ("Open Risks", "open_risks", 8),
            ("Open Questions", "open_questions", 8),
        ]:
            items = [item for item in (synopsis.get(key) or []) if item]
            if items:
                sections.append(f"### {title}\n" + "\n".join([f"- {item}" for item in items[:limit]]))
        current_state = (synopsis.get("current_state") or "").strip()
        if current_state:
            sections.append(f"### Current State\n- {current_state}")
        next_step_intent = (synopsis.get("next_step_intent") or "").strip()
        if next_step_intent:
            sections.append(f"### Next Step Intent\n- {next_step_intent}")
        return "\n\n".join(sections)

    def _generate_synopsis_with_llm(
        self,
        existing_synopsis: Dict[str, Any],
        turns: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if self._summarization_model is None:
            raise RuntimeError("LLM summarization model is not available.")

        prompt = (
            "Update the durable conversation synopsis.\n\n"
            f"Existing synopsis JSON:\n{json.dumps(existing_synopsis or {}, ensure_ascii=True, indent=2)}\n\n"
            f"New turn block JSON:\n{json.dumps([self._turn_payload(turn) for turn in turns], ensure_ascii=True, indent=2)}"
        )
        summarizer = new_summary_agent(
            self._summarization_model, system_prompt=LLM_COMPACTION_SYSTEM_PROMPT
        )
        result = summarizer(prompt, structured_output_model=StructuredConversationSynopsis)
        structured_output = result.structured_output
        if structured_output is None:
            raise RuntimeError("LLM compactor did not return structured output.")
        synopsis = structured_output.model_dump()
        if not synopsis.get("summary_markdown"):
            synopsis["summary_markdown"] = self._build_markdown_from_output(synopsis)
        return synopsis

    def _merge_synopsis(self, existing: Dict[str, Any], turns: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            return self._generate_synopsis_with_llm(existing, turns)
        except Exception as exc:
            self.logger.warning(
                "LLM-backed conversation compaction failed; falling back to heuristic synopsis merge: %s",
                exc,
            )
            return super()._merge_synopsis(existing, turns)
