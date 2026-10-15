"""Human-readable requester-local timestamps for user-facing runtime text."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger(__name__)


def format_user_timestamp(
    value: datetime | str | None,
    *,
    timezone_name: str | None,
    empty: str = "-",
) -> str:
    """Render a stored timestamp in the requester's IANA timezone.

    Runtime storage and transport use ISO 8601 UTC values. This function is a
    presentation boundary: user-facing text gets a readable local value while
    persistence, APIs, and logs retain canonical machine-readable timestamps.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return empty

    parsed = _parse_timestamp(value)
    if parsed is None:
        return "Unavailable"

    display_timezone = _resolve_timezone(timezone_name)
    local_value = parsed.astimezone(display_timezone)
    timezone_label = local_value.tzname() or display_timezone.key
    hour = local_value.hour % 12 or 12
    meridiem = "AM" if local_value.hour < 12 else "PM"
    return (
        f"{local_value.strftime('%b')} {local_value.day}, {local_value.year} at "
        f"{hour}:{local_value.minute:02d}:{local_value.second:02d} "
        f"{meridiem} {timezone_label}"
    )


def _parse_timestamp(value: datetime | str) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        rendered = value.strip()
        if rendered.endswith("Z"):
            rendered = f"{rendered[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(rendered)
        except ValueError:
            LOGGER.warning(
                "Could not render invalid user-facing timestamp. "
                "event=user_timestamp_parse_failed value=%r",
                value,
            )
            return None
    if parsed.tzinfo is None:
        LOGGER.warning(
            "Assuming UTC for a timezone-naive stored timestamp. "
            "event=user_timestamp_timezone_missing value=%r",
            value,
        )
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_timezone(timezone_name: str | None) -> ZoneInfo:
    normalized = str(timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        LOGGER.warning(
            "Falling back to UTC for an invalid user timezone. "
            "event=user_timezone_invalid timezone=%r",
            normalized,
        )
        return ZoneInfo("UTC")
