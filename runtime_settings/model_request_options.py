"""Validated request-transport options shared across runtime configuration paths."""

from __future__ import annotations

from typing import Any, Optional


LITELLM_API_MODE_CHAT_COMPLETIONS = "chat_completions"
LITELLM_API_MODE_RESPONSES = "responses"
LITELLM_API_MODES = frozenset(
    {
        LITELLM_API_MODE_CHAT_COMPLETIONS,
        LITELLM_API_MODE_RESPONSES,
    }
)
REASONING_SUMMARY_NATIVE_RESPONSES_PROVIDERS = frozenset(
    {
        "openai",
        "openai-codex",
    }
)

REASONING_SUMMARY_AUTO = "auto"
REASONING_SUMMARY_CONCISE = "concise"
REASONING_SUMMARY_DETAILED = "detailed"
REASONING_SUMMARY_VALUES = frozenset(
    {
        REASONING_SUMMARY_AUTO,
        REASONING_SUMMARY_CONCISE,
        REASONING_SUMMARY_DETAILED,
    }
)


def normalize_litellm_api_mode(value: Any) -> str:
    """Return a canonical LiteLLM transport mode or raise for invalid config."""

    normalized = str(value or LITELLM_API_MODE_CHAT_COMPLETIONS).strip().lower()
    if normalized not in LITELLM_API_MODES:
        allowed = ", ".join(sorted(LITELLM_API_MODES))
        raise ValueError(f"litellm_api_mode must be one of: {allowed}.")
    return normalized


def normalize_reasoning_summary(value: Any) -> Optional[str]:
    """Return a canonical OpenAI reasoning-summary request value."""

    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized not in REASONING_SUMMARY_VALUES:
        allowed = ", ".join(sorted(REASONING_SUMMARY_VALUES))
        raise ValueError(f"reasoning_summary must be null or one of: {allowed}.")
    return normalized


def is_reasoning_summary_responses_transport(
    provider: Any,
    litellm_api_mode: Any = None,
) -> bool:
    """Return whether a canonical provider route can carry reasoning.summary."""

    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider in REASONING_SUMMARY_NATIVE_RESPONSES_PROVIDERS:
        return True
    return (
        normalized_provider == "litellm"
        and normalize_litellm_api_mode(litellm_api_mode)
        == LITELLM_API_MODE_RESPONSES
    )
