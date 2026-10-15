"""Pure builders for gateway automation-message submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Protocol

from assistant_gateway.delivery import (
    DeliveryPolicy,
    is_within_quiet_hours,
    next_digest_release_at,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import AutomationDeliveryPreferenceSnapshot, AutomationEnvelopeSnapshot, AutomationMessage, AutomationQuietHoursWindow
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import RequesterType


@dataclass(frozen=True)
class AutomationSessionRoute:
    """Session routing facts needed to build one automation request."""

    session_key: str
    conversation_id: str | None


@dataclass(frozen=True)
class AutomationSubmission:
    """Built automation request plus the state facts its caller must persist."""

    correlation_id: str
    session_key: str
    conversation_id: str | None
    followup_to_task_id: str | None
    request_text: str
    envelope: MessageEnvelope
    routing_key: str


class AutomationDefaultTarget(Protocol):
    """Default delivery route required by the automation snapshot builder."""

    @property
    def channel_name(self) -> str | None: ...

    @property
    def chat_id(self) -> str | None: ...

    @property
    def thread_id(self) -> str | None: ...


class AutomationDeliveryPolicy(DeliveryPolicy, Protocol):
    """Minimal delivery configuration used in an automation message."""

    @property
    def default_target(self) -> AutomationDefaultTarget: ...

    @property
    def summarize_deferred(self) -> bool: ...


def resolve_automation_timezone_name(
    preferred: str | None,
    fallback: str | None,
) -> str:
    candidate = preferred or fallback or "UTC"
    try:
        return ZoneInfo(candidate).key
    except ZoneInfoNotFoundError:
        return "UTC"


def _requester_local_time_iso(timezone_name: str, *, now_utc: datetime) -> str:
    return now_utc.astimezone(ZoneInfo(timezone_name)).isoformat()


def _snapshot_delivery_preferences(
    settings: AutomationDeliveryPolicy,
) -> AutomationDeliveryPreferenceSnapshot:
    return AutomationDeliveryPreferenceSnapshot(
        preferred_channel=settings.default_target.channel_name,
        preferred_output_chat_id=settings.default_target.chat_id,
        preferred_channel_thread_id=settings.default_target.thread_id,
        summarize_deferred=settings.summarize_deferred,
        digest_delivery_hour_local=settings.digest_delivery_hour_local,
        urgent_keywords=list(settings.urgent_keywords),
    )


def _snapshot_quiet_hours(
    settings: AutomationDeliveryPolicy,
    *,
    timezone_name: str,
    now_utc: datetime,
) -> AutomationQuietHoursWindow:
    quiet = settings.quiet_hours
    return AutomationQuietHoursWindow(
        timezone_name=timezone_name,
        enabled=quiet.enabled,
        start_hour_local=quiet.start_hour_local,
        end_hour_local=quiet.end_hour_local,
        allow_urgent_delivery=quiet.allow_urgent_delivery,
        currently_within_quiet_hours=is_within_quiet_hours(
            timezone_name=timezone_name,
            delivery_settings=settings,
            now_utc=now_utc,
        ),
    )


def _snapshot_envelope(envelope: MessageEnvelope) -> AutomationEnvelopeSnapshot:
    return AutomationEnvelopeSnapshot(
        msg_header=(
            envelope.msg_header.model_copy(deep=True) if envelope.msg_header else None
        ),
        msg_meta=(
            envelope.msg_meta.model_copy(deep=True) if envelope.msg_meta else None
        ),
        msg_content=envelope.msg_content.model_dump(mode="json"),
    )


def _default_suggested_later_time(
    settings: AutomationDeliveryPolicy,
    *,
    timezone_name: str,
    now_utc: datetime,
) -> str | None:
    if not settings.quiet_hours.enabled:
        return None
    return next_digest_release_at(
        timezone_name=timezone_name,
        delivery_settings=settings,
        now_utc=now_utc,
    )


def build_automation_submission(
    *,
    instance_name: str,
    response_queue_name: str,
    manager_agent_id: str,
    default_timezone: str,
    delivery_settings: AutomationDeliveryPolicy,
    now_utc: datetime,
    correlation_id: str,
    session: AutomationSessionRoute,
    gateway_context: GatewayContext,
    automation_id: str,
    hook_name: str,
    original_envelope: MessageEnvelope,
    rendered_original_user_text: str,
    allowed_decisions: list[str],
    decision_instructions: str,
    suggested_later_time: str | None = None,
    conversation_id: str | None = None,
    followup_to_task_id: str | None = None,
) -> AutomationSubmission:
    """Build one automation envelope without persistence or transport access."""

    snapshot_meta = original_envelope.msg_meta
    timezone_name = resolve_automation_timezone_name(
        snapshot_meta.request_timezone if snapshot_meta else None,
        default_timezone,
    )
    source_task_id = (
        original_envelope.msg_content.task_id
        if isinstance(original_envelope.msg_content, Task)
        else None
    )
    resolved_conversation_id = (
        conversation_id
        or (snapshot_meta.conversation_id if snapshot_meta else None)
        or session.conversation_id
    )
    resolved_followup_to_task_id = (
        followup_to_task_id
        or (snapshot_meta.followup_to_task_id if snapshot_meta else None)
        or source_task_id
    )
    automation_message = AutomationMessage(
        automation_id=automation_id,
        hook_name=hook_name,
        original_envelope_snapshot=_snapshot_envelope(original_envelope),
        rendered_original_user_text=rendered_original_user_text,
        delivery_preference_snapshot=_snapshot_delivery_preferences(delivery_settings),
        requester_local_time=_requester_local_time_iso(
            timezone_name,
            now_utc=now_utc,
        ),
        quiet_hours_window=_snapshot_quiet_hours(
            delivery_settings,
            timezone_name=timezone_name,
            now_utc=now_utc,
        ),
        suggested_later_time=(
            suggested_later_time
            or _default_suggested_later_time(
                delivery_settings,
                timezone_name=timezone_name,
                now_utc=now_utc,
            )
        ),
        allowed_decisions=list(allowed_decisions),
        decision_instructions=decision_instructions,
    )
    envelope = MessageEnvelope(
        msg_content=automation_message,
        msg_meta=MessageMeta(
            from_agent_name=f"{instance_name}_gateway",
            msg_correlation_id=correlation_id,
            conversation_id=resolved_conversation_id,
            followup_to_task_id=resolved_followup_to_task_id,
            request_timezone=timezone_name,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=True,
            gateway_context=gateway_context,
        ),
        msg_header=MessageHeader(
            from_agent_id=response_queue_name,
            to_routing_path=manager_agent_id,
            response_routing_key=response_queue_name,
            agent_signature="",
        ),
    )
    return AutomationSubmission(
        correlation_id=correlation_id,
        session_key=session.session_key,
        conversation_id=resolved_conversation_id,
        followup_to_task_id=resolved_followup_to_task_id,
        request_text=rendered_original_user_text,
        envelope=envelope,
        routing_key=manager_agent_id,
    )
