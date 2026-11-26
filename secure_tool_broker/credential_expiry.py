"""Shared temporary-credential expiry parsing, validation, and form helpers.

This module is pure and has no storage or HTTP dependencies so both the secure
credential collection form, broker storage/read paths, team transfer, and
future agent-captured temporary credentials share one exact rule set.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from secure_tool_broker.credential_diagnostics import (
    ACCOUNT_LINK_CREDENTIAL_EXPIRED,
    CREDENTIAL_EXPIRED,
)

# The form requires a chosen deletion time to be at least 5 minutes in the
# future. Storage paths (transfer import, automated capture, live tests) only
# require a timestamp strictly in the future so short TTLs remain possible.
MIN_EXPIRY_LEAD_SECONDS = 300

CUSTOM_EXPIRY_OPTION = "custom"

# Presets shown on the secure credential form. Month/quarter use fixed 30/90-day
# durations so preset behaviour is deterministic across calendar boundaries.
EXPIRY_PRESET_SECONDS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
    "90d": 90 * 24 * 60 * 60,
}

EXPIRY_PRESET_OPTIONS: tuple[tuple[str, str], ...] = (
    ("15m", "In 15 minutes"),
    ("1h", "In 1 hour"),
    ("1d", "In 1 day"),
    ("7d", "In 7 days"),
    ("30d", "In 1 month"),
    ("90d", "In 3 months"),
    (CUSTOM_EXPIRY_OPTION, "Choose a date and time"),
)

CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE = "credential_expiry_unsupported_source"
CREDENTIAL_EXPIRY_FORM_ONLY = "credential_expiry_form_only"
CREDENTIAL_EXPIRY_TOO_SOON = "credential_expiry_too_soon"
CREDENTIAL_EXPIRY_INVALID = "credential_expiry_invalid"

# IANA offsets span UTC-12 to UTC+14; allow +-14h (840 minutes).
_MAX_UTC_OFFSET_MINUTES = 14 * 60


class CredentialExpiryError(ValueError):
    """Raised when a temporary credential expiry timestamp or form input is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Reference clock 'now' must be timezone-aware.")
    return now.astimezone(timezone.utc)


def parse_utc_timestamp(value: datetime | str) -> datetime:
    """Parse a timezone-aware datetime or ISO-8601 string and normalize it to UTC."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CredentialExpiryError(
                CREDENTIAL_EXPIRY_INVALID,
                "Expiry timestamp must include a time zone.",
            )
        try:
            return value.astimezone(timezone.utc)
        except OverflowError as exc:
            raise CredentialExpiryError(
                CREDENTIAL_EXPIRY_INVALID,
                "Enter a valid deletion date and time.",
            ) from exc
    if not isinstance(value, str):
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Expiry timestamp must be an ISO-8601 UTC string or timezone-aware datetime.",
        )
    text = value.strip()
    if not text:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Expiry timestamp cannot be empty.",
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Expiry timestamp is not a valid ISO-8601 date and time.",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Expiry timestamp must include a time zone offset or 'Z'.",
        )
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Enter a valid deletion date and time.",
        ) from exc


def format_utc_timestamp(value: datetime | str) -> str:
    """Format a timezone-aware timestamp as whole-second UTC ISO-8601 ending in Z.

    Cutting sub-second microseconds down to the whole second keeps stored
    timestamps aligned with Sealbox's whole-second TTLs so the catalog expiry
    never lies after the Sealbox expiry.
    """

    parsed = parse_utc_timestamp(value).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def is_expired(expires_at: datetime | str | None, *, now: datetime | None = None) -> bool:
    """Return True once ``now`` has reached or passed ``expires_at``."""

    if expires_at is None:
        return False
    parsed = parse_utc_timestamp(expires_at)
    return _utc_now(now) >= parsed


def remaining_ttl_seconds(
    expires_at: datetime | str,
    *,
    now: datetime | None = None,
) -> int:
    """Whole seconds remaining until ``expires_at`` (0 when expired, else ceil >= 1).

    Rounding up guarantees Sealbox never expires a secret before the catalog's
    authoritative ``expires_at`` timestamp.
    """

    parsed = parse_utc_timestamp(expires_at)
    delta = (parsed - _utc_now(now)).total_seconds()
    if delta <= 0:
        return 0
    return max(1, math.ceil(delta))


def validate_storage_expiry(
    expires_at: datetime | str,
    *,
    now: datetime | None = None,
) -> datetime:
    """Validate an expiry timestamp for broker storage (strictly in the future, whole seconds)."""

    parsed = parse_utc_timestamp(expires_at).replace(microsecond=0)
    current = _utc_now(now)
    if parsed <= current:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_TOO_SOON,
            "Expiry time must be in the future.",
        )
    return parsed


def validate_form_expiry(
    expires_at: datetime | str,
    *,
    now: datetime | None = None,
    min_lead_seconds: int = MIN_EXPIRY_LEAD_SECONDS,
) -> datetime:
    """Validate an expiry timestamp chosen on the secure form (>= 5 minutes from now, whole seconds)."""

    parsed = parse_utc_timestamp(expires_at).replace(microsecond=0)
    current = _utc_now(now)
    remaining = (parsed - current).total_seconds()
    if remaining <= 0:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_TOO_SOON,
            "Choose an expiry time in the future (at least 5 minutes from now).",
        )
    if remaining < min_lead_seconds:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_TOO_SOON,
            "Expiry time must be at least 5 minutes from now.",
        )
    return parsed


def _parse_local_datetime(value: str | None) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Choose a deletion date and time.",
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Enter a valid deletion date and time.",
        ) from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return parsed


def _parse_js_offset_minutes_behind_utc(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Browser time zone offset is invalid.",
        )
    if isinstance(raw, int):
        minutes = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            minutes = int(text)
        except ValueError as exc:
            raise CredentialExpiryError(
                CREDENTIAL_EXPIRY_INVALID,
                "Browser time zone offset is invalid.",
            ) from exc
    if abs(minutes) > _MAX_UTC_OFFSET_MINUTES:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Browser time zone offset is out of range.",
        )
    return minutes


def resolve_form_expiry(
    *,
    enabled: bool,
    when: str | None,
    custom_local: str | None = None,
    tz_offset_minutes_behind_utc: str | int | None = None,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Resolve the secure form's expiry controls into a validated UTC datetime (or None).

    Presets are computed server-side at submission. Custom local date-time
    values are converted using ``tz_offset_minutes_behind_utc`` (JavaScript's
    ``Date.prototype.getTimezoneOffset()`` computed for the *selected* local
    date-time, where positive values are west of UTC so ``UTC = local + offset``)
    when provided; otherwise ``timezone_name`` via ``ZoneInfo`` is used. If
    neither a usable offset nor a usable IANA time zone arrives, the submission
    is refused rather than silently assuming UTC.
    """

    if not enabled:
        return None
    current = _utc_now(now)
    choice = str(when or "").strip()
    if choice in EXPIRY_PRESET_SECONDS:
        candidate = current + timedelta(seconds=EXPIRY_PRESET_SECONDS[choice])
        return validate_form_expiry(candidate, now=current)
    if choice != CUSTOM_EXPIRY_OPTION:
        raise CredentialExpiryError(
            CREDENTIAL_EXPIRY_INVALID,
            "Choose when this credential should be deleted.",
        )

    local_dt = _parse_local_datetime(custom_local)
    if local_dt.tzinfo is not None:
        return validate_form_expiry(local_dt, now=current)

    offset_minutes = _parse_js_offset_minutes_behind_utc(tz_offset_minutes_behind_utc)
    if offset_minutes is not None:
        # JS getTimezoneOffset() is (UTC - local) in minutes: e.g. UTC-5 -> +300,
        # UTC+2 -> -120. Adding offset_minutes to wall-clock local time yields UTC.
        try:
            utc_dt = (local_dt + timedelta(minutes=offset_minutes)).replace(tzinfo=timezone.utc)
        except OverflowError as exc:
            raise CredentialExpiryError(
                CREDENTIAL_EXPIRY_INVALID,
                "Enter a valid deletion date and time.",
            ) from exc
        return validate_form_expiry(utc_dt, now=current)

    zone_name = str(timezone_name or "").strip()
    if zone_name:
        try:
            tz = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise CredentialExpiryError(
                CREDENTIAL_EXPIRY_INVALID,
                "Could not recognize your browser's time zone. Choose a preset duration or try again.",
            ) from exc
        try:
            utc_dt = local_dt.replace(tzinfo=tz).astimezone(timezone.utc)
        except OverflowError as exc:
            raise CredentialExpiryError(
                CREDENTIAL_EXPIRY_INVALID,
                "Enter a valid deletion date and time.",
            ) from exc
        return validate_form_expiry(utc_dt, now=current)

    raise CredentialExpiryError(
        CREDENTIAL_EXPIRY_INVALID,
        "Could not determine your browser's time zone to convert the chosen date and time. "
        "Choose a preset duration instead.",
    )


def format_expiry_label(expires_at: datetime | str) -> str:
    """Short human-readable UTC timestamp for account-link pickers and labels."""

    parsed = parse_utc_timestamp(expires_at)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


__all__ = [
    "ACCOUNT_LINK_CREDENTIAL_EXPIRED",
    "CREDENTIAL_EXPIRED",
    "CREDENTIAL_EXPIRY_FORM_ONLY",
    "CREDENTIAL_EXPIRY_INVALID",
    "CREDENTIAL_EXPIRY_TOO_SOON",
    "CREDENTIAL_EXPIRY_UNSUPPORTED_SOURCE",
    "CUSTOM_EXPIRY_OPTION",
    "CredentialExpiryError",
    "EXPIRY_PRESET_OPTIONS",
    "EXPIRY_PRESET_SECONDS",
    "MIN_EXPIRY_LEAD_SECONDS",
    "format_expiry_label",
    "format_utc_timestamp",
    "is_expired",
    "parse_utc_timestamp",
    "remaining_ttl_seconds",
    "resolve_form_expiry",
    "validate_form_expiry",
    "validate_storage_expiry",
]
