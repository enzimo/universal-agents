"""Deterministic, configuration-independent delivery policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class QuietHoursPolicy(Protocol):
    """Quiet-hours values required by the delivery policy."""

    @property
    def enabled(self) -> bool: ...

    @property
    def start_hour_local(self) -> int: ...

    @property
    def end_hour_local(self) -> int: ...

    @property
    def allow_urgent_delivery(self) -> bool: ...


class DeliveryPolicy(Protocol):
    """Minimal delivery-policy view accepted by deterministic helpers."""

    @property
    def quiet_hours(self) -> QuietHoursPolicy: ...

    @property
    def digest_delivery_hour_local(self) -> int: ...

    @property
    def urgent_keywords(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class GatewayQuietHoursPolicy:
    """Application-owned snapshot of reloadable quiet-hours configuration."""

    enabled: bool
    start_hour_local: int
    end_hour_local: int
    allow_urgent_delivery: bool


@dataclass(frozen=True)
class GatewayDeliveryPolicy:
    """Application-owned snapshot used to decide one delivery outcome."""

    quiet_hours: GatewayQuietHoursPolicy
    digest_delivery_hour_local: int
    urgent_keywords: tuple[str, ...]


@dataclass
class DeliveryDecision:
    action: str
    available_after: str | None
    reason: str


def _now_in_tz(timezone_name: str, now_utc: datetime | None = None) -> datetime:
    current = now_utc or datetime.now(timezone.utc)
    return current.astimezone(ZoneInfo(timezone_name))


def is_within_quiet_hours(
    *,
    timezone_name: str,
    delivery_settings: DeliveryPolicy,
    now_utc: datetime | None = None,
) -> bool:
    quiet = delivery_settings.quiet_hours
    if not quiet.enabled:
        return False

    local_now = _now_in_tz(timezone_name, now_utc=now_utc)
    current_hour = local_now.hour
    start = quiet.start_hour_local
    end = quiet.end_hour_local
    if start == end:
        return True
    if start < end:
        return start <= current_hour < end
    return current_hour >= start or current_hour < end


def looks_urgent(text: str, delivery_settings: DeliveryPolicy) -> bool:
    haystack = (text or "").lower()
    return any(
        keyword.lower() in haystack for keyword in delivery_settings.urgent_keywords
    )


def next_digest_release_at(
    *,
    timezone_name: str,
    delivery_settings: DeliveryPolicy,
    now_utc: datetime | None = None,
) -> str:
    local_now = _now_in_tz(timezone_name, now_utc=now_utc)
    target = local_now.replace(
        hour=delivery_settings.digest_delivery_hour_local,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= local_now:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat()


def evaluate_delivery(
    *,
    origin_kind: str,
    rendered_text: str,
    timezone_name: str,
    delivery_settings: DeliveryPolicy,
    now_utc: datetime | None = None,
) -> DeliveryDecision:
    if origin_kind == "interactive":
        return DeliveryDecision(
            action="deliver_now", available_after=None, reason="interactive"
        )

    if not is_within_quiet_hours(
        timezone_name=timezone_name,
        delivery_settings=delivery_settings,
        now_utc=now_utc,
    ):
        return DeliveryDecision(
            action="deliver_now", available_after=None, reason="outside_quiet_hours"
        )

    if delivery_settings.quiet_hours.allow_urgent_delivery and looks_urgent(
        rendered_text,
        delivery_settings,
    ):
        return DeliveryDecision(
            action="deliver_now", available_after=None, reason="urgent_override"
        )

    return DeliveryDecision(
        action="defer_to_digest",
        available_after=next_digest_release_at(
            timezone_name=timezone_name,
            delivery_settings=delivery_settings,
            now_utc=now_utc,
        ),
        reason="quiet_hours",
    )


def summarize_delivery_batch(items: list[str]) -> str:
    if not items:
        return "No deferred items."
    lines = [f"Deferred summary ({len(items)} items):"]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item}")
    return "\n".join(lines)
