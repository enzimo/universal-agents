"""Web requests that the web research tools send through the secure tool broker.

Exa, Brave Search and this instance's Crawl4AI server need a key. The keys are
linked accounts in the credential store (``http:exa``, ``http:brave-search``,
``http:crawl4ai`` in ``ua_definitions/access/tools/broker.yaml``), not
environment variables. Each request goes to the broker's ``secure_requests``
action: the broker's access gate checks the signed task context, decides the
request with the access service (the three tools are in the ``internet`` set
that every agent and person holds unless someone blocks it) and chooses the
account link, the person's own first and then the team's. The broker then adds
the key where the link says and sends the request.

The request built here carries no key, header value or placeholder, so the
agent process never holds a key. Every refusal comes back as
``BrokerWebRequestError`` with a plain message and the next step (for a missing
key: the ``/link`` commands), which the tools pass to the model unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from id_generation import new_id

logger = logging.getLogger(__name__)

WEB_REQUEST_PROVIDER = "secure_requests"
WEB_REQUEST_ACTION = "request"
# The broker stops one web request after this long (credential_catalog._bounded_api_timeout).
BROKER_MAX_TIMEOUT_SECONDS = 120
# Extra time for the broker's own checks and the response on top of the request timeout.
_TRANSPORT_MARGIN_SECONDS = 15

REASON_BROKER_UNAVAILABLE = "web_request_broker_unavailable"
REASON_NO_TASK_CONTEXT = "web_request_task_context_missing"
REASON_BROKER_ERROR = "web_request_broker_error"
REASON_REQUEST_FAILED = "web_request_failed"
# Reasons the broker gives (secure_tool_broker.access_gate), kept as they are.
REASON_ACCOUNT_LINK_MISSING = "account_link_missing"
REASON_ACCESS_DENIED = "access_denied"


@dataclass(frozen=True)
class BrokerWebResponse:
    """The service's answer as the broker returned it (credential values redacted)."""

    http_status: int
    content_type: str
    json: Any
    text: str
    truncated: bool
    # Where a 3xx answer pointed; the broker never follows it, so the linked
    # credential reaches only the configured address.
    redirect_location: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.http_status < 300


class BrokerWebRequestError(Exception):
    """The request was not sent, or failed on the way; ``message`` says why and what to do."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        retryable: bool = False,
        tool_id: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        self.retryable = retryable
        self.tool_id = tool_id


def send_web_request(
    method: str,
    url: str,
    *,
    reason: str,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 30,
    max_response_bytes: int | None = None,
) -> BrokerWebResponse:
    """Send one request through the broker, which adds the linked key; raise on refusal or failure."""

    base_url, token = _broker_connection()
    context = _task_context()
    timeout = max(1, min(int(timeout_seconds), BROKER_MAX_TIMEOUT_SECONDS))
    request_params: dict[str, Any] = {"method": method.upper(), "url": url, "timeout_seconds": timeout}
    optional = {"params": params, "json": json_body, "headers": headers, "max_response_bytes": max_response_bytes}
    request_params.update({key: value for key, value in optional.items() if value is not None})
    body = {
        "provider": WEB_REQUEST_PROVIDER,
        "action": WEB_REQUEST_ACTION,
        "params": request_params,
        "reason": reason,
        "context": {**context, "invocation_id": new_id("web_req")},
    }
    payload = _post(f"{base_url}/v1/tools/{WEB_REQUEST_PROVIDER}", body, token, timeout + _TRANSPORT_MARGIN_SECONDS)
    return _response(payload)


def _broker_connection() -> tuple[str, str]:
    base_url = str(os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise BrokerWebRequestError(
            "This tool sends its requests through the secure tool broker, but this agent process was "
            "started without the broker's address (SECURE_TOOL_BROKER_INTERNAL_BASE_URL). The launcher "
            "sets it for every agent process; an operator restarts the team through the launcher.",
            reason_code=REASON_BROKER_UNAVAILABLE,
        )
    return base_url, str(os.getenv("SECURE_TOOL_BROKER_TOKEN") or "")


def _task_context() -> dict[str, Any]:
    from custom_tools.secure_cli_tool import trusted_broker_context

    context = trusted_broker_context(allow_system_work=True)
    if context is None or not ({"task_context", "schedule_run"} & context.keys()):
        raise BrokerWebRequestError(
            "This tool works only inside an agent task, because the broker checks whom the task works "
            "for. This is a runtime problem, not a missing key.",
            reason_code=REASON_NO_TASK_CONTEXT,
        )
    return context


def _post(url: str, body: Mapping[str, Any], token: str, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise _refusal(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("The broker did not answer a web request. event=broker_web_request_unreachable error=%s", exc)
        raise BrokerWebRequestError(
            f"The secure tool broker did not answer ({exc}). Try again shortly.",
            reason_code=REASON_BROKER_ERROR,
            retryable=True,
        ) from exc
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise BrokerWebRequestError(
            "The secure tool broker sent an answer that is not JSON.", reason_code=REASON_BROKER_ERROR, retryable=True
        ) from exc
    return payload if isinstance(payload, dict) else {}


def _refusal(status: int, detail: str) -> BrokerWebRequestError:
    """The broker's refusal (access denied, no linked key, identity) as one plain message."""

    try:
        payload = json.loads(detail) if detail else {}
    except json.JSONDecodeError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    reason_code = str(payload.get("reason_code") or REASON_BROKER_ERROR)
    tool_id = str(payload.get("tool_id") or "")
    message = " ".join(
        part for part in (str(payload.get("error") or detail[:500]).strip(), str(payload.get("next_step") or "").strip()) if part
    )
    logger.warning(
        "The broker refused a web request. event=broker_web_request_refused http_status=%s reason_code=%s tool_id=%s",
        status,
        reason_code,
        tool_id or "-",
    )
    return BrokerWebRequestError(
        message or f"The secure tool broker refused the request (HTTP {status}).",
        reason_code=reason_code,
        tool_id=tool_id,
    )


def _response(payload: Mapping[str, Any]) -> BrokerWebResponse:
    raw_content = payload.get("content")
    content: Mapping[str, Any] = raw_content if isinstance(raw_content, dict) else {}
    if str(payload.get("status") or "") != "success":
        error_code = str(content.get("error_code") or REASON_REQUEST_FAILED)
        raise BrokerWebRequestError(
            str(payload.get("error") or "The broker could not send the request."),
            reason_code=error_code,
            retryable=error_code == "api_request_failed",
        )
    return BrokerWebResponse(
        http_status=int(content.get("http_status") or 0),
        content_type=str(content.get("content_type") or ""),
        json=content.get("json"),
        text=str(content.get("text") or ""),
        truncated=bool(content.get("truncated")),
        redirect_location=str(content.get("redirect_location") or ""),
    )


def is_missing_key(error: BrokerWebRequestError) -> bool:
    return error.reason_code == REASON_ACCOUNT_LINK_MISSING


__all__ = [
    "BROKER_MAX_TIMEOUT_SECONDS",
    "BrokerWebRequestError",
    "BrokerWebResponse",
    "REASON_ACCESS_DENIED",
    "REASON_ACCOUNT_LINK_MISSING",
    "is_missing_key",
    "send_web_request",
]
