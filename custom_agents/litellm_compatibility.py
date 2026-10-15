"""Compatibility routing helpers for OpenAI-compatible LiteLLM endpoints."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit


_SCHEMA_COMPATIBILITY_HOST_SUFFIXES = {
    "fireworks": ("fireworks.ai",),
    "novita": ("novita.ai",),
}


def api_base_hostname(api_base: object) -> Optional[str]:
    """Return a normalized API hostname without interpreting malformed URLs."""

    value = str(api_base or "").strip()
    if not value:
        return None
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return None
    return str(hostname).strip().lower() if hostname else None


def litellm_schema_compatibility_vendor(
    *,
    provider: object,
    model_id: object,
    api_base: object = None,
) -> Optional[str]:
    """Identify endpoint families that require the reduced UA JSON schemas.

    LiteLLM deployments may route a provider through either a provider-prefixed
    model id or a direct OpenAI-compatible API base with an unprefixed model id.
    Both are part of the effective route and must participate in compatibility
    selection.
    """

    if str(provider or "").strip().lower() != "litellm":
        return None

    normalized_model_id = str(model_id or "").strip().lower()
    for vendor in _SCHEMA_COMPATIBILITY_HOST_SUFFIXES:
        if vendor in normalized_model_id:
            return vendor

    hostname = api_base_hostname(api_base)
    if hostname is None:
        return None
    for vendor, suffixes in _SCHEMA_COMPATIBILITY_HOST_SUFFIXES.items():
        if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes):
            return vendor
    return None


def uses_litellm_compatible_schema(
    *,
    provider: object,
    model_id: object,
    api_base: object = None,
) -> bool:
    """Return whether the effective LiteLLM route needs reduced JSON schemas."""

    return litellm_schema_compatibility_vendor(
        provider=provider,
        model_id=model_id,
        api_base=api_base,
    ) is not None
