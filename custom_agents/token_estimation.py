"""Shared token-counting helpers for context-budget decisions."""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
from collections.abc import Callable
from typing import Any


def estimate_tokens(text: Any) -> int:
    """Best-effort local estimate for utility paths that do not have a Strands model."""

    rendered = str(text or "")
    if not rendered:
        return 0
    return max(1, int(math.ceil(len(rendered) / 4)))


def count_text_tokens(model: Any, text: Any) -> int:
    """Count a text block with the active Strands model."""

    rendered = str(text or "")
    if not rendered:
        return 0
    return count_message_tokens(model, [{"role": "user", "content": [{"text": rendered}]}])


def count_message_tokens(
    model: Any,
    messages: list[dict[str, Any]],
    *,
    tool_specs: list[Any] | None = None,
    system_prompt: str | None = None,
    system_prompt_content: list[Any] | None = None,
) -> int:
    """Count tokens by delegating to Strands model.count_tokens()."""

    counter = getattr(model, "count_tokens", None)
    if not callable(counter):
        raise RuntimeError("Strands model does not expose count_tokens(); install strands-agents>=1.36.0.")
    try:
        value = counter(
            messages=messages,
            tool_specs=tool_specs,
            system_prompt=system_prompt,
            system_prompt_content=system_prompt_content,
        )
    except TypeError:
        value = counter(messages)
    return max(0, int(_resolve_count_tokens_result(value) or 0))


def truncate_to_token_budget(
    text: str,
    *,
    max_tokens: int,
    estimate: Callable[[str], int],
    from_tail: bool = False,
) -> str:
    """Return a prefix or suffix that fits the estimated token budget."""

    cleaned = str(text or "")
    if max_tokens <= 0 or not cleaned:
        return ""
    if estimate(cleaned) <= max_tokens:
        return cleaned

    low = 0
    high = len(cleaned)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = cleaned[-mid:] if from_tail else cleaned[:mid]
        if estimate(candidate) <= max_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _resolve_count_tokens_result(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(value)
        except Exception as exc:  # pragma: no cover - re-raised in caller thread
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")
