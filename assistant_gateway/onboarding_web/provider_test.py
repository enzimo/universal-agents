"""Provider connection checks used by web onboarding.

The request itself is ``model_providers.connection_test``, shared with the
broker's ``/auth model`` page so a key passes both checks the same way. This
module adds what only onboarding has: its provider names ("OpenRouter",
"OpenAI Compatible", "Google AI") and the two sign-in providers (OpenAI
Codex, xAI sign-in), whose token comes from the instance's own sign-in store
rather than from the person.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from custom_agents.openai_codex_auth import (
    DEFAULT_OPENAI_CODEX_BASE_URL,
    OpenAICodexAuthError,
    codex_backend_headers,
    is_openai_codex_provider,
    resolve_openai_codex_credentials,
)
from custom_agents.xai_oauth_auth import (
    DEFAULT_XAI_OAUTH_BASE_URL,
    XAIOAuthError,
    is_xai_oauth_provider,
    resolve_xai_oauth_credentials,
)
from model_providers.connection_test import (
    ConnectionTestRequest,
    ConnectionTestResult,
    call_endpoint,
    post_responses_stream,
    responses_payload,
    responses_url,
    run_provider_call,
)
from model_providers.endpoints import EndpointKind
from runtime_settings.model_request_options import normalize_litellm_api_mode, normalize_reasoning_summary

# Onboarding's provider names -> the endpoint kind, and whether the endpoint
# takes the current chat parameters (max_completion_tokens).
_KEY_PROVIDERS: dict[str, tuple[EndpointKind, bool]] = {
    "openai": (EndpointKind.OPENAI, True),
    "openrouter": (EndpointKind.OPENAI_COMPATIBLE, True),
    "openai compatible": (EndpointKind.OPENAI_COMPATIBLE, False),
    "anthropic": (EndpointKind.ANTHROPIC, False),
    "google ai": (EndpointKind.GEMINI, False),
}

# The exported function is named for the UI action; it must not be collected
# as a pytest test.
__test__ = False


@dataclass(frozen=True)
class ProviderTestResult:
    ok: bool
    message: str
    provider: str
    sample: str = ""


def test_provider_connection(
    *,
    provider: str,
    endpoint: str,
    token: str,
    model: str,
    reasoning_effort: str = "",
    reasoning_summary: str | None = None,
    litellm_api_mode: str = "chat_completions",
    stream: bool | None = None,
    xai_oauth: Mapping[str, Any] | None = None,
    instance_root: str | Path | None = None,
) -> ProviderTestResult:
    provider = str(provider or "").strip()
    endpoint = str(endpoint or "").strip()
    token = str(token or "").strip()
    model = str(model or "").strip()
    try:
        reasoning_summary = normalize_reasoning_summary(reasoning_summary)
        litellm_api_mode = normalize_litellm_api_mode(litellm_api_mode)
    except ValueError as exc:
        return ProviderTestResult(ok=False, provider=provider, message=str(exc))
    provider_key = provider.lower()
    signs_in = is_openai_codex_provider(provider_key) or is_xai_oauth_provider(provider_key)
    missing = [
        label
        for label, value in (
            ("provider", provider),
            ("endpoint", endpoint),
            ("token", token if not signs_in else token or "resolved"),
            ("model", model),
        )
        if not value
    ]
    if missing:
        return ProviderTestResult(
            ok=False, provider=provider, message="Missing required connection fields: " + ", ".join(missing)
        )
    if not endpoint.startswith(("http://", "https://")):
        return ProviderTestResult(ok=False, provider=provider, message="API Base URL must start with http:// or https://")
    if signs_in:
        result = run_provider_call(
            lambda: _call_sign_in_provider(
                provider_key, endpoint, token, model, str(reasoning_effort or "").strip(), reasoning_summary,
                xai_oauth=xai_oauth, instance_root=instance_root,
            ),
            label=provider,
            key=token,
        )
        return _onboarding_result(result, provider)
    kind_and_parameters = _KEY_PROVIDERS.get(provider_key)
    if kind_and_parameters is None:
        return ProviderTestResult(ok=False, provider=provider, message=f"Unsupported model provider: {provider}")
    kind, current_chat_parameters = kind_and_parameters
    request = ConnectionTestRequest(
        kind=kind,
        base_url=endpoint,
        key=token,
        model=model,
        api_mode=litellm_api_mode,
        reasoning_effort=str(reasoning_effort or "").strip(),
        reasoning_summary=reasoning_summary,
        stream=stream,
        current_chat_parameters=current_chat_parameters,
    )
    return _onboarding_result(run_provider_call(lambda: call_endpoint(request), label=provider, key=token), provider)


def _onboarding_result(result: ConnectionTestResult, provider: str) -> ProviderTestResult:
    return ProviderTestResult(ok=result.ok, message=result.message, provider=provider, sample=result.sample)


def _call_sign_in_provider(
    provider_key: str,
    endpoint: str,
    token: str,
    model: str,
    reasoning_effort: str,
    reasoning_summary: str | None,
    *,
    xai_oauth: Mapping[str, Any] | None,
    instance_root: str | Path | None,
) -> dict[str, object]:
    """OpenAI Codex or xAI sign-in: the token comes from the instance's sign-in store, never the browser."""

    if is_openai_codex_provider(provider_key):
        access_token = token
        base_url = endpoint.strip() or DEFAULT_OPENAI_CODEX_BASE_URL
        if not access_token:
            try:
                credentials = resolve_openai_codex_credentials({"base_url": base_url})
            except OpenAICodexAuthError as exc:
                raise ValueError(str(exc)) from exc
            access_token = credentials.api_key
            base_url = credentials.base_url
        payload = responses_payload(model, reasoning_effort=reasoning_effort, reasoning_summary=reasoning_summary)
        payload.pop("max_output_tokens", None)
        return post_responses_stream(
            responses_url(base_url),
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                **codex_backend_headers(access_token),
            },
            payload,
            provider_label="Codex",
        )
    raw_settings = dict(xai_oauth or {})
    raw_settings["base_url"] = endpoint.strip() or DEFAULT_XAI_OAUTH_BASE_URL
    try:
        xai_credentials = resolve_xai_oauth_credentials(raw_settings, instance_root=instance_root)
    except XAIOAuthError as exc:
        raise ValueError(str(exc)) from exc
    payload = responses_payload(model)
    payload["max_output_tokens"] = 10
    return post_responses_stream(
        responses_url(xai_credentials.base_url),
        {
            "Authorization": f"Bearer {xai_credentials.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
        payload,
        provider_label="xAI",
    )
