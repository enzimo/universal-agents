"""Thin HTTP client from agent SSH tools to the secure tool broker's SSH actions.

Sealbox-backed SSH credentials, ephemeral ``ssh-agent`` sockets, and live SSH
sessions live inside the ``secure_tool_broker`` process (never in agent
processes). This client posts each SSH action to ``POST /v1/tools/execution_ssh``
with the active task's signed context (``trusted_broker_context``) so the
broker's ``BrokerAccessGate`` verifies the calling process token, signed task
context, and Cedar access under ``fn:execution_control`` before
``SSHSessionBroker`` runs the operation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from id_generation import new_id

logger = logging.getLogger(__name__)

EXECUTION_SSH_PROVIDER = "execution_ssh"
_TRANSPORT_MARGIN_SECONDS = 20.0
_MIN_TRANSPORT_TIMEOUT_SECONDS = 30.0
_MAX_TRANSPORT_TIMEOUT_SECONDS = 3660.0

REASON_BROKER_UNAVAILABLE = "broker_unavailable"
REASON_BROKER_ERROR = "broker_error"
REASON_TASK_CONTEXT_MISSING = "ssh_task_context_missing"


class ExecutionSSHBrokerError(Exception):
    """Structured refusal or failure returned by the secure tool broker SSH endpoint."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        reason_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.reason_code = reason_code
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.message,
            "error_code": self.error_code,
        }
        if self.reason_code:
            payload["reason_code"] = self.reason_code
        for key in ("credential_key", "field_name", "next_step", "tool_id", "operation", "level"):
            if key in self.details and self.details[key] is not None and key not in payload:
                payload[key] = self.details[key]
        return payload


def broker_execution_ssh_available() -> bool:
    """Return True when this process has the secure tool broker's internal address."""

    return bool(str(os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL") or "").strip())


def _broker_connection() -> tuple[str, str]:
    base_url = str(os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise ExecutionSSHBrokerError(
            "Brokered SSH operations run only in the secure tool broker process, "
            "but SECURE_TOOL_BROKER_INTERNAL_BASE_URL is not configured in this agent process.",
            error_code=REASON_BROKER_UNAVAILABLE,
        )
    return base_url, str(os.getenv("SECURE_TOOL_BROKER_TOKEN") or "")


def _trusted_context() -> dict[str, Any]:
    from custom_tools.secure_cli_tool import trusted_broker_context

    context = trusted_broker_context(allow_system_work=False)
    if context is None or not ({"task_context", "schedule_run"} & context.keys()):
        raise ExecutionSSHBrokerError(
            "Brokered SSH operations require an active task working for a verified registered person.",
            error_code=REASON_TASK_CONTEXT_MISSING,
        )
    return context


def _parse_error_response(status: int, raw: str) -> ExecutionSSHBrokerError:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    reason_code = str(parsed.get("reason_code") or "").strip() or None
    if status == 403 and reason_code in {"access_denied", "access_identity_rejected"}:
        error_code = "execution_access_refused"
    elif status == 403:
        error_code = "execution_access_refused"
    elif status == 404:
        error_code = "not_found"
    elif status == 400:
        error_code = "invalid_argument"
    else:
        error_code = REASON_BROKER_ERROR
    parts = [
        str(parsed.get("error") or raw[:500]).strip(),
        str(parsed.get("next_step") or "").strip(),
    ]
    message = " ".join(part for part in parts if part) or (
        f"The secure tool broker refused the SSH request (HTTP {status})."
    )
    logger.warning(
        "Secure tool broker refused an SSH action. event=broker_ssh_action_refused "
        "http_status=%s error_code=%s reason_code=%s",
        status,
        error_code,
        reason_code or "-",
    )
    return ExecutionSSHBrokerError(
        message,
        error_code=error_code,
        reason_code=reason_code,
        details=parsed,
    )


def call_execution_ssh_broker_sync(
    action: str,
    params: Mapping[str, Any],
    *,
    reason: str,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Send one SSH action synchronously to ``secure_tool_broker`` and return its ``content`` dict."""

    base_url, token = _broker_connection()
    context = _trusted_context()
    body = {
        "provider": EXECUTION_SSH_PROVIDER,
        "action": str(action),
        "params": dict(params or {}),
        "reason": str(reason or "").strip() or f"Execute SSH action {action}",
        "context": {**context, "invocation_id": new_id("ssh_brk")},
    }
    request_timeout = max(
        _MIN_TRANSPORT_TIMEOUT_SECONDS,
        min(float(timeout_seconds) + _TRANSPORT_MARGIN_SECONDS, _MAX_TRANSPORT_TIMEOUT_SECONDS),
    )
    request = Request(
        f"{base_url}/v1/tools/{EXECUTION_SSH_PROVIDER}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=request_timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise _parse_error_response(exc.code, detail) from exc
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning(
            "Secure tool broker did not answer an SSH action. "
            "event=broker_ssh_action_unreachable action=%s error=%s",
            action,
            exc,
        )
        raise ExecutionSSHBrokerError(
            f"The secure tool broker did not answer ({exc}). Try again shortly.",
            error_code=REASON_BROKER_ERROR,
        ) from exc
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ExecutionSSHBrokerError(
            "The secure tool broker returned a non-JSON SSH response.",
            error_code=REASON_BROKER_ERROR,
        ) from exc
    if not isinstance(payload, dict):
        raise ExecutionSSHBrokerError(
            "The secure tool broker returned an invalid SSH response.",
            error_code=REASON_BROKER_ERROR,
        )
    content = payload.get("content")
    content_map = dict(content) if isinstance(content, dict) else {}
    if str(payload.get("status") or "").strip().lower() != "success":
        error_code = str(content_map.get("error_code") or REASON_BROKER_ERROR)
        reason_code = str(content_map.get("reason_code") or "").strip() or None
        raise ExecutionSSHBrokerError(
            str(payload.get("error") or "The broker SSH action failed."),
            error_code=error_code,
            reason_code=reason_code,
            details=content_map,
        )
    return content_map


async def call_execution_ssh_broker(
    action: str,
    params: Mapping[str, Any],
    *,
    reason: str,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Send one SSH action to ``secure_tool_broker`` without blocking the caller's event loop."""

    return await asyncio.to_thread(
        call_execution_ssh_broker_sync,
        action,
        params,
        reason=reason,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "EXECUTION_SSH_PROVIDER",
    "ExecutionSSHBrokerError",
    "REASON_BROKER_ERROR",
    "REASON_BROKER_UNAVAILABLE",
    "REASON_TASK_CONTEXT_MISSING",
    "broker_execution_ssh_available",
    "call_execution_ssh_broker",
    "call_execution_ssh_broker_sync",
]
