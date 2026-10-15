from __future__ import annotations

import json

import pytest
import requests

from custom_agents.embedding_service import (
    EmbeddingServiceClient,
    EmbeddingServiceRequestError,
    create_embedding_service_settings,
)


def test_embedding_service_client_openai_compatible_provider_calls_service(monkeypatch):
    captured = {}

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    def _fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("custom_agents.embedding_service.requests.post", _fake_post)

    client = EmbeddingServiceClient(
        create_embedding_service_settings(
            {
                "provider": "openai_compatible",
                "model_id": "text-embedding-3-large",
                "api_base": "https://embeddings.example.com/v1",
                "dimensions": 3,
                "timeout_seconds": 12,
                "organization": "org_test",
                "extra_headers": {"X-Test": "1"},
            }
        ),
        key=_Key("secret"),
    )
    embedding = client.embed_text("Use the configured embedding service.")

    assert captured["url"] == "https://embeddings.example.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["OpenAI-Organization"] == "org_test"
    assert captured["headers"]["X-Test"] == "1"
    assert captured["json"]["model"] == "text-embedding-3-large"
    assert captured["json"]["input"] == "Use the configured embedding service."
    assert captured["json"]["dimensions"] == 3
    assert captured["timeout"] == 12
    assert json.loads(embedding) == [0.1, 0.2, 0.3]


def test_embedding_service_client_openai_compatible_includes_upstream_error_body(monkeypatch):
    class _FakeResponse:
        status_code = 400
        url = "https://litellm.example/v1/embeddings"

        def raise_for_status(self):
            raise requests.HTTPError("400 Client Error")

        def json(self):
            return {
                "error": {
                    "message": "model does not support embeddings",
                    "type": "bad_request",
                }
            }

    def _fake_post(url, headers, json, timeout):
        return _FakeResponse()

    monkeypatch.setattr("custom_agents.embedding_service.requests.post", _fake_post)

    client = EmbeddingServiceClient(
        create_embedding_service_settings(
            {
                "provider": "openai_compatible",
                "model_id": "chat-only-model",
                "api_base": "https://litellm.example/v1",
            }
        )
    )

    with pytest.raises(EmbeddingServiceRequestError) as exc_info:
        client.embed_text("Find top restaurants.")

    error_text = str(exc_info.value)
    assert "HTTP 400" in error_text
    assert "chat-only-model" in error_text
    assert "model does not support embeddings" in error_text


def test_embedding_service_client_rejects_non_finite_embedding_values(monkeypatch):
    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, float("nan")]}]}

    def _fake_post(url, headers, json, timeout):
        return _FakeResponse()

    monkeypatch.setattr("custom_agents.embedding_service.requests.post", _fake_post)

    client = EmbeddingServiceClient(
        create_embedding_service_settings(
            {
                "provider": "openai_compatible",
                "model_id": "nan-model",
                "api_base": "https://litellm.example/v1",
            }
        )
    )

    with pytest.raises(ValueError, match="non-finite"):
        client.embed_text("Reject non-finite embeddings.")


class _Key:
    """The embedding endpoint's key (custom_agents.model_keys.EndpointKey) as a test double."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.rejections: list[int | None] = []

    def value(self) -> str:
        return self._value

    def rejected(self, status_code):
        self.rejections.append(status_code)
        return RuntimeError("The model provider at embeddings.example.com rejected the team's key.")


def test_embedding_service_client_reports_a_rejected_key(monkeypatch):
    class _FakeResponse:
        status_code = 401

    monkeypatch.setattr("custom_agents.embedding_service.requests.post", lambda url, headers, json, timeout: _FakeResponse())
    key = _Key("bad")
    client = EmbeddingServiceClient(
        create_embedding_service_settings(
            {"provider": "openai_compatible", "model_id": "embed", "api_base": "https://embeddings.example.com/v1"}
        ),
        key=key,
    )

    with pytest.raises(RuntimeError, match="rejected the team's key"):
        client.embed_text("x")
    assert key.rejections == [401]
