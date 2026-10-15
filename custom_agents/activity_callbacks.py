"""Strands callback helpers for runtime activity and local diagnostics."""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, Optional, TextIO

from custom_messages.enums import TaskActivityStage, TaskActivityType


class ActivityPhaseCallbackHandler:
    """Emit coarse user-safe tool phase activity updates during LLM execution."""

    def __init__(
        self,
        *,
        emit_activity: Callable[..., None],
        task_id: Optional[str],
    ) -> None:
        self.emit_activity = emit_activity
        self.task_id = task_id
        self.previous_tool_use: Optional[Dict[str, Any]] = None

    def __call__(self, **kwargs: Any) -> None:
        current_tool_use = kwargs.get("current_tool_use") or {}
        if not current_tool_use or not current_tool_use.get("name"):
            return
        normalized_tool_use = dict(current_tool_use)
        if self.previous_tool_use == normalized_tool_use:
            return
        self.previous_tool_use = normalized_tool_use
        tool_name = str(normalized_tool_use.get("name") or "unknown")
        summary, detail = self._classify_tool_use(tool_name)
        self.emit_activity(
            activity_type=TaskActivityType.TOOL_PHASE,
            stage=TaskActivityStage.RUNNING,
            task_id=self.task_id,
            summary=summary,
            detail=detail,
            details={"tool_name": tool_name},
        )

    @staticmethod
    def _classify_tool_use(tool_name: str) -> tuple[str, str]:
        normalized = tool_name.strip().lower()
        if any(token in normalized for token in ("search", "exa", "web", "browser")):
            return "Researching references.", f"Using `{tool_name}` to gather external information."
        if any(token in normalized for token in ("read", "file", "cat", "rg", "grep", "codebase")):
            return "Inspecting codebase.", f"Using `{tool_name}` to inspect local files or code."
        if any(token in normalized for token in ("shell", "bash", "pytest", "test", "uv")):
            return "Running verification.", f"Using `{tool_name}` to execute a shell or validation step."
        if any(token in normalized for token in ("agent", "instantiate", "team_template")):
            return "Starting specialist agent.", f"Using `{tool_name}` to create or coordinate a worker."
        return "Using a tool.", f"Using `{tool_name}` as part of task execution."


class TerminalReasoningCallbackHandler:
    """Print provider-exposed reasoning deltas to a local terminal stream."""

    def __init__(self, *, stream: TextIO | None = None, label: str = "[provider reasoning] ") -> None:
        self.stream = stream or sys.stderr
        self.label = label
        self._started = False

    def __call__(self, **kwargs: Any) -> None:
        reasoning_text = kwargs.get("reasoningText")
        if reasoning_text:
            if not self._started:
                print(self.label, end="", file=self.stream, flush=True)
                self._started = True
            print(str(reasoning_text), end="", file=self.stream, flush=True)

        if self._started and (kwargs.get("complete") or kwargs.get("result") is not None):
            print("", file=self.stream, flush=True)
            self._started = False


class TerminalReasoningDiagnosticsCallbackHandler:
    """Count callback-level reasoning events without logging reasoning text."""

    def __init__(
        self,
        *,
        logger: Any,
        agent_id: str,
        task_id: Optional[str],
    ) -> None:
        self.logger = logger
        self.agent_id = agent_id
        self.task_id = task_id or "-"
        self.callback_calls = 0
        self.callback_keys: set[str] = set()
        self.raw_event_keys: set[str] = set()
        self.raw_delta_keys: set[str] = set()
        self.raw_reasoning_chunks = 0
        self.raw_reasoning_text_chars = 0
        self.raw_reasoning_signature_chunks = 0
        self.raw_reasoning_redacted_chunks = 0
        self.normalized_reasoning_events = 0
        self.normalized_reasoning_chars = 0
        self.normalized_reasoning_signature_events = 0
        self.normalized_reasoning_redacted_events = 0
        self.raw_text_chunks = 0
        self.raw_text_chars = 0
        self.normalized_data_events = 0
        self.normalized_data_chars = 0
        self._logged_summary = False

    def __call__(self, **kwargs: Any) -> None:
        self.callback_calls += 1
        self.callback_keys.update(str(key) for key in kwargs.keys())
        self._count_raw_stream_event(kwargs.get("event"))
        self._count_normalized_callback_event(kwargs)

        if kwargs.get("result") is not None:
            self.log_summary()

    def _count_raw_stream_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        self.raw_event_keys.update(str(key) for key in event.keys())
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if not isinstance(delta, dict):
            return
        self.raw_delta_keys.update(str(key) for key in delta.keys())
        text = delta.get("text")
        if text:
            self.raw_text_chunks += 1
            self.raw_text_chars += len(str(text))

        reasoning = delta.get("reasoningContent")
        if not isinstance(reasoning, dict):
            return
        self.raw_reasoning_chunks += 1
        if reasoning.get("text"):
            self.raw_reasoning_text_chars += len(str(reasoning["text"]))
        if reasoning.get("signature"):
            self.raw_reasoning_signature_chunks += 1
        if reasoning.get("redactedContent"):
            self.raw_reasoning_redacted_chunks += 1

    def _count_normalized_callback_event(self, kwargs: dict[str, Any]) -> None:
        reasoning_text = kwargs.get("reasoningText")
        if reasoning_text:
            self.normalized_reasoning_events += 1
            self.normalized_reasoning_chars += len(str(reasoning_text))
        if kwargs.get("reasoning_signature"):
            self.normalized_reasoning_signature_events += 1
        if kwargs.get("reasoningRedactedContent"):
            self.normalized_reasoning_redacted_events += 1
        data = kwargs.get("data")
        if data:
            self.normalized_data_events += 1
            self.normalized_data_chars += len(str(data))

    def log_summary(self) -> None:
        if self._logged_summary:
            return
        self._logged_summary = True
        log_method = self.logger.info
        if self.raw_reasoning_chunks == 0 and self.normalized_reasoning_events == 0:
            log_method = self.logger.warning
        elif self.raw_reasoning_chunks > 0 and self.normalized_reasoning_events == 0:
            log_method = self.logger.warning

        log_method(
            "[%s] Terminal reasoning diagnostics summary: task_id=%s "
            "callback_calls=%s raw_reasoning_chunks=%s raw_reasoning_text_chars=%s "
            "raw_reasoning_signature_chunks=%s raw_reasoning_redacted_chunks=%s "
            "normalized_reasoning_events=%s normalized_reasoning_chars=%s "
            "normalized_reasoning_signature_events=%s normalized_reasoning_redacted_events=%s "
            "raw_text_chunks=%s raw_text_chars=%s normalized_data_events=%s normalized_data_chars=%s "
            "callback_keys=%s raw_event_keys=%s raw_delta_keys=%s",
            self.agent_id,
            self.task_id,
            self.callback_calls,
            self.raw_reasoning_chunks,
            self.raw_reasoning_text_chars,
            self.raw_reasoning_signature_chunks,
            self.raw_reasoning_redacted_chunks,
            self.normalized_reasoning_events,
            self.normalized_reasoning_chars,
            self.normalized_reasoning_signature_events,
            self.normalized_reasoning_redacted_events,
            self.raw_text_chunks,
            self.raw_text_chars,
            self.normalized_data_events,
            self.normalized_data_chars,
            sorted(self.callback_keys),
            sorted(self.raw_event_keys),
            sorted(self.raw_delta_keys),
        )
