"""xAI OAuth device login and refreshable credential storage.

The xAI OAuth bearer is intentionally kept out of YAML configuration.  A
Universal Agents instance owns a small, permission-restricted auth store and
resolves (and, when needed, refreshes) the bearer for each model request.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse

from env_secrets import get_env_with_file_fallback
from file_locking import FileLockUnavailable, exclusive_file_lock
from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

XAI_OAUTH_PROVIDER = "xai-oauth"
AUTH_STORE_LOCK_PURPOSE = "xAI sign-in store"
DEFAULT_XAI_OAUTH_MODEL = "grok-4.5"
DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_XAI_OAUTH_REFRESH_SKEW_SECONDS = 3600
DEFAULT_XAI_OAUTH_REFRESH_TIMEOUT_SECONDS = 20.0
SHORT_LIVED_TOKEN_THRESHOLD_SECONDS = 45 * 60
SHORT_LIVED_TOKEN_REFRESH_SKEW_SECONDS = 120
MAX_AUTH_RESPONSE_BYTES = 128 * 1024


class XAIOAuthError(RuntimeError):
    """Raised when xAI OAuth credentials cannot be obtained or refreshed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "xai_oauth_error",
        relogin_required: bool = False,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required
        self.http_status = http_status


@dataclass(frozen=True)
class XAIOAuthSettings:
    base_url: str = DEFAULT_XAI_OAUTH_BASE_URL
    auth_store_path: Optional[str] = None
    refresh_skew_seconds: int = DEFAULT_XAI_OAUTH_REFRESH_SKEW_SECONDS
    refresh_timeout_seconds: float = DEFAULT_XAI_OAUTH_REFRESH_TIMEOUT_SECONDS


@dataclass(frozen=True)
class XAIOAuthCredentials:
    provider: str
    base_url: str
    api_key: str
    source: str
    auth_mode: str
    auth_store_path: Optional[str] = None
    last_refresh: Optional[str] = None


@dataclass(frozen=True)
class XAIDeviceAuthorization:
    """Server-side state returned by xAI's device authorization endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    token_endpoint: str
    device_authorization_endpoint: str
    requested_at_epoch_seconds: float

    @property
    def expires_at_epoch_seconds(self) -> float:
        return self.requested_at_epoch_seconds + self.expires_in

    def public_payload(self) -> dict[str, Any]:
        """Return the browser-safe subset; device and bearer tokens stay server-side."""
        return {
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "verification_uri_complete": self.verification_uri_complete,
            "expires_in": self.expires_in,
            "interval": self.interval,
            "expires_at_epoch_seconds": self.expires_at_epoch_seconds,
        }


def default_xai_oauth_config() -> dict[str, Any]:
    return {
        "base_url": DEFAULT_XAI_OAUTH_BASE_URL,
        "auth_store_path": None,
        "refresh_skew_seconds": DEFAULT_XAI_OAUTH_REFRESH_SKEW_SECONDS,
        "refresh_timeout_seconds": DEFAULT_XAI_OAUTH_REFRESH_TIMEOUT_SECONDS,
    }


def normalize_xai_oauth_provider(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "xai-oauth",
        "x-ai-oauth",
        "xai grok oauth",
        "xai-grok-oauth",
        "grok-oauth",
    }
    return XAI_OAUTH_PROVIDER if normalized in aliases else normalized


def is_xai_oauth_provider(value: Any) -> bool:
    return normalize_xai_oauth_provider(value) == XAI_OAUTH_PROVIDER


def resolve_xai_oauth_model_id(
    provider: Any,
    requested_model_id: Any,
    *,
    inherited_provider: Any = None,
    inherited_model_id: Any = None,
) -> str:
    """Resolve model inheritance when selecting the xAI OAuth provider.

    A model explicitly selected for xAI remains authoritative.  An inherited
    model is preserved only when it already belongs to an xAI OAuth selection;
    switching from another provider without a model uses the xAI default.
    """
    requested = str(requested_model_id or "").strip()
    if requested:
        return requested
    inherited = str(inherited_model_id or "").strip()
    if is_xai_oauth_provider(provider):
        if is_xai_oauth_provider(inherited_provider) and inherited:
            return inherited
        return DEFAULT_XAI_OAUTH_MODEL
    return inherited


def resolve_xai_oauth_auth_store_path(
    configured_path: Any,
    *,
    instance_root: str | Path | None = None,
) -> Path:
    raw = str(configured_path or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path.resolve()
        if instance_root:
            return (Path(instance_root).expanduser().resolve() / path).resolve()
        return path.resolve()
    if instance_root:
        return InstanceLayout.at(instance_root).xai_oauth_auth_path
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "team_nats_agents" / "xai_oauth_auth.json").resolve()


def resolve_xai_oauth_settings(
    raw_settings: Mapping[str, Any] | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> XAIOAuthSettings:
    env_mapping = env if env is not None else os.environ
    merged = default_xai_oauth_config()
    if isinstance(raw_settings, Mapping):
        merged.update(raw_settings)

    raw_base_url = str(env_mapping.get("XAI_OAUTH_BASE_URL") or merged["base_url"]).strip().rstrip("/")
    base_url = validate_xai_url(raw_base_url or DEFAULT_XAI_OAUTH_BASE_URL, field="base_url")
    auth_store_path = env_mapping.get("XAI_OAUTH_AUTH_STORE_PATH") or merged.get("auth_store_path")
    refresh_skew = int(
        env_mapping.get("XAI_OAUTH_REFRESH_SKEW_SECONDS")
        or merged.get("refresh_skew_seconds")
        or DEFAULT_XAI_OAUTH_REFRESH_SKEW_SECONDS
    )
    refresh_timeout = float(
        env_mapping.get("XAI_OAUTH_REFRESH_TIMEOUT_SECONDS")
        or merged.get("refresh_timeout_seconds")
        or DEFAULT_XAI_OAUTH_REFRESH_TIMEOUT_SECONDS
    )
    resolved_store = resolve_xai_oauth_auth_store_path(
        auth_store_path,
        instance_root=instance_root,
    )
    return XAIOAuthSettings(
        base_url=base_url,
        auth_store_path=str(resolved_store),
        refresh_skew_seconds=max(0, refresh_skew),
        refresh_timeout_seconds=max(5.0, refresh_timeout),
    )


def validate_xai_url(url: str, *, field: str) -> str:
    """Pin every bearer-carrying or OAuth endpoint to HTTPS on x.ai."""
    candidate = str(url or "").strip().rstrip("/")
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or (host != "x.ai" and not host.endswith(".x.ai")):
        raise XAIOAuthError(
            f"xAI OAuth {field} must use HTTPS on x.ai or an x.ai subdomain; got {candidate!r}.",
            code="xai_oauth_endpoint_invalid",
        )
    if parsed.username or parsed.password:
        raise XAIOAuthError(
            f"xAI OAuth {field} must not contain embedded credentials.",
            code="xai_oauth_endpoint_invalid",
        )
    return candidate


def discover_xai_oauth_endpoints(*, timeout_seconds: float = 20.0) -> dict[str, str]:
    request = urllib_request.Request(
        XAI_OAUTH_DISCOVERY_URL,
        headers={"Accept": "application/json"},
        method="GET",
    )
    status, body = _urlopen_response(
        request,
        timeout_seconds=timeout_seconds,
        operation="xAI OIDC discovery",
        network_error_code="xai_oauth_discovery_network_error",
    )
    if status != 200:
        raise XAIOAuthError(
            f"xAI OIDC discovery failed with HTTP {status}.",
            code="xai_oauth_discovery_failed",
            http_status=status,
        )
    payload = _decode_json_object(body, operation="xAI OIDC discovery", code="xai_oauth_discovery_invalid")
    issuer = str(payload.get("issuer") or "").strip()
    if issuer and issuer.rstrip("/") != XAI_OAUTH_ISSUER:
        raise XAIOAuthError(
            f"xAI OIDC discovery returned unexpected issuer {issuer!r}.",
            code="xai_oauth_discovery_invalid",
        )
    token_endpoint = validate_xai_url(str(payload.get("token_endpoint") or ""), field="token_endpoint")
    device_endpoint = validate_xai_url(
        str(payload.get("device_authorization_endpoint") or XAI_OAUTH_DEVICE_CODE_URL),
        field="device_authorization_endpoint",
    )
    return {
        "token_endpoint": token_endpoint,
        "device_authorization_endpoint": device_endpoint,
    }


def request_xai_device_authorization(
    raw_settings: Mapping[str, Any] | XAIOAuthSettings | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    scope: str = XAI_OAUTH_SCOPE,
) -> XAIDeviceAuthorization:
    settings = (
        raw_settings
        if isinstance(raw_settings, XAIOAuthSettings)
        else resolve_xai_oauth_settings(
            raw_settings,
            instance_root=instance_root,
            env=env,
        )
    )
    discovery = discover_xai_oauth_endpoints(timeout_seconds=settings.refresh_timeout_seconds)
    endpoint = discovery["device_authorization_endpoint"]
    request = _form_request(endpoint, {"client_id": XAI_OAUTH_CLIENT_ID, "scope": scope})
    status, body = _urlopen_response(
        request,
        timeout_seconds=settings.refresh_timeout_seconds,
        operation="xAI device authorization",
        network_error_code="xai_oauth_device_network_error",
    )
    if status != 200:
        raise XAIOAuthError(
            _oauth_http_message("xAI device authorization failed", status, body),
            code="xai_oauth_device_request_failed",
            http_status=status,
        )
    payload = _decode_json_object(
        body,
        operation="xAI device authorization",
        code="xai_oauth_device_response_invalid",
    )
    required = ("device_code", "user_code", "verification_uri", "expires_in")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise XAIOAuthError(
            f"xAI device authorization response is missing: {', '.join(missing)}.",
            code="xai_oauth_device_response_invalid",
        )
    verification_uri = validate_xai_url(str(payload["verification_uri"]), field="verification_uri")
    verification_uri_complete = str(payload.get("verification_uri_complete") or verification_uri).strip()
    validate_xai_url(verification_uri_complete, field="verification_uri_complete")
    return XAIDeviceAuthorization(
        device_code=str(payload["device_code"]),
        user_code=str(payload["user_code"]),
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete,
        expires_in=max(1, int(payload["expires_in"])),
        interval=max(1, int(payload.get("interval") or 5)),
        token_endpoint=discovery["token_endpoint"],
        device_authorization_endpoint=endpoint,
        requested_at_epoch_seconds=time.time(),
    )


def poll_xai_device_token(
    authorization: XAIDeviceAuthorization,
    *,
    timeout_seconds: float = DEFAULT_XAI_OAUTH_REFRESH_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
    max_wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Poll xAI until approval, cancellation, denial, or device-code expiry."""
    endpoint = validate_xai_url(authorization.token_endpoint, field="token_endpoint")
    remaining_authorization_seconds = max(1.0, authorization.expires_at_epoch_seconds - time.time())
    bounded_wait_seconds = (
        remaining_authorization_seconds
        if max_wait_seconds is None
        else min(remaining_authorization_seconds, max(1.0, float(max_wait_seconds)))
    )
    deadline = time.time() + bounded_wait_seconds
    interval = max(1, authorization.interval)
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise XAIOAuthError(
                "xAI device authorization was cancelled.",
                code="xai_oauth_device_cancelled",
            )
        request = _form_request(
            endpoint,
            {
                "grant_type": XAI_OAUTH_DEVICE_GRANT,
                "client_id": XAI_OAUTH_CLIENT_ID,
                "device_code": authorization.device_code,
            },
        )
        status, body = _urlopen_response(
            request,
            timeout_seconds=timeout_seconds,
            operation="xAI device token polling",
            network_error_code="xai_oauth_device_network_error",
            return_http_errors=True,
        )
        if status == 200:
            payload = _decode_json_object(
                body,
                operation="xAI device token polling",
                code="xai_oauth_device_token_invalid",
            )
            if not str(payload.get("access_token") or "").strip() or not str(
                payload.get("refresh_token") or ""
            ).strip():
                raise XAIOAuthError(
                    "xAI device token response did not include both access_token and refresh_token.",
                    code="xai_oauth_device_token_invalid",
                    relogin_required=True,
                )
            return payload

        payload = _try_decode_json_object(body)
        error_code = str(payload.get("error") or "").strip()
        if error_code == "authorization_pending":
            _wait_for_poll_interval(interval, cancel_event)
            continue
        if error_code == "slow_down":
            interval = min(interval + 5, 30)
            _wait_for_poll_interval(interval, cancel_event)
            continue
        if error_code in {"expired_token", "access_denied", "authorization_declined"}:
            relogin_required = error_code == "expired_token"
            description = str(payload.get("error_description") or error_code).strip()
            raise XAIOAuthError(
                f"xAI device authorization failed: {description}.",
                code=f"xai_oauth_{error_code}",
                relogin_required=relogin_required,
                http_status=status,
            )
        raise XAIOAuthError(
            _oauth_http_message("xAI device token polling failed", status, body),
            code=error_code or "xai_oauth_device_token_failed",
            http_status=status,
        )
    raise XAIOAuthError(
        "Timed out waiting for xAI device authorization.",
        code="xai_oauth_device_expired",
        relogin_required=True,
    )


def save_xai_oauth_device_tokens(
    authorization: XAIDeviceAuthorization,
    token_payload: Mapping[str, Any],
    raw_settings: Mapping[str, Any] | XAIOAuthSettings | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    settings = (
        raw_settings
        if isinstance(raw_settings, XAIOAuthSettings)
        else resolve_xai_oauth_settings(
            raw_settings,
            instance_root=instance_root,
            env=env,
        )
    )
    access_token = str(token_payload.get("access_token") or "").strip()
    refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise XAIOAuthError(
            "xAI device token response did not include both access_token and refresh_token.",
            code="xai_oauth_device_token_invalid",
            relogin_required=True,
        )
    store_path = Path(str(settings.auth_store_path))
    discovery = {
        "token_endpoint": validate_xai_url(authorization.token_endpoint, field="token_endpoint"),
        "device_authorization_endpoint": validate_xai_url(
            authorization.device_authorization_endpoint,
            field="device_authorization_endpoint",
        ),
    }
    with _auth_store_lock(store_path):
        _write_owned_auth_store(
            store_path,
            _normalized_tokens(token_payload),
            discovery=discovery,
            source="device_code",
        )
    return store_path


def refresh_xai_oauth_tokens(
    tokens: Mapping[str, Any],
    *,
    token_endpoint: str = "",
    timeout_seconds: float = DEFAULT_XAI_OAUTH_REFRESH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise XAIOAuthError(
            "xAI OAuth auth store is missing refresh_token. Run xai-oauth-login again.",
            code="xai_oauth_missing_refresh_token",
            relogin_required=True,
        )
    endpoint = token_endpoint.strip()
    if not endpoint:
        endpoint = discover_xai_oauth_endpoints(timeout_seconds=timeout_seconds)["token_endpoint"]
    endpoint = validate_xai_url(endpoint, field="token_endpoint")
    request = _form_request(
        endpoint,
        {
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    status, body = _urlopen_response(
        request,
        timeout_seconds=timeout_seconds,
        operation="xAI token refresh",
        network_error_code="xai_oauth_refresh_network_error",
        return_http_errors=True,
    )
    if status != 200:
        _raise_refresh_http_error(status, body)
    payload = _decode_json_object(
        body,
        operation="xAI token refresh",
        code="xai_oauth_refresh_invalid_json",
    )
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XAIOAuthError(
            "xAI token refresh response was missing access_token.",
            code="xai_oauth_refresh_missing_access_token",
            relogin_required=True,
        )
    updated = dict(tokens)
    updated.update(payload)
    updated["access_token"] = access_token
    updated["refresh_token"] = str(payload.get("refresh_token") or refresh_token).strip()
    return _normalized_tokens(updated)


def resolve_xai_oauth_credentials(
    raw_settings: Mapping[str, Any] | XAIOAuthSettings | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
) -> XAIOAuthCredentials:
    env_mapping = env if env is not None else os.environ
    settings = (
        raw_settings
        if isinstance(raw_settings, XAIOAuthSettings)
        else resolve_xai_oauth_settings(
            raw_settings,
            instance_root=instance_root,
            env=env_mapping,
        )
    )
    env_token = get_env_with_file_fallback("XAI_OAUTH_ACCESS_TOKEN", env=env_mapping)
    if env_token:
        if _token_is_expiring({"access_token": env_token}, 0):
            raise XAIOAuthError(
                "XAI_OAUTH_ACCESS_TOKEN is expired. Run xai-oauth-login or replace the token.",
                code="xai_oauth_access_token_expired",
                relogin_required=True,
            )
        return XAIOAuthCredentials(
            provider=XAI_OAUTH_PROVIDER,
            base_url=settings.base_url,
            api_key=env_token,
            source="XAI_OAUTH_ACCESS_TOKEN",
            auth_mode="access_token",
        )

    store_path = Path(str(settings.auth_store_path))
    owned = _read_owned_auth_store(store_path)
    if not owned:
        raise XAIOAuthError(
            "xAI OAuth credentials are not available. Run `assistant_cli.py xai-oauth-login` "
            "or set XAI_OAUTH_ACCESS_TOKEN.",
            code="xai_oauth_credentials_unavailable",
            relogin_required=True,
        )
    tokens = dict(owned.get("tokens") or {})
    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise XAIOAuthError(
            "xAI OAuth auth store is missing access_token. Run xai-oauth-login again.",
            code="xai_oauth_missing_access_token",
            relogin_required=True,
        )

    should_refresh = _should_refresh_tokens(
        tokens,
        configured_skew_seconds=settings.refresh_skew_seconds,
        force_refresh=force_refresh,
        refresh_if_expiring=refresh_if_expiring,
    )
    if should_refresh:
        with _auth_store_lock(store_path):
            owned = _read_owned_auth_store(store_path) or owned
            tokens = dict(owned.get("tokens") or tokens)
            if _should_refresh_tokens(
                tokens,
                configured_skew_seconds=settings.refresh_skew_seconds,
                force_refresh=force_refresh,
                refresh_if_expiring=refresh_if_expiring,
            ):
                discovery = dict(owned.get("discovery") or {})
                token_endpoint = str(discovery.get("token_endpoint") or "").strip()
                if token_endpoint:
                    validate_xai_url(token_endpoint, field="token_endpoint")
                else:
                    refreshed_discovery = discover_xai_oauth_endpoints(
                        timeout_seconds=settings.refresh_timeout_seconds
                    )
                    discovery.update(refreshed_discovery)
                    token_endpoint = refreshed_discovery["token_endpoint"]
                tokens = refresh_xai_oauth_tokens(
                    tokens,
                    token_endpoint=token_endpoint,
                    timeout_seconds=settings.refresh_timeout_seconds,
                )
                _write_owned_auth_store(
                    store_path,
                    tokens,
                    discovery=discovery,
                    source="refresh",
                    auth_mode=str(owned.get("auth_mode") or "oauth_device_code"),
                )
                owned = _read_owned_auth_store(store_path) or owned

    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token or _token_is_expiring(tokens, 0):
        raise XAIOAuthError(
            "xAI OAuth access token is expired and could not be refreshed. Run xai-oauth-login again.",
            code="xai_oauth_access_token_expired",
            relogin_required=True,
        )
    return XAIOAuthCredentials(
        provider=XAI_OAUTH_PROVIDER,
        base_url=settings.base_url,
        api_key=access_token,
        source="auth_store",
        auth_mode=str(owned.get("auth_mode") or "oauth_device_code"),
        auth_store_path=str(store_path),
        last_refresh=str(owned.get("last_refresh") or "") or None,
    )


def make_xai_oauth_api_key_provider(
    raw_settings: Mapping[str, Any] | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Callable[[], Awaitable[str]]:
    """Build an OpenAI-SDK async key provider that refreshes per request."""
    captured_settings = dict(raw_settings or {})
    captured_env = dict(env) if env is not None else None

    async def resolve_api_key() -> str:
        credentials = await asyncio.to_thread(
            resolve_xai_oauth_credentials,
            captured_settings,
            instance_root=instance_root,
            env=captured_env,
        )
        return credentials.api_key

    return resolve_api_key


def xai_oauth_status(
    raw_settings: Mapping[str, Any] | XAIOAuthSettings | None = None,
    *,
    instance_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return credential metadata without ever returning bearer material."""
    settings = (
        raw_settings
        if isinstance(raw_settings, XAIOAuthSettings)
        else resolve_xai_oauth_settings(
            raw_settings,
            instance_root=instance_root,
            env=env,
        )
    )
    try:
        credentials = resolve_xai_oauth_credentials(
            settings,
            env=env,
            force_refresh=refresh,
        )
    except XAIOAuthError as exc:
        return {
            "provider": XAI_OAUTH_PROVIDER,
            "authenticated": False,
            "base_url": settings.base_url,
            "auth_store_path": settings.auth_store_path,
            "error_code": exc.code,
            "message": str(exc),
            "relogin_required": exc.relogin_required,
        }
    return {
        "provider": XAI_OAUTH_PROVIDER,
        "authenticated": True,
        "base_url": credentials.base_url,
        "source": credentials.source,
        "auth_mode": credentials.auth_mode,
        "auth_store_path": credentials.auth_store_path or settings.auth_store_path,
        "last_refresh": credentials.last_refresh,
    }


def _should_refresh_tokens(
    tokens: Mapping[str, Any],
    *,
    configured_skew_seconds: int,
    force_refresh: bool,
    refresh_if_expiring: bool,
) -> bool:
    if force_refresh:
        return True
    if not refresh_if_expiring:
        return False
    effective_skew = _proactive_refresh_skew_seconds(tokens, configured_skew_seconds)
    return _token_is_expiring(tokens, effective_skew)


def _proactive_refresh_skew_seconds(tokens: Mapping[str, Any], configured_skew_seconds: int) -> int:
    max_skew = max(0, int(configured_skew_seconds))
    expires_at = _token_expiration_epoch_seconds(tokens)
    if expires_at is None:
        return max_skew
    remaining = expires_at - time.time()
    if 0 < remaining <= SHORT_LIVED_TOKEN_THRESHOLD_SECONDS:
        return min(SHORT_LIVED_TOKEN_REFRESH_SKEW_SECONDS, max_skew)
    return max_skew


def _token_is_expiring(tokens: Mapping[str, Any], skew_seconds: int) -> bool:
    expires_at = _token_expiration_epoch_seconds(tokens)
    if expires_at is None:
        return False
    return expires_at <= time.time() + max(0, int(skew_seconds))


def _token_expiration_epoch_seconds(tokens: Mapping[str, Any]) -> Optional[float]:
    access_token = str(tokens.get("access_token") or "").strip()
    claims = decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    expires_at = tokens.get("expires_at_epoch_seconds")
    if isinstance(expires_at, (int, float)):
        return float(expires_at)
    obtained_at = tokens.get("obtained_at_epoch_seconds")
    expires_in = tokens.get("expires_in")
    if isinstance(obtained_at, (int, float)) and isinstance(expires_in, (int, float)):
        return float(obtained_at) + float(expires_in)
    return None


def decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = str(token).split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _normalized_tokens(tokens: Mapping[str, Any]) -> dict[str, Any]:
    now_epoch = time.time()
    result: dict[str, Any] = {
        "access_token": str(tokens.get("access_token") or "").strip(),
        "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        "token_type": str(tokens.get("token_type") or "Bearer").strip() or "Bearer",
    }
    id_token = str(tokens.get("id_token") or "").strip()
    if id_token:
        result["id_token"] = id_token
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and float(expires_in) > 0:
        result["expires_in"] = int(expires_in)
        result["obtained_at_epoch_seconds"] = now_epoch
        result["expires_at_epoch_seconds"] = now_epoch + float(expires_in)
    else:
        existing_obtained = tokens.get("obtained_at_epoch_seconds")
        existing_expires_at = tokens.get("expires_at_epoch_seconds")
        if isinstance(existing_obtained, (int, float)):
            result["obtained_at_epoch_seconds"] = float(existing_obtained)
        if isinstance(existing_expires_at, (int, float)):
            result["expires_at_epoch_seconds"] = float(existing_expires_at)
    return result


def _read_owned_auth_store(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise XAIOAuthError(
            f"xAI OAuth auth store at {path} is unreadable.",
            code="xai_oauth_store_unreadable",
            relogin_required=True,
        ) from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("tokens"), Mapping):
        raise XAIOAuthError(
            f"xAI OAuth auth store at {path} has an invalid shape.",
            code="xai_oauth_store_invalid",
            relogin_required=True,
        )
    provider = str(payload.get("provider") or XAI_OAUTH_PROVIDER)
    if provider != XAI_OAUTH_PROVIDER:
        raise XAIOAuthError(
            f"Auth store at {path} belongs to provider {provider!r}, not xai-oauth.",
            code="xai_oauth_store_invalid",
            relogin_required=True,
        )
    return dict(payload)


def _write_owned_auth_store(
    path: Path,
    tokens: Mapping[str, Any],
    *,
    discovery: Mapping[str, Any],
    source: str,
    auth_mode: str = "oauth_device_code",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    validated_discovery: dict[str, str] = {}
    for key in ("token_endpoint", "device_authorization_endpoint"):
        value = str(discovery.get(key) or "").strip()
        if value:
            validated_discovery[key] = validate_xai_url(value, field=key)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "version": 1,
        "provider": XAI_OAUTH_PROVIDER,
        "auth_mode": auth_mode,
        "source": source,
        "last_refresh": now,
        "discovery": validated_discovery,
        "tokens": _normalized_tokens(tokens),
    }
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        with contextlib.suppress(OSError):
            tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


@contextlib.contextmanager
def _auth_store_lock(path: Path) -> Iterator[None]:
    """Hold the sign-in store's lock while its tokens are refreshed or replaced.

    Agent processes, the onboarding server and ``assistant_cli.py`` share this
    store, and xAI replaces the refresh token on every refresh, so two unlocked
    refreshes can sign the whole team out. A lock failure therefore stops the
    work (``file_locking``) and is reported as an ``XAIOAuthError`` that does
    not ask for a new sign-in, since signing in again cannot fix the folder.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(exclusive_file_lock(path, purpose=AUTH_STORE_LOCK_PURPOSE))
        except FileLockUnavailable as exc:
            raise XAIOAuthError(
                f"{exc} Signing in again will not help until this is fixed.",
                code="xai_oauth_auth_store_lock_unavailable",
                relogin_required=False,
            ) from exc
        yield


def _form_request(url: str, values: Mapping[str, Any]) -> urllib_request.Request:
    return urllib_request.Request(
        validate_xai_url(url, field="request endpoint"),
        data=urlencode({key: str(value) for key, value in values.items()}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )


def _urlopen_response(
    request: urllib_request.Request,
    *,
    timeout_seconds: float,
    operation: str,
    network_error_code: str,
    return_http_errors: bool = False,
) -> tuple[int, str]:
    try:
        with urllib_request.urlopen(request, timeout=max(5.0, float(timeout_seconds))) as response:
            return int(response.status), response.read(MAX_AUTH_RESPONSE_BYTES).decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        body = exc.read(MAX_AUTH_RESPONSE_BYTES).decode("utf-8", errors="replace")
        if return_http_errors:
            return int(exc.code), body
        raise XAIOAuthError(
            _oauth_http_message(f"{operation} failed", int(exc.code), body),
            code=network_error_code.replace("network_error", "http_error"),
            http_status=int(exc.code),
        ) from exc
    except urllib_error.URLError as exc:
        raise XAIOAuthError(
            f"{operation} failed: {exc.reason}",
            code=network_error_code,
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise XAIOAuthError(
            f"{operation} failed: {exc}",
            code=network_error_code,
        ) from exc


def _decode_json_object(body: str, *, operation: str, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise XAIOAuthError(f"{operation} returned invalid JSON.", code=code) from exc
    if not isinstance(payload, Mapping):
        raise XAIOAuthError(f"{operation} did not return a JSON object.", code=code)
    return dict(payload)


def _try_decode_json_object(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _oauth_http_message(prefix: str, status: int, body: str) -> str:
    payload = _try_decode_json_object(body)
    detail = payload.get("error_description") or payload.get("message") or payload.get("error")
    suffix = f": {detail}" if isinstance(detail, str) and detail.strip() else ""
    return f"{prefix} with HTTP {status}{suffix}."


def _raise_refresh_http_error(status: int, body: str) -> None:
    payload = _try_decode_json_object(body)
    error_code = str(payload.get("error") or "").strip()
    detail = str(payload.get("error_description") or payload.get("message") or error_code).strip()
    if status == 403:
        raise XAIOAuthError(
            "xAI OAuth refresh was denied by the account tier or API entitlement"
            + (f": {detail}" if detail else "."),
            code="xai_oauth_tier_denied",
            relogin_required=False,
            http_status=status,
        )
    if status == 429:
        raise XAIOAuthError(
            "xAI OAuth refresh was rate limited; retry later.",
            code="xai_oauth_refresh_rate_limited",
            relogin_required=False,
            http_status=status,
        )
    relogin_required = status in {400, 401} or error_code in {
        "invalid_grant",
        "invalid_token",
        "invalid_request",
    }
    raise XAIOAuthError(
        "xAI OAuth token refresh failed"
        + (f": {detail}." if detail else f" with HTTP {status}."),
        code=error_code or "xai_oauth_refresh_failed",
        relogin_required=relogin_required,
        http_status=status,
    )


def _wait_for_poll_interval(seconds: int, cancel_event: threading.Event | None) -> None:
    if cancel_event is None:
        time.sleep(seconds)
        return
    if cancel_event.wait(timeout=seconds):
        raise XAIOAuthError(
            "xAI device authorization was cancelled.",
            code="xai_oauth_device_cancelled",
        )
