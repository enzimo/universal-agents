from __future__ import annotations

from typing import Any

import pytest

from runtime_settings.voice import VoiceSettings
from assistant_gateway.voice import (
    GrokVoiceClient,
    SpeechRequest,
    TranscriptionRequest,
    build_voice_client,
    should_synthesize_voice_reply,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        json_payload: dict[str, Any] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status = status
        self._json_payload = json_payload or {}
        self._body = body
        self.headers = headers or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self):
        return self._json_payload

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._text


class _FakeSession:
    next_responses: list[_FakeResponse] = []
    instances: list["_FakeSession"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.posts: list[dict[str, Any]] = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return self.__class__.next_responses.pop(0)

    @classmethod
    def clear(cls) -> None:
        cls.next_responses.clear()
        cls.instances.clear()


async def _xai_key() -> str:
    return "xai-key"


@pytest.mark.asyncio
async def test_grok_voice_client_transcribes_with_xai_stt() -> None:
    _FakeSession.clear()
    _FakeSession.next_responses.append(
        _FakeResponse(
            json_payload={"text": "hello agent", "language": "English", "duration": 1.25},
        )
    )
    settings = VoiceSettings(request_timeout_seconds=5.0)
    client = GrokVoiceClient(
        settings=settings,
        api_key=_xai_key,
        session_factory=_FakeSession,
    )

    response = await client.transcribe(
        TranscriptionRequest(
            audio=b"ogg-bytes",
            filename="voice.ogg",
            content_type="audio/ogg",
        )
    )

    session = _FakeSession.instances[0]
    post = session.posts[0]
    assert post["url"] == "https://api.x.ai/v1/stt"
    assert post["headers"] == {"Authorization": "Bearer xai-key"}
    assert post["data"] is not None
    assert response.text == "hello agent"
    assert response.language == "English"
    assert response.duration == 1.25


@pytest.mark.asyncio
async def test_grok_voice_client_synthesizes_with_xai_tts() -> None:
    _FakeSession.clear()
    _FakeSession.next_responses.append(
        _FakeResponse(
            body=b"mp3-bytes",
            headers={"Content-Type": "audio/mpeg"},
        )
    )
    settings = VoiceSettings(request_timeout_seconds=5.0, tts_voice="ara", output_enabled=True)
    client = GrokVoiceClient(
        settings=settings,
        api_key=_xai_key,
        session_factory=_FakeSession,
    )

    response = await client.synthesize(SpeechRequest(text="Reply from the agent."))

    session = _FakeSession.instances[0]
    post = session.posts[0]
    assert post["url"] == "https://api.x.ai/v1/tts"
    assert post["headers"] == {
        "Authorization": "Bearer xai-key",
        "Content-Type": "application/json",
    }
    assert post["json"] == {
        "text": "Reply from the agent.",
        "voice_id": "ara",
        "language": "auto",
        "output_format": {
            "codec": "mp3",
            "sample_rate": 24000,
            "bit_rate": 128000,
        },
    }
    assert response.audio == b"mp3-bytes"
    assert response.content_type == "audio/mpeg"


@pytest.mark.asyncio
async def test_grok_voice_client_raises_http_errors() -> None:
    _FakeSession.clear()
    _FakeSession.next_responses.append(_FakeResponse(status=401, text="bad key"))
    client = GrokVoiceClient(
        settings=VoiceSettings(request_timeout_seconds=5.0),
        api_key=_xai_key,
        session_factory=_FakeSession,
    )

    with pytest.raises(RuntimeError, match="Grok STT failed with HTTP 401"):
        await client.transcribe(TranscriptionRequest(audio=b"audio"))


@pytest.mark.asyncio
async def test_grok_voice_uses_the_xai_key_from_the_key_lookup() -> None:
    from custom_agents.model_keys import ModelKeyMissingError, ModelKeys
    from tests.model_key_support import env_model_keys

    with_key = build_voice_client(VoiceSettings(enabled=True), keys=env_model_keys("xai-env-key", env_var="XAI_API_KEY"))
    without = build_voice_client(VoiceSettings(enabled=True), keys=ModelKeys(None, env_lookup={}.get))

    assert await with_key._auth_headers() == {"Authorization": "Bearer xai-env-key"}
    with pytest.raises(ModelKeyMissingError, match="api.x.ai"):
        await without._auth_headers()


def test_should_synthesize_voice_reply_rejects_error_text() -> None:
    assert should_synthesize_voice_reply("Manager reply") is True
    assert should_synthesize_voice_reply("Task failed:\n- boom") is False
    assert should_synthesize_voice_reply("Voice request failed: bad audio") is False


@pytest.mark.asyncio
async def test_voice_client_rejects_disabled_input() -> None:
    client = GrokVoiceClient(
        settings=VoiceSettings(enabled=True, input_enabled=False),
        api_key=_xai_key,
        session_factory=_FakeSession,
    )

    with pytest.raises(RuntimeError, match="Voice input is disabled"):
        await client.transcribe(TranscriptionRequest(audio=b"audio"))


@pytest.mark.asyncio
async def test_voice_client_rejects_disabled_output() -> None:
    client = GrokVoiceClient(
        settings=VoiceSettings(enabled=True, output_enabled=False),
        api_key=_xai_key,
        session_factory=_FakeSession,
    )

    with pytest.raises(RuntimeError, match="Voice output is disabled"):
        await client.synthesize(SpeechRequest(text="hello"))


@pytest.mark.asyncio
async def test_build_voice_client_reserves_litellm_proxy_provider() -> None:
    client = build_voice_client(VoiceSettings(enabled=True, provider="litellm_proxy"))
    assert client is not None

    with pytest.raises(NotImplementedError, match="litellm_proxy"):
        await client.transcribe(TranscriptionRequest(audio=b"audio"))
