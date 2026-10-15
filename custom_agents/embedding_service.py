"""Embedding service abstraction for graph ingestion and retrieval."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol
from urllib.parse import urlparse

import requests

from default_values import EMBEDDING_DEFAULTS

DEFAULT_LOCAL_EMBEDDING_DIMENSIONS = 48
MAX_EMBEDDING_ERROR_BODY_CHARS = 2000


@dataclass(frozen=True)
class EmbeddingServiceSettings:
    provider: str = EMBEDDING_DEFAULTS.provider.value
    model_id: Optional[str] = EMBEDDING_DEFAULTS.model_id.value
    api_base: Optional[str] = EMBEDDING_DEFAULTS.api_base.value
    dimensions: Optional[int] = None
    timeout_seconds: float = EMBEDDING_DEFAULTS.timeout_seconds.value
    organization: Optional[str] = EMBEDDING_DEFAULTS.organization.value
    extra_headers: Optional[Dict[str, str]] = field(
        default_factory=lambda: dict(EMBEDDING_DEFAULTS.extra_headers.value)
    )

    @property
    def normalized_provider(self) -> str:
        raw = str(self.provider or EMBEDDING_DEFAULTS.provider.value).strip().lower()
        if raw in {"openai-compatible", "openai_compatible", "openai"}:
            return "openai_compatible"
        if raw in {"local", "deterministic_local"}:
            return "local"
        return raw


class EmbeddingKey(Protocol):
    """The embedding endpoint's key (``custom_agents.model_keys.EndpointKey``)."""

    def value(self) -> str: ...

    def rejected(self, status_code: int | None) -> Exception: ...


class EmbeddingServiceClient:
    """Build embeddings either locally or via a configured service.

    A service-backed client asks ``key`` for the endpoint's key on every
    request, so a key saved with ``/auth model`` is used without a restart.
    """

    def __init__(
        self,
        settings: EmbeddingServiceSettings,
        *,
        key: EmbeddingKey | None = None,
        logger: Optional[logging.Logger] = None,
    ):
        self._settings = settings
        self._key = key
        self._logger = logger or logging.getLogger(__name__)
        provider = settings.normalized_provider
        if provider == "openai_compatible":
            if not settings.api_base:
                raise ValueError("embedding.api_base is required when embedding.provider=openai_compatible")
            if not settings.model_id:
                raise ValueError("embedding.model_id is required when embedding.provider=openai_compatible")
            parsed_api_base = urlparse(str(settings.api_base or ""))
            if parsed_api_base.path in {"", "/"}:
                self._logger.warning(
                    "embedding.api_base=%r has no path component. Many OpenAI-compatible embedding services, "
                    "including LiteLLM proxies, expect a base URL ending in /v1.",
                    settings.api_base,
                )
        elif provider != "local":
            raise ValueError(f"Unsupported embedding provider {settings.provider!r}.")

    @property
    def settings(self) -> EmbeddingServiceSettings:
        return self._settings

    def embed_text(self, text: str) -> str:
        provider = self._settings.normalized_provider
        if provider == "local":
            return _build_local_embedding(
                text,
                dimensions=self._settings.dimensions or DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
            )
        return self._embed_via_openai_compatible(text)

    def _embed_via_openai_compatible(self, text: str) -> str:
        api_base = str(self._settings.api_base or "").rstrip("/")
        url = f"{api_base}/embeddings"
        headers = {
            "Content-Type": "application/json",
        }
        key = self._key.value() if self._key is not None else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if self._settings.organization:
            headers["OpenAI-Organization"] = self._settings.organization
        for key, value in dict(self._settings.extra_headers or {}).items():
            headers[str(key)] = str(value)

        payload: Dict[str, Any] = {
            "model": self._settings.model_id,
            "input": text,
        }
        if self._settings.dimensions is not None and self._settings.dimensions > 0:
            payload["dimensions"] = int(self._settings.dimensions)

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=max(1.0, float(self._settings.timeout_seconds or EMBEDDING_DEFAULTS.timeout_seconds.value)),
        )
        if response.status_code == 401 and self._key is not None:
            raise self._key.rejected(401)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise EmbeddingServiceRequestError.from_response(
                response=response,
                model_id=self._settings.model_id,
            ) from exc
        body = response.json()
        data = list(body.get("data") or [])
        if not data or not isinstance(data[0], dict) or "embedding" not in data[0]:
            raise ValueError("Embedding service response did not include data[0].embedding.")
        embedding = data[0]["embedding"]
        if not isinstance(embedding, list):
            raise ValueError("Embedding service response returned a non-list embedding.")
        values = []
        for value in embedding:
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("Embedding service response contained a non-finite embedding value.")
            values.append(parsed)
        return json.dumps(values, allow_nan=False)


class EmbeddingServiceRequestError(RuntimeError):
    """HTTP failure from an OpenAI-compatible embedding service with safe diagnostics."""

    def __init__(self, *, status_code: int, url: str, model_id: Optional[str], body_preview: str):
        self.status_code = status_code
        self.url = url
        self.model_id = model_id
        self.body_preview = body_preview
        detail = f"Embedding service request failed with HTTP {status_code} for model {model_id!r} at {url}."
        if body_preview:
            detail = f"{detail} Upstream response: {body_preview}"
        super().__init__(detail)

    @classmethod
    def from_response(cls, *, response: requests.Response, model_id: Optional[str]) -> "EmbeddingServiceRequestError":
        body_preview = _safe_response_body_preview(response)
        return cls(
            status_code=int(getattr(response, "status_code", 0) or 0),
            url=str(getattr(response, "url", "") or ""),
            model_id=model_id,
            body_preview=body_preview,
        )


def create_embedding_service_settings(raw: Optional[Dict[str, Any]]) -> EmbeddingServiceSettings:
    cfg = dict(raw or {})
    headers = cfg.get("extra_headers")
    if headers is not None and not isinstance(headers, dict):
        headers = {}
    dimensions_raw = cfg.get("dimensions")
    if dimensions_raw in (None, ""):
        dimensions = None
    else:
        try:
            dimensions = int(dimensions_raw)
        except (TypeError, ValueError):
            dimensions = None
    timeout_raw = cfg.get("timeout_seconds", EMBEDDING_DEFAULTS.timeout_seconds.value)
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError):
        timeout_seconds = EMBEDDING_DEFAULTS.timeout_seconds.value
    return EmbeddingServiceSettings(
        provider=str(cfg.get("provider") or EMBEDDING_DEFAULTS.provider.value),
        model_id=str(cfg.get("model_id") or EMBEDDING_DEFAULTS.model_id.value or "").strip() or None,
        api_base=str(cfg.get("api_base") or EMBEDDING_DEFAULTS.api_base.value or "").strip() or None,
        dimensions=dimensions,
        timeout_seconds=timeout_seconds,
        organization=str(cfg.get("organization", EMBEDDING_DEFAULTS.organization.value) or "").strip() or None,
        extra_headers={
            str(key): str(value)
            for key, value in dict(headers or EMBEDDING_DEFAULTS.extra_headers.value).items()
        },
    )


def _safe_response_body_preview(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        raw_text = str(getattr(response, "text", "") or "")
        return _redact_sensitive_text(raw_text[:MAX_EMBEDDING_ERROR_BODY_CHARS])
    try:
        rendered = json.dumps(body, sort_keys=True)
    except TypeError:
        rendered = str(body)
    return _redact_sensitive_text(rendered[:MAX_EMBEDDING_ERROR_BODY_CHARS])


def _redact_sensitive_text(value: str) -> str:
    redacted = str(value or "")
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", redacted)
    redacted = re.sub(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"',}\s]+", r"\1***", redacted)
    return redacted


def _build_local_embedding(text: str, *, dimensions: int = DEFAULT_LOCAL_EMBEDDING_DIMENSIONS) -> str:
    values = [0.0] * max(1, int(dimensions or DEFAULT_LOCAL_EMBEDDING_DIMENSIONS))
    tokens = [token for token in _normalize_tokens(text) if token]
    if not tokens:
        return json.dumps(values)
    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % len(values)
        values[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    normalized = [round(value / norm, 6) for value in values]
    return json.dumps(normalized)


def _normalize_tokens(text: str) -> list[str]:
    cleaned: list[str] = []
    current: list[str] = []
    for char in str(text or "").lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            cleaned.append("".join(current))
            current = []
    if current:
        cleaned.append("".join(current))
    return cleaned
