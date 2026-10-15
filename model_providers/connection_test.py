"""Check a model key with one real request before anyone relies on it.

Web onboarding tests the key a person typed before it saves anything
(``assistant_gateway.onboarding_web.provider_test``), and the broker's
``/auth model`` page does the same before it stores a key
(``secure_tool_broker.model_key_page``). Both use this module so a key that
passes one check passes the other.

The request is the smallest one that proves the key works for the model that
will use it: a short chat or Responses completion with one tool offered (so
an endpoint that cannot route tool calls fails here, not in the first real
task), or one embedding. Errors never contain the key: every message passes
through ``_without_key`` before it leaves this module.

People type an OpenAI-compatible address with or without its ``/v1`` part
(the endpoint table shows only the host, so nothing tells them which).
``check_endpoint_address`` therefore tries the other form when the first one
answers as if nothing were there (HTTP 404 or 405, or a web page instead of
a model answer) and reports the address that worked, so whoever saves it
saves the form agents can call. Well-known providers have fixed addresses
and are tested as given.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from default_values import LLM_DEFAULTS
from model_providers.endpoints import WELL_KNOWN_PROVIDERS, EndpointKind
from runtime_settings.model_request_options import (
    LITELLM_API_MODE_RESPONSES,
    normalize_litellm_api_mode,
)

# The longest wait to connect or for the next bytes of an answer: the same as
# agents' own model requests (llm.request_timeout_seconds). A reasoning model
# can think in silence for a minute before its first output, so a shorter wait
# fails endpoints that work (owner's report 2026-10-02: a 20-second wait refused
# a working Bifrost endpoint). Callers pass the team's configured value.
CONNECTION_TEST_TIMEOUT_SECONDS = LLM_DEFAULTS.request_timeout_seconds.value
CONNECTION_TEST_MAX_ERROR_CHARS = 800
CONNECTION_TEST_MAX_RESPONSE_BYTES = 65_536
CONNECTION_TEST_MAX_COMPLETION_TOKENS = 128
CONNECTION_TEST_MAX_OUTPUT_TOKENS = 1_024
CONNECTION_TEST_PROMPT = "Reply to me: 'I'm connected and ready to go!'"
CONNECTION_TEST_EMBEDDING_INPUT = "connection test"
CONNECTION_TEST_TOOL_NAME = "connection_probe"
_TOOL_DESCRIPTION = "Return a connection status when the model chooses to use this tool."
_TOOL_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": False}
CONNECTION_TEST_CHAT_TOOL = {
    "type": "function",
    "function": {"name": CONNECTION_TEST_TOOL_NAME, "description": _TOOL_DESCRIPTION, "parameters": _TOOL_PARAMETERS},
}
CONNECTION_TEST_RESPONSES_TOOL = {
    "type": "function",
    "name": CONNECTION_TEST_TOOL_NAME,
    "description": _TOOL_DESCRIPTION,
    "parameters": _TOOL_PARAMETERS,
}
_REDACTED = "[redacted]"
API_VERSION_PATH = "/v1"
# Statuses that mean "nothing that takes model requests lives at this path".
PATH_PROBLEM_STATUS_CODES = frozenset({404, 405})
KEY_REJECTED_STATUS_CODES = frozenset({401, 403})


class NotAModelAnswer(ValueError):
    """The address answered, but not like a model API (for example with a web page)."""


class ConnectionPurpose(StrEnum):
    """What the endpoint is used for: model turns, or embeddings for memory search."""

    CHAT = "chat"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class ConnectionTestRequest:
    """One connection test.

    ``current_chat_parameters`` sends ``max_completion_tokens`` (OpenRouter
    and native OpenAI) instead of the older ``max_tokens`` that generic
    compatible servers accept.
    """

    kind: EndpointKind
    base_url: str
    key: str
    model: str
    purpose: ConnectionPurpose = ConnectionPurpose.CHAT
    api_mode: str = "chat_completions"
    reasoning_effort: str = ""
    reasoning_summary: str | None = None
    stream: bool | None = None
    current_chat_parameters: bool = False
    timeout_seconds: float = CONNECTION_TEST_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ConnectionTestResult:
    """How a test ended: ``status_code`` is the provider's HTTP status when it answered with an error."""

    ok: bool
    message: str
    sample: str = ""
    status_code: int | None = None
    not_a_model_answer: bool = False
    # The address the request went to (never with a key) and how long the answer took.
    url: str = ""
    elapsed_seconds: float | None = None

    @property
    def key_rejected(self) -> bool:
        return self.status_code in KEY_REJECTED_STATUS_CODES

    @property
    def may_be_wrong_path(self) -> bool:
        """The address answered as if no model API lived there, so another path may work."""

        return self.not_a_model_answer or self.status_code in PATH_PROBLEM_STATUS_CODES


@dataclass(frozen=True)
class AddressTestResult:
    """A test of an address and, when needed, its other form; ``base_url`` is the address that worked (or was tried first)."""

    result: ConnectionTestResult
    base_url: str
    tried: tuple[str, ...]
    typed_url: str = ""

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def moved(self) -> bool:
        """The address that worked is not the one that was typed (trailing slash aside)."""

        return self.ok and self.base_url != self.typed_url.rstrip("/")


def run_connection_test(request: ConnectionTestRequest) -> ConnectionTestResult:
    """Send the test request and describe the outcome in plain words."""

    missing = [label for label, value in (("endpoint", request.base_url), ("model", request.model)) if not value]
    if missing:
        return ConnectionTestResult(ok=False, message="Missing: " + ", ".join(missing) + ".")
    if not request.base_url.startswith(("http://", "https://")):
        return ConnectionTestResult(ok=False, message="The endpoint address must start with http:// or https://.")
    return run_provider_call(
        lambda: call_endpoint(request),
        label=_label(request.kind),
        key=request.key,
        timeout_seconds=request.timeout_seconds,
        url=request_url(request),
    )


def check_endpoint_address(request: ConnectionTestRequest) -> AddressTestResult:
    """Test ``request.base_url`` and, for an OpenAI-compatible address that seems wrong, its form with or without ``/v1``."""

    if request.kind is not EndpointKind.OPENAI_COMPATIBLE:
        return AddressTestResult(
            result=run_connection_test(request), base_url=request.base_url, tried=(request.base_url,), typed_url=request.base_url
        )
    first_url, other_url = _address_forms_in_order(request.base_url)
    first = run_connection_test(replace(request, base_url=first_url))
    if first.ok or not first.may_be_wrong_path:
        return AddressTestResult(result=first, base_url=first_url, tried=(first_url,), typed_url=request.base_url)
    second = run_connection_test(replace(request, base_url=other_url))
    tried = (first_url, other_url)
    if second.ok or not second.may_be_wrong_path:
        # The other form reached a model API, so its answer (a rejected key, an unknown model) is the useful one.
        return AddressTestResult(result=second, base_url=other_url, tried=tried, typed_url=request.base_url)
    return AddressTestResult(result=first, base_url=first_url, tried=tried, typed_url=request.base_url)


def _address_forms_in_order(base_url: str) -> tuple[str, str]:
    """A bare host is tried with ``/v1`` first, the usual form; an address with a path is tried as typed first.

    Trying the host's root first sent the model request to whatever serves the
    root (a web interface, a proxy that answers 504), which may stay silent until
    the timeout instead of saying "not here".
    """

    typed = base_url.rstrip("/")
    other = other_address_form(typed)
    return (other, typed) if not urllib_parse.urlsplit(typed).path else (typed, other)


def other_address_form(base_url: str) -> str:
    """``https://host/v1`` for ``https://host`` and the other way round."""

    base = base_url.rstrip("/")
    return base.removesuffix(API_VERSION_PATH) if base.endswith(API_VERSION_PATH) else base + API_VERSION_PATH


def failure_explanation(outcome: AddressTestResult) -> str:
    """What a failed test most likely means and what to do, in plain words (never the provider's raw answer)."""

    result = outcome.result
    tried = " and ".join(outcome.tried)
    if result.key_rejected:
        return "The provider refused the key. Check that you copied the whole key and that it is still active."
    if result.may_be_wrong_path:
        return (
            f"Nothing at {tried} accepts model requests. Check the address: it is usually the one the provider's "
            "documentation gives for OpenAI-compatible requests."
        )
    if result.status_code == 400 or result.status_code == 422:
        return "The endpoint did not accept the request. Check the model name and the request API."
    if result.status_code == 429:
        return "The provider is limiting requests, or the account has run out of credit. Try again later or check the account."
    if result.status_code == 504:
        waited = f" after {result.elapsed_seconds:.0f} seconds" if result.elapsed_seconds is not None else ""
        return (
            f"A gateway in front of the model gave up waiting for the model's answer{waited} (HTTP 504). The address "
            "and key reached it; the model was slower than the gateway's time limit, or the gateway could not reach "
            "the model's provider. Check that gateway's request timeout (for Bifrost, the provider's network "
            "timeout) and try again."
        )
    if result.status_code in (502, 503):
        return "A gateway in front of the model could not get an answer from it. Try again in a few minutes."
    if result.status_code is not None and result.status_code >= 500:
        return "The endpoint reported a problem of its own. Try again in a few minutes."
    if result.status_code is None:
        return "Check the address and that this server can reach it over the network."
    return "Check the address, the model name and the key."


def run_provider_call(
    call: Callable[[], dict[str, object]],
    *,
    label: str,
    key: str,
    timeout_seconds: float = CONNECTION_TEST_TIMEOUT_SECONDS,
    url: str = "",
) -> ConnectionTestResult:
    """Run one provider request and turn every failure into a message without the key.

    ``url`` (without any key) names the address in messages and logs, with how
    long the answer took, so a failure can be traced to the part that gave it.
    """

    started = time.monotonic()
    result = _provider_call_result(call, label=url or label, key=key, timeout_seconds=timeout_seconds, started=started)
    return replace(result, url=url, elapsed_seconds=round(time.monotonic() - started, 1))


def _provider_call_result(
    call: Callable[[], dict[str, object]], *, label: str, key: str, timeout_seconds: float, started: float
) -> ConnectionTestResult:
    try:
        payload = call()
    except urllib_error.HTTPError as exc:
        waited = time.monotonic() - started
        return ConnectionTestResult(
            ok=False,
            message=_without_key(f"{label} answered HTTP {exc.code} after {waited:.1f} seconds: {read_provider_error(exc)}", key),
            status_code=int(exc.code),
        )
    except urllib_error.URLError as exc:
        return ConnectionTestResult(ok=False, message=_without_key(f"Could not reach {_in_sentence(label)}: {exc.reason}", key))
    except TimeoutError:
        return ConnectionTestResult(
            ok=False,
            message=(
                f"{label} sent nothing for {timeout_seconds:g} seconds, so the test gave up. Check the endpoint "
                "address and network access; a slow model may need a longer llm.request_timeout_seconds."
            ),
        )
    except NotAModelAnswer as exc:
        return ConnectionTestResult(ok=False, message=_without_key(str(exc), key), not_a_model_answer=True)
    except ValueError as exc:
        return ConnectionTestResult(ok=False, message=_without_key(str(exc), key))
    except OSError as exc:
        return ConnectionTestResult(
            ok=False, message=_without_key(f"Connection to {_in_sentence(label)} failed: {exc}", key)
        )
    return ConnectionTestResult(ok=True, message="Connection test succeeded.", sample=extract_sample(payload))


def request_url(request: ConnectionTestRequest) -> str:
    """The address the test request goes to, never with a key (Gemini's key travels in the query)."""

    if request.purpose is ConnectionPurpose.EMBEDDING:
        return _with_suffix(request.base_url, "/embeddings")
    if request.kind is EndpointKind.ANTHROPIC:
        return _with_suffix(request.base_url, "/messages")
    if request.kind is EndpointKind.GEMINI:
        return _gemini_generate_content_url(request.base_url, "", request.model).split("?", 1)[0]
    uses_responses = request.kind in (EndpointKind.OPENAI, EndpointKind.XAI) or (
        normalize_litellm_api_mode(request.api_mode) == LITELLM_API_MODE_RESPONSES
    )
    return responses_url(request.base_url) if uses_responses else chat_completions_url(request.base_url)


def call_endpoint(request: ConnectionTestRequest) -> dict[str, object]:
    """Send the provider's test request and return its parsed answer; HTTP errors are raised."""

    if request.purpose is ConnectionPurpose.EMBEDDING:
        return _call_embeddings(request)
    api_mode = normalize_litellm_api_mode(request.api_mode)
    if request.kind is EndpointKind.ANTHROPIC:
        return _call_anthropic(request)
    if request.kind is EndpointKind.GEMINI:
        return _call_gemini(request)
    uses_responses = request.kind in (EndpointKind.OPENAI, EndpointKind.XAI) or api_mode == LITELLM_API_MODE_RESPONSES
    if request.reasoning_summary and not uses_responses:
        raise ValueError(
            "Reasoning summaries need the OpenAI Responses API. Choose the Responses request API for this endpoint."
        )
    if uses_responses:
        if request.stream is False:
            raise ValueError("The Responses request API needs streaming; turn streaming on or leave it unset.")
        return _call_responses(request)
    return _call_chat_completions(request)


def responses_payload(model: str, *, reasoning_effort: str = "", reasoning_summary: str | None = None) -> dict[str, object]:
    """The Responses API test request (also used by onboarding's sign-in providers)."""

    payload: dict[str, object] = {
        "model": model,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": CONNECTION_TEST_PROMPT}]}],
        "max_output_tokens": CONNECTION_TEST_MAX_OUTPUT_TOKENS,
        "tools": [CONNECTION_TEST_RESPONSES_TOOL],
        "stream": True,
        "store": False,
    }
    reasoning: dict[str, str] = {}
    if reasoning_effort:
        reasoning["effort"] = reasoning_effort
    if reasoning_summary:
        reasoning["summary"] = reasoning_summary
    if reasoning:
        payload["reasoning"] = reasoning
    return payload


def _call_responses(request: ConnectionTestRequest) -> dict[str, object]:
    return post_responses_stream(
        responses_url(request.base_url),
        {"Authorization": f"Bearer {request.key}", "Accept": "text/event-stream", "Content-Type": "application/json"},
        responses_payload(
            request.model, reasoning_effort=request.reasoning_effort, reasoning_summary=request.reasoning_summary
        ),
        provider_label=_label(request.kind),
        timeout_seconds=request.timeout_seconds,
    )


def _call_chat_completions(request: ConnectionTestRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": request.model,
        "messages": [{"role": "user", "content": CONNECTION_TEST_PROMPT}],
        "tools": [CONNECTION_TEST_CHAT_TOOL],
    }
    if request.current_chat_parameters:
        payload["max_completion_tokens"] = CONNECTION_TEST_MAX_COMPLETION_TOKENS
    else:
        # Generic compatible endpoints do not share one versioned API
        # contract; the older parameter is the one they all accept.
        payload["max_tokens"] = 10
    if request.reasoning_effort:
        payload["reasoning_effort"] = request.reasoning_effort
    return _model_answer(
        post_json(
            chat_completions_url(request.base_url), _bearer(request.key), payload, timeout_seconds=request.timeout_seconds
        ),
        request.kind,
    )


def _model_answer(payload: dict[str, object], kind: EndpointKind) -> dict[str, object]:
    """Refuse an answer that is not JSON (a web page at the wrong path answers 200 too)."""

    if "_raw_text" in payload:
        raise NotAModelAnswer(f"{_label(kind)} answered with something that is not a model response, such as a web page.")
    return payload


def _call_anthropic(request: ConnectionTestRequest) -> dict[str, object]:
    return post_json(
        _with_suffix(request.base_url, "/messages"),
        {"Content-Type": "application/json", "x-api-key": request.key, "anthropic-version": "2023-06-01"},
        {"model": request.model, "max_tokens": 10, "messages": [{"role": "user", "content": CONNECTION_TEST_PROMPT}]},
        timeout_seconds=request.timeout_seconds,
    )


def _call_gemini(request: ConnectionTestRequest) -> dict[str, object]:
    return post_json(
        _gemini_generate_content_url(request.base_url, request.key, request.model),
        {"Content-Type": "application/json"},
        {"contents": [{"parts": [{"text": CONNECTION_TEST_PROMPT}]}], "generationConfig": {"maxOutputTokens": 10}},
        timeout_seconds=request.timeout_seconds,
    )


def _call_embeddings(request: ConnectionTestRequest) -> dict[str, object]:
    payload = _model_answer(
        post_json(
            _with_suffix(request.base_url, "/embeddings"),
            _bearer(request.key),
            {"model": request.model, "input": CONNECTION_TEST_EMBEDDING_INPUT},
            timeout_seconds=request.timeout_seconds,
        ),
        request.kind,
    )
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or "embedding" not in data[0]:
        raise ValueError("The embedding service answered without an embedding.")
    return {"output_text": f"Embedding with {len(data[0]['embedding'])} values."}


def _bearer(key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    *,
    timeout_seconds: float = CONNECTION_TEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """POST JSON and return the answer as a mapping (raw text under ``_raw_text``)."""

    http_request = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib_request.urlopen(http_request, timeout=timeout_seconds) as response:
        body = response.read(CONNECTION_TEST_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"_raw_text": body[:CONNECTION_TEST_MAX_ERROR_CHARS]}
    return parsed if isinstance(parsed, dict) else {"_raw_text": body[:CONNECTION_TEST_MAX_ERROR_CHARS]}


def post_responses_stream(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    *,
    provider_label: str,
    timeout_seconds: float = CONNECTION_TEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """POST a streaming Responses request and return the completed response."""

    http_request = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib_request.urlopen(http_request, timeout=timeout_seconds) as response:
        body = response.read(CONNECTION_TEST_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
    return parse_responses_event_stream(body, provider_label=provider_label)


def parse_responses_event_stream(body: str, *, provider_label: str) -> dict[str, object]:
    """The completed response of a Responses event stream, with its text as ``output_text``."""

    output_parts: list[str] = []
    completed: dict[str, object] | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{provider_label} returned an event stream that is not valid JSON.") from exc
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            output_parts.append(event["delta"])
        elif event_type == "response.output_text.done" and not output_parts and isinstance(event.get("text"), str):
            output_parts.append(event["text"])
        elif event_type == "response.completed" and isinstance(event.get("response"), dict):
            completed = dict(event["response"])
        elif event_type in {"error", "response.failed", "response.incomplete"}:
            raise ValueError(f"{provider_label} stream failed: {_stream_error_message(event)}")
    if completed is None:
        if not any(line.strip().startswith("data:") for line in body.splitlines()):
            raise NotAModelAnswer(f"{provider_label} answered with something that is not a model event stream, such as a web page.")
        raise ValueError(f"{provider_label} stream ended without a completed response.")
    if output_parts:
        completed["output_text"] = "".join(output_parts)
    return completed


def _stream_error_message(event: dict[str, object]) -> str:
    error_payload = event.get("error")
    if not isinstance(error_payload, dict):
        response_payload = event.get("response")
        if isinstance(response_payload, dict) and isinstance(response_payload.get("error"), dict):
            error_payload = response_payload["error"]
    if isinstance(error_payload, dict):
        message = error_payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:CONNECTION_TEST_MAX_ERROR_CHARS]
    return str(event.get("type") or "unknown stream error")


def chat_completions_url(base_url: str) -> str:
    return _with_suffix(base_url, "/chat/completions")


def responses_url(base_url: str) -> str:
    return _with_suffix(base_url, "/responses")


def _with_suffix(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _gemini_generate_content_url(base_url: str, key: str, model: str) -> str:
    base = base_url.rstrip("/")
    quoted_key = urllib_parse.quote(key, safe="")
    if base.endswith(":generateContent"):
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}key={quoted_key}"
    model_path = model if model.startswith("models/") else f"models/{model}"
    return f"{base}/{urllib_parse.quote(model_path, safe='/')}:generateContent?key={quoted_key}"


def read_provider_error(exc: urllib_error.HTTPError) -> str:
    """The provider's own error message from an HTTP error body, shortened."""

    try:
        body = exc.read(CONNECTION_TEST_MAX_ERROR_CHARS).decode("utf-8", errors="replace").strip()
    except OSError:
        body = ""
    if not body:
        return str(exc.reason)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict):
        return body
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("message") or "").strip():
        details = [
            f"{name}: {str(error.get(field) or '').strip()}"
            for name, field in (("parameter", "param"), ("code", "code"))
            if str(error.get(field) or "").strip()
        ]
        message = str(error["message"]).strip()
        return f"{message} ({', '.join(details)})" if details else message
    return str(payload.get("message") or "").strip() or body


def extract_sample(payload: dict[str, object]) -> str:
    """A short piece of the model's answer, to show that the request really ran."""

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()[:300]
    texts = _responses_output_texts(payload) or _chat_texts(payload) or _anthropic_texts(payload) or _gemini_texts(payload)
    if texts:
        return " ".join(texts)[:300]
    raw = payload.get("_raw_text")
    return str(raw).strip()[:300] if raw else ""


def _responses_output_texts(payload: dict[str, object]) -> list[str]:
    items = payload.get("output")
    if not isinstance(items, list):
        return []
    return [
        str(content["text"]).strip()
        for item in items
        if isinstance(item, dict) and isinstance(item.get("content"), list)
        for content in item["content"]
        if isinstance(content, dict) and isinstance(content.get("text"), str)
    ]


def _chat_texts(payload: dict[str, object]) -> list[str]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return []
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    tool_calls = message.get("tool_calls")
    return ["The model answered with a tool call."] if isinstance(tool_calls, list) and tool_calls else []


def _anthropic_texts(payload: dict[str, object]) -> list[str]:
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    return [str(item["text"]).strip() for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]


def _gemini_texts(payload: dict[str, object]) -> list[str]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        return []
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return []
    return [str(part["text"]).strip() for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]


def _label(kind: EndpointKind) -> str:
    provider = WELL_KNOWN_PROVIDERS.get(kind)
    return provider.label if provider is not None else "The endpoint"


def _in_sentence(label: str) -> str:
    """"The endpoint" inside a sentence; provider names keep their capitals."""

    return "the" + label[3:] if label.startswith("The ") else label


def _without_key(message: str, key: str) -> str:
    return message.replace(key, _REDACTED) if key else message


__all__ = [
    "AddressTestResult",
    "CONNECTION_TEST_CHAT_TOOL",
    "CONNECTION_TEST_MAX_OUTPUT_TOKENS",
    "CONNECTION_TEST_RESPONSES_TOOL",
    "ConnectionPurpose",
    "ConnectionTestRequest",
    "ConnectionTestResult",
    "call_endpoint",
    "chat_completions_url",
    "extract_sample",
    "failure_explanation",
    "other_address_form",
    "parse_responses_event_stream",
    "post_json",
    "post_responses_stream",
    "read_provider_error",
    "responses_payload",
    "request_url",
    "responses_url",
    "run_connection_test",
    "run_provider_call",
    "check_endpoint_address",
]
