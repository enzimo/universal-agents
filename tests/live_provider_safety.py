"""Credential-safe failure formatting for opt-in provider tests."""

from __future__ import annotations

from collections.abc import Mapping

from secure_tool_broker.redaction import redact_text


_SECRET_ENV_NAME_PARTS = ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "SESSION")


def sanitize_provider_failure(
    exc: Exception,
    *,
    environ: Mapping[str, str],
) -> str:
    """Return bounded exception text with known environment credentials removed."""

    message = redact_text(str(exc))
    for name, value in environ.items():
        if not any(marker in name.upper() for marker in _SECRET_ENV_NAME_PARTS):
            continue
        secret = str(value or "")
        if len(secret) >= 8:
            message = message.replace(secret, "[REDACTED]")
    message = " ".join(message.split())
    return message[:2_000] or "No provider error detail was returned."
