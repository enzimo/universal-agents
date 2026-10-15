"""Tool-call budget guardrails for Strands agent invocations."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable, Dict, List, Optional


def compact_debug_preview(value: Any, *, max_chars: int = 1200) -> str:
    try:
        rendered = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    if len(rendered) <= max_chars:
        return rendered
    return f"{rendered[:max_chars]}...<truncated {len(rendered) - max_chars} chars>"


def format_tool_count_summary(tool_sequence: List[str]) -> str:
    if not tool_sequence:
        return "none"
    tool_counts = Counter(tool_sequence)
    return ", ".join(
        f"{tool_name} x{count}" for tool_name, count in tool_counts.most_common(5)
    )


def collect_recent_strands_tool_transcript(strands_agent: Any, *, max_blocks: int = 8) -> List[Dict[str, Any]]:
    messages = getattr(strands_agent, "messages", None)
    if not messages:
        return []

    blocks: List[Dict[str, Any]] = []
    for message in messages[-12:]:
        role = message.get("role") if isinstance(message, dict) else None
        content_blocks = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content_blocks, list):
            continue
        for content_block in content_blocks:
            if not isinstance(content_block, dict):
                continue
            if "toolUse" in content_block and isinstance(content_block["toolUse"], dict):
                tool_use = content_block["toolUse"]
                tool_input = tool_use.get("input")
                input_keys = sorted(str(key) for key in tool_input.keys()) if isinstance(tool_input, dict) else None
                blocks.append(
                    {
                        "role": role,
                        "event": "toolUse",
                        "name": tool_use.get("name"),
                        "toolUseId": tool_use.get("toolUseId"),
                        "input_type": type(tool_input).__name__,
                        "input_keys": input_keys,
                        "input_preview": compact_debug_preview(tool_input, max_chars=1200),
                    }
                )
            elif "toolResult" in content_block and isinstance(content_block["toolResult"], dict):
                tool_result = content_block["toolResult"]
                blocks.append(
                    {
                        "role": role,
                        "event": "toolResult",
                        "toolUseId": tool_result.get("toolUseId"),
                        "status": tool_result.get("status"),
                        "content_preview": compact_debug_preview(tool_result.get("content"), max_chars=1200),
                    }
                )
    return blocks[-max_blocks:]


class ToolCallLimitExceeded(RuntimeError):
    """Raised when one Strands invocation exceeds the configured tool-use cap."""

    def __init__(
        self,
        *,
        max_tool_calls: int,
        observed_tool_calls: int,
        tool_sequence: List[str],
        last_attempted_tool: Optional[str],
        recent_tool_uses: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.max_tool_calls = max_tool_calls
        self.observed_tool_calls = observed_tool_calls
        self.tool_sequence = list(tool_sequence)
        self.last_attempted_tool = last_attempted_tool
        self.recent_tool_uses = list(recent_tool_uses or [])
        count_summary = format_tool_count_summary(self.tool_sequence)
        recent_summary = ""
        if self.recent_tool_uses:
            recent_summary = (
                " Recent attempted tools: "
                + compact_debug_preview(self.recent_tool_uses[-3:], max_chars=900)
                + "."
            )
        message = (
            f"Tool call limit exceeded during a single LLM invocation: "
            f"attempted tool call {observed_tool_calls} with limit {max_tool_calls}. "
            f"Last attempted tool: {last_attempted_tool or 'unknown'}. "
            f"Observed tool counts: {count_summary}.{recent_summary}"
        )
        super().__init__(message)


class ToolCallLimitCallbackHandler:
    """Track distinct tool-use events and abort when the cap is exceeded."""

    def __init__(
        self,
        *,
        max_tool_calls: int,
        delegate: Optional[Callable[..., None]] = None,
        raise_on_exceeded: bool = True,
        cancel_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.max_tool_calls = max_tool_calls
        self.delegate = delegate
        self.raise_on_exceeded = raise_on_exceeded
        self.cancel_callback = cancel_callback
        self.tool_sequence: List[str] = []
        self.tool_observations: List[Dict[str, Any]] = []
        self._tool_index_by_key: Dict[str, int] = {}
        self.limit_error: Optional[ToolCallLimitExceeded] = None

    @staticmethod
    def _tool_key(current_tool_use: Dict[str, Any]) -> str:
        tool_use_id = current_tool_use.get("toolUseId")
        if tool_use_id:
            return f"id:{tool_use_id}"
        return f"name:{current_tool_use.get('name') or 'unknown'}"

    @staticmethod
    def _summarize_tool_use(current_tool_use: Dict[str, Any], *, sequence_number: int) -> Dict[str, Any]:
        tool_input = current_tool_use.get("input")
        input_keys = sorted(str(key) for key in tool_input.keys()) if isinstance(tool_input, dict) else None
        return {
            "sequence": sequence_number,
            "name": str(current_tool_use.get("name") or "unknown"),
            "toolUseId": current_tool_use.get("toolUseId"),
            "input_type": type(tool_input).__name__,
            "input_keys": input_keys,
            "input_preview": compact_debug_preview(tool_input, max_chars=1200),
        }

    def __call__(self, **kwargs: Any) -> None:
        current_tool_use = kwargs.get("current_tool_use") or {}
        if current_tool_use and current_tool_use.get("name"):
            normalized_tool_use = dict(current_tool_use)
            tool_key = self._tool_key(normalized_tool_use)
            existing_index = self._tool_index_by_key.get(tool_key)
            if existing_index is not None:
                self.tool_observations[existing_index] = self._summarize_tool_use(
                    normalized_tool_use,
                    sequence_number=existing_index + 1,
                )
            else:
                tool_name = str(normalized_tool_use.get("name") or "unknown")
                self.tool_sequence.append(tool_name)
                self._tool_index_by_key[tool_key] = len(self.tool_observations)
                self.tool_observations.append(
                    self._summarize_tool_use(
                        normalized_tool_use,
                        sequence_number=len(self.tool_sequence),
                    )
                )
                observed_tool_calls = len(self.tool_sequence)
                if observed_tool_calls > self.max_tool_calls:
                    self.limit_error = ToolCallLimitExceeded(
                        max_tool_calls=self.max_tool_calls,
                        observed_tool_calls=observed_tool_calls,
                        tool_sequence=self.tool_sequence,
                        last_attempted_tool=tool_name,
                        recent_tool_uses=self.tool_observations[-8:],
                    )
                    if self.cancel_callback is not None:
                        self.cancel_callback()
                    if self.raise_on_exceeded:
                        raise self.limit_error

        if self.delegate is not None:
            self.delegate(**kwargs)
