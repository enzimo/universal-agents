"""Filtered inner-loop context management for Strands tool transcripts."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

from strands.agent.conversation_manager.conversation_manager import ConversationManager
from strands.hooks import AfterModelCallEvent, BeforeInvocationEvent, BeforeModelCallEvent, HookRegistry
from strands.types.exceptions import ContextWindowOverflowException

from custom_agents.context_recovery import (
    claim_context_recovery_attempt,
    invocation_request_state,
    max_total_recovery_attempts,
    min_reduction_tokens,
)
from custom_agents.token_estimation import count_message_tokens, count_text_tokens, truncate_to_token_budget
from custom_agents.tool_result_offloader import SAVED_RESULT_MESSAGE, TaskAwareWorkspaceStorage

logger = logging.getLogger(__name__)


DEFAULT_FILTER_CONFIG: Dict[str, Dict[str, Any]] = {
    "html_document": {"enabled": True, "trigger_tokens": 500},
    "javascript_or_bundle": {"enabled": True, "trigger_tokens": 250},
    "css_or_style_blob": {"enabled": True, "trigger_tokens": 250},
    "base64_or_binary_like": {"enabled": True, "trigger_tokens": 250},
    "minified_or_low_entropy_noise": {"enabled": True, "trigger_tokens": 750},
    "json_large": {
        "enabled": True,
        "trigger_tokens": 7500,
        "comms_json_lenient": True,
        "comms_json_trigger_tokens": 30000,
    },
    "xml_or_svg": {"enabled": True, "trigger_tokens": 1000},
    "log_or_trace": {"enabled": True, "trigger_tokens": 2000},
    "csv_or_table": {"enabled": True, "trigger_tokens": 2000},
    "html_boilerplate_text": {"enabled": True, "trigger_tokens": 1250},
    "agent_transcript": {"enabled": True, "trigger_tokens": 2000},
    "code_file": {"enabled": True, "trigger_tokens": 5000},
}


DEFAULT_INNER_LOOP_CONTEXT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "max_total_tokens": 225000,
    "warning_ratio": 0.75,
    "aggressive_ratio": 0.90,
    "preserve_recent_messages": 8,
    "preserve_head_tokens": 300,
    "preserve_tail_tokens": 200,
    "projected_token_pressure": {
        "enabled": True,
        "threshold_ratio": 0.85,
        "target_ratio": 0.75,
        "fallback_context_window_tokens": None,
    },
    "reactive_overflow": {
        "max_total_recovery_attempts": 3,
        "min_reduction_tokens": 1,
    },
    "filters": DEFAULT_FILTER_CONFIG,
}


def merge_inner_loop_context_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge user inner-loop context config over runtime defaults."""

    def merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(base)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = value
        return result

    return merge(DEFAULT_INNER_LOOP_CONTEXT_CONFIG, dict(config or {}))


@dataclass
class _ToolTextBlock:
    message_index: int
    content_index: int
    item_index: int
    tool_use_id: str
    text: str


@dataclass
class _FilterDecision:
    filter_name: str
    reason: str


class FilteredToolResultConversationManager(ConversationManager):
    """Filter and offload historical tool results using token budgets."""

    _WARNING_KEY = "filtered_tool_result_context_warning_level"
    _PROJECTED_PRESSURE_WARNING_KEY = "filtered_tool_result_projected_pressure_warning"
    _PROJECTED_ACCOUNTING_KEY = "filtered_tool_result_projected_accounting"

    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        storage: TaskAwareWorkspaceStorage,
        logger_: Optional[logging.Logger] = None,
        model: Any = None,
    ) -> None:
        super().__init__()
        self.config = merge_inner_loop_context_config(config)
        # Offloaded results are runtime records in state/tool_results, which
        # agent tools cannot open directly; inspect_tool_result_artifact reads
        # them with an ownership check. The runtime agent hands in the per-call
        # offloader's storage, which knows the agent, team and task: Strands
        # passes its own Agent to hooks, and that object knows none of them.
        self.storage = storage
        self.logger = logger_ or logger
        self.model = model
        self._seen_tool_result_ids: set[str] = set()
        self._pending_tool_result_ids: set[str] = set()
        self._pending_model_call_event: BeforeModelCallEvent | None = None
        self._projected_pressure_missing_limit_warned = False
        self._active_invocation_state: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
            f"filtered_tool_result_context_invocation_state_{id(self)}",
            default=None,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        super().register_hooks(registry, **kwargs)
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(AfterModelCallEvent, self._on_after_model_call)

    def apply_management(self, agent: Any, **kwargs: Any) -> None:
        self._manage_messages(agent, invocation_state=kwargs.get("invocation_state") or {})

    def reduce_context(self, agent: Any, e: Exception | None = None, **kwargs: Any) -> None:
        invocation_state = kwargs.get("invocation_state") or self._active_invocation_state.get() or {}
        attempt = claim_context_recovery_attempt(
            invocation_request_state(invocation_state),
            max_attempts=max_total_recovery_attempts(self.config),
            phase="inner_reduce",
        )
        if not attempt.allowed:
            self._raise_context_overflow(
                e,
                "Context recovery attempt budget was exhausted before the active transcript could be reduced.",
            )

        messages = getattr(agent, "messages", None)
        if not self.enabled or not isinstance(messages, list) or not messages:
            self._raise_context_overflow(e, "Active transcript has no reducible messages.")

        tokens_before = self._message_token_count(messages)
        fingerprint_before = self._message_fingerprint(messages)
        changed_blocks = self._manage_messages(
            agent,
            invocation_state=invocation_state,
            force_aggressive=True,
            reduce_current_tool_results=True,
            emit_pressure_warnings=False,
        )
        tokens_after = self._message_token_count(messages)
        fingerprint_after = self._message_fingerprint(messages)
        projected_before, projected_after, fixed_overhead = self._projected_totals(
            invocation_state,
            message_tokens_before=tokens_before,
            message_tokens_after=tokens_after,
        )
        reduction_tokens = tokens_before - tokens_after
        changed = fingerprint_before != fingerprint_after
        task_id = self.storage.identity_for(agent).get("task_id")
        context_window_limit = self._positive_int(getattr(getattr(agent, "model", None), "context_window_limit", None))
        retry_can_help = context_window_limit is None or fixed_overhead < context_window_limit
        made_progress = changed and reduction_tokens >= min_reduction_tokens(self.config)

        self.logger.warning(
            "event=inner_context_overflow_reduction task_id=%s attempt=%s max_attempts=%s "
            "changed_blocks=%s changed=%s message_tokens_before=%s message_tokens_after=%s "
            "projected_tokens_before=%s projected_tokens_after=%s fixed_overhead_tokens=%s "
            "context_window_limit=%s action=%s",
            task_id,
            attempt.attempt,
            attempt.max_attempts,
            changed_blocks,
            changed,
            tokens_before,
            tokens_after,
            projected_before,
            projected_after,
            fixed_overhead,
            context_window_limit,
            "retry" if made_progress and retry_can_help else "raise",
        )
        if not made_progress:
            self._raise_context_overflow(e, "Active transcript reduction made no measurable token progress.")
        if not retry_can_help:
            self._raise_context_overflow(e, "Fixed provider input overhead exhausts the model context window.")

    def _on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        self._seen_tool_result_ids.clear()
        self._pending_tool_result_ids.clear()
        self._pending_model_call_event = None
        self._active_invocation_state.set(getattr(event, "invocation_state", {}) or {})

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        self._active_invocation_state.set(event.invocation_state)
        if not self.enabled:
            return
        self._record_projected_accounting(event)
        pressure_metadata = self._projected_pressure_metadata(event)
        if pressure_metadata is not None:
            self._manage_messages(
                event.agent,
                invocation_state=event.invocation_state,
                force_aggressive=True,
                forced_target_tokens=pressure_metadata["forced_target_message_tokens"],
                projected_pressure_metadata=pressure_metadata,
            )
        else:
            self._manage_messages(event.agent, invocation_state=event.invocation_state)

        # A request may be rejected, cancelled by a later hook, or retried.
        # Keep the attempted input separate until the model returns successfully.
        self._pending_model_call_event = event
        self._pending_tool_result_ids = {
            block.tool_use_id
            for block in self._collect_tool_text_blocks(event.agent.messages)
            if block.tool_use_id
        }

    def _on_after_model_call(self, event: AfterModelCallEvent) -> None:
        before_event = self._pending_model_call_event
        pending_ids = self._pending_tool_result_ids
        self._pending_model_call_event = None
        self._pending_tool_result_ids = set()
        # Strands calls After hooks in reverse order. The manager registers
        # before retry hooks, so their decision is final when this hook runs.
        if (
            before_event is not None
            and not before_event.cancel
            and event.stop_response is not None
            and event.exception is None
            and not event.retry
        ):
            self._seen_tool_result_ids.update(pending_ids)

    def _manage_messages(
        self,
        agent: Any,
        *,
        invocation_state: Dict[str, Any],
        force_aggressive: bool = False,
        forced_target_tokens: Optional[int] = None,
        projected_pressure_metadata: Optional[Dict[str, Any]] = None,
        reduce_current_tool_results: bool = False,
        emit_pressure_warnings: bool = True,
    ) -> int:
        messages = getattr(agent, "messages", None)
        if not isinstance(messages, list) or not messages:
            return 0

        all_blocks = self._collect_tool_text_blocks(messages)
        current_tool_result_ids = {block.tool_use_id for block in all_blocks if block.tool_use_id}
        unseen_tool_result_ids = current_tool_result_ids - self._seen_tool_result_ids
        changed_blocks = 0

        for block in all_blocks:
            if block.tool_use_id in unseen_tool_result_ids:
                continue
            if self._apply_hygiene_filter(agent, messages, block):
                changed_blocks += 1

        max_total = self._positive_int(self.config.get("max_total_tokens")) or 225000
        warning_ratio = self._bounded_ratio(self.config.get("warning_ratio"), default=0.75)
        aggressive_ratio = self._bounded_ratio(self.config.get("aggressive_ratio"), default=0.90)
        total_tokens = self._message_token_count(messages)
        self._emit_pressure_notices(
            messages,
            invocation_state=invocation_state,
            emit=emit_pressure_warnings,
            total_tokens=total_tokens,
            max_total=max_total,
            warning_ratio=warning_ratio,
            aggressive_ratio=aggressive_ratio,
            force_aggressive=force_aggressive,
            projected_pressure_metadata=projected_pressure_metadata,
        )
        changed_blocks += self._reduce_for_pressure(
            agent,
            messages,
            total_tokens=total_tokens,
            max_total=max_total,
            warning_ratio=warning_ratio,
            aggressive_ratio=aggressive_ratio,
            force_aggressive=force_aggressive,
            forced_target_tokens=forced_target_tokens,
            reduce_current_tool_results=reduce_current_tool_results,
            unseen_tool_result_ids=unseen_tool_result_ids,
            projected_pressure_metadata=projected_pressure_metadata,
        )

        return changed_blocks

    def _emit_pressure_notices(
        self,
        messages: list[dict[str, Any]],
        *,
        invocation_state: Dict[str, Any],
        emit: bool,
        total_tokens: int,
        max_total: int,
        warning_ratio: float,
        aggressive_ratio: float,
        force_aggressive: bool,
        projected_pressure_metadata: Optional[Dict[str, Any]],
    ) -> None:
        if not emit:
            return
        if total_tokens >= int(max_total * warning_ratio):
            self._append_pressure_warning(
                messages,
                invocation_state=invocation_state,
                level="warning",
                total_tokens=total_tokens,
                max_total=max_total,
                ratio=warning_ratio,
            )
        if projected_pressure_metadata is not None:
            self._append_projected_pressure_warning(
                messages,
                invocation_state=invocation_state,
                metadata=projected_pressure_metadata,
            )
        should_emit_aggressive = force_aggressive or total_tokens >= int(max_total * aggressive_ratio)
        projected_only = projected_pressure_metadata is not None and total_tokens < int(max_total * warning_ratio)
        if should_emit_aggressive and not projected_only:
            self._append_pressure_warning(
                messages,
                invocation_state=invocation_state,
                level="aggressive",
                total_tokens=total_tokens,
                max_total=max_total,
                ratio=aggressive_ratio,
            )

    def _reduce_for_pressure(
        self,
        agent: Any,
        messages: list[dict[str, Any]],
        *,
        total_tokens: int,
        max_total: int,
        warning_ratio: float,
        aggressive_ratio: float,
        force_aggressive: bool,
        forced_target_tokens: Optional[int],
        reduce_current_tool_results: bool,
        unseen_tool_result_ids: set[str],
        projected_pressure_metadata: Optional[Dict[str, Any]],
    ) -> int:
        if not force_aggressive and total_tokens < int(max_total * aggressive_ratio):
            return 0
        target_tokens = forced_target_tokens if forced_target_tokens is not None else int(max_total * warning_ratio)
        # Only a provider's actual rejection (reduce_context) may shrink results
        # the model has not read yet. Projected pressure is an early signal with
        # headroom left: hiding a result the agent needs for its next step (a
        # staged change id, for example) costs more than the call it might save.
        include_current = reduce_current_tool_results
        reduced_count = self._aggressively_reduce_tool_results(
            agent,
            messages,
            target_tokens=target_tokens,
            skip_tool_result_ids=set() if include_current else unseen_tool_result_ids,
            preserve_recent=not include_current,
            require_seen=not include_current,
        )
        if projected_pressure_metadata is not None:
            self._log_projected_pressure_reduction(projected_pressure_metadata, reduced_count)
        return reduced_count

    def _log_projected_pressure_reduction(
        self,
        metadata: Dict[str, Any],
        reduced_count: int,
    ) -> None:
        self.logger.warning(
            "strategy=<filtered_tool_result_context.projected_token_pressure> "
            "projected_input_tokens=<%s> context_window_limit=<%s> estimated_utilization=<%s> "
            "threshold_ratio=<%s> target_ratio=<%s> current_message_tokens=<%s> "
            "forced_target_message_tokens=<%s> reduced_tool_results=<%s>",
            metadata.get("projected_input_tokens"),
            metadata.get("context_window_limit"),
            metadata.get("estimated_utilization"),
            metadata.get("threshold_ratio"),
            metadata.get("target_ratio"),
            metadata.get("current_message_tokens"),
            metadata.get("forced_target_message_tokens"),
            reduced_count,
        )

    def _record_projected_accounting(self, event: BeforeModelCallEvent) -> None:
        projected_input_tokens = self._positive_int(getattr(event, "projected_input_tokens", None))
        messages = getattr(getattr(event, "agent", None), "messages", None)
        if projected_input_tokens is None or not isinstance(messages, list):
            return
        message_tokens = self._message_token_count(messages)
        request_state = invocation_request_state(event.invocation_state)
        request_state[self._PROJECTED_ACCOUNTING_KEY] = {
            "projected_input_tokens": projected_input_tokens,
            "message_tokens": message_tokens,
            "fixed_overhead_tokens": max(0, projected_input_tokens - message_tokens),
        }

    def _projected_totals(
        self,
        invocation_state: Dict[str, Any],
        *,
        message_tokens_before: int,
        message_tokens_after: int,
    ) -> tuple[int, int, int]:
        request_state = invocation_request_state(invocation_state)
        accounting = request_state.get(self._PROJECTED_ACCOUNTING_KEY)
        fixed_overhead = 0
        if isinstance(accounting, dict):
            fixed_overhead = max(0, int(accounting.get("fixed_overhead_tokens") or 0))
        return (
            fixed_overhead + message_tokens_before,
            fixed_overhead + message_tokens_after,
            fixed_overhead,
        )

    @staticmethod
    def _message_fingerprint(messages: list[dict[str, Any]]) -> str:
        rendered = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _raise_context_overflow(error: Exception | None, message: str) -> None:
        if error is not None:
            raise error
        raise ContextWindowOverflowException(message)

    def _apply_hygiene_filter(self, agent: Any, messages: list[dict[str, Any]], block: _ToolTextBlock) -> bool:
        text = block.text
        if self._is_managed_replacement(text):
            return False
        decision = self._classify_text(text)
        if decision is None:
            return False
        artifact_path = self._write_artifact(
            agent=agent,
            messages=messages,
            tool_use_id=block.tool_use_id,
            filter_name=decision.filter_name,
            text=text,
        )
        replacement = self._build_replacement_text(
            filter_name=decision.filter_name,
            reason=decision.reason,
            text=text,
            artifact_path=artifact_path,
            artifact_only=False,
        )
        self._set_tool_text(messages, block, replacement)
        return True

    def _aggressively_reduce_tool_results(
        self,
        agent: Any,
        messages: list[dict[str, Any]],
        *,
        target_tokens: int,
        skip_tool_result_ids: set[str],
        preserve_recent: bool = True,
        require_seen: bool = True,
    ) -> int:
        preserve_recent_count = self._positive_int(self.config.get("preserve_recent_messages")) or 8
        oldest_allowed_index = max(0, len(messages) - preserve_recent_count) if preserve_recent else len(messages)
        blocks = [
            block
            for block in self._collect_tool_text_blocks(messages)
            if block.message_index < oldest_allowed_index
            and (not require_seen or block.tool_use_id in self._seen_tool_result_ids)
            and block.tool_use_id not in skip_tool_result_ids
            and self._artifact_only_reduction_worthwhile(block.text)
        ]
        # Results the model already read go first, largest first; results it
        # has not read are shrunk only when that is still not enough.
        blocks.sort(
            key=lambda block: (
                block.tool_use_id not in self._seen_tool_result_ids,
                -self.estimate_tokens(block.text),
            )
        )

        reduced_count = 0
        for block in blocks:
            if self._message_token_count(messages) <= target_tokens:
                return reduced_count
            text = block.text
            artifact_path = self._artifact_path_from_replacement(text)
            if artifact_path is None:
                artifact_path = self._write_artifact(
                    agent=agent,
                    messages=messages,
                    tool_use_id=block.tool_use_id,
                    filter_name="artifact_only_context_reduction",
                    text=text,
                )
            existing_metadata = self._replacement_metadata(text)
            replacement = self._build_replacement_text(
                filter_name="artifact_only_context_reduction",
                reason="active transcript exceeded aggressive inner-loop token budget",
                text=text,
                artifact_path=artifact_path,
                artifact_only=True,
                original_chars=(
                    int(existing_metadata.get("original_chars"))
                    if isinstance(existing_metadata, dict) and existing_metadata.get("original_chars")
                    else None
                ),
                original_tokens=(
                    int(existing_metadata.get("original_tokens"))
                    if isinstance(existing_metadata, dict) and existing_metadata.get("original_tokens")
                    else None
                ),
            )
            self._set_tool_text(messages, block, replacement)
            reduced_count += 1
        return reduced_count

    def _projected_pressure_metadata(self, event: BeforeModelCallEvent) -> Optional[Dict[str, Any]]:
        if not self._projected_pressure_enabled():
            return None
        projected_input_tokens = getattr(event, "projected_input_tokens", None)
        if projected_input_tokens is None:
            return None
        try:
            projected_tokens = int(projected_input_tokens)
        except (TypeError, ValueError):
            return None
        if projected_tokens <= 0:
            return None

        context_window_limit = self._projected_pressure_context_window_limit(event)
        if context_window_limit is None:
            return None
        threshold_ratio = self._projected_pressure_threshold_ratio()
        utilization = self._projected_pressure_utilization(
            event,
            projected_tokens=projected_tokens,
            context_window_limit=context_window_limit,
        )
        if utilization < threshold_ratio:
            return None
        target_ratio = self._projected_pressure_target_ratio(threshold_ratio)
        desired_projected_tokens = int(context_window_limit * target_ratio)
        required_reduction = max(
            0,
            math.ceil((utilization - target_ratio) * context_window_limit),
        )
        if required_reduction <= 0:
            return None

        messages = getattr(getattr(event, "agent", None), "messages", None)
        if not isinstance(messages, list) or not messages:
            return None
        current_message_tokens = self._message_token_count(messages)
        forced_target_message_tokens = max(0, current_message_tokens - required_reduction)
        return {
            "projected_input_tokens": projected_tokens,
            "context_window_limit": context_window_limit,
            "estimated_utilization": utilization,
            "threshold_ratio": threshold_ratio,
            "target_ratio": target_ratio,
            "desired_projected_tokens": desired_projected_tokens,
            "required_reduction_tokens": required_reduction,
            "current_message_tokens": current_message_tokens,
            "forced_target_message_tokens": forced_target_message_tokens,
        }

    def _projected_pressure_utilization(
        self,
        event: BeforeModelCallEvent,
        *,
        projected_tokens: int,
        context_window_limit: int,
    ) -> float:
        """Use the Strands model contract unless an operator fallback owns the limit."""

        model = getattr(getattr(event, "agent", None), "model", None)
        model_limit = self._positive_int(getattr(model, "context_window_limit", None))
        if model_limit == context_window_limit:
            return float(model.estimate_utilization(projected_tokens))

        # An explicit projected-pressure fallback is deliberately independent
        # of the model's own default-limit behavior. It is used only when the
        # model did not publish a usable context window.
        return projected_tokens / context_window_limit

    def _projected_pressure_config(self) -> Dict[str, Any]:
        value = self.config.get("projected_token_pressure")
        return dict(value) if isinstance(value, dict) else {}

    def _projected_pressure_enabled(self) -> bool:
        return bool(self._projected_pressure_config().get("enabled", True))

    def _projected_pressure_threshold_ratio(self) -> float:
        value = self._projected_pressure_config().get("threshold_ratio", 0.85)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            self.logger.warning("Invalid projected_token_pressure.threshold_ratio=%r; using 0.85.", value)
            return 0.85
        if parsed <= 0 or parsed > 1:
            self.logger.warning("Invalid projected_token_pressure.threshold_ratio=%r; using 0.85.", value)
            return 0.85
        return parsed

    def _projected_pressure_target_ratio(self, threshold_ratio: float) -> float:
        value = self._projected_pressure_config().get("target_ratio", 0.75)
        fallback = min(0.75, max(0.01, threshold_ratio - 0.05))
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            self.logger.warning("Invalid projected_token_pressure.target_ratio=%r; using %s.", value, fallback)
            return fallback
        if parsed <= 0 or parsed >= threshold_ratio:
            self.logger.warning("Invalid projected_token_pressure.target_ratio=%r; using %s.", value, fallback)
            return fallback
        return parsed

    def _projected_pressure_context_window_limit(self, event: BeforeModelCallEvent) -> Optional[int]:
        model = getattr(getattr(event, "agent", None), "model", None)
        limit = getattr(model, "context_window_limit", None)
        parsed = self._positive_int(limit)
        if parsed is not None:
            return parsed
        fallback = self._positive_int(self._projected_pressure_config().get("fallback_context_window_tokens"))
        if fallback is not None:
            return fallback
        if not self._projected_pressure_missing_limit_warned:
            self._projected_pressure_missing_limit_warned = True
            self.logger.warning(
                "strategy=<filtered_tool_result_context.projected_token_pressure> "
                "context_window_limit missing; skipping projected-token pressure handling. "
                "Set llm.context_window_tokens or inner_loop_context.projected_token_pressure.fallback_context_window_tokens."
            )
        return None

    def _append_pressure_warning(
        self,
        messages: list[dict[str, Any]],
        *,
        invocation_state: Dict[str, Any],
        level: str,
        total_tokens: int,
        max_total: int,
        ratio: float,
    ) -> None:
        level_value = 2 if level == "aggressive" else 1
        request_state = invocation_state.setdefault("request_state", {})
        emitted = int(request_state.get(self._WARNING_KEY) or 0)
        if emitted >= level_value:
            return
        request_state[self._WARNING_KEY] = level_value
        percent = round((total_tokens / max_total) * 100, 1)
        warning = {
            "inner_loop_context_warning": True,
            "level": level,
            "message": (
                f"Active tool transcript is {percent}% of the configured inner-loop token budget "
                f"({total_tokens} / {max_total} estimated tokens). "
                "Narrow further retrieval, summarize current findings, or use artifact-backed inspection."
            ),
            "threshold_ratio": ratio,
            "current_tokens": total_tokens,
            "max_total_tokens": max_total,
            "strategy": "filtered_tool_result_context",
        }
        messages.append({"role": "user", "content": [{"text": json.dumps(warning, sort_keys=True)}]})

    def _append_projected_pressure_warning(
        self,
        messages: list[dict[str, Any]],
        *,
        invocation_state: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> None:
        request_state = invocation_state.setdefault("request_state", {})
        if request_state.get(self._PROJECTED_PRESSURE_WARNING_KEY):
            return
        request_state[self._PROJECTED_PRESSURE_WARNING_KEY] = True
        warning = {
            "inner_loop_context_warning": True,
            "level": "projected_pressure",
            "message": (
                "Projected model input is near the configured context window. Old artifact-backed tool results "
                "may be reduced before this model call."
            ),
            "projected_input_tokens": metadata.get("projected_input_tokens"),
            "context_window_limit": metadata.get("context_window_limit"),
            "estimated_utilization": metadata.get("estimated_utilization"),
            "threshold_ratio": metadata.get("threshold_ratio"),
            "target_ratio": metadata.get("target_ratio"),
            "strategy": "filtered_tool_result_context.projected_token_pressure",
        }
        messages.append({"role": "user", "content": [{"text": json.dumps(warning, sort_keys=True)}]})

    def _collect_tool_text_blocks(self, messages: Iterable[Mapping[str, Any]]) -> list[_ToolTextBlock]:
        blocks: list[_ToolTextBlock] = []
        for message_index, message in enumerate(messages):
            if self._is_system_message(message):
                continue
            for content_index, content in enumerate(message.get("content", []) or []):
                if not isinstance(content, dict) or "toolResult" not in content:
                    continue
                tool_result = content.get("toolResult")
                if not isinstance(tool_result, dict):
                    continue
                tool_use_id = str(tool_result.get("toolUseId") or "")
                for item_index, item in enumerate(tool_result.get("content", []) or []):
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        blocks.append(
                            _ToolTextBlock(
                                message_index=message_index,
                                content_index=content_index,
                                item_index=item_index,
                                tool_use_id=tool_use_id,
                                text=item["text"],
                            )
                        )
        return blocks

    def _set_tool_text(self, messages: list[dict[str, Any]], block: _ToolTextBlock, text: str) -> None:
        tool_result = messages[block.message_index]["content"][block.content_index]["toolResult"]
        tool_result["content"][block.item_index]["text"] = text

    def _classify_text(self, text: str) -> Optional[_FilterDecision]:
        filters = self.config.get("filters") if isinstance(self.config.get("filters"), dict) else {}
        normalized = text[:200000]
        lower = normalized.lower()
        for name in DEFAULT_FILTER_CONFIG:
            filter_config = self._filter_config(filters, name)
            if not filter_config.get("enabled", True):
                continue
            trigger = self._positive_int(filter_config.get("trigger_tokens")) or 1
            if self.estimate_tokens(text) < trigger:
                continue
            reason = self._match_filter(name, text, lower, filter_config)
            if reason:
                return _FilterDecision(filter_name=name, reason=reason)
        return None

    def _filter_config(self, filters: Dict[str, Any], name: str) -> Dict[str, Any]:
        defaults = dict(DEFAULT_FILTER_CONFIG.get(name, {}))
        override = filters.get(name) if isinstance(filters, dict) else None
        if isinstance(override, dict):
            defaults.update(override)
        return defaults

    def _match_filter(self, name: str, text: str, lower: str, config: Dict[str, Any]) -> Optional[str]:
        if name == "html_document":
            if any(marker in lower for marker in ("<!doctype html", "<html", "<head", "<body")):
                return "HTML document markup detected"
            if self._count_matches(lower, ("<div", "<span", "<script", "<style", "<meta", "<link")) >= 8:
                return "high HTML tag density detected"
        if name == "javascript_or_bundle":
            if any(marker in lower for marker in ("<script", "webpack", "__next_data__", "self.__", "window.__")):
                return "JavaScript/script payload detected"
            if ("=>" in text or "function(" in text) and self._long_line_ratio(text) > 0.30:
                return "JavaScript-like minified payload detected"
        if name == "css_or_style_blob":
            if "<style" in lower or self._count_matches(lower, ("@media", "font-", "display:", "color:", "margin:", "padding:")) >= 10:
                return "CSS/style payload detected"
        if name == "base64_or_binary_like":
            if "base64," in lower or "data:image/" in lower:
                return "base64/data URI payload detected"
            if re.search(r"[A-Za-z0-9+/=]{1200,}", text):
                return "long base64-like run detected"
        if name == "minified_or_low_entropy_noise":
            if self._long_line_ratio(text) > 0.55 and self._space_ratio(text) < 0.08:
                return "minified or low-whitespace payload detected"
        if name == "json_large":
            return self._match_json_large(text, config)
        if name == "xml_or_svg":
            if any(marker in lower for marker in ("<?xml", "<svg", "<rss", "<feed", "<urlset", "<sitemapindex")):
                return "XML/SVG/feed/sitemap payload detected"
        if name == "log_or_trace":
            if self._count_matches(text, ("Traceback", "Exception", "Caused by", " ERROR ", " WARN ", " INFO ", " DEBUG ")) >= 6:
                return "large log or traceback detected"
            if len(re.findall(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", text)) >= 10:
                return "timestamped log payload detected"
        if name == "csv_or_table":
            if self._looks_like_csv_or_table(text):
                return "large tabular payload detected"
        if name == "html_boilerplate_text":
            if self._count_matches(lower, ("cookie", "privacy", "subscribe", "sign in", "related articles", "advertisement")) >= 8:
                return "web boilerplate text detected"
        if name == "agent_transcript":
            if self._count_matches(lower, ("assistant:", "user:", "tool:", "thought:", "action:", "observation:")) >= 8:
                return "agent/transcript payload detected"
        if name == "code_file":
            if self._count_matches(text, ("import ", "def ", "class ", "function ", "const ", "let ", "export ")) >= 20:
                return "large source-code payload detected"
        return None

    def _match_json_large(self, text: str, config: Dict[str, Any]) -> Optional[str]:
        stripped = text.strip()
        if not stripped.startswith(("{", "[")):
            return None
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return None
        if bool(config.get("comms_json_lenient", True)) and self._looks_like_comms_json(parsed):
            comms_trigger = self._positive_int(config.get("comms_json_trigger_tokens")) or 30000
            if self.estimate_tokens(text) < comms_trigger:
                return None
        if self._json_large_signal(parsed):
            return "large or repetitive JSON payload detected"
        return None

    def _json_large_signal(self, value: Any) -> bool:
        if isinstance(value, list):
            return len(value) >= 20 or any(self._json_large_signal(item) for item in value[:20])
        if isinstance(value, dict):
            if len(value) >= 40:
                return True
            return any(self._json_large_signal(item) for item in value.values())
        return False

    def _looks_like_comms_json(self, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        comms_keys = {
            "message_class",
            "msg_header",
            "msg_meta",
            "msg_content",
            "selected_message_type",
            "task_id",
            "task",
            "task_query",
        }
        return bool(comms_keys.intersection(value.keys()))

    def _write_artifact(
        self,
        *,
        agent: Any,
        messages: list[dict[str, Any]],
        tool_use_id: str,
        filter_name: str,
        text: str,
    ) -> str:
        """Save the full text where inspect_tool_result_artifact can open it; returns its manifest path."""

        stored = self.storage.write_artifact(
            result={"content": [{"text": text}]},
            identity=self.storage.identity_for(agent),
            tool_name=self._tool_name_for(messages, tool_use_id),
            tool_use_id=tool_use_id or "tool",
            reason=filter_name,
            details={"original_chars": len(text), "original_tokens": self.estimate_tokens(text)},
        )
        return str(stored["artifact_manifest_path"])

    @staticmethod
    def _tool_name_for(messages: list[dict[str, Any]], tool_use_id: str) -> str:
        for message in messages:
            for content in message.get("content", []) or []:
                tool_use = content.get("toolUse") if isinstance(content, dict) else None
                if isinstance(tool_use, dict) and tool_use.get("toolUseId") == tool_use_id:
                    return str(tool_use.get("name") or "unknown")
        return "unknown"

    def _build_replacement_text(
        self,
        *,
        filter_name: str,
        reason: str,
        text: str,
        artifact_path: str,
        artifact_only: bool,
        original_chars: Optional[int] = None,
        original_tokens: Optional[int] = None,
    ) -> str:
        head_tokens = 0 if artifact_only else (self._positive_int(self.config.get("preserve_head_tokens")) or 300)
        tail_tokens = 0 if artifact_only else (self._positive_int(self.config.get("preserve_tail_tokens")) or 200)
        head = (
            truncate_to_token_budget(text, max_tokens=head_tokens, estimate=self.estimate_tokens)
            if head_tokens
            else ""
        )
        tail = (
            truncate_to_token_budget(text, max_tokens=tail_tokens, estimate=self.estimate_tokens, from_tail=True)
            if tail_tokens and self.estimate_tokens(text) > head_tokens
            else ""
        )
        payload = {
            "filtered_tool_result": True,
            "filter": filter_name,
            "reason": reason,
            "message": SAVED_RESULT_MESSAGE,
            "artifact_manifest_path": str(artifact_path),
            "original_chars": original_chars if original_chars is not None else len(text),
            "original_tokens": original_tokens if original_tokens is not None else self.estimate_tokens(text),
            "retained_tokens": self.estimate_tokens(head) + self.estimate_tokens(tail),
            "artifact_only": artifact_only,
            "head": head,
            "tail": tail,
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def _artifact_path_from_replacement(self, text: str) -> Optional[str]:
        parsed = self._replacement_metadata(text)
        if isinstance(parsed, dict) and parsed.get("artifact_manifest_path"):
            return str(parsed["artifact_manifest_path"])
        return None

    @staticmethod
    def _replacement_metadata(text: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict) and (
            parsed.get("filtered_tool_result")
            or parsed.get("offloaded_tool_result")
            or parsed.get("inner_loop_context_warning")
        ):
            return parsed
        return None

    def _is_managed_replacement(self, text: str) -> bool:
        return self._artifact_path_from_replacement(text) is not None

    def _artifact_only_reduction_worthwhile(self, text: str) -> bool:
        if self.estimate_tokens(text) > 250:
            return True
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return False
        return bool(
            isinstance(parsed, dict)
            and (parsed.get("filtered_tool_result") or parsed.get("offloaded_tool_result"))
            and not parsed.get("artifact_only")
        )

    @staticmethod
    def _is_system_message(message: Any) -> bool:
        return isinstance(message, dict) and str(message.get("role") or "").lower() == "system"

    def _message_token_count(self, messages: list[dict[str, Any]]) -> int:
        return count_message_tokens(self.model, messages)

    def estimate_tokens(self, text: str) -> int:
        return count_text_tokens(self.model, text)

    @staticmethod
    def _count_matches(text: str, needles: Iterable[str]) -> int:
        return sum(text.count(needle) for needle in needles)

    @staticmethod
    def _long_line_ratio(text: str) -> float:
        lines = text.splitlines() or [text]
        if not lines:
            return 0.0
        return sum(1 for line in lines if len(line) > 500) / len(lines)

    @staticmethod
    def _space_ratio(text: str) -> float:
        if not text:
            return 1.0
        return sum(1 for char in text if char.isspace()) / len(text)

    @staticmethod
    def _looks_like_csv_or_table(text: str) -> bool:
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 25:
            return False
        comma_counts = [line.count(",") for line in lines[:50]]
        pipe_counts = [line.count("|") for line in lines[:50]]
        return max(comma_counts or [0]) >= 3 and len(set(comma_counts[:20])) <= 4 or max(pipe_counts or [0]) >= 3

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _bounded_ratio(value: Any, *, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return min(1.0, max(0.01, parsed))
