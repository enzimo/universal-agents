from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Protocol

from pydantic import BaseModel, Field

from default_values import LLM_DEFAULTS
from custom_messages.contracts.task_v1 import Task, TaskAnswer
from custom_messages.enums import TaskHistoryType
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model


def stringify_task_answer(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, TaskAnswer):
        return answer.answer
    if hasattr(answer, "model_dump_json"):
        return answer.model_dump_json(indent=2)
    return str(answer)


def stringify_task_errors(errors: Optional[List[str]]) -> str:
    if not errors:
        return ""
    return "\n".join(f"- {error}" for error in errors)


@dataclass
class TurnSummaryRequest:
    task: Task
    final_answer_text: str = ""
    artifact_manifest: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TurnSummaryResult:
    user_request_summary: str = ""
    assistant_response_summary: str = ""
    context_summary_text: str = ""
    artifact_summary: list[dict[str, Any]] = field(default_factory=list)
    legacy_turn_summary_text: str = ""
    summary_generator: str = "heuristic"


@dataclass
class ConversationSummaryConfig:
    summary_model_provider: Optional[str] = None
    summary_model_id: Optional[str] = None
    default_model_provider: str = LLM_DEFAULTS.provider.value
    default_model_id: str = LLM_DEFAULTS.model_id.value
    temperature: float = LLM_DEFAULTS.temperature.value
    litellm_api_base: Optional[str] = None
    litellm_api_mode: str = LLM_DEFAULTS.litellm_api_mode.value
    openai_codex: Optional[Mapping[str, Any]] = None
    xai_oauth: Optional[Mapping[str, Any]] = None
    instance_root: Optional[str] = None


class ConversationSummaryGenerator(Protocol):
    generator_name: str

    def summarize_turn(self, request: TurnSummaryRequest) -> TurnSummaryResult:
        ...


def _normalize_summary_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _truncate_text(text: str, limit: int) -> str:
    cleaned = _normalize_summary_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def strip_redundant_answer_from_context_summary(
    context_summary_text: str,
    final_answer_text: str,
) -> str:
    """Remove the legacy heuristic's exact leading answer copy from context notes."""
    summary = str(context_summary_text or "").strip()
    answer = str(final_answer_text or "").strip()
    if not summary or not answer:
        return summary
    legacy_answer_block = f"Answer:\n{answer}"
    if summary == legacy_answer_block:
        return ""
    if summary.startswith(f"{legacy_answer_block}\n\n"):
        return summary[len(legacy_answer_block) :].lstrip()
    return summary


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for value in values:
        normalized = _normalize_summary_text(value).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value.strip())
    return deduped


def _history_entries_for_type(task: Task, *entry_types: TaskHistoryType) -> List[str]:
    wanted = set(entry_types)
    return [
        entry.summary.strip()
        for entry in (task.execution_history or [])
        if entry.entry_type in wanted and (entry.summary or "").strip()
    ]


def _filter_reasoning_lines(entries: List[str], pattern: str) -> List[str]:
    matched = []
    for entry in entries:
        if re.search(pattern, entry, flags=re.IGNORECASE):
            matched.append(entry)
    return _dedupe_preserve_order(matched)


def _build_bulleted_section(header: str, values: List[str]) -> str:
    if not values:
        return ""
    return f"{header}\n" + "\n".join(f"- {value}" for value in values)


class HeuristicConversationSummaryGenerator:
    generator_name = "heuristic"

    def summarize_turn(self, request: TurnSummaryRequest) -> TurnSummaryResult:
        task = request.task
        final_answer_text = request.final_answer_text or stringify_task_answer(task.answer).strip()
        error_text = stringify_task_errors(task.errors).strip()
        agent_notes = _history_entries_for_type(task, TaskHistoryType.AGENT_NOTE)
        plan_notes = _history_entries_for_type(task, TaskHistoryType.PLAN_EVENT)
        error_notes = _history_entries_for_type(task, TaskHistoryType.ERROR_EVENT)

        decision_rationales = _filter_reasoning_lines(
            agent_notes,
            r"\b(?:because|therefore|so that|in order to|decid|chose|chosen|select|selected|prefer|preferred)\b",
        )
        active_assumptions = _filter_reasoning_lines(
            agent_notes,
            r"\b(?:assume|assumes|assumed|assuming|assumption|assumptions)\b",
        )
        rejected_approaches = _filter_reasoning_lines(
            agent_notes,
            r"\b(?:reject|rejected|avoid|avoided|not using|declined|ruled out)\b",
        )
        open_risks = _filter_reasoning_lines(
            error_notes + agent_notes,
            r"\b(?:risk|risky|uncertain|uncertainty|concern|blocker|could fail|might fail|might be|may be)\b",
        )
        next_step_intent = _filter_reasoning_lines(
            plan_notes + agent_notes,
            r"\b(?:next step|next steps|next,|then\b|follow-up|follow up|plan to|will next|should next)\b",
        )

        sections = []
        if error_text:
            sections.append(f"Errors:\n{error_text}")

        reasoning_sections = [
            _build_bulleted_section("Decision rationale:", decision_rationales),
            _build_bulleted_section("Active assumptions:", active_assumptions),
            _build_bulleted_section("Rejected approaches:", rejected_approaches),
            _build_bulleted_section("Open risks:", open_risks),
            _build_bulleted_section("Next-step intent:", next_step_intent),
        ]
        sections.extend([section for section in reasoning_sections if section])
        context_summary = "\n\n".join(sections).strip()

        return TurnSummaryResult(
            user_request_summary=_truncate_text(task.problem_statement or "", 1200),
            assistant_response_summary=_truncate_text(final_answer_text or error_text, 1600),
            context_summary_text=context_summary,
            artifact_summary=self._summarize_artifacts(request.artifact_manifest),
            legacy_turn_summary_text=context_summary,
            summary_generator=self.generator_name,
        )

    def _summarize_artifacts(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for artifact in artifacts or []:
            if not isinstance(artifact, dict):
                continue
            result.append(
                {
                    "artifact_id": artifact.get("artifact_id"),
                    "artifact_type": artifact.get("artifact_type") or artifact.get("type"),
                    "storage_path": artifact.get("storage_path") or artifact.get("path"),
                    "summary_text": _truncate_text(str(artifact.get("summary_text") or artifact.get("summary") or ""), 600),
                    "created_at": artifact.get("created_at"),
                }
            )
        return result


class StructuredArtifactSummary(BaseModel):
    artifact_id: str = ""
    artifact_type: str = ""
    storage_path: str = ""
    summary_text: str = ""
    created_at: str = ""


class StructuredTurnSummary(BaseModel):
    user_request_summary: str = Field(
        default="",
        description="Concise summary of what the user asked for in this turn.",
    )
    assistant_response_summary: str = Field(
        default="",
        description="Concise summary of the final user-visible assistant answer or failure.",
    )
    context_summary_text: str = Field(
        default="",
        description=(
            "Durable rationale, assumptions, rejected approaches, open risks, open questions, or next-step "
            "intent needed to continue after this turn. Do not repeat or paraphrase the final answer here; "
            "assistant_response_summary owns the answer summary."
        ),
    )
    artifact_summary: List[StructuredArtifactSummary] = Field(
        default_factory=list,
        description="Compact artifact references with type, path, and useful summary text.",
    )


LLM_TURN_SUMMARY_SYSTEM_PROMPT = """You summarize one completed task-oriented conversation turn.

Return only faithful structured summary fields. Put the bounded final outcome in
assistant_response_summary. Put only durable rationale, constraints,
assumptions, rejected approaches, open risks, open questions, and next-step
intent in context_summary_text; never repeat or paraphrase the final answer in
that field. Preserve artifact references, remove filler and transient
implementation noise, and do not invent facts.
"""


class LLMTurnSummaryGenerator:
    generator_name = "llm_summarizing"

    def __init__(
        self,
        config: Optional[ConversationSummaryConfig] = None,
        logger: Optional[logging.Logger] = None,
        summarization_agent: Any = None,
    ) -> None:
        self.config = config or ConversationSummaryConfig()
        self.logger = logger or logging.getLogger(__name__)
        self._fallback = HeuristicConversationSummaryGenerator()
        self._summarization_agent = summarization_agent
        from custom_agents.person_model_runtime import SummaryAgents

        # A person's own model for their conversations (custom_agents.person_model_runtime).
        self._person_agents = SummaryAgents(LLM_TURN_SUMMARY_SYSTEM_PROMPT)
        if self._summarization_agent is not None:
            return
        try:
            from strands import Agent
            from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager

            self._summarization_agent = Agent(
                model=self._build_model(),
                tools=[],
                system_prompt=LLM_TURN_SUMMARY_SYSTEM_PROMPT,
                conversation_manager=NullConversationManager(),
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to initialize LLM conversation summary generator; falling back to heuristic summaries: %s",
                exc,
            )
            self._summarization_agent = None

    def _build_model(self) -> Any:
        provider = self.config.summary_model_provider or self.config.default_model_provider
        model_id = self.config.summary_model_id or self.config.default_model_id
        return build_strands_model(
            StrandsModelFactoryConfig(
                provider=provider,
                model_id=model_id,
                temperature=self.config.temperature if str(provider).strip().lower() == "litellm" else None,
                litellm_api_base=self.config.litellm_api_base,
                litellm_api_mode=self.config.litellm_api_mode,
                openai_codex=self.config.openai_codex,
                xai_oauth=self.config.xai_oauth,
                instance_root=self.config.instance_root,
            )
        )

    def summarize_turn(self, request: TurnSummaryRequest) -> TurnSummaryResult:
        if self._summarization_agent is None:
            return self._fallback.summarize_turn(request)
        try:
            prompt = self._build_prompt(request)
            summarizer = self._person_agents.agent_for(self._summarization_agent)
            result = summarizer(prompt, structured_output_model=StructuredTurnSummary)
            structured_output = getattr(result, "structured_output", None)
            if structured_output is None:
                raise RuntimeError("LLM turn summary generator did not return structured output.")
            summary = structured_output.model_dump()
            context_summary = _normalize_summary_text(str(summary.get("context_summary_text") or ""))
            assistant_summary = _truncate_text(str(summary.get("assistant_response_summary") or ""), 1600)
            user_summary = _truncate_text(str(summary.get("user_request_summary") or ""), 1200)
            artifact_summary = self._normalize_artifact_summary(
                summary.get("artifact_summary"),
                request.artifact_manifest,
            )
            if not user_summary:
                user_summary = _truncate_text(request.task.problem_statement or "", 1200)
            if not assistant_summary:
                fallback_text = request.final_answer_text or stringify_task_answer(request.task.answer) or stringify_task_errors(request.task.errors)
                assistant_summary = _truncate_text(fallback_text, 1600)
            return TurnSummaryResult(
                user_request_summary=user_summary,
                assistant_response_summary=assistant_summary,
                context_summary_text=context_summary,
                artifact_summary=artifact_summary,
                legacy_turn_summary_text=context_summary,
                summary_generator=self.generator_name,
            )
        except Exception as exc:
            self.logger.warning(
                "LLM-backed turn summary generation failed; falling back to heuristic summary: %s",
                exc,
            )
            return self._fallback.summarize_turn(request)

    def _build_prompt(self, request: TurnSummaryRequest) -> str:
        payload = {
            "task_id": request.task.task_id,
            "task_state": [
                (getattr(state, "value", state), timestamp)
                for state, timestamp in (request.task.task_state or [])
            ],
            "user_request": request.task.problem_statement or "",
            "final_answer": request.final_answer_text or stringify_task_answer(request.task.answer),
            "errors": request.task.errors or [],
            "execution_history": [
                {
                    "entry_type": getattr(entry.entry_type, "value", entry.entry_type),
                    "importance": getattr(entry.importance, "value", entry.importance),
                    "summary": entry.summary,
                    "details": entry.details,
                    "related_task_id": entry.related_task_id,
                }
                for entry in (request.task.execution_history or [])
            ],
            "artifacts": request.artifact_manifest or [],
        }
        return (
            "Summarize this completed conversation turn for durable future context.\n\n"
            f"Turn JSON:\n{json.dumps(payload, ensure_ascii=True, indent=2, default=str)}"
        )

    def _normalize_artifact_summary(
        self,
        artifact_summary: Any,
        artifact_manifest: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(artifact_summary, list):
            return self._fallback._summarize_artifacts(artifact_manifest)
        normalized = []
        manifest_by_index = [
            artifact for artifact in (artifact_manifest or []) if isinstance(artifact, dict)
        ]
        for index, artifact in enumerate(artifact_summary[:10]):
            if not isinstance(artifact, dict):
                continue
            source_artifact = manifest_by_index[index] if index < len(manifest_by_index) else {}
            normalized.append(
                {
                    "artifact_id": artifact.get("artifact_id") or source_artifact.get("artifact_id"),
                    "artifact_type": (
                        artifact.get("artifact_type")
                        or artifact.get("type")
                        or source_artifact.get("artifact_type")
                        or source_artifact.get("type")
                    ),
                    "storage_path": (
                        artifact.get("storage_path")
                        or artifact.get("path")
                        or source_artifact.get("storage_path")
                        or source_artifact.get("path")
                    ),
                    "summary_text": _truncate_text(
                        str(artifact.get("summary_text") or artifact.get("summary") or ""),
                        600,
                    ),
                    "created_at": artifact.get("created_at") or source_artifact.get("created_at"),
                }
            )
        return normalized or self._fallback._summarize_artifacts(artifact_manifest)


def build_conversation_summary_generator(
    generator_type: str | None = None,
    *,
    config: Optional[ConversationSummaryConfig] = None,
    logger: Optional[logging.Logger] = None,
) -> ConversationSummaryGenerator:
    normalized = (generator_type or "heuristic").strip().lower()
    if normalized in {"heuristic", "default"}:
        return HeuristicConversationSummaryGenerator()
    if normalized in {"llm", "llm_summarizing", "llm_summary"}:
        return LLMTurnSummaryGenerator(config=config, logger=logger)
    raise ValueError(f"Unsupported conversation summary generator: {generator_type!r}")


def build_turn_summary(task: Task) -> str:
    return HeuristicConversationSummaryGenerator().summarize_turn(TurnSummaryRequest(task=task)).legacy_turn_summary_text
