"""After a team skill changes: refresh running agents' skills and tell administrators.

Called by the skill change adapters (``custom_agents.change_approvals``) once
a team skill install or removal was applied (``access_control.changes``).
Failures here never undo the change; they are reported in its result.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from custom_agents.skill_catalog_runtime import (
    publish_managed_skill_catalog_notification,
    refresh_agent_skill_catalog,
)

logger = logging.getLogger(__name__)


async def reload_agent_skills(agent: Any) -> None:
    """Rebuild the current SDK cache and immediately republish skill presence."""

    await refresh_agent_skill_catalog(
        agent,
        force=True,
        publish_presence=True,
        reason="local_managed_skill_mutation",
    )


async def reload_runtime_skills_after_change(
    agent: Any,
    result: dict[str, Any],
    *,
    operation: Literal["install", "uninstall"],
) -> dict[str, Any]:
    """Reload locally and notify peers without obscuring a completed mutation."""

    local_status = "completed"
    notification_status = "published"
    notification_id: str | None = None
    failures: list[str] = []
    try:
        await reload_agent_skills(agent)
    except Exception as exc:
        local_status = "failed"
        failures.append("the current agent cache or presence update failed")
        logger.exception(
            "Managed skill mutation completed but the local cache refresh failed. "
            "event=managed_skill_cache_reload_failed operation=%s change_id=%s "
            "error_type=%s",
            operation,
            result.get("change_id"),
            type(exc).__name__,
        )
    try:
        notification = await publish_managed_skill_catalog_notification(
            agent,
            result,
            operation=operation,
        )
        notification_id = notification.notification_id
    except Exception as exc:
        notification_status = "failed"
        failures.append("the live team refresh notification failed")
        logger.exception(
            "Managed skill mutation completed but its team notification failed. "
            "event=managed_skill_catalog_notification_publish_failed "
            "operation=%s change_id=%s error_type=%s",
            operation,
            result.get("change_id"),
            type(exc).__name__,
        )

    refresh_status: dict[str, Any] = {
        "status": "completed" if not failures else "failed",
        "current_agent": local_status,
        "team_notification": notification_status,
    }
    if notification_id:
        refresh_status["notification_id"] = notification_id
    if failures:
        refresh_status["warning"] = (
            "The managed skill change completed, but "
            + " and ".join(failures)
            + ". The remaining refresh work will be retried by agent invocation "
            "and presence-heartbeat fingerprint checks without a process restart."
        )
    result["runtime_skill_refresh"] = refresh_status
    return result


def notice_team_skill_change(agent: Any, result: dict[str, Any], verb: str) -> None:
    """Tell the team's administrators about every team skill change (access plan, decision L2)."""

    from custom_agents.access_runtime import send_access_notice
    from custom_messages.contracts.access_v2 import AccessNoticeKind

    skill = str(result.get("skill_name") or (result.get("receipt") or {}).get("skill_name") or "a skill")
    who = str(getattr(agent, "named_agent_type", "") or "An agent")
    send_access_notice(
        agent,
        kind=AccessNoticeKind.TEAM_SKILL_CHANGED,
        title=f"{who} {verb} the team skill {skill}",
        summary=(
            f"{who} {verb} the team skill {skill}. Skills give agents instructions, not access; "
            "their scripts still need code access. Nothing else is needed."
        ),
        roles=("admin",),
    )


__all__ = ["notice_team_skill_change", "reload_agent_skills", "reload_runtime_skills_after_change"]
