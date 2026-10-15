"""Prompt rendering helpers for AutomationMessage follow-ups."""

from __future__ import annotations

import json

from custom_messages.contracts.schedule_v1 import AutomationMessage


def build_automation_prompt_context(message: AutomationMessage) -> str:
    snapshot_json = json.dumps(
        {
            "msg_header": (
                message.original_envelope_snapshot.msg_header.model_dump(mode="json")
                if message.original_envelope_snapshot.msg_header is not None
                else None
            ),
            "msg_meta": (
                message.original_envelope_snapshot.msg_meta.model_dump(mode="json")
                if message.original_envelope_snapshot.msg_meta is not None
                else None
            ),
            "msg_content": message.original_envelope_snapshot.msg_content,
        },
        indent=2,
        sort_keys=True,
    )
    lines = [
        "[Automation Context]",
        f"- Automation ID: {message.automation_id}",
        f"- Hook Name: {message.hook_name}",
        f"- Requester Local Time: {message.requester_local_time}",
        f"- Suggested Later Time: {message.suggested_later_time or 'none'}",
        "- Delivery timing is handled by the gateway; do not invent alternate delivery mechanics.",
        "",
        "[Allowed Decisions]",
        ", ".join(message.allowed_decisions) if message.allowed_decisions else "none",
        "",
        "[Decision Instructions]",
        message.decision_instructions,
        "",
        "[Delivery Preference Snapshot]",
        f"- Preferred Channel: {message.delivery_preference_snapshot.preferred_channel or 'none'}",
        f"- Preferred Output Chat ID: {message.delivery_preference_snapshot.preferred_output_chat_id or 'none'}",
        f"- Preferred Thread ID: {message.delivery_preference_snapshot.preferred_channel_thread_id or 'none'}",
        f"- Summarize Deferred: {message.delivery_preference_snapshot.summarize_deferred}",
        f"- Digest Delivery Hour Local: {message.delivery_preference_snapshot.digest_delivery_hour_local}",
        f"- Urgent Keywords: {', '.join(message.delivery_preference_snapshot.urgent_keywords) or 'none'}",
        "",
        "[Quiet Hours Snapshot]",
        f"- Timezone: {message.quiet_hours_window.timezone_name or 'unknown'}",
        f"- Enabled: {message.quiet_hours_window.enabled}",
        f"- Start Hour Local: {message.quiet_hours_window.start_hour_local if message.quiet_hours_window.start_hour_local is not None else 'none'}",
        f"- End Hour Local: {message.quiet_hours_window.end_hour_local if message.quiet_hours_window.end_hour_local is not None else 'none'}",
        f"- Allow Urgent Delivery: {message.quiet_hours_window.allow_urgent_delivery}",
        f"- Currently Within Quiet Hours: {message.quiet_hours_window.currently_within_quiet_hours if message.quiet_hours_window.currently_within_quiet_hours is not None else 'unknown'}",
        "",
        "[Rendered Original User Text]",
        message.rendered_original_user_text,
        "",
        "[Original Envelope Snapshot]",
        "```json",
        snapshot_json,
        "```",
    ]
    return "\n".join(lines).strip()
