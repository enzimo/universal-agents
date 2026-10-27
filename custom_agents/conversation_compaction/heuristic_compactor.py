import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from custom_agents.conversation_compaction.base import (
    CompactionResult,
    ConversationCompactionConfig,
    ConversationCompactor,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.output_retention import with_deleted_outputs_marked
from custom_agents.conversation_summary import strip_redundant_answer_from_context_summary


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_strings(values: List[str]) -> List[str]:
    seen = set()
    deduped = []
    for value in values:
        normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value.strip())
    return deduped


def _extract_bullets(summary_text: str, header: str) -> List[str]:
    lines = [line.rstrip() for line in (summary_text or "").splitlines()]
    collected: List[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == header:
            in_section = True
            continue
        if in_section and not stripped.startswith("- "):
            break
        if in_section and stripped.startswith("- "):
            collected.append(stripped[2:].strip())
    return collected


class HeuristicSlidingWindowCompactor(ConversationCompactor):
    compactor_type = "heuristic_sliding_window"

    def __init__(
        self,
        config: ConversationCompactionConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(config=config, logger=logger)

    def estimate_prompt_tokens(self, text: str) -> int:
        return max(1, int(math.ceil(len(text or "") / 4)))

    def _prune_text(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return ""
        patterns = [
            r"\b(?:sure|certainly|absolutely|of course|understood)\b[:,]?\s*",
            r"\b(?:here(?:'s| is) the result|here(?:'s| is) what I found)\b[:,]?\s*",
            r"\b(?:let me know if you need anything else)\b[.!]?",
        ]
        for pattern in patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _truncate_text(self, text: str, limit: int = 1200) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _merge_synopsis(self, existing: Dict[str, Any], turns: List[Dict[str, Any]]) -> Dict[str, Any]:
        synopsis = dict(ConversationStore.default_synopsis())
        synopsis.update(existing or {})

        goals = list(synopsis.get("goals") or [])
        decisions = list(synopsis.get("decisions") or [])
        rationales = list(synopsis.get("rationales") or [])
        constraints = list(synopsis.get("constraints") or [])
        active_assumptions = list(synopsis.get("active_assumptions") or [])
        important_facts = list(synopsis.get("important_facts") or [])
        tool_results = list(synopsis.get("tool_results") or [])
        artifacts = list(synopsis.get("artifacts") or [])
        rejected_approaches = list(synopsis.get("rejected_approaches") or [])
        open_risks = list(synopsis.get("open_risks") or [])
        open_questions = list(synopsis.get("open_questions") or [])
        next_step_intent = (synopsis.get("next_step_intent") or "").strip()

        for turn in turns:
            request_text = self._prune_text(turn.get("problem_text", ""))
            raw_answer_text = str(turn.get("final_answer_text") or "")
            answer_text = self._prune_text(raw_answer_text)
            execution_history_text = self._prune_text(turn.get("execution_history_text", ""))
            summary_text = self._prune_text(
                strip_redundant_answer_from_context_summary(
                    turn.get("context_summary_text") or turn.get("turn_summary_text") or "",
                    raw_answer_text,
                )
            )
            clarification_text = self._prune_text(turn.get("clarification_summary_text", ""))
            rationale_lines = _extract_bullets(summary_text, "Decision rationale:")
            assumption_lines = _extract_bullets(summary_text, "Active assumptions:")
            rejected_lines = _extract_bullets(summary_text, "Rejected approaches:")
            risk_lines = _extract_bullets(summary_text, "Open risks:")
            next_step_lines = _extract_bullets(summary_text, "Next-step intent:")

            if request_text:
                goals.append(self._truncate_text(request_text, 240))
                if any(marker in request_text.lower() for marker in ("must", "should", "need to", "required")):
                    constraints.append(self._truncate_text(request_text, 240))
                if "?" in request_text and not answer_text:
                    open_questions.append(self._truncate_text(request_text, 240))
            if summary_text:
                important_facts.append(self._truncate_text(summary_text, 280))
                if any(marker in summary_text.lower() for marker in ("decide", "decision", "chosen", "selected")):
                    decisions.append(self._truncate_text(summary_text, 240))
            if answer_text:
                important_facts.append(self._truncate_text(answer_text, 280))
            if execution_history_text:
                important_facts.append(self._truncate_text(execution_history_text, 320))
            if clarification_text and "?" in clarification_text:
                open_questions.append(self._truncate_text(clarification_text, 240))
            rationales.extend([self._truncate_text(item, 240) for item in rationale_lines])
            active_assumptions.extend([self._truncate_text(item, 240) for item in assumption_lines])
            rejected_approaches.extend([self._truncate_text(item, 240) for item in rejected_lines])
            open_risks.extend([self._truncate_text(item, 240) for item in risk_lines])
            if next_step_lines:
                next_step_intent = self._truncate_text(" ".join(next_step_lines), 280)

            # A saved output deleted under the retention policy says so instead of naming its file.
            for artifact in with_deleted_outputs_marked(turn.get("tool_artifact_manifest")):
                artifact_summary = (artifact or {}).get("summary_text")
                storage_path = (artifact or {}).get("storage_path")
                artifact_type = (artifact or {}).get("artifact_type")
                if artifact_summary:
                    tool_results.append(self._truncate_text(artifact_summary, 240))
                if storage_path:
                    artifacts.append(f"{artifact_type or 'artifact'}: {storage_path}")

        goals = _dedupe_strings(goals)
        decisions = _dedupe_strings(decisions)
        rationales = _dedupe_strings(rationales)
        constraints = _dedupe_strings(constraints)
        active_assumptions = _dedupe_strings(active_assumptions)
        important_facts = _dedupe_strings(important_facts)
        tool_results = _dedupe_strings(tool_results)
        artifacts = _dedupe_strings(artifacts)
        rejected_approaches = _dedupe_strings(rejected_approaches)
        open_risks = _dedupe_strings(open_risks)
        open_questions = _dedupe_strings(open_questions)

        current_state = ""
        if turns:
            latest_turn = turns[-1]
            current_state = self._truncate_text(
                self._prune_text(latest_turn.get("turn_summary_text") or latest_turn.get("final_answer_text") or ""),
                280,
            )

        markdown_sections = []
        if goals:
            markdown_sections.append("### Goals\n" + "\n".join([f"- {item}" for item in goals[:8]]))
        if decisions:
            markdown_sections.append("### Decisions\n" + "\n".join([f"- {item}" for item in decisions[:8]]))
        if rationales:
            markdown_sections.append("### Decision Rationales\n" + "\n".join([f"- {item}" for item in rationales[:8]]))
        if constraints:
            markdown_sections.append("### Constraints\n" + "\n".join([f"- {item}" for item in constraints[:8]]))
        if active_assumptions:
            markdown_sections.append("### Active Assumptions\n" + "\n".join([f"- {item}" for item in active_assumptions[:8]]))
        if important_facts:
            markdown_sections.append("### Important Facts\n" + "\n".join([f"- {item}" for item in important_facts[:12]]))
        if tool_results:
            markdown_sections.append("### Tool Results\n" + "\n".join([f"- {item}" for item in tool_results[:8]]))
        if artifacts:
            markdown_sections.append("### Artifacts\n" + "\n".join([f"- {item}" for item in artifacts[:8]]))
        if rejected_approaches:
            markdown_sections.append("### Rejected Approaches\n" + "\n".join([f"- {item}" for item in rejected_approaches[:8]]))
        if open_risks:
            markdown_sections.append("### Open Risks\n" + "\n".join([f"- {item}" for item in open_risks[:8]]))
        if open_questions:
            markdown_sections.append("### Open Questions\n" + "\n".join([f"- {item}" for item in open_questions[:8]]))
        if current_state:
            markdown_sections.append(f"### Current State\n- {current_state}")
        if next_step_intent:
            markdown_sections.append(f"### Next Step Intent\n- {next_step_intent}")

        synopsis.update(
            {
                "goals": goals,
                "decisions": decisions,
                "rationales": rationales,
                "constraints": constraints,
                "active_assumptions": active_assumptions,
                "important_facts": important_facts,
                "tool_results": tool_results,
                "artifacts": artifacts,
                "rejected_approaches": rejected_approaches,
                "open_risks": open_risks,
                "open_questions": open_questions,
                "current_state": current_state,
                "next_step_intent": next_step_intent,
                "summary_markdown": "\n\n".join(markdown_sections),
            }
        )
        return synopsis

    def _render_synopsis_section(self, thread: Dict[str, Any]) -> str:
        synopsis_markdown = (thread or {}).get("synopsis_markdown") or ""
        if not synopsis_markdown.strip():
            return ""
        return "## Conversation Synopsis\n" + synopsis_markdown.strip()

    def _render_raw_turn(self, turn: Dict[str, Any]) -> str:
        lines = [
            f"### Turn {turn['turn_index']}",
            f"- Task ID: {turn['task_id']}",
            f"- Status: {turn.get('task_status') or 'unknown'}",
        ]
        if turn.get("correlation_id"):
            lines.append(f"- Correlation ID: {turn['correlation_id']}")
        if turn.get("problem_text"):
            lines.append(f"\nUser Request:\n{turn['problem_text']}")
        if turn.get("clarification_summary_text"):
            lines.append(f"\nClarifications:\n{turn['clarification_summary_text']}")
        if turn.get("final_answer_text"):
            lines.append(f"\nFinal Answer:\n{turn['final_answer_text']}")
        if turn.get("execution_history_text"):
            lines.append(f"\nExecution History:\n{turn['execution_history_text']}")
        if turn.get("turn_summary_text") and not turn.get("final_answer_text"):
            lines.append(f"\nTurn Summary:\n{turn['turn_summary_text']}")
        artifact_manifest = with_deleted_outputs_marked(turn.get("tool_artifact_manifest"))
        if artifact_manifest:
            artifact_lines = []
            for artifact in artifact_manifest:
                summary_text = (artifact or {}).get("summary_text")
                storage_path = (artifact or {}).get("storage_path")
                artifact_type = (artifact or {}).get("artifact_type") or "artifact"
                rendered = f"- {artifact_type}"
                if summary_text:
                    rendered += f": {summary_text}"
                if storage_path:
                    rendered += f" [{storage_path}]"
                artifact_lines.append(rendered)
            if artifact_lines:
                lines.append("\nArtifacts:\n" + "\n".join(artifact_lines))
        return "\n".join(lines)

    def _render_recent_raw_turns(self, turns: List[Dict[str, Any]]) -> str:
        if not turns:
            return ""
        rendered_turns = "\n\n".join([self._render_raw_turn(turn) for turn in turns])
        return "## Recent Raw Turns\n" + rendered_turns

    def _candidate_prompt_context(
        self,
        *,
        thread: Dict[str, Any],
        turns: List[Dict[str, Any]],
        latest_request_text: str,
    ) -> str:
        sections = [
            "## Use Of This Context\n"
            "Prior turns are reference material only. The final `Task.answer` for the current turn must contain only "
            "the new reply to the current request, not a replay or concatenation of earlier `Final Answer` blocks."
        ]
        synopsis_section = self._render_synopsis_section(thread)
        if synopsis_section:
            sections.append(synopsis_section)
        raw_turn_section = self._render_recent_raw_turns(turns)
        if raw_turn_section:
            sections.append(raw_turn_section)
        if latest_request_text:
            sections.append("## Current Request\n" + latest_request_text.strip())
        return "\n\n".join([section for section in sections if section]).strip()

    def _create_context_epoch(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        thread: Dict[str, Any],
        raw_turns_before: List[Dict[str, Any]],
        compacted_turns: List[Dict[str, Any]],
        merged_synopsis: Dict[str, Any],
        new_summarized_through: int,
        pre_compact_estimated_tokens: int,
        post_compact_estimated_tokens: Optional[int] = None,
        trigger: str = "post_turn",
    ) -> Optional[Dict[str, Any]]:
        creator = getattr(store, "create_context_epoch", None)
        if not callable(creator):
            return None
        source_turn_ids = [str(turn.get("task_id") or "") for turn in compacted_turns if turn.get("task_id")]
        all_turn_indexes = [int(turn.get("turn_index") or 0) for turn in raw_turns_before if turn.get("turn_index")]
        latest_turn_index = max(all_turn_indexes or [new_summarized_through])
        preserved_suffix_start = min(latest_turn_index + 1, int(new_summarized_through) + 1)
        source_epoch = None
        latest_epoch_getter = getattr(store, "get_latest_context_epoch", None)
        if callable(latest_epoch_getter):
            source_epoch = latest_epoch_getter(conversation_id)
        rehydration_manifest = {
            "source_turn_range": [1, int(new_summarized_through)],
            "source_turn_ids": source_turn_ids,
            "source_summary_fields": [
                "user_request_summary",
                "assistant_response_summary",
                "context_summary_text",
                "artifact_summary_json",
                "tool_artifact_manifest_json",
            ],
            "source_epoch_ids": (
                [source_epoch.get("context_epoch_id")]
                if isinstance(source_epoch, dict) and source_epoch.get("context_epoch_id")
                else []
            ),
            "artifact_refs": self._artifact_refs(compacted_turns),
            "preserved_suffix_start_turn": preserved_suffix_start,
            "summary_layering": "epoch_rollup; lower-level turn summaries and prior epochs are not overwritten",
        }
        try:
            return creator(
                conversation_id=conversation_id,
                trigger=trigger,
                through_turn_index=int(new_summarized_through),
                preserved_suffix_start_turn=preserved_suffix_start,
                latest_task_id=thread.get("latest_task_id"),
                summary_json=merged_synopsis,
                summary_markdown=merged_synopsis.get("summary_markdown", ""),
                rehydration_manifest=rehydration_manifest,
                source_lengths_before=[
                    {
                        "name": "conversation_compaction_candidate",
                        "estimated_tokens": pre_compact_estimated_tokens,
                        "raw_turn_count": len(raw_turns_before),
                    }
                ],
                source_lengths_after=(
                    [
                        {
                            "name": "conversation_compaction_candidate",
                            "estimated_tokens": post_compact_estimated_tokens,
                            "raw_turn_count": max(0, len(raw_turns_before) - len(compacted_turns)),
                        }
                    ]
                    if post_compact_estimated_tokens is not None
                    else []
                ),
                pre_compact_estimated_tokens=pre_compact_estimated_tokens,
                post_compact_estimated_tokens=post_compact_estimated_tokens,
                compactor_type=self.compactor_type,
            )
        except Exception as exc:
            self.logger.warning("Failed to create context epoch for conversation %s: %s", conversation_id, exc)
            return None

    def _artifact_refs(self, turns: List[Dict[str, Any]]) -> List[str]:
        refs: List[str] = []
        for turn in turns:
            for artifact in turn.get("tool_artifact_manifest") or []:
                if not isinstance(artifact, dict):
                    continue
                storage_path = artifact.get("storage_path") or artifact.get("path")
                if storage_path:
                    refs.append(str(storage_path))
        return _dedupe_strings(refs)

    def compact_if_needed(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        latest_request_text: str = "",
        exclude_task_id: Optional[str] = None,
    ) -> CompactionResult:
        thread = store.get_thread(conversation_id)
        if not thread:
            return CompactionResult(compacted=False, token_estimate=0, prompt_context="")

        compacted = False
        last_epoch: Optional[Dict[str, Any]] = None
        last_through_turn_index: Optional[int] = None
        while True:
            turns = store.get_recent_turns(conversation_id, include_pending_delivery=False)
            if exclude_task_id:
                turns = [turn for turn in turns if turn["task_id"] != exclude_task_id]
            summarized_through = thread.get("summarized_through_turn_index", 0)
            raw_turns = [turn for turn in turns if int(turn["turn_index"]) > int(summarized_through)]
            candidate = self._candidate_prompt_context(
                thread=thread,
                turns=raw_turns,
                latest_request_text=latest_request_text,
            )
            token_estimate = self.estimate_prompt_tokens(candidate)
            over_turn_limit = bool(
                self.config.trigger_turn_count and len(raw_turns) > self.config.trigger_turn_count
            )
            over_recent_limit = len(raw_turns) > self.config.max_recent_turns
            over_token_limit = token_estimate > int(self.config.context_window_tokens * self.config.trigger_ratio)

            if not (over_token_limit or over_recent_limit or over_turn_limit):
                store.update_thread_token_estimate(
                    conversation_id,
                    token_estimate=token_estimate,
                    compactor_type=self.compactor_type,
                    compaction_state={
                        "raw_turn_count": len(raw_turns),
                        "last_mode": "raw",
                    },
                )
                return CompactionResult(
                    compacted=compacted,
                    token_estimate=token_estimate,
                    prompt_context=candidate,
                    context_epoch_id=(last_epoch or {}).get("context_epoch_id"),
                    through_turn_index=last_through_turn_index,
                    preserved_suffix_start_turn=(last_epoch or {}).get("preserved_suffix_start_turn"),
                )

            if len(raw_turns) <= max(1, self.config.min_recent_turns):
                store.update_thread_token_estimate(
                    conversation_id,
                    token_estimate=token_estimate,
                    compactor_type=self.compactor_type,
                    compaction_state={
                        "raw_turn_count": len(raw_turns),
                        "last_mode": "minimum_recent_only",
                    },
                )
                return CompactionResult(
                    compacted=compacted,
                    token_estimate=token_estimate,
                    prompt_context=candidate,
                    context_epoch_id=(last_epoch or {}).get("context_epoch_id"),
                    through_turn_index=last_through_turn_index,
                    preserved_suffix_start_turn=(last_epoch or {}).get("preserved_suffix_start_turn"),
                )

            turns_to_compact = [raw_turns[0]]
            merged_synopsis = self._merge_synopsis(thread.get("synopsis_json") or {}, turns_to_compact)
            new_summarized_through = turns_to_compact[-1]["turn_index"]
            now_ts = _utc_now()
            store.update_thread_compaction(
                conversation_id,
                synopsis_json=merged_synopsis,
                synopsis_markdown=merged_synopsis.get("summary_markdown", ""),
                summarized_through_turn_index=new_summarized_through,
                last_compacted_at=now_ts,
                compaction_version=int(thread.get("compaction_version", 1) or 1) + 1,
                compactor_type=self.compactor_type,
                compaction_state={
                    "raw_turn_count": len(raw_turns) - len(turns_to_compact),
                    "last_mode": "compacted",
                },
            )
            compacted = True
            thread = store.get_thread(conversation_id) or thread

            refreshed_turns = store.get_recent_turns(conversation_id, include_pending_delivery=False)
            if exclude_task_id:
                refreshed_turns = [turn for turn in refreshed_turns if turn["task_id"] != exclude_task_id]
            refreshed_raw_turns = [
                turn for turn in refreshed_turns
                if int(turn["turn_index"]) > int(thread.get("summarized_through_turn_index", 0))
            ]
            refreshed_candidate = self._candidate_prompt_context(
                thread=thread,
                turns=refreshed_raw_turns,
                latest_request_text=latest_request_text,
            )
            refreshed_tokens = self.estimate_prompt_tokens(refreshed_candidate)
            epoch = self._create_context_epoch(
                store=store,
                conversation_id=conversation_id,
                thread=thread,
                raw_turns_before=raw_turns,
                compacted_turns=turns_to_compact,
                merged_synopsis=merged_synopsis,
                new_summarized_through=int(new_summarized_through),
                pre_compact_estimated_tokens=token_estimate,
                post_compact_estimated_tokens=refreshed_tokens,
            )
            if epoch:
                last_epoch = epoch
                last_through_turn_index = int(new_summarized_through)
            if (
                refreshed_tokens <= int(self.config.context_window_tokens * self.config.target_ratio)
                and len(refreshed_raw_turns) <= self.config.max_recent_turns
            ):
                store.update_thread_token_estimate(
                    conversation_id,
                    token_estimate=refreshed_tokens,
                    compactor_type=self.compactor_type,
                    compaction_state={
                        "raw_turn_count": len(refreshed_raw_turns),
                        "last_mode": "compacted",
                    },
                )
                return CompactionResult(
                    compacted=compacted,
                    token_estimate=refreshed_tokens,
                    prompt_context=refreshed_candidate,
                    context_epoch_id=(epoch or {}).get("context_epoch_id"),
                    through_turn_index=int(new_summarized_through),
                    preserved_suffix_start_turn=(epoch or {}).get("preserved_suffix_start_turn"),
                )

    def build_prompt_context(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        latest_request_text: str,
        exclude_task_id: Optional[str] = None,
    ) -> str:
        return self.compact_if_needed(
            store=store,
            conversation_id=conversation_id,
            latest_request_text=latest_request_text,
            exclude_task_id=exclude_task_id,
        ).prompt_context
