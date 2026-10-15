"""HTTP adapter for gateway-assisted secure credential workflows."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

import aiohttp

from assistant_gateway.application.ports import GatewaySecureBrokerRefusal
from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.network import compose_secure_tool_broker_public_base_url, rewrite_url_to_public_base
from secure_tool_broker.defaults import (
    MODEL_KEY_STATUS_ROUTE,
    ONBOARDING_ACCOUNT_KEYS_ROUTE,
    PERSON_CREDENTIAL_DELETION_ROUTE,
    resolve_secure_tool_broker_internal_base_url,
)


async def _call_broker_json(
    *,
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one auth API call inside the broker infrastructure adapter."""

    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.request(
                method,
                f"{base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            ) as response:
                raw = await response.text()
                if response.status >= 400:
                    # A refusal keeps the broker's own plain message and reason
                    # code (GatewaySecureBrokerRefusal is a RuntimeError).
                    body = _parse_json_body(raw)
                    detail, reason_code = _error_detail(body, raw)
                    if not detail and isinstance(body, dict):
                        detail = str(body.get("message") or "").strip()
                    raise GatewaySecureBrokerRefusal(
                        detail or f"Broker returned HTTP {response.status}.",
                        status=response.status,
                        reason_code=reason_code,
                    )
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"Broker request failed: {exc}") from exc
    response_payload = json.loads(raw) if raw else {}
    if not isinstance(response_payload, dict):
        raise RuntimeError("Secure broker returned a non-object response.")
    return {str(key): value for key, value in response_payload.items()}


class _JsonRequestModel(Protocol):
    """A request contract this adapter sends as JSON without knowing its type.

    The application port names the concrete contract (``AccountLinkRequestV1``);
    this HTTP adapter stays free of message-contract imports.
    """

    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


def _parse_json_body(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return None


def _error_detail(payload: Any, raw: str) -> tuple[str, str | None]:
    """Return the broker's error message and reason code, if it sent them."""

    if isinstance(payload, dict):
        reason_code = str(payload.get("reason_code") or "").strip() or None
        return str(payload.get("error") or "").strip(), reason_code
    return raw.strip()[:500], None


class HttpGatewaySecureBroker:
    """Authenticated secure-broker client with no gateway-state access."""

    def __init__(
        self,
        *,
        internal_base_url: str | None,
        public_base_url: str | None,
        token: str | None,
        collection_ttl_seconds: int = 900,
        logger: logging.Logger | None = None,
    ) -> None:
        self._internal_base_url = str(internal_base_url or "").rstrip("/") or None
        self._public_base_url = str(public_base_url or "").rstrip("/") or None
        self._token = str(token or "").strip() or None
        self._collection_ttl_seconds = max(60, int(collection_ttl_seconds))
        self._logger = logger or logging.getLogger(__name__)

    @classmethod
    def from_gateway_config(
        cls,
        config: GatewayInstanceConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> "HttpGatewaySecureBroker":
        security_layer = config.security_layer
        internal_base_url = os.getenv(
            "SECURE_TOOL_BROKER_INTERNAL_BASE_URL"
        ) or resolve_secure_tool_broker_internal_base_url(
            security_layer.broker_internal_base_url
        )
        public_base_url = compose_secure_tool_broker_public_base_url(config)
        try:
            collection_ttl_seconds = int(
                os.getenv("SECURE_TOOL_BROKER_COLLECTION_SESSION_TTL_SECONDS") or 900
            )
        except ValueError:
            collection_ttl_seconds = 900
        return cls(
            internal_base_url=internal_base_url,
            public_base_url=public_base_url,
            token=os.getenv("SECURE_TOOL_BROKER_TOKEN", "").strip() or None,
            collection_ttl_seconds=collection_ttl_seconds,
            logger=logger,
        )

    @property
    def available(self) -> bool:
        return bool(self._internal_base_url and self._token)

    @property
    def collection_ttl_seconds(self) -> int:
        return self._collection_ttl_seconds

    @property
    def auth_unavailable_message(self) -> str | None:
        if not self._internal_base_url:
            return "Secure tool broker is not enabled for this gateway."
        if not self._token:
            return "SECURE_TOOL_BROKER_TOKEN is not available to the gateway process."
        return None

    def user_form_url(self, internal_url: str) -> str:
        return str(
            rewrite_url_to_public_base(
                internal_url,
                public_base_url=self._public_base_url or "",
            )
        )

    async def get_auth_status(
        self,
        *,
        provider: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        base_url, token = self._require_connection()
        path = "/v1/auth/status" if provider is None else f"/v1/auth/status/{provider}"
        return await _call_broker_json(
            base_url=base_url,
            token=token,
            method="POST",
            path=path,
            payload={"context": context},
        )

    async def configure_bitwarden_server(
        self,
        *,
        server_url: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        base_url, token = self._require_connection()
        return await _call_broker_json(
            base_url=base_url,
            token=token,
            method="POST",
            path="/v1/auth/bitwarden/server",
            payload={"server_url": server_url, "context": context},
        )

    async def create_auth_session(
        self,
        *,
        provider: str,
        method: str | None,
        context: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url, token = self._require_connection()
        payload: dict[str, Any] = {**(options or {}), "context": context}
        if method:
            payload["method"] = method
        self._logger.info(
            "Requesting broker auth session. "
            "event=gateway_broker_auth_session_request provider=%s method=%s",
            provider,
            method or "",
        )
        return await _call_broker_json(
            base_url=base_url,
            token=token,
            method="POST",
            path=f"/v1/auth/{provider}/sessions",
            payload=payload,
        )

    async def get_model_key_status(self) -> dict[str, Any]:
        base_url, token = self._require_connection()
        return await _call_broker_json(base_url=base_url, token=token, method="GET", path=MODEL_KEY_STATUS_ROUTE)

    async def create_collection_session(
        self,
        *,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        base_url, token = self._require_connection()
        return await self._request_json(
            "POST",
            f"{base_url}/v1/credential-collection/sessions",
            token=token,
            payload={"request": request, "context": context},
            timeout_seconds=15,
        )

    async def create_account_link_session(
        self, request: _JsonRequestModel
    ) -> dict[str, Any]:
        """Open the broker's account link form for the person in ``request``.

        Only the gateway's broker token may call this route: the gateway
        vouches for the person and their current team roles. A refusal
        raises ``GatewaySecureBrokerRefusal`` carrying the broker's message
        and ``reason_code``. The caller (``GatewayAccountLinks``) logs the
        request and its outcome.
        """

        base_url, token = self._require_connection()
        return await self._request_json(
            "POST",
            f"{base_url}/v1/account-link/sessions",
            token=token,
            payload=request.model_dump(mode="json"),
            timeout_seconds=15,
        )

    async def delete_person_credentials(
        self, request: _JsonRequestModel
    ) -> dict[str, Any]:
        """Delete a removed person's stored credentials (``PersonCredentialDeletionV1``).

        Only the gateway's broker token may call this route
        (``secure_tool_broker.departed_person_credentials``).
        """

        base_url, token = self._require_connection()
        return await self._request_json(
            "POST",
            f"{base_url}{PERSON_CREDENTIAL_DELETION_ROUTE}",
            token=token,
            payload=request.model_dump(mode="json"),
            timeout_seconds=60,
        )

    async def link_onboarding_account_keys(
        self, request: _JsonRequestModel
    ) -> dict[str, Any]:
        """Link the keys typed during onboarding as the team's accounts (``OnboardingAccountKeysV1``).

        The body holds key values. Only the gateway's broker token may call
        this route (``secure_tool_broker.onboarding_account_keys``); nothing
        here logs the request.
        """

        base_url, token = self._require_connection()
        return await self._request_json(
            "POST",
            f"{base_url}{ONBOARDING_ACCOUNT_KEYS_ROUTE}",
            token=token,
            payload=request.model_dump(mode="json"),
            timeout_seconds=60,
        )

    async def execute_approved_signup(
        self,
        *,
        request: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        base_url, token = self._require_connection()
        payload = await self._request_json(
            "POST",
            f"{base_url}/v1/tools/credential_catalog",
            token=token,
            payload={
                "provider": "credential_catalog",
                "action": "signup.execute_approved",
                "params": request,
                "reason": (
                    "Attempt approved signup or classify manual fallback boundary."
                ),
                "context": context,
            },
            timeout_seconds=30,
            raise_for_status=False,
        )
        return payload if isinstance(payload, dict) else None

    async def get_collection_status(
        self,
        *,
        status_url: str,
        session_id: str,
    ) -> dict[str, Any]:
        base_url, token = self._require_connection()
        resolved_url = (
            status_url or f"{base_url}/v1/credential-collection/sessions/{session_id}"
        )
        return await self._request_json(
            "GET",
            resolved_url,
            token=token,
            timeout_seconds=10,
        )

    async def get_account_link_status(
        self,
        *,
        status_url: str,
        session_id: str,
    ) -> dict[str, Any]:
        base_url, token = self._require_connection()
        resolved_url = (
            status_url or f"{base_url}/v1/account-link/sessions/{session_id}"
        )
        return await self._request_json(
            "GET",
            resolved_url,
            token=token,
            timeout_seconds=10,
        )

    async def get_sign_in_status(self, *, status_url: str) -> dict[str, Any]:
        """Read a sign-in page's status from the broker's internal address.

        ``status_url`` is the page's broker-internal address plus ``/status``
        (``GatewayBrokerSignIns``); it is never a browser-facing address. The
        route is the page's own (its script reads it), so no broker token is
        sent.
        """

        self._require_connection()
        return await self._request_json("GET", status_url, token=None, timeout_seconds=10)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        token: str | None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float,
        raise_for_status: bool = True,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.request(
                method,
                url,
                headers=headers,
                json=payload,
            ) as response:
                if response.status < 400:
                    response_payload = await response.json()
                else:
                    # Broker refusals may arrive as a JSON object with
                    # ``error``/``reason_code`` sent as text/plain, or as plain
                    # text from aiohttp's HTTP exceptions, so read raw text.
                    raw = await response.text()
                    response_payload = _parse_json_body(raw)
                    detail, reason_code = _error_detail(response_payload, raw)
                    if raise_for_status:
                        raise GatewaySecureBrokerRefusal(
                            detail or f"HTTP {response.status}",
                            status=response.status,
                            reason_code=reason_code,
                        )
                    self._logger.warning(
                        "Secure broker request failed. "
                        "event=gateway_secure_broker_request_failed "
                        "method=%s url=%s status=%s reason_code=%s detail=%s",
                        method,
                        url,
                        response.status,
                        reason_code or "-",
                        detail,
                    )
                if not isinstance(response_payload, dict):
                    raise RuntimeError("Secure broker returned a non-object response.")
                return response_payload

    def _require_connection(self) -> tuple[str, str]:
        if not self._internal_base_url or not self._token:
            raise RuntimeError("Secure broker connection is not configured.")
        return self._internal_base_url, self._token
