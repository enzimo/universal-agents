"""Provider error classification shared by retry, failover, and reporting paths."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Optional

from custom_agents.model_key_errors import ModelKeyError

class ProviderConnectivityError(RuntimeError):
    """A provider request could not reach its configured endpoint."""


PROVIDER_TIMEOUT_USER_MESSAGE = (
    "The model service timed out before completing this request, and no fallback "
    "model was available. Please try again."
)


class ProviderTimeoutError(RuntimeError):
    """A provider timeout remained after configured failover was unavailable."""

    def __init__(self) -> None:
        super().__init__(PROVIDER_TIMEOUT_USER_MESSAGE)


MODEL_STREAM_IDLE_LIMIT = "idle"
MODEL_STREAM_CALL_LIMIT = "call"
MODEL_STREAM_READ_LIMIT = "read"
_MODEL_STREAM_TIMEOUT_MESSAGES = {
    MODEL_STREAM_IDLE_LIMIT: (
        "Model stream timed out: the model sent no output within the idle limit."
    ),
    MODEL_STREAM_CALL_LIMIT: (
        "Model stream timed out: the model call ran past its total time limit."
    ),
    MODEL_STREAM_READ_LIMIT: (
        "Model stream timed out: the connection stopped delivering data."
    ),
}


class ModelStreamTimeoutError(TimeoutError):
    """A model call stopped making progress, as decided by the stream watchdog.

    ``custom_agents.model_stream_watchdog`` raises this in three cases, named by
    ``limit``: no model output within the idle limit (``idle``), the whole call
    ran past its limit (``call``), or the provider's HTTP client gave up while
    reading the open stream (``read``). The message carries no numbers on
    purpose: other classifiers match text such as "413", and the limits are
    kept as attributes and in the watchdog's log line instead.
    """

    def __init__(
        self,
        *,
        limit: str,
        limit_seconds: float,
        elapsed_seconds: float,
        events_received: int,
        model_id: str,
    ) -> None:
        self.limit = limit
        self.limit_seconds = limit_seconds
        self.elapsed_seconds = elapsed_seconds
        self.events_received = events_received
        self.model_id = model_id
        super().__init__(_MODEL_STREAM_TIMEOUT_MESSAGES[limit])


_OVERLOAD_MARKERS = (
    "engine is currently overloaded",
    "currently overloaded",
    "overloaded. please try again later",
    "provider overloaded",
    "throttling_error",
    "throttl",
    "ratelimiterror",
    "rate limit",
    "too many requests",
    "server is busy",
    "provider is busy",
    "temporarily unavailable",
    "service unavailable",
    "error code: 429",
    "status code: 429",
    "code': '429",
    '"code": "429',
    "error code: 503",
    "status code: 503",
)

_CONNECTIVITY_TYPE_MARKERS = (
    "apiconnectionerror",
    "clientconnectorerror",
    "connectionrefusederror",
    "connecterror",
    "gaierror",
)

_CONNECTIVITY_TEXT_MARKERS = (
    "connection refused",
    "connect call failed",
    "cannot connect to host",
    "failed to establish a new connection",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "getaddrinfo failed",
    "network is unreachable",
    "no route to host",
    "certificate verify failed",
    "litellm_proxyexception - connection error",
)

_TIMEOUT_TYPE_MARKERS = (
    "apitimeouterror",
)

_TIMEOUT_TEXT_MARKERS = (
    "gateway timeout",
    "request timed out",
    "timed out",
    "request timeout",
    "timeout error",
    "error code: 504",
    "status code: 504",
    "code': '504",
    '"code": "504',
)

_FAILOVER_ONLY_MARKERS = (
    "midstreamfallbackerror",
    "an error occurred during streaming",
    "error code: 500",
    "status code: 500",
    "code': '500",
    '"code": "500',
)


def iter_exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield an exception and its explicit/implicit causes without looping."""

    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        # Strands also retains the original error on wrappers constructed
        # outside an except block, where Python sets neither cause nor context.
        wrapped = current.__cause__ or current.__context__ or getattr(current, "original_exception", None)
        current = wrapped if isinstance(wrapped, BaseException) else None


def is_model_key_error(error: BaseException) -> bool:
    """Whether this chain contains a local model-key failure, even under an SDK wrapper."""

    return any(isinstance(current, ModelKeyError) for current in iter_exception_chain(error))


def _matches_error_chain(
    error: BaseException,
    *,
    text_markers: tuple[str, ...],
    type_markers: tuple[str, ...] = (),
) -> bool:
    for current in iter_exception_chain(error):
        error_type = type(current).__name__.lower()
        if error_type in type_markers:
            return True
        text = str(current).lower()
        if any(marker in text for marker in text_markers):
            return True
    return False


def is_provider_connectivity_error(error: BaseException) -> bool:
    """Return whether the failure represents endpoint connection establishment."""

    if is_model_key_error(error):
        return False
    return _matches_error_chain(
        error,
        text_markers=_CONNECTIVITY_TEXT_MARKERS,
        type_markers=_CONNECTIVITY_TYPE_MARKERS,
    )


def is_provider_overload_error(error: BaseException) -> bool:
    """Return whether the provider explicitly reported overload or throttling."""

    if is_model_key_error(error) or is_provider_connectivity_error(error):
        return False
    return _matches_error_chain(error, text_markers=_OVERLOAD_MARKERS)


def is_provider_timeout_error(error: BaseException) -> bool:
    """Return whether the provider request ended in an explicit timeout."""

    if is_model_key_error(error):
        return False
    for current in iter_exception_chain(error):
        if isinstance(current, ModelStreamTimeoutError):
            return True
        status_code = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        response_status_code = getattr(response, "status_code", None)
        if status_code == 504 or response_status_code == 504:
            return True
    return _matches_error_chain(
        error,
        text_markers=_TIMEOUT_TEXT_MARKERS,
        type_markers=_TIMEOUT_TYPE_MARKERS,
    )


_KEY_REJECTED_TYPE_MARKERS = ("authenticationerror",)
_KEY_REJECTED_TEXT_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "api key not valid",
    "api_key_invalid",
    "invalid x-api-key",
)


def provider_error_status_code(error: BaseException) -> int | None:
    """The HTTP status a provider answered with, from the first error in the chain that has one."""

    if is_model_key_error(error):
        return None
    for current in iter_exception_chain(error):
        for candidate in (getattr(current, "status_code", None), getattr(getattr(current, "response", None), "status_code", None)):
            if isinstance(candidate, int):
                return candidate
    return None


def is_provider_key_rejected_error(error: BaseException) -> bool:
    """Whether the provider refused the key itself (HTTP 401, an authentication error, or its wording).

    A 403 alone is not counted: providers also use it for a model the key may
    not use or for content they refused, which a new key would not fix. A
    rejected key is never a reason to fail over to another model
    (``is_provider_failover_eligible_error``); the person and administrators
    are told instead (``custom_agents.model_keys``).
    """

    if is_model_key_error(error):
        return False
    if provider_error_status_code(error) == 401:
        return True
    return _matches_error_chain(error, text_markers=_KEY_REJECTED_TEXT_MARKERS, type_markers=_KEY_REJECTED_TYPE_MARKERS)


def is_provider_failover_eligible_error(error: BaseException) -> bool:
    """Return whether retrying through another configured model may succeed."""

    if is_model_key_error(error):
        return False
    return (
        is_provider_overload_error(error)
        or is_provider_connectivity_error(error)
        or is_provider_timeout_error(error)
        or _matches_error_chain(error, text_markers=_FAILOVER_ONLY_MARKERS)
    )
