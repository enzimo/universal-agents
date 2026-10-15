from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from default_values import AGENT_DEFAULTS

from custom_agents.conversation_delivery_states import MODEL_VISIBLE_TIMER_DELIVERY_STATES
from custom_agents.prompt_context_sanitization import extract_display_request_text
from custom_agents.token_estimation import estimate_tokens


_LOGGER = logging.getLogger(__name__)
_THREAD_DEFAULTS = AGENT_DEFAULTS.context_engine.value["thread_window"]


@dataclass
class BudgetedThreadContextRendererConfig:
    recent_full_turns: int = _THREAD_DEFAULTS["recent_full_turns"]
    per_turn_summary_max_tokens: int = _THREAD_DEFAULTS["per_turn_summary_max_tokens"]
    max_turns_to_load: int = _THREAD_DEFAULTS["max_turns_to_load"]
    omitted_notice_enabled: bool = _THREAD_DEFAULTS["omitted_notice_enabled"]
    include_turn_ids: bool = _THREAD_DEFAULTS["include_turn_ids"]
    retrieval_tool_hint: bool = _THREAD_DEFAULTS["retrieval_tool_hint"]


@dataclass
class RenderedThreadContext:
    text: str
    visible_turn_count: int = 0
    omitted_turn_count: int = 0
    downgraded_turn_count: int = 0
    available_tokens: Optional[int] = None
    estimated_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class BudgetedThreadContextRenderer:
    """Render durable conversation turns into a bounded prompt section."""

    def __init__(
        self,
        *,
        config: BudgetedThreadContextRendererConfig | None = None,
        estimate_tokens: Callable[[str], int] | None = None,
    ) -> None:
        self.config = config or BudgetedThreadContextRendererConfig()
        self.estimate_tokens = estimate_tokens or self._estimate_tokens

    def render(
        self,
        *,
        store: Any,
        conversation_id: str,
        latest_request_text: str = "",
        exclude_task_id: str | None = None,
        available_tokens: int | None = None,
    ) -> RenderedThreadContext:
        del latest_request_text
        if store is None or not conversation_id:
            return RenderedThreadContext(text="", available_tokens=available_tokens)

        epoch = self._load_latest_epoch(store, conversation_id=conversation_id)
        epoch_through_turn = int((epoch or {}).get("through_turn_index") or 0)
        turns = self._load_turns(store, conversation_id=conversation_id)
        turns, duplicate_task_ids = self._dedupe_turns(turns)
        if duplicate_task_ids:
            _LOGGER.warning(
                "conversation_id=<%s>, duplicate_task_ids=<%s> | duplicate conversation turns removed before rendering",
                conversation_id,
                ",".join(duplicate_task_ids),
            )
        visible_turns = [
            turn
            for turn in turns
            if turn.get("task_id") != exclude_task_id and self._is_model_visible_turn(turn)
            and int(turn.get("turn_index") or 0) > epoch_through_turn
        ]
        if not visible_turns and not epoch:
            return RenderedThreadContext(text="", available_tokens=available_tokens)

        recent_full_count = max(1, int(self.config.recent_full_turns or 1))
        recent_full_start = max(0, len(visible_turns) - recent_full_count)
        selected: dict[int, str] = {}
        omitted: list[dict[str, Any]] = []
        downgraded = 0
        used_tokens = 0
        budget = None if available_tokens is None else max(0, int(available_tokens))

        for position in range(len(visible_turns) - 1, -1, -1):
            turn = visible_turns[position]
            preferred_mode = "full" if position >= recent_full_start else "summary"
            rendered = self._render_turn(turn, mode=preferred_mode)
            rendered_tokens = self.estimate_tokens(rendered)
            if self._fits(used_tokens, rendered_tokens, budget):
                selected[position] = rendered
                used_tokens += rendered_tokens
                continue

            if preferred_mode == "full":
                summary_rendered = self._render_turn(turn, mode="summary", budget_downgrade=True)
                summary_tokens = self.estimate_tokens(summary_rendered)
                if self._fits(used_tokens, summary_tokens, budget):
                    selected[position] = summary_rendered
                    used_tokens += summary_tokens
                    downgraded += 1
                    continue

            omitted.append(turn)

        parts: list[str] = []
        epoch_text = self._render_epoch(epoch)
        if epoch_text:
            epoch_tokens = self.estimate_tokens(epoch_text)
            if self._fits(used_tokens, epoch_tokens, budget):
                parts.append(epoch_text)
                used_tokens += epoch_tokens
            else:
                parts.append(self._render_epoch_notice(epoch))
        if omitted and self.config.omitted_notice_enabled:
            parts.append(self._render_omitted_notice(conversation_id=conversation_id, omitted=omitted, epoch=epoch))
        for position in sorted(selected):
            parts.append(selected[position])

        text = "\n\n".join(part for part in parts if part)
        return RenderedThreadContext(
            text=text,
            visible_turn_count=len(selected),
            omitted_turn_count=len(omitted),
            downgraded_turn_count=downgraded,
            available_tokens=available_tokens,
            estimated_tokens=self.estimate_tokens(text),
            metadata={
                "conversation_id": conversation_id,
                "total_visible_turns": len(visible_turns),
                "selected_turn_indexes": [
                    int(visible_turns[position].get("turn_index") or 0)
                    for position in sorted(selected)
                ],
                "selected_task_ids": [
                    str(visible_turns[position].get("task_id") or "")
                    for position in sorted(selected)
                ],
                "omitted_turn_indexes": sorted(int(turn.get("turn_index") or 0) for turn in omitted),
                "duplicate_turn_count": len(duplicate_task_ids),
                "duplicate_task_ids": duplicate_task_ids,
                "context_epoch_id": (epoch or {}).get("context_epoch_id"),
                "epoch_through_turn_index": epoch_through_turn,
            },
        )

    def _load_latest_epoch(self, store: Any, *, conversation_id: str) -> dict[str, Any] | None:
        if hasattr(store, "get_latest_context_epoch"):
            return store.get_latest_context_epoch(conversation_id)
        return None

    def _load_turns(self, store: Any, *, conversation_id: str) -> list[dict[str, Any]]:
        if hasattr(store, "get_recent_turns"):
            return list(
                store.get_recent_turns(
                    conversation_id,
                    limit=self.config.max_turns_to_load,
                    include_pending_delivery=False,
                )
                or []
            )
        if hasattr(store, "get_conversation_turns"):
            return list(
                store.get_conversation_turns(
                    conversation_id=conversation_id,
                    start_turn=1,
                    limit=self.config.max_turns_to_load,
                )
                or []
            )
        return []

    def _dedupe_turns(self, turns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """Keep the latest copy of repeated task rows while preserving chronological order."""
        seen: set[tuple[Any, ...]] = set()
        kept_reversed: list[dict[str, Any]] = []
        duplicate_task_ids: list[str] = []
        for turn in reversed(turns):
            task_id = str(turn.get("task_id") or "").strip()
            if task_id:
                identity: tuple[Any, ...] = ("task_id", task_id)
            else:
                identity = (
                    "fallback",
                    int(turn.get("turn_index") or 0),
                    str(turn.get("problem_text") or ""),
                    str(turn.get("final_answer_text") or ""),
                )
            if identity in seen:
                duplicate_task_ids.append(task_id or f"turn_index:{int(turn.get('turn_index') or 0)}")
                continue
            seen.add(identity)
            kept_reversed.append(turn)
        return list(reversed(kept_reversed)), sorted(set(duplicate_task_ids))

    def _is_model_visible_turn(self, turn: dict[str, Any]) -> bool:
        if (
            turn.get("requester_type") == "timer_req"
            and turn.get("delivery_state") not in MODEL_VISIBLE_TIMER_DELIVERY_STATES
        ):
            return False
        return True

    def _render_turn(self, turn: dict[str, Any], *, mode: str, budget_downgrade: bool = False) -> str:
        turn_index = int(turn.get("turn_index") or 0)
        task_id = str(turn.get("task_id") or "unknown")
        label = "summary due to budget" if budget_downgrade else mode
        header = f"### Turn {turn_index} [{label}]"
        if self.config.include_turn_ids:
            header = f"{header} task_id={task_id}"

        if mode == "full":
            parts = [
                header,
                "User:",
                self._display_user_text(turn.get("problem_text") or "(no user text stored)"),
                "Assistant:",
                self._clean_text(turn.get("final_answer_text") or "(no assistant answer stored)"),
            ]
            artifacts = self._render_artifacts(turn.get("artifact_summary") or turn.get("tool_artifact_manifest") or [])
            if artifacts:
                parts.extend(["Artifacts:", artifacts])
            return "\n".join(parts)

        total_summary_tokens = max(1, self.config.per_turn_summary_max_tokens)
        user_summary_budget = max(50, total_summary_tokens // 3)
        assistant_summary_budget = max(50, total_summary_tokens - user_summary_budget)
        user_summary = turn.get("user_request_summary") or self._display_user_text(turn.get("problem_text") or "")
        assistant_summary = (
            turn.get("assistant_response_summary")
            or turn.get("context_summary_text")
            or turn.get("turn_summary_text")
            or turn.get("final_answer_text")
            or ""
        )
        return "\n".join(
            [
                header,
                "User summary:",
                self._truncate_tokens(self._clean_text(user_summary), user_summary_budget),
                "",
                "Assistant summary:",
                self._truncate_tokens(self._clean_text(assistant_summary), assistant_summary_budget),
            ]
        )

    def _render_artifacts(self, artifacts: list[dict[str, Any]]) -> str:
        lines = []
        for artifact in artifacts[:5]:
            artifact_type = artifact.get("artifact_type") or artifact.get("type") or "artifact"
            storage_path = artifact.get("storage_path") or artifact.get("path") or "(path unavailable)"
            summary = artifact.get("summary_text") or artifact.get("summary") or ""
            line = f"- {artifact_type}: {storage_path}"
            if summary:
                line = f"{line} ({self._truncate_tokens(str(summary), 60)})"
            lines.append(line)
        if len(artifacts) > 5:
            lines.append(f"- {len(artifacts) - 5} additional artifact(s) omitted from this prompt.")
        return "\n".join(lines)

    def _render_epoch(self, epoch: dict[str, Any] | None) -> str:
        if not epoch:
            return ""
        summary = str(epoch.get("summary_markdown") or "").strip()
        if not summary:
            summary_json = epoch.get("summary_json") if isinstance(epoch.get("summary_json"), dict) else {}
            summary = str(summary_json.get("summary_markdown") or "").strip()
        if not summary:
            summary = "(epoch summary unavailable; use context epoch tools for details)"
        return "\n".join(
            [
                "### Context Epoch Boundary",
                f"- Epoch ID: {epoch.get('context_epoch_id')}",
                f"- Summarized through turn: {epoch.get('through_turn_index')}",
                f"- Preserved suffix starts at turn: {epoch.get('preserved_suffix_start_turn')}",
                "Epoch summary:",
                summary,
            ]
        )

    def _render_epoch_notice(self, epoch: dict[str, Any] | None) -> str:
        if not epoch:
            return ""
        return (
            "[Context epoch summary omitted because the thread budget is exhausted. "
            f"Use get_context_epoch(context_epoch_id=\"{epoch.get('context_epoch_id')}\") "
            "to inspect the compacted boundary.]"
        )

    def _render_omitted_notice(
        self,
        *,
        conversation_id: str,
        omitted: list[dict[str, Any]],
        epoch: dict[str, Any] | None = None,
    ) -> str:
        omitted_indexes = sorted(int(turn.get("turn_index") or 0) for turn in omitted)
        first_turn = omitted_indexes[0] if omitted_indexes else 1
        last_turn = omitted_indexes[-1] if omitted_indexes else first_turn
        notice = (
            f"[Context trimmed: {len(omitted)} older conversation turn(s) "
            f"({first_turn}-{last_turn}) were omitted from this prompt to stay within the context budget."
        )
        if epoch:
            notice += (
                f" Earlier turns through {epoch.get('through_turn_index')} are represented by "
                f"context epoch {epoch.get('context_epoch_id')}."
            )
        if self.config.retrieval_tool_hint:
            notice += (
                f" Use get_conversation_context_slice(conversation_id=\"{conversation_id}\", "
                f"start_turn={first_turn}, limit={len(omitted)}, mode=\"summary\") to inspect them if needed."
            )
        return f"{notice}]"

    def _fits(self, used_tokens: int, next_tokens: int, budget: int | None) -> bool:
        return budget is None or used_tokens + next_tokens <= budget

    def _clean_text(self, text: Any) -> str:
        return str(text or "").strip()

    def _display_user_text(self, text: Any) -> str:
        return extract_display_request_text(str(text or "")).strip()

    def _truncate_tokens(self, text: str, max_tokens: int) -> str:
        cleaned = self._clean_text(text)
        if self.estimate_tokens(cleaned) <= max_tokens:
            return cleaned
        suffix = "\n[truncated to summary token budget]"
        suffix_tokens = self.estimate_tokens(suffix)
        available = max(1, max_tokens - suffix_tokens)
        head_chars = max(0, available * 4)
        candidate = cleaned[:head_chars].rstrip()
        while candidate and self.estimate_tokens(f"{candidate}{suffix}") > max_tokens:
            candidate = candidate[:-16].rstrip()
        return f"{candidate}{suffix}" if candidate else suffix.strip()

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return estimate_tokens(text)
