"""Shared Strands model construction for Universal Agents."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from custom_agents.model_keys import (
    NO_KEY_PLACEHOLDER,
    CallKeyedClientArgs,
    ModelKeys,
    bind_model_keys,
    call_key,
    openai_key_function,
    process_model_keys,
)
from custom_agents.model_stream_watchdog import ModelStreamLimits, guard_model_stream
from model_providers.endpoints import (
    WELL_KNOWN_PROVIDERS,
    EndpointKind,
    openai_compatible_endpoint,
    well_known_endpoint,
)
from default_values import LLM_DEFAULTS
from runtime_settings.model_request_options import (
    LITELLM_API_MODE_RESPONSES,
    is_reasoning_summary_responses_transport,
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)
from custom_agents.openai_codex_auth import (
    OPENAI_CODEX_PROVIDER,
    is_openai_codex_provider,
    normalize_openai_codex_provider,
    resolve_openai_codex_credentials,
)
from custom_agents.xai_oauth_auth import (
    XAI_OAUTH_PROVIDER,
    is_xai_oauth_provider,
    make_xai_oauth_api_key_provider,
    normalize_xai_oauth_provider,
    resolve_xai_oauth_model_id,
    resolve_xai_oauth_settings,
)

OPENAI_CODEX_CONTEXT_WINDOW_TOKENS = 272000
XAI_OAUTH_CONTEXT_WINDOW_TOKENS = 500000
# The key-based xAI provider (an xAI API key, one fixed endpoint); xai-oauth signs in instead.
XAI_PROVIDER = "xai"
_GROK_REASONING_EFFORT_MODEL_PREFIXES = (
    "grok-3-mini",
    "grok-4.20-multi-agent",
    "grok-4.3",
    "grok-4.5",
)
# OpenAI reasoning families: the o-series and every numbered GPT generation
# from gpt-5 onward. Matching the generation number instead of listing
# generations keeps new families such as gpt-6 on the reasoning request
# contract (max_completion_tokens, no temperature, reasoning Responses adapter);
# LiteLLM's own parameter translation may lag behind new generations.
_OPENAI_REASONING_MODEL_PATTERN = re.compile(
    r"^(?:gpt-(?:[5-9]|[1-9]\d+)(?:$|[-.])|o[1-9](?:$|[-.]))"
)
_OPENAI_TEMPERATURE_SUPPORTED_MODEL_PREFIXES = ("gpt-5-image",)
_OPENAI_TEMPERATURE_FILTER_PROVIDERS = {"openai", "litellm"}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrandsModelFactoryConfig:
    provider: str
    model_id: str
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    max_output_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    stream: Optional[bool] = None
    litellm_api_mode: str = "chat_completions"
    request_timeout_seconds: Optional[float] = None
    stream_idle_timeout_seconds: float = LLM_DEFAULTS.stream_idle_timeout_seconds.value
    model_call_timeout_seconds: float = LLM_DEFAULTS.model_call_timeout_seconds.value
    reasoning_trace_retention_messages: Optional[int] = None
    litellm_api_base: Optional[str] = None
    openai_codex: Optional[Mapping[str, Any]] = None
    xai_oauth: Optional[Mapping[str, Any]] = None
    instance_root: Optional[str] = None
    # Where model keys come from (custom_agents.model_keys); None is this
    # process's own lookup. Tests pass their own.
    model_keys: Optional[ModelKeys] = None


def build_strands_model(config: StrandsModelFactoryConfig) -> Any:
    """Build the provider model and bound every model call it makes.

    The provider HTTP timeout (``request_timeout_seconds``) only notices a
    connection that sends no bytes; the stream watchdog also ends calls that
    stay open without producing output (see ``model_stream_watchdog``).
    """

    return guard_model_stream(
        _build_provider_model(config),
        ModelStreamLimits(
            idle_timeout_seconds=float(config.stream_idle_timeout_seconds),
            call_timeout_seconds=float(config.model_call_timeout_seconds),
        ),
    )


def _build_provider_model(config: StrandsModelFactoryConfig) -> Any:
    provider = normalize_model_provider(config.provider)
    reasoning_summary = normalize_reasoning_summary(config.reasoning_summary)
    litellm_api_mode = normalize_litellm_api_mode(config.litellm_api_mode)
    responses_transport = is_reasoning_summary_responses_transport(
        provider,
        litellm_api_mode,
    )
    if reasoning_summary and not responses_transport:
        raise ValueError(
            "reasoning_summary requires the OpenAI Responses API: use provider "
            "'openai', 'openai-codex', or set llm.litellm_api_mode to 'responses'"
        )
    if provider == XAI_OAUTH_PROVIDER:
        resolved_model_id = resolve_xai_oauth_model_id(provider, config.model_id)
        if resolved_model_id != config.model_id:
            config = replace(config, model_id=resolved_model_id)
    model_metadata: dict[str, Any] = {
        "context_window_limit": resolve_context_window_tokens(
            provider=provider,
            model_id=config.model_id,
            explicit_context_window_tokens=config.context_window_tokens,
        )
    }
    builder = _PROVIDER_MODEL_BUILDERS.get(provider, _build_openai_chat_model)
    return builder(config, provider, litellm_api_mode, model_metadata)


def _responses_model_class(provider: str, model_id: str, litellm_api_mode: str) -> Any:
    """Pick the reasoning-replay adapter for OpenAI reasoning models."""

    from custom_agents.openai_reasoning_responses_model import OpenAIReasoningResponsesModel
    from strands.models.openai_responses import OpenAIResponsesModel

    if uses_openai_reasoning_responses_adapter(
        provider=provider,
        model_id=model_id,
        litellm_api_mode=litellm_api_mode,
    ):
        return OpenAIReasoningResponsesModel
    return OpenAIResponsesModel


def _build_openai_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    endpoint = well_known_endpoint(EndpointKind.OPENAI)
    keys = _model_keys(config)
    model_class = _responses_model_class(provider, config.model_id, litellm_api_mode)
    model = model_class(
        # The fixed address, so OPENAI_BASE_URL cannot send this key elsewhere.
        client_args={"api_key": openai_key_function(endpoint, keys), "base_url": _well_known_base(EndpointKind.OPENAI)},
        model_id=config.model_id,
        params=_openai_responses_model_params(config),
        # Universal Agents owns conversation compaction and tool-result
        # filtering through a Strands conversation manager. Strands rejects
        # that manager when the model also claims server-side state.
        stateful=False,
        use_native_token_count=False,
        **model_metadata,
    )
    return bind_model_keys(model, endpoint, keys, provider=provider, model_id=config.model_id)


def _build_gemini_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    _litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    from strands.models.gemini import GeminiModel

    model_params = _chat_model_params(config, provider=provider)
    endpoint = well_known_endpoint(EndpointKind.GEMINI)
    # Strands builds a genai.Client from these on every call, so the key is the call's own.
    model = GeminiModel(
        client_args=CallKeyedClientArgs({}, endpoint.address),  # type: ignore[arg-type]
        model_id=config.model_id,
        params=model_params,
        **model_metadata,
    )
    return bind_model_keys(model, endpoint, _model_keys(config), provider=provider, model_id=config.model_id)


def _build_bedrock_model(
    config: StrandsModelFactoryConfig,
    _provider: str,
    _litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=config.model_id,
        **model_metadata,
    )


def _build_anthropic_model(
    config: StrandsModelFactoryConfig,
    _provider: str,
    _litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    import anthropic
    from strands.models.anthropic import AnthropicModel

    endpoint = well_known_endpoint(EndpointKind.ANTHROPIC)

    class _CallKeyedAnthropic(anthropic.AsyncAnthropic):
        """Sends the key the current call looked up (``bind_model_keys``); the model holds none."""

        @property
        def _api_key_auth(self) -> dict[str, str]:
            resolved = call_key(endpoint.address)
            if resolved is None:
                raise RuntimeError("An Anthropic call ran without its key; model calls go through bind_model_keys.")
            return {"X-Api-Key": resolved.sendable}

    model = AnthropicModel(model_id=config.model_id, use_native_token_count=False, **model_metadata)
    client_args: dict[str, Any] = {"api_key": NO_KEY_PLACEHOLDER, "base_url": endpoint.address}
    timeout = _positive_request_timeout(config)
    if timeout is not None:
        client_args["timeout"] = timeout
    # The fixed address, so ANTHROPIC_BASE_URL cannot send this key elsewhere.
    model.client = _CallKeyedAnthropic(**client_args)
    return bind_model_keys(model, endpoint, _model_keys(config), provider=_provider, model_id=config.model_id)


def _build_litellm_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    if litellm_api_mode == LITELLM_API_MODE_RESPONSES:
        return _build_litellm_responses_model(
            config, provider, litellm_api_mode, model_metadata
        )

    from custom_agents.reasoning_litellm_model import ReasoningAwareLiteLLMModel

    model_params = _chat_model_params(config, provider=provider)
    if config.stream is not None:
        model_params["stream"] = bool(config.stream)
    request_timeout = _positive_request_timeout(config)
    if request_timeout is not None:
        model_params["timeout"] = request_timeout
    if not config.litellm_api_base:
        # Without an endpoint LiteLLM picks a provider per model prefix and
        # reads that provider's key variable itself (OPENROUTER_API_KEY, ...);
        # there is no address to bind a stored key to.
        return ReasoningAwareLiteLLMModel(
            client_args=None,
            model_id=config.model_id,
            params=model_params if model_params else None,
            reasoning_trace_retention_messages=config.reasoning_trace_retention_messages,
            **model_metadata,
        )
    endpoint = openai_compatible_endpoint(config.litellm_api_base)
    model = ReasoningAwareLiteLLMModel(
        client_args=CallKeyedClientArgs(
            {"api_base": config.litellm_api_base, "use_litellm_proxy": True}, endpoint.address
        ),
        model_id=config.model_id,
        params=model_params if model_params else None,
        reasoning_trace_retention_messages=config.reasoning_trace_retention_messages,
        **model_metadata,
    )
    return bind_model_keys(model, endpoint, _model_keys(config), provider=provider, model_id=config.model_id)


def _build_litellm_responses_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    if config.stream is False:
        raise ValueError(
            "litellm_api_mode='responses' requires streaming because Strands "
            "consumes Responses API events"
        )
    if not config.litellm_api_base:
        raise ValueError(
            "litellm_api_mode='responses' needs an endpoint address (llm.litellm_api_base): the Responses "
            "request goes straight to that OpenAI-compatible endpoint."
        )
    endpoint = openai_compatible_endpoint(config.litellm_api_base)
    keys = _model_keys(config)
    client_args: dict[str, Any] = {
        "base_url": config.litellm_api_base,
        "api_key": openai_key_function(endpoint, keys),
    }
    request_timeout = _positive_request_timeout(config)
    if request_timeout is not None:
        client_args["timeout"] = request_timeout
    model_class = _responses_model_class(provider, config.model_id, litellm_api_mode)
    model = model_class(
        client_args=client_args,
        model_id=config.model_id,
        params=_openai_responses_model_params(config),
        stateful=False,
        use_native_token_count=False,
        **model_metadata,
    )
    return bind_model_keys(model, endpoint, keys, provider=provider, model_id=config.model_id)


def _well_known_base(kind: EndpointKind) -> str:
    return WELL_KNOWN_PROVIDERS[kind].base_url


def _model_keys(config: StrandsModelFactoryConfig) -> ModelKeys:
    return config.model_keys if config.model_keys is not None else process_model_keys()


def _build_openai_codex_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    credentials = resolve_openai_codex_credentials(
        config.openai_codex,
        instance_root=config.instance_root,
    )
    model_class = _responses_model_class(provider, config.model_id, litellm_api_mode)
    return model_class(
        client_args={
            "api_key": credentials.api_key,
            "base_url": credentials.base_url,
            "default_headers": credentials.default_headers,
        },
        model_id=config.model_id,
        params=_openai_codex_model_params(config),
        stateful=False,
        use_native_token_count=False,
        **model_metadata,
    )


def _build_xai_oauth_model(
    config: StrandsModelFactoryConfig,
    _provider: str,
    _litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    from strands.models.openai_responses import OpenAIResponsesModel

    settings = resolve_xai_oauth_settings(
        config.xai_oauth,
        instance_root=config.instance_root,
    )
    xai_client_args: dict[str, Any] = {
        "api_key": make_xai_oauth_api_key_provider(
            config.xai_oauth,
            instance_root=config.instance_root,
        ),
        "base_url": settings.base_url,
    }
    request_timeout = _positive_request_timeout(config)
    if request_timeout is not None:
        xai_client_args["timeout"] = request_timeout
    return OpenAIResponsesModel(
        client_args=xai_client_args,
        model_id=config.model_id,
        params=_xai_oauth_model_params(config),
        stateful=False,
        use_native_token_count=False,
        **model_metadata,
    )


def _build_xai_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    _litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    """xAI with an API key at its one endpoint (the key also serves Grok voice)."""

    from strands.models.openai_responses import OpenAIResponsesModel

    endpoint = well_known_endpoint(EndpointKind.XAI)
    keys = _model_keys(config)
    client_args: dict[str, Any] = {
        "api_key": openai_key_function(endpoint, keys),
        "base_url": _well_known_base(EndpointKind.XAI),
    }
    request_timeout = _positive_request_timeout(config)
    if request_timeout is not None:
        client_args["timeout"] = request_timeout
    model = OpenAIResponsesModel(
        client_args=client_args,
        model_id=config.model_id,
        params=_xai_oauth_model_params(config),
        stateful=False,
        use_native_token_count=False,
        **model_metadata,
    )
    return bind_model_keys(model, endpoint, keys, provider=provider, model_id=config.model_id)


def _build_openai_chat_model(
    config: StrandsModelFactoryConfig,
    provider: str,
    _litellm_api_mode: str,
    model_metadata: dict[str, Any],
) -> Any:
    """Providers without their own builder use OpenAI's Chat Completions with OpenAI's key."""

    from strands.models.openai import OpenAIModel

    endpoint = well_known_endpoint(EndpointKind.OPENAI)
    keys = _model_keys(config)
    model = OpenAIModel(
        # The fixed address, so OPENAI_BASE_URL cannot send this key elsewhere.
        client_args={"api_key": openai_key_function(endpoint, keys), "base_url": _well_known_base(EndpointKind.OPENAI)},
        model_id=config.model_id,
        **model_metadata,
    )
    return bind_model_keys(model, endpoint, keys, provider=provider, model_id=config.model_id)


def _positive_request_timeout(config: StrandsModelFactoryConfig) -> Optional[float]:
    """Return the configured HTTP timeout, or None to keep the client default."""

    if config.request_timeout_seconds is None:
        return None
    timeout = float(config.request_timeout_seconds)
    return timeout if timeout > 0 else None


# Provider id -> builder. Unlisted providers use the OpenAI Chat Completions model.
_PROVIDER_MODEL_BUILDERS = {
    "openai": _build_openai_model,
    "gemini": _build_gemini_model,
    "bedrock": _build_bedrock_model,
    "anthropic": _build_anthropic_model,
    "litellm": _build_litellm_model,
    XAI_PROVIDER: _build_xai_model,
    OPENAI_CODEX_PROVIDER: _build_openai_codex_model,
    XAI_OAUTH_PROVIDER: _build_xai_oauth_model,
}


def normalize_model_provider(provider: Any) -> str:
    normalized = normalize_openai_codex_provider(provider)
    if is_openai_codex_provider(normalized):
        return OPENAI_CODEX_PROVIDER
    normalized = normalize_xai_oauth_provider(normalized)
    if is_xai_oauth_provider(normalized):
        return XAI_OAUTH_PROVIDER
    return str(normalized or "").strip().lower()


def uses_openai_reasoning_responses_adapter(
    *,
    provider: Any,
    model_id: Any,
    litellm_api_mode: Any = None,
) -> bool:
    """Return whether this route is an OpenAI reasoning Responses transport."""

    normalized_provider = normalize_model_provider(provider)
    responses_transport = normalized_provider in {"openai", OPENAI_CODEX_PROVIDER} or (
        normalized_provider == "litellm"
        and normalize_litellm_api_mode(litellm_api_mode) == LITELLM_API_MODE_RESPONSES
    )
    return responses_transport and is_openai_reasoning_model(str(model_id or ""))


def uses_openai_reasoning_summary_transport(
    *,
    provider: Any,
    model_id: Any,
    litellm_api_mode: Any = None,
) -> bool:
    """Return whether this OpenAI reasoning route can request a summary."""

    normalized_provider = normalize_model_provider(provider)
    return is_reasoning_summary_responses_transport(
        normalized_provider,
        litellm_api_mode,
    ) and is_openai_reasoning_model(str(model_id or ""))


def _chat_model_params(config: StrandsModelFactoryConfig, *, provider: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    _add_supported_temperature(params, config, provider=provider)
    if config.max_output_tokens is not None and int(config.max_output_tokens) > 0:
        token_parameter = (
            "max_completion_tokens"
            if _uses_max_completion_tokens(config, provider=provider)
            else "max_tokens"
        )
        params[token_parameter] = int(config.max_output_tokens)
    if config.reasoning_effort:
        params["reasoning_effort"] = str(config.reasoning_effort)
    return params


def _uses_max_completion_tokens(config: StrandsModelFactoryConfig, *, provider: str) -> bool:
    if provider in {"openai", "openrouter"}:
        return True
    if provider != "litellm":
        return False
    model_id = str(config.model_id or "").strip().lower()
    return (
        model_id.startswith("openrouter/")
        or _is_openrouter_api_base(config.litellm_api_base)
        or is_openai_reasoning_model(model_id)
    )


def _is_openrouter_api_base(value: str | None) -> bool:
    try:
        hostname = (urlparse(str(value or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")


def _openai_codex_model_params(config: StrandsModelFactoryConfig) -> dict[str, Any]:
    """Return only request parameters accepted by the native Codex backend."""
    params: dict[str, Any] = {}
    unsupported_params: list[str] = []
    if config.temperature is not None:
        unsupported_params.append("temperature")
    if config.max_output_tokens is not None and int(config.max_output_tokens) > 0:
        unsupported_params.append("max_output_tokens")
    reasoning: dict[str, str] = {}
    if config.reasoning_effort:
        reasoning["effort"] = str(config.reasoning_effort)
    reasoning_summary = normalize_reasoning_summary(config.reasoning_summary)
    if reasoning_summary:
        reasoning["summary"] = reasoning_summary
    if reasoning:
        params["reasoning"] = reasoning
    if unsupported_params:
        logger.warning(
            "event=openai_codex_unsupported_parameters_omitted "
            "provider=%s model_id=%s parameter_names=%s",
            OPENAI_CODEX_PROVIDER,
            config.model_id,
            ",".join(unsupported_params),
        )
    return params


def _openai_responses_model_params(config: StrandsModelFactoryConfig) -> dict[str, Any]:
    """Return request parameters for the public OpenAI Responses API."""
    params: dict[str, Any] = {}
    _add_supported_temperature(params, config, provider="openai")
    if config.max_output_tokens is not None and int(config.max_output_tokens) > 0:
        params["max_output_tokens"] = int(config.max_output_tokens)
    reasoning: dict[str, str] = {}
    if config.reasoning_effort:
        reasoning["effort"] = str(config.reasoning_effort)
    reasoning_summary = normalize_reasoning_summary(config.reasoning_summary)
    if reasoning_summary:
        reasoning["summary"] = reasoning_summary
    if reasoning:
        params["reasoning"] = reasoning
    return params


def _add_supported_temperature(
    params: dict[str, Any],
    config: StrandsModelFactoryConfig,
    *,
    provider: str,
) -> None:
    """Add temperature unless the selected OpenAI reasoning model rejects it."""
    if config.temperature is None:
        return
    if provider in _OPENAI_TEMPERATURE_FILTER_PROVIDERS and is_openai_reasoning_model(
        config.model_id
    ):
        logger.warning(
            "event=model_parameter_omitted provider=%s model_id=%s "
            "parameter=temperature reason=unsupported_openai_reasoning_model",
            provider,
            config.model_id,
        )
        return
    params["temperature"] = config.temperature


def is_openai_reasoning_model(model_id: str) -> bool:
    """Return whether a native or routed model id identifies an OpenAI reasoning model."""
    normalized = str(model_id or "").strip().lower().rsplit("/", 1)[-1]
    if normalized.startswith(_OPENAI_TEMPERATURE_SUPPORTED_MODEL_PREFIXES):
        return False
    return _OPENAI_REASONING_MODEL_PATTERN.match(normalized) is not None


def _xai_oauth_model_params(config: StrandsModelFactoryConfig) -> dict[str, Any]:
    """Return request parameters supported by xAI's Responses endpoint."""
    params: dict[str, Any] = {}
    if config.temperature is not None:
        params["temperature"] = config.temperature
    if config.max_output_tokens is not None and int(config.max_output_tokens) > 0:
        params["max_output_tokens"] = int(config.max_output_tokens)
    if config.reasoning_effort:
        model_id = str(config.model_id or "").strip().lower()
        if model_id.startswith(_GROK_REASONING_EFFORT_MODEL_PREFIXES):
            requested_effort = str(config.reasoning_effort).strip().lower()
            resolved_effort = "high" if requested_effort in {"max", "ultra"} else requested_effort
            if resolved_effort != requested_effort:
                logger.warning(
                    "event=xai_oauth_reasoning_effort_mapped provider=%s model_id=%s "
                    "requested_effort=%s resolved_effort=%s",
                    XAI_OAUTH_PROVIDER,
                    config.model_id,
                    requested_effort,
                    resolved_effort,
                )
            params["reasoning"] = {"effort": resolved_effort}
        else:
            logger.warning(
                "event=xai_oauth_reasoning_effort_omitted provider=%s model_id=%s "
                "reason=unsupported_model",
                XAI_OAUTH_PROVIDER,
                config.model_id,
            )
    return params


# Context windows of models the runtime knows by name. Any other model with no
# configured context_window_tokens gets FALLBACK_CONTEXT_WINDOW_TOKENS, with a
# warning: a wrong window misjudges context pressure (too small hides tool
# results; too large overflows the provider). A model switch sets the new
# model's window from the capability lookup (custom_agents.model_choice).
_KNOWN_CONTEXT_WINDOWS = {
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4-turbo": 128000,
    "gpt-3.5-turbo": 16385,
    "gemini-2.5-flash-preview": 1048576,
    "gemini-2.0-flash": 1048576,
    "gemini-1.5-pro": 1048576,
    "claude-3-sonnet-20240229": 200000,
    "claude-3-haiku-20240307": 200000,
    "anthropic.claude-3-sonnet-20240229-v1:0": 200000,
    "anthropic.claude-3-haiku-20240307-v1:0": 200000,
}
FALLBACK_CONTEXT_WINDOW_TOKENS = 128000
_warned_context_window_fallbacks: set[tuple[str, str]] = set()


def known_context_window_tokens(*, provider: Any, model_id: Any) -> Optional[int]:
    """The window the runtime knows for this provider or model, or None when it would guess."""

    normalized = normalize_model_provider(provider) if str(provider or "").strip() else ""
    if normalized == OPENAI_CODEX_PROVIDER:
        return OPENAI_CODEX_CONTEXT_WINDOW_TOKENS
    if normalized in (XAI_OAUTH_PROVIDER, XAI_PROVIDER):
        return XAI_OAUTH_CONTEXT_WINDOW_TOKENS
    return _KNOWN_CONTEXT_WINDOWS.get(str(model_id or ""))


def resolve_context_window_tokens(
    *,
    provider: Any,
    model_id: Any,
    explicit_context_window_tokens: Any = None,
) -> int:
    """The one context-window rule for agents, model objects and diagnostics."""

    if explicit_context_window_tokens:
        return max(1024, int(explicit_context_window_tokens))
    known = known_context_window_tokens(provider=provider, model_id=model_id)
    if known is not None:
        return known
    key = (str(provider or ""), str(model_id or ""))
    if key not in _warned_context_window_fallbacks:
        _warned_context_window_fallbacks.add(key)
        logger.warning(
            "No context window is set for this model, so the runtime assumes %s tokens. Set "
            "context_window_tokens for it (a model switch sets it from the capability lookup). "
            "event=context_window_fallback provider=%s model_id=%s fallback_tokens=%s",
            FALLBACK_CONTEXT_WINDOW_TOKENS,
            key[0],
            key[1],
            FALLBACK_CONTEXT_WINDOW_TOKENS,
        )
    return FALLBACK_CONTEXT_WINDOW_TOKENS
