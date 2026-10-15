"""Prompt rendering for delivery settings owned by instance_config.yaml."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from assistant_gateway.config import DeliverySettings, GatewayInstanceConfig
from custom_messages.contracts.routing_v1 import GatewayContext


def default_notification_gateway_context(config: GatewayInstanceConfig) -> GatewayContext:
    """Build the configured owner's proactive route for tools and runtime notices."""
    target = config.delivery.default_target
    channel_name = str(target.channel_name or "cli").strip().lower() or "cli"
    chat_id = str(target.chat_id or "").strip()
    if channel_name == "cli" and not chat_id:
        chat_id = f"cli:{config.instance_name}"
    return GatewayContext(
        gateway_instance_name=config.instance_name,
        session_key=config.user.default_session_key,
        channel_name=channel_name,
        user_id=config.user.canonical_user_id,
        canonical_user_id=config.user.canonical_user_id,
        chat_id=chat_id,
        thread_id=str(target.thread_id or "").strip() or None,
        delivery_mode="agent_notification",
        schedule_owner_agent_id=config.manager.manager_agent_id,
        heartbeat_source_id=config.heartbeat.source_id,
    )


def configured_owner_route(
    path: str | Path | None, owner: str
) -> tuple[str, GatewayContext] | None:
    """The gateway route and chat of the instance's configured user, for ``owner`` only.

    Runtime notices about a person's work use it when the work carries no chat
    route of its own. It never routes another person's work to the instance's
    default user, and returns None when no chat is configured.
    """
    if not path:
        return None
    config = GatewayInstanceConfig.load_for_process(path)
    if config.user.canonical_user_id != owner:
        return None
    context = default_notification_gateway_context(config)
    route = str(config.manager.response_queue_name or "").strip()
    if not route or (
        context.channel_name not in {"cli", "local"} and not context.chat_id
    ):
        return None
    return route, context


def render_delivery_settings_context(
    delivery: DeliverySettings,
    *,
    source_path: Optional[Path] = None,
) -> str:
    quiet_hours = delivery.quiet_hours
    default_target = delivery.default_target
    notification_routing = getattr(delivery, "notification_routing", None)
    notification_routing_mode = getattr(notification_routing, "mode", "source_thread_or_default")
    lines = ["Instance Delivery Context:"]
    if source_path is not None:
        lines.append(f"- Source: {source_path}")
    lines.extend(
        [
            f"- Quiet hours enabled: {str(quiet_hours.enabled).lower()}",
            f"- Quiet hours: {quiet_hours.start_hour_local}:00-{quiet_hours.end_hour_local}:00 local time",
            f"- Urgent delivery allowed during quiet hours: {str(quiet_hours.allow_urgent_delivery).lower()}",
            f"- Digest release hour: {delivery.digest_delivery_hour_local}:00 local time",
            f"- Default delivery channel: {default_target.channel_name}",
            f"- Default delivery chat id: {default_target.chat_id or '(not set)'}",
            f"- Default delivery thread/topic id: {default_target.thread_id or '(not set)'}",
            f"- Notification routing mode: {notification_routing_mode}",
            "- User communication profile: notify_user target='auto' follows notification routing mode. source_thread_or_default returns notifications to the active interactive chat/thread when available; default_target uses the default delivery target.",
            "- Preferred user communication tool: notify_user(message, urgency='normal'|'urgent', reason='short_audit_reason', target='auto'|'default'|'current_thread').",
            "- For screenshots or other binary outputs, save the file locally first and pass notify_user attachments with local file paths; do not inline bytes or base64.",
            "- User-provided files are available as Task.attachments metadata; use inspect_user_attachment for bounded local inspection and ask the team lead for a gateway-backed NATS Object Store mirror when local access is unavailable.",
            "- To directly analyze a workspace-local screenshot/image or supported document with the model, use load_local_media_for_model_context; it inserts native content blocks into the next inner-loop LLM input and should be used only when direct model inspection is needed.",
            "- Source-specific tools such as GitHub update source artifacts; user-visible proactive communication goes through the gateway.",
            f"- Summarize deferred deliveries: {str(delivery.summarize_deferred).lower()}",
            "- Delivery timing and channel mechanics are handled by runtime delivery components, not by the model.",
        ]
    )
    return "\n".join(lines)


def build_instance_delivery_context(
    *,
    instance_config_path: str | Path | None,
    logger: Optional[logging.Logger] = None,
) -> str:
    if not instance_config_path:
        return ""
    config_path = Path(instance_config_path).expanduser().resolve()
    if not config_path.exists():
        if logger is not None:
            logger.warning("Instance config path for delivery context does not exist: %s", config_path)
        return ""
    try:
        config = GatewayInstanceConfig.load_for_process(config_path)
    except Exception as exc:
        if logger is not None:
            logger.warning("Failed to load instance delivery context from %s: %s", config_path, exc)
        return ""
    return render_delivery_settings_context(config.delivery, source_path=config_path)
