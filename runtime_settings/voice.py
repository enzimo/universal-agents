"""Voice configuration and contracts."""

from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, field_validator
from default_values import VOICE_DEFAULTS
from runtime_settings.common import _normalized_optional_string


class VoiceSettings(BaseModel):
    enabled: bool = VOICE_DEFAULTS.enabled.value
    input_enabled: bool = VOICE_DEFAULTS.input_enabled.value
    output_enabled: bool = VOICE_DEFAULTS.output_enabled.value
    response_mode: str = VOICE_DEFAULTS.response_mode.value
    provider: str = VOICE_DEFAULTS.provider.value
    api_base: str = VOICE_DEFAULTS.api_base.value
    stt_language: Optional[str] = VOICE_DEFAULTS.stt_language.value
    stt_format_text: bool = VOICE_DEFAULTS.stt_format_text.value
    tts_voice: str = VOICE_DEFAULTS.tts_voice.value
    tts_language: str = VOICE_DEFAULTS.tts_language.value
    tts_output_codec: str = VOICE_DEFAULTS.tts_output_codec.value
    tts_sample_rate: int = VOICE_DEFAULTS.tts_sample_rate.value
    tts_bit_rate: Optional[int] = VOICE_DEFAULTS.tts_bit_rate.value
    connect_timeout_seconds: float = VOICE_DEFAULTS.connect_timeout_seconds.value
    request_timeout_seconds: float = VOICE_DEFAULTS.request_timeout_seconds.value

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: Any) -> str:
        normalized = str(value or VOICE_DEFAULTS.provider.value).strip().lower()
        if normalized not in {"grok", "litellm_proxy"}:
            raise ValueError("voice.provider must be 'grok' or 'litellm_proxy'.")
        return normalized

    @field_validator("response_mode", mode="before")
    @classmethod
    def _validate_response_mode(cls, value: Any) -> str:
        normalized = str(value or VOICE_DEFAULTS.response_mode.value).strip().lower()
        if normalized not in {"voice_only", "voice_and_text", "text_only"}:
            raise ValueError(
                "voice.response_mode must be 'voice_only', 'voice_and_text', or 'text_only'."
            )
        return normalized

    @field_validator("api_base", mode="before")
    @classmethod
    def _normalize_api_base(cls, value: Any) -> str:
        normalized = str(value or "").strip().rstrip("/")
        return normalized or VOICE_DEFAULTS.api_base.value

    @field_validator("stt_language", mode="before")
    @classmethod
    def _normalize_optional_string_setting(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("tts_voice", "tts_language", "tts_output_codec", mode="before")
    @classmethod
    def _normalize_required_string(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "connect_timeout_seconds", "request_timeout_seconds", mode="before"
    )
    @classmethod
    def _normalize_timeout(cls, value: Any) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise ValueError("voice timeouts must be greater than zero.")
        return parsed

    @field_validator("tts_sample_rate", mode="before")
    @classmethod
    def _validate_tts_sample_rate(cls, value: Any) -> int:
        parsed = int(value)
        if parsed not in {8000, 16000, 22050, 24000, 44100, 48000}:
            raise ValueError(
                "voice.tts_sample_rate must be one of 8000, 16000, 22050, 24000, 44100, or 48000."
            )
        return parsed

    @field_validator("tts_bit_rate", mode="before")
    @classmethod
    def _normalize_optional_int(cls, value: Any) -> Optional[int]:
        if value is None or str(value).strip() == "":
            return None
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("voice.tts_bit_rate must be greater than zero when set.")
        return parsed

