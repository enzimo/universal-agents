import math
from typing import Dict, List, Optional

from custom_agents.conversation_compaction.base import (
    CompactionResult,
    ConversationCompactionConfig,
    ConversationCompactor,
)
from custom_agents.conversation_store import ConversationStore


class NoOpConversationCompactor(ConversationCompactor):
    compactor_type = "noop"

    def __init__(self, config: ConversationCompactionConfig, logger=None) -> None:
        super().__init__(config=config, logger=logger)

    @staticmethod
    def _render_raw_turn(turn: Dict[str, str]) -> str:
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
        artifact_manifest = turn.get("tool_artifact_manifest") or []
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

    def _render(self, turns: List[Dict[str, str]], latest_request_text: str) -> str:
        sections = [
            "## Use Of This Context\n"
            "Prior turns are reference material only. The final `Task.answer` for the current turn must contain only "
            "the new reply to the current request, not a replay or concatenation of earlier `Final Answer` blocks."
        ]
        if turns:
            sections.append("## Recent Raw Turns\n" + "\n\n".join([self._render_raw_turn(turn) for turn in turns]))
        if latest_request_text:
            sections.append("## Current Request\n" + latest_request_text.strip())
        return "\n\n".join(sections).strip()

    @staticmethod
    def _estimate(text: str) -> int:
        return max(1, int(math.ceil(len(text or "") / 4)))

    def compact_if_needed(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        latest_request_text: str = "",
        exclude_task_id: Optional[str] = None,
    ) -> CompactionResult:
        turns = store.get_recent_turns(conversation_id, include_pending_delivery=False)
        if exclude_task_id:
            turns = [turn for turn in turns if turn["task_id"] != exclude_task_id]
        prompt_context = self._render(turns, latest_request_text)
        token_estimate = self._estimate(prompt_context)
        store.update_thread_token_estimate(
            conversation_id,
            token_estimate=token_estimate,
            compactor_type=self.compactor_type,
            compaction_state={
                "raw_turn_count": len(turns),
                "last_mode": "noop",
            },
        )
        return CompactionResult(
            compacted=False,
            token_estimate=token_estimate,
            prompt_context=prompt_context,
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
