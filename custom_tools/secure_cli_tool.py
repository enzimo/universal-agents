"""Structured client tool for credentialed CLI operations through a broker."""

from __future__ import annotations

import json
import os
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from strands import tool

from id_generation import new_id
from secure_tools.basic_providers import register_builtin_providers  # noqa: F401
from secure_tools.registry import get_provider, list_providers
from secure_tools.hub_providers import (
    HF_PROVIDER, X_PROVIDER, HUB_CLI_REQUEST_TIMEOUT_SECONDS, is_hub_write,
)


def _provider_action_summary(provider: Any) -> str:
    actions = getattr(provider, "actions", {}) or {}
    return ", ".join(sorted(str(name) for name in actions)) or "none"


def _tool_result(
    status: str, text: str, *, payload: dict[str, Any] | None = None
) -> Dict[str, Any]:
    content = [{"text": text}]
    if payload is not None:
        content.append({"text": json.dumps(payload, sort_keys=True)})
    return {"status": status, "content": content}


def _trusted_runtime_context() -> dict[str, Any] | None:
    return trusted_broker_context()


def trusted_broker_context(*, allow_system_work: bool = False) -> dict[str, Any] | None:
    """Who the active task works for, for a broker request, or None without an active task.

    Built inside the trusted tool boundary, never from model arguments; the
    broker trusts only the signed task context or the scheduled run identity
    in it (``access_control.task_identity``). Work for no person
    (system work) is allowed only when ``allow_system_work`` is set: web
    requests then use the team's account links.
    """

    # Imported lazily so assembling the tool registry does not load the runtime context.
    from custom_tools.runtime_context import (
        active_task_context,
        active_task_identity,
        get_agent_ref,
    )

    agent = get_agent_ref()
    if agent is None:
        return None
    task_id, task_meta = active_task_context(agent)
    gateway_context = (
        getattr(task_meta, "gateway_context", None) if task_meta is not None else None
    )
    canonical_user_id = str(
        getattr(gateway_context, "canonical_user_id", "") or ""
    ).strip()
    if not canonical_user_id and not allow_system_work:
        return None
    context = {
        "canonical_user_id": canonical_user_id,
        "principal_type": "user" if canonical_user_id else "system",
        "agent_id": str(getattr(agent, "id", "") or "").strip(),
        "team_id": str(getattr(agent, "team_id", "") or "").strip(),
        "task_id": str(task_id or "").strip(),
        "conversation_id": str(getattr(task_meta, "conversation_id", "") or "").strip(),
        "named_agent_type": str(getattr(agent, "named_agent_type", "") or "").strip(),
    }
    trusted: dict[str, Any] = {key: value for key, value in context.items() if value}
    trusted.update(active_task_identity(agent))
    return trusted


def _installed_cli(provider: str) -> Any:
    """A CLI the team installed (``cli:<name>`` in this agent's catalog), or None."""

    from custom_agents.access_runtime import runtime_access
    from custom_tools.runtime_context import get_agent_ref
    from secure_tools.installed_clis import installed_cli_provider

    agent = get_agent_ref()
    if agent is None:
        return None
    try:
        return installed_cli_provider(runtime_access(agent).catalog, provider)
    except RuntimeError:
        return None


def _secure_cli_impl(
    provider: str, action: str, params: dict[str, Any], reason: str
) -> Dict[str, Any]:
    """Run validated broker transport; model guidance lives in the mandatory workflow skill."""
    provider_impl = get_provider(provider) or _installed_cli(provider)
    if provider_impl is None:
        return _tool_result(
            "error",
            f"Unknown secure_cli provider: {provider}. Available providers: {', '.join(list_providers())}",
        )
    try:
        validated = provider_impl.validate(action, params or {})
    except Exception as exc:
        return _tool_result(
            "error",
            (
                f"Invalid secure_cli action: {exc}. "
                f"Available {provider_impl.name} actions: {_provider_action_summary(provider_impl)}"
            ),
        )

    base_url = (os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL") or "").rstrip("/")
    if not base_url:
        return _tool_result(
            "error",
            "SECURE_TOOL_BROKER_INTERNAL_BASE_URL is required when secure_cli is enabled.",
        )

    token = os.getenv("SECURE_TOOL_BROKER_TOKEN") or ""
    trusted_context = _trusted_runtime_context()
    if trusted_context is None:
        return _tool_result(
            "error",
            "canonical_user_required: secure_cli credential operations require an active registered canonical "
            "user. This is a runtime identity propagation error, not provider authentication; do not request "
            "re-authentication.",
            payload={
                "error": "canonical_user_required",
                "message": "secure_cli requires trusted canonical-user context from the active task.",
            },
        )
    body = {
        "provider": validated.provider,
        "action": validated.action,
        "params": dict(validated.params),
        "reason": str(reason or "").strip(),
        "context": {
            **trusted_context,
            # Generated inside the trusted tool boundary rather than supplied
            # by the model; it names this request in the broker's tool
            # context. Access decisions do not use it: the broker decides
            # each request once, and each request takes its own one-time use.
            "invocation_id": new_id("sec_cli_inv"),
        },
    }
    request = Request(
        f"{base_url}/v1/tools/{validated.provider}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        timeout = HUB_CLI_REQUEST_TIMEOUT_SECONDS if validated.provider in {X_PROVIDER, HF_PROVIDER} else 30
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            error_payload = {}
        if isinstance(error_payload, dict) and error_payload:
            message = str(
                error_payload.get("error")
                or f"secure_cli broker returned HTTP {exc.code}"
            )
            return _tool_result(
                "error",
                message,
                payload={"http_status": exc.code, **error_payload},
            )
        return _tool_result(
            "error",
            f"secure_cli broker returned HTTP {exc.code}: {detail[:2000]}",
        )
    except URLError as exc:
        if is_hub_write(validated.provider, validated.action):
            return _unknown_write_result()
        return _tool_result("error", f"secure_cli broker request failed: {exc}")
    except Exception as exc:
        if is_hub_write(validated.provider, validated.action):
            return _unknown_write_result()
        return _tool_result("error", f"secure_cli error: {exc}")

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        if is_hub_write(validated.provider, validated.action):
            return _unknown_write_result()
        return _tool_result(
            "error", f"secure_cli broker returned non-JSON output: {raw[:2000]}"
        )
    broker_status = str(payload.get("status") or "success").strip().lower()
    if broker_status != "success":
        message = str(payload.get("error") or "secure_cli broker action failed.")
        return _tool_result("error", message, payload=payload)
    return _tool_result(
        "success", "secure_cli broker action completed.", payload=payload
    )


def _unknown_write_result() -> Dict[str, Any]:
    return _tool_result(
        "error", "Write outcome unknown. Reconcile the remote result before another write; do not retry automatically.",
        payload={"error_code": "outcome_unknown", "retry_safe": False},
    )


@tool
def secure_cli(
    provider: str, action: str, params: dict[str, Any], reason: str
) -> Dict[str, Any]:
    """Execute one credentialed provider action through the secure broker.

    Before calling, use the mandatory `secure-credential-workflows` skill for
    the complete provider/action catalog, parameter rules, authentication
    recovery, credential collection/deletion, HTTP placeholder, and OpenShell
    lease workflows. Put provider-specific arguments inside `params`; never put
    raw secrets or user/owner identifiers in any argument. `reason` must be a
    concise, non-secret explanation of the requested operation.
    """
    return _secure_cli_impl(
        provider=provider, action=action, params=params, reason=reason
    )
