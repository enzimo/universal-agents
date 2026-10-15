"""Voice adapter boundary for gateway app integrations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

import aiohttp

from runtime_settings.voice import VoiceSettings


logger = logging.getLogger(__name__)

_CONVERSATION_HISTORY_MARKER_RE = re.compile(
    r"(?im)^(?:#{2,3}\s+(?:Recent Raw Turns|Turn\s+\d+|Current Request)|User Request:|Final Answer:)"
)
_FINAL_ANSWER_BLOCK_RE = re.compile(
    r"(?ims)(?:^|\n)Final Answer:\s*(.*?)(?=\n(?:#{2,3}\s+Turn\s+\d+|User Request:|Clarifications:|Execution History:|Turn Summary:|Artifacts:)|\Z)"
)
_TEXT_ONLY_ERROR_PREFIXES = (
    "Task failed:",
    "Task cancelled:",
    "Timed out after ",
    "Voice request failed:",
    "Voice reply failed:",
    "I could not transcribe",
)


@dataclass(frozen=True)
class TranscriptionRequest:
    audio: bytes
    filename: str = "audio.ogg"
    content_type: str = "audio/ogg"
    language: Optional[str] = None
    format_text: Optional[bool] = None
    audio_format: Optional[str] = None
    sample_rate: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.audio:
            raise ValueError("TranscriptionRequest.audio must not be empty.")


@dataclass(frozen=True)
class TranscriptionResponse:
    text: str
    language: Optional[str] = None
    duration: Optional[float] = None
    raw_response: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice: Optional[str] = None
    language: Optional[str] = None
    output_codec: Optional[str] = None
    sample_rate: Optional[int] = None
    bit_rate: Optional[int] = None

    def __post_init__(self) -> None:
        if not (self.text or "").strip():
            raise ValueError("SpeechRequest.text must not be empty.")


@dataclass(frozen=True)
class SpeechResponse:
    audio: bytes
    content_type: str


class VoiceClient(Protocol):
    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """Convert speech audio into text."""

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        """Convert text into speech audio."""


def speech_text_for_outbound_reply(text: str) -> tuple[str, bool]:
    """Return the text TTS should speak for one outbound manager reply.

    Manager replies should already contain only the current reply. This guard is
    intentionally narrow: it only trims when the text looks like copied durable
    conversation-context blocks, then speaks the last copied final-answer block.
    """

    normalized = (text or "").strip()
    if not normalized or not _CONVERSATION_HISTORY_MARKER_RE.search(normalized):
        return normalized, False

    final_answer_blocks = [match.group(1).strip() for match in _FINAL_ANSWER_BLOCK_RE.finditer(normalized)]
    final_answer_blocks = [block for block in final_answer_blocks if block]
    if len(final_answer_blocks) < 2:
        return normalized, False
    return final_answer_blocks[-1], final_answer_blocks[-1] != normalized


def text_for_voice_turn_history(*, request_text: str, reply_text: str) -> str:
    """Build the durable text companion for a voice-originated turn."""

    request = (request_text or "").strip()
    reply = (reply_text or "").strip()
    if not request:
        return reply
    if not reply:
        return f"Voice request:\n{request}"
    return f"Voice request:\n{request}\n\nAssistant reply:\n{reply}"


def should_synthesize_voice_reply(text: str) -> bool:
    """Return whether a voice-originated outbound reply should get TTS audio."""

    normalized = (text or "").strip()
    if not normalized:
        return False
    return not any(normalized.startswith(prefix) for prefix in _TEXT_ONLY_ERROR_PREFIXES)


SessionFactory = Callable[..., aiohttp.ClientSession]


class GrokVoiceClient:
    """Thin xAI STT/TTS adapter.

    The public request/response types are provider-neutral so a future LiteLLM
    proxy client can implement the same ``VoiceClient`` protocol.
    """

    def __init__(
        self,
        *,
        settings: VoiceSettings,
        api_key: Callable[[], Awaitable[str]],
        session_factory: Optional[SessionFactory] = None,
    ) -> None:
        self.settings = settings
        # Looked up for each request (the key saved for api.x.ai with /auth
        # model, or XAI_API_KEY), so a new key works without a restart.
        self._api_key = api_key
        self._session_factory = session_factory or aiohttp.ClientSession

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        if not self.settings.input_enabled:
            raise RuntimeError("Voice input is disabled by voice.input_enabled=false.")
        timeout = aiohttp.ClientTimeout(
            total=self.settings.request_timeout_seconds,
            connect=self.settings.connect_timeout_seconds,
        )
        async with self._session_factory(timeout=timeout) as session:
            form = aiohttp.FormData()
            form.add_field("format", "true" if self._resolve_format_text(request) else "false")
            language = request.language if request.language is not None else self.settings.stt_language
            if language:
                form.add_field("language", language)
            if request.audio_format:
                form.add_field("audio_format", request.audio_format)
            if request.sample_rate:
                form.add_field("sample_rate", str(request.sample_rate))
            form.add_field(
                "file",
                request.audio,
                filename=request.filename or "audio",
                content_type=request.content_type or "application/octet-stream",
            )
            async with session.post(
                self._url("stt"),
                headers=await self._auth_headers(),
                data=form,
            ) as response:
                await self._raise_for_status(response, operation="Grok STT")
                payload = await response.json()
        text = str(payload.get("text") or "").strip()
        return TranscriptionResponse(
            text=text,
            language=payload.get("language") if isinstance(payload.get("language"), str) else None,
            duration=float(payload["duration"]) if payload.get("duration") is not None else None,
            raw_response=payload,
        )

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        if not self.settings.output_enabled:
            raise RuntimeError("Voice output is disabled by voice.output_enabled=false.")
        timeout = aiohttp.ClientTimeout(
            total=self.settings.request_timeout_seconds,
            connect=self.settings.connect_timeout_seconds,
        )
        payload: dict[str, Any] = {
            "text": request.text,
            "voice_id": request.voice or self.settings.tts_voice,
            "language": request.language or self.settings.tts_language,
        }
        output_codec = request.output_codec or self.settings.tts_output_codec
        output_format: dict[str, Any] = {}
        if output_codec:
            output_format["codec"] = output_codec
        sample_rate = request.sample_rate or self.settings.tts_sample_rate
        if sample_rate:
            output_format["sample_rate"] = sample_rate
        bit_rate = request.bit_rate or self.settings.tts_bit_rate
        if bit_rate:
            output_format["bit_rate"] = bit_rate
        if output_format:
            payload["output_format"] = output_format

        async with self._session_factory(timeout=timeout) as session:
            async with session.post(
                self._url("tts"),
                headers={**(await self._auth_headers()), "Content-Type": "application/json"},
                json=payload,
            ) as response:
                await self._raise_for_status(response, operation="Grok TTS")
                audio = await response.read()
                content_type = response.headers.get(
                    "Content-Type",
                    self._default_content_type(output_codec),
                )
        return SpeechResponse(audio=audio, content_type=content_type)

    def _url(self, path: str) -> str:
        return f"{self.settings.api_base}/{path.lstrip('/')}"

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._api_key()}"}

    def _resolve_format_text(self, request: TranscriptionRequest) -> bool:
        if request.format_text is not None:
            return request.format_text
        return self.settings.stt_format_text

    @staticmethod
    async def _raise_for_status(response: aiohttp.ClientResponse, *, operation: str) -> None:
        if response.status < 400:
            return
        body = await response.text()
        raise RuntimeError(f"{operation} failed with HTTP {response.status}: {body}")

    @staticmethod
    def _default_content_type(codec: Optional[str]) -> str:
        normalized = str(codec or "").strip().lower()
        if normalized == "wav":
            return "audio/wav"
        if normalized == "pcm":
            return "audio/pcm"
        if normalized == "mulaw":
            return "audio/basic"
        if normalized == "alaw":
            return "audio/alaw"
        return "audio/mpeg"


def _xai_key_function(keys: Optional[Any]) -> Callable[[], Awaitable[str]]:
    from custom_agents.model_keys import process_model_keys
    from model_providers.endpoints import EndpointKind, well_known_endpoint

    endpoint = well_known_endpoint(EndpointKind.XAI)

    async def api_key() -> str:
        return (await (keys or process_model_keys()).resolve_async(endpoint)).sendable

    return api_key


class LiteLLMProxyVoiceClient:
    """Placeholder for the future LiteLLM proxy-backed implementation."""

    def __init__(self, *, settings: VoiceSettings) -> None:
        self.settings = settings

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        raise NotImplementedError(
            "voice.provider=litellm_proxy is reserved until the LiteLLM proxy exposes STT/TTS."
        )

    async def synthesize(self, request: SpeechRequest) -> SpeechResponse:
        raise NotImplementedError(
            "voice.provider=litellm_proxy is reserved until the LiteLLM proxy exposes STT/TTS."
        )


def build_voice_client(
    settings: VoiceSettings,
    *,
    keys: Optional[Any] = None,
) -> Optional[VoiceClient]:
    """Build the configured voice client, or return None when voice is disabled.

    Grok voice uses the xAI key (``XAI_API_KEY``, else the key saved for
    api.x.ai with ``/auth model``) through ``custom_agents.model_keys``;
    ``keys`` replaces this process's lookup in tests.
    """

    if not settings.enabled:
        return None
    if not settings.input_enabled and not settings.output_enabled:
        return None

    if settings.provider == "grok":
        return GrokVoiceClient(settings=settings, api_key=_xai_key_function(keys))
    if settings.provider == "litellm_proxy":
        return LiteLLMProxyVoiceClient(settings=settings)
    raise ValueError(f"Unsupported voice provider: {settings.provider}")
