"""Shared authorization presentation-mode contract."""

from __future__ import annotations

from enum import StrEnum


class AuthorizationPresentationMode(StrEnum):
    """Choose plain-language or exact technical authorization copy."""

    FRIENDLY = "friendly"
    TECHNICAL = "technical"


def normalize_authorization_presentation_mode(
    value: object,
) -> AuthorizationPresentationMode:
    """Normalize configuration and application inputs to the shared enum."""

    if isinstance(value, AuthorizationPresentationMode):
        return value
    normalized = (
        str(value or AuthorizationPresentationMode.FRIENDLY.value).strip().lower()
    )
    try:
        return AuthorizationPresentationMode(normalized)
    except ValueError as exc:
        raise ValueError(
            "authorization.presentation.mode must be 'friendly' or 'technical'."
        ) from exc


__all__ = [
    "AuthorizationPresentationMode",
    "normalize_authorization_presentation_mode",
]
