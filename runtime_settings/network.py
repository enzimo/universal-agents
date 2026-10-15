"""Network configuration and contracts."""

from __future__ import annotations
import os
from typing import Any, Mapping, Optional
from urllib.parse import urlparse
from pydantic import BaseModel, field_validator
from default_values import NETWORK_DEFAULTS
from runtime_settings.common import REMOVED_PUBLIC_URL_ENV_VARS, _render_url_host


def compose_public_base_url(public_origin: str, public_port: Optional[int]) -> str:
    """Compose a browser-facing base URL from the shared origin and a service port."""
    origin = str(public_origin or "").strip().rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("network.public_origin must be an absolute http(s) origin.")
    if parsed.username or parsed.password:
        raise ValueError("network.public_origin must not include credentials.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(
            "network.public_origin must not include path, params, query, or fragment."
        )
    if public_port is None:
        return f"{parsed.scheme}://{parsed.netloc.rstrip('/')}"
    parsed_port = int(public_port)
    if parsed_port < 1 or parsed_port > 65_535:
        raise ValueError("public ports must be between 1 and 65535.")
    return f"{parsed.scheme}://{_render_url_host(parsed.hostname)}:{parsed_port}"


def compose_secure_tool_broker_public_base_url(config: Any) -> str:
    """Return the configured browser-facing secure broker base URL."""
    return compose_public_base_url(
        getattr(
            getattr(config, "network", None),
            "public_origin",
            NETWORK_DEFAULTS.public_origin.value,
        ),
        getattr(getattr(config, "security_layer", None), "broker_public_port", None),
    ).rstrip("/")


def rewrite_url_to_public_base(url: str, *, public_base_url: str) -> str:
    """Replace an internal service URL origin with the configured public base."""
    if not url or not public_base_url:
        return url
    parsed = urlparse(url)
    suffix = parsed.path or ""
    if parsed.query:
        suffix = f"{suffix}?{parsed.query}"
    if parsed.fragment:
        suffix = f"{suffix}#{parsed.fragment}"
    return f"{public_base_url.rstrip('/')}{suffix}"


def reject_removed_public_url_env_vars(env: Optional[Mapping[str, str]] = None) -> None:
    env_mapping = env or os.environ
    present = sorted(key for key in REMOVED_PUBLIC_URL_ENV_VARS if key in env_mapping)
    if present:
        rendered = ", ".join(present)
        raise ValueError(
            f"Removed environment variable(s) are set: {rendered}. Use NETWORK_PUBLIC_ORIGIN, "
            "INTERNAL_WEBSITE_*, and SECURE_TOOL_BROKER_PUBLIC_PORT instead."
        )


class NetworkSettings(BaseModel):
    public_origin: str = NETWORK_DEFAULTS.public_origin.value

    @field_validator("public_origin", mode="before")
    @classmethod
    def _normalize_public_origin(cls, value: Any) -> str:
        normalized = (
            str(value or NETWORK_DEFAULTS.public_origin.value).strip().rstrip("/")
        )
        compose_public_base_url(normalized, None)
        return normalized
