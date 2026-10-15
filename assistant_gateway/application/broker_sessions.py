"""Checks for the one-time form sessions the secure broker returns.

Credential collection and account links both answer with the same session
shape: a one-time browser form, a private status URL the gateway polls, and an
expiry. The gateway checks that shape before it shows any link to a person, so
a malformed or already-expired broker answer never reaches a chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class BrokerFormSession:
    """A validated broker form session; ``form_url`` is still the internal URL."""

    form_url: str
    session_id: str
    status_url: str
    expires_at: str


def validated_broker_form_session(
    payload: dict[str, Any], *, now: datetime
) -> BrokerFormSession:
    """Return the session fields, or raise ``ValueError`` naming what is wrong."""

    values = {
        "form_url": str(payload.get("form_url") or "").strip(),
        "session_id": str(payload.get("session_id") or "").strip(),
        "status_url": str(payload.get("status_url") or "").strip(),
        "expires_at": str(payload.get("expires_at") or "").strip(),
    }
    missing = sorted(key for key, value in values.items() if not value)
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    for key in ("form_url", "status_url"):
        validate_absolute_http_url(values[key], name=key)
    normalized_expiry = values["expires_at"].replace("Z", "+00:00")
    try:
        parsed_expiry = datetime.fromisoformat(normalized_expiry)
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed_expiry.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    if parsed_expiry <= now:
        raise ValueError("expires_at must be in the future")
    return BrokerFormSession(**values)


def validated_user_form_url(value: str) -> str:
    """Return the browser-facing form URL after checking it is a plain HTTP(S) URL."""

    normalized = str(value or "").strip()
    validate_absolute_http_url(normalized, name="public form URL")
    return normalized


def validate_absolute_http_url(value: str, *, name: str) -> None:
    """Reject control characters, credentials in the URL, and non-HTTP schemes."""

    if any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or any(character.isspace() for character in hostname)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
