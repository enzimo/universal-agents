"""Shared active-model profile contract for runtime switching and persistence."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from runtime_settings.model_request_options import (
    LITELLM_API_MODE_CHAT_COMPLETIONS,
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)


# How many tools one request to a model may carry (custom_agents.tool_loading).
# A fact about the model, set only in model_capabilities maps, never per agent
# type. OpenAI models refuse more than 128, so that is the default for a model
# without an entry.
MODEL_TOOL_LIMIT_FIELD = "max_tools_per_request"
DEFAULT_MAX_TOOLS_PER_REQUEST = 128

MODEL_CAPABILITY_FIELDS = (
    "supports_image_input",
    "supports_audio_input",
    "supports_file_input",
)
MODEL_TUNING_FIELDS = (
    "reasoning_effort",
    "reasoning_summary",
    "litellm_api_mode",
    "context_window_tokens",
)
MODEL_PROFILE_FIELDS = (
    "provider",
    "model_id",
    *MODEL_TUNING_FIELDS,
    *MODEL_CAPABILITY_FIELDS,
)
# Fields that describe how the runtime reaches a provider rather than one model.
# The LiteLLM API mode is chosen for the LiteLLM route (some models, such as
# gpt-6 with tools and reasoning, only work over /v1/responses), so a model-only
# switch keeps it; a provider change resets it because it applies only to LiteLLM.
PROVIDER_ROUTE_FIELDS = ("litellm_api_mode",)


# The agent's parts rebuilt when its model or endpoint changes; saved first and
# put back when a rebuild fails (apply_runtime_llm_profile, endpoint_refresh).
RUNTIME_LLM_REFERENCES = (
    "_strands_model",
    "_conversation_compactor",
    "_conversation_summary_generator",
    "_context_engine",
    "_strands_agent",
    "_strands_parts",
    "_strands_generation",
    "_context_debug_dump_plugin",
    "_skills_fingerprint",
)


class RuntimeLLMProfile(BaseModel):
    """Complete non-secret profile for one running model selection."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model_id: str
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    litellm_api_mode: str = LITELLM_API_MODE_CHAT_COMPLETIONS
    context_window_tokens: Optional[int] = None
    supports_image_input: Optional[bool] = None
    supports_audio_input: Optional[bool] = None
    supports_file_input: Optional[bool] = None

    @field_validator("provider", "model_id", mode="before")
    @classmethod
    def _normalize_required_strings(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> Optional[str]:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("reasoning_summary", mode="before")
    @classmethod
    def _normalize_reasoning_summary(cls, value: Any) -> Optional[str]:
        return normalize_reasoning_summary(value)

    @field_validator("litellm_api_mode", mode="before")
    @classmethod
    def _normalize_litellm_api_mode(cls, value: Any) -> str:
        return normalize_litellm_api_mode(value)

    @field_validator("context_window_tokens", mode="before")
    @classmethod
    def _normalize_context_window_tokens(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        parsed = int(value)
        if parsed < 1024:
            raise ValueError("context_window_tokens must be at least 1024 when provided")
        return parsed

    @field_validator("model_id")
    @classmethod
    def _require_model_id(cls, value: str) -> str:
        if not value:
            raise ValueError("model_id is required")
        return value

    @field_validator("provider")
    @classmethod
    def _require_provider(cls, value: str) -> str:
        if not value:
            raise ValueError("provider is required")
        return value

    def persistence_entry(self) -> dict[str, Any]:
        """Return the stable LLM_OVERRIDES.yaml entry shape."""
        return self.model_dump(mode="python")

    def capability_kwargs(self) -> dict[str, Optional[bool]]:
        return {
            field: getattr(self, field)
            for field in MODEL_CAPABILITY_FIELDS
        }


def runtime_llm_profile_from_agent(
    agent: Any,
    *,
    overrides: Mapping[str, Any] | None = None,
    reset_model_specific_fields: bool = False,
) -> RuntimeLLMProfile:
    """Resolve a validated candidate profile from live state plus staged overrides."""

    staged = dict(overrides or {})
    model_changed = any(field in staged for field in ("provider", "model_id"))
    current_provider = getattr(agent, "model_provider", None)
    reset_fields = (
        stale_model_fields(
            previous_provider=current_provider,
            candidate_provider=staged.get("provider", current_provider),
        )
        if reset_model_specific_fields and model_changed
        else ()
    )
    values: dict[str, Any] = {
        "provider": current_provider,
        "model_id": getattr(agent, "model_id", None),
    }
    for field in (*MODEL_TUNING_FIELDS, *MODEL_CAPABILITY_FIELDS):
        values[field] = None if field in reset_fields else getattr(agent, field, None)
    values.update(staged)
    return RuntimeLLMProfile.model_validate(values)


def stale_model_fields(*, previous_provider: Any, candidate_provider: Any) -> tuple[str, ...]:
    """Return the profile fields a model switch must reset unless explicitly staged.

    Reasoning, context, and capability fields belong to one model and are always
    reset so stale values never carry over. Provider route fields are kept while
    the provider stays the same.
    """

    provider_changed = _provider_key(previous_provider) != _provider_key(candidate_provider)
    return tuple(
        field
        for field in (*MODEL_TUNING_FIELDS, *MODEL_CAPABILITY_FIELDS)
        if provider_changed or field not in PROVIDER_ROUTE_FIELDS
    )


def _provider_key(provider: Any) -> str:
    return str(provider or "").strip().lower()
