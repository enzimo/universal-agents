"""Models configuration and contracts."""

from __future__ import annotations
import copy
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from runtime_settings.model_request_options import (
    is_reasoning_summary_responses_transport,
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)
from default_values import EMBEDDING_DEFAULTS, LLM_DEFAULTS
from runtime_settings.common import _merge_missing_defaults, _normalized_optional_string


class OpenAICodexModelDiscoverySettings(BaseModel):
    enabled: bool = True
    cache_path: Optional[str] = None
    cache_ttl_seconds: int = 3600
    client_version: str = "1.0.0"

    @field_validator("cache_path", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("client_version", mode="before")
    @classmethod
    def _normalize_client_version(cls, value: Any) -> str:
        return _normalized_optional_string(value) or "1.0.0"

    @field_validator("cache_ttl_seconds", mode="before")
    @classmethod
    def _normalize_cache_ttl_seconds(cls, value: Any) -> int:
        if value in (None, ""):
            return 3600
        return max(0, int(value))


class OpenAICodexSettings(BaseModel):
    base_url: str = LLM_DEFAULTS.openai_codex.value["base_url"]
    auth_store_path: Optional[str] = None
    shared_codex_home: Optional[str] = None
    allow_shared_codex_auth_read: bool = True
    refresh_skew_seconds: int = 120
    refresh_timeout_seconds: float = 20.0
    model_discovery: OpenAICodexModelDiscoverySettings = Field(
        default_factory=OpenAICodexModelDiscoverySettings
    )

    @field_validator("base_url", "auth_store_path", "shared_codex_home", mode="before")
    @classmethod
    def _normalize_strings(cls, value: Any, info: Any) -> Optional[str]:
        normalized = _normalized_optional_string(value)
        if info.field_name == "base_url":
            return normalized or LLM_DEFAULTS.openai_codex.value["base_url"]
        return normalized

    @field_validator("refresh_skew_seconds", mode="before")
    @classmethod
    def _normalize_refresh_skew_seconds(cls, value: Any) -> int:
        if value in (None, ""):
            return 120
        return max(0, int(value))

    @field_validator("refresh_timeout_seconds", mode="before")
    @classmethod
    def _normalize_refresh_timeout_seconds(cls, value: Any) -> float:
        if value in (None, ""):
            return 20.0
        return max(5.0, float(value))


class XAIOAuthSettings(BaseModel):
    base_url: str = LLM_DEFAULTS.xai_oauth.value["base_url"]
    auth_store_path: Optional[str] = None
    refresh_skew_seconds: int = LLM_DEFAULTS.xai_oauth.value["refresh_skew_seconds"]
    refresh_timeout_seconds: float = LLM_DEFAULTS.xai_oauth.value[
        "refresh_timeout_seconds"
    ]

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, value: Any) -> str:
        normalized = (
            _normalized_optional_string(value)
            or LLM_DEFAULTS.xai_oauth.value["base_url"]
        )
        normalized = normalized.rstrip("/")
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or (host != "x.ai" and not host.endswith(".x.ai"))
        ):
            raise ValueError(
                "xai_oauth.base_url must use HTTPS on x.ai or an x.ai subdomain."
            )
        if parsed.username or parsed.password:
            raise ValueError(
                "xai_oauth.base_url must not contain embedded credentials."
            )
        return normalized

    @field_validator("auth_store_path", mode="before")
    @classmethod
    def _normalize_auth_store_path(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("refresh_skew_seconds", mode="before")
    @classmethod
    def _normalize_refresh_skew_seconds(cls, value: Any) -> int:
        if value in (None, ""):
            return int(LLM_DEFAULTS.xai_oauth.value["refresh_skew_seconds"])
        return max(0, int(value))

    @field_validator("refresh_timeout_seconds", mode="before")
    @classmethod
    def _normalize_refresh_timeout_seconds(cls, value: Any) -> float:
        if value in (None, ""):
            return float(LLM_DEFAULTS.xai_oauth.value["refresh_timeout_seconds"])
        return max(5.0, float(value))


class AgentTypeLLMOverrideSettings(BaseModel):
    provider: Optional[str] = None
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    context_window_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    stream: Optional[bool] = None
    litellm_api_mode: Optional[str] = None
    request_timeout_seconds: Optional[float] = None
    stream_idle_timeout_seconds: Optional[float] = None
    model_call_timeout_seconds: Optional[float] = None
    supports_image_input: Optional[bool] = None
    supports_audio_input: Optional[bool] = None
    supports_file_input: Optional[bool] = None
    model_capabilities: Optional[Dict[str, Any]] = None
    reasoning_trace_retention_messages: Optional[int] = None
    litellm_api_base: Optional[str] = None
    openai_codex: Optional[OpenAICodexSettings] = None
    xai_oauth: Optional[XAIOAuthSettings] = None
    compaction_model_provider: Optional[str] = None
    compaction_model_id: Optional[str] = None
    failover: Optional[Dict[str, Any]] = None

    @field_validator(
        "provider",
        "model_id",
        "reasoning_effort",
        "litellm_api_base",
        "compaction_model_provider",
        "compaction_model_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("reasoning_summary", mode="before")
    @classmethod
    def _normalize_reasoning_summary(cls, value: Any) -> Optional[str]:
        return normalize_reasoning_summary(value)

    @field_validator("litellm_api_mode", mode="before")
    @classmethod
    def _normalize_litellm_api_mode(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return normalize_litellm_api_mode(value)

    @field_validator("failover", mode="before")
    @classmethod
    def _merge_failover_defaults(cls, value: Any) -> Any:
        if value is None:
            return None
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(LLM_DEFAULTS.failover.value),
        )

    @field_validator("reasoning_trace_retention_messages")
    @classmethod
    def _validate_reasoning_trace_retention_messages(
        cls, value: Optional[int]
    ) -> Optional[int]:
        if value is not None and value < 1:
            raise ValueError(
                "reasoning_trace_retention_messages must be >= 1 when provided."
            )
        return value

    @field_validator(
        "request_timeout_seconds",
        "stream_idle_timeout_seconds",
        "model_call_timeout_seconds",
    )
    @classmethod
    def _validate_positive_timeouts(
        cls, value: Optional[float], info: ValidationInfo
    ) -> Optional[float]:
        if value is not None and value <= 0:
            raise ValueError(
                f"{info.field_name} must be greater than 0 when provided."
            )
        return value


class InstanceEmbeddingSettings(BaseModel):
    provider: Optional[str] = EMBEDDING_DEFAULTS.provider.value
    model_id: Optional[str] = EMBEDDING_DEFAULTS.model_id.value
    api_base: Optional[str] = EMBEDDING_DEFAULTS.api_base.value
    dimensions: Optional[int] = None
    timeout_seconds: Optional[float] = EMBEDDING_DEFAULTS.timeout_seconds.value
    organization: Optional[str] = EMBEDDING_DEFAULTS.organization.value
    extra_headers: Dict[str, str] = Field(
        default_factory=lambda: copy.deepcopy(EMBEDDING_DEFAULTS.extra_headers.value)
    )

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: Any) -> Optional[str]:
        normalized = _normalized_optional_string(value)
        if not normalized:
            return None
        lowered = normalized.lower()
        if lowered in {"openai-compatible", "openai_compatible", "openai"}:
            return "openai_compatible"
        if lowered in {"local", "deterministic_local"}:
            return "local"
        raise ValueError("embedding.provider must be 'local' or 'openai_compatible'.")

    @field_validator("model_id", "api_base", "organization", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("dimensions", mode="before")
    @classmethod
    def _normalize_dimensions(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        return int(value)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _normalize_timeout_seconds(cls, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        return float(value)


class InstanceLLMSettings(BaseModel):
    provider: Optional[str] = LLM_DEFAULTS.provider.value
    model_id: Optional[str] = LLM_DEFAULTS.model_id.value
    temperature: Optional[float] = LLM_DEFAULTS.temperature.value
    reasoning_effort: Optional[str] = LLM_DEFAULTS.reasoning_effort.value
    reasoning_summary: Optional[str] = LLM_DEFAULTS.reasoning_summary.value
    context_window_tokens: Optional[int] = LLM_DEFAULTS.context_window_tokens.value
    max_output_tokens: Optional[int] = LLM_DEFAULTS.max_output_tokens.value
    stream: Optional[bool] = LLM_DEFAULTS.stream.value
    litellm_api_mode: str = LLM_DEFAULTS.litellm_api_mode.value
    request_timeout_seconds: float = LLM_DEFAULTS.request_timeout_seconds.value
    stream_idle_timeout_seconds: float = LLM_DEFAULTS.stream_idle_timeout_seconds.value
    model_call_timeout_seconds: float = LLM_DEFAULTS.model_call_timeout_seconds.value
    supports_image_input: Optional[bool] = None
    supports_audio_input: Optional[bool] = None
    supports_file_input: Optional[bool] = None
    model_capabilities: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.model_capabilities.value)
    )
    reasoning_trace_retention_messages: Optional[int] = (
        LLM_DEFAULTS.reasoning_trace_retention_messages.value
    )
    litellm_api_base: Optional[str] = LLM_DEFAULTS.litellm_api_base.value
    openai_codex: OpenAICodexSettings = Field(default_factory=OpenAICodexSettings)
    xai_oauth: XAIOAuthSettings = Field(default_factory=XAIOAuthSettings)
    compaction_model_provider: Optional[str] = None
    compaction_model_id: Optional[str] = None
    failover: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.failover.value)
    )

    @field_validator(
        "provider",
        "model_id",
        "reasoning_effort",
        "litellm_api_base",
        "compaction_model_provider",
        "compaction_model_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("reasoning_summary", mode="before")
    @classmethod
    def _normalize_reasoning_summary(cls, value: Any) -> Optional[str]:
        return normalize_reasoning_summary(value)

    @field_validator("litellm_api_mode", mode="before")
    @classmethod
    def _normalize_litellm_api_mode(cls, value: Any) -> str:
        return normalize_litellm_api_mode(value)

    @field_validator("failover", mode="before")
    @classmethod
    def _merge_failover_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(LLM_DEFAULTS.failover.value),
        )

    @field_validator("temperature", mode="before")
    @classmethod
    def _normalize_temperature(cls, value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        return float(value)

    @field_validator(
        "context_window_tokens",
        "max_output_tokens",
        "reasoning_trace_retention_messages",
        mode="before",
    )
    @classmethod
    def _normalize_optional_ints(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        return int(value)

    @field_validator("reasoning_trace_retention_messages")
    @classmethod
    def _validate_reasoning_trace_retention_messages(
        cls, value: Optional[int]
    ) -> Optional[int]:
        if value is not None and value < 1:
            raise ValueError(
                "reasoning_trace_retention_messages must be >= 1 when provided."
            )
        return value

    @field_validator(
        "request_timeout_seconds",
        "stream_idle_timeout_seconds",
        "model_call_timeout_seconds",
        mode="before",
    )
    @classmethod
    def _normalize_positive_timeouts(cls, value: Any, info: ValidationInfo) -> float:
        if value in (None, ""):
            return float(getattr(LLM_DEFAULTS, str(info.field_name)).value)
        normalized = float(value)
        if normalized <= 0:
            raise ValueError(
                f"{info.field_name} must be greater than 0 when provided."
            )
        return normalized

    @model_validator(mode="after")
    def _validate_reasoning_summary_transport(self) -> "InstanceLLMSettings":
        provider = str(self.provider or "").strip().lower()
        responses_transport = is_reasoning_summary_responses_transport(
            provider,
            self.litellm_api_mode,
        )
        if self.reasoning_summary and not responses_transport:
            raise ValueError(
                "reasoning_summary requires provider=openai, provider=openai-codex, "
                "or provider=litellm with litellm_api_mode=responses."
            )
        if (
            provider == "litellm"
            and self.litellm_api_mode == "responses"
            and self.stream is False
        ):
            raise ValueError(
                "stream=false is incompatible with litellm_api_mode=responses."
            )
        return self
