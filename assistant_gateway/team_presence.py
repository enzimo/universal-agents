"""The live team for Settings → Team, the Tasks view and the activity mesh.

The members come from the gateway's team view (``assistant_gateway.team_view``), which
builds them from the team's signed agent records each time they are read: a member is
listed while the records count it live, and for a minute after it left, as exiting.
Nothing is stored. The pages get a redacted view: name, type, whether it leads the team, a
status label, the skills it can activate, and when it was last seen (now, for a live
member). Skills come from the agents' own answers, which the view asks for only when the
Team tab is opened or refreshed (``list_web_chat_team_members`` without
``presence_only``); the two-second live update, the mesh and Tasks never ask.

The Team view also lists the agent classes defined for the team (from the
agent catalog, without prompts), so people can see who could be started.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from pydantic import ValidationError

from assistant_gateway.application.website_application import (
    WebAgentClassV1,
    WebTeamMemberListV1,
    WebTeamMemberV1,
)
from custom_messages.contracts.control_v1 import AgentStatus


def configured_agent_classes(config: Any) -> list[dict[str, str]]:
    """The team's defined agent classes (name, display name, description; never prompts)."""

    from custom_agents.agent_catalog import instance_agent_catalog

    catalog = instance_agent_catalog(str(config.instance_root_path), config.agent.agent_definition_paths)
    return [
        {
            "named_agent_type": item.named_agent_type,
            "display_name": item.display_name,
            "description": item.description,
        }
        for _, item in sorted(catalog.agent_types.items())
    ]


def _status_label(status: AgentStatus) -> dict[str, str]:
    """The label and its dot colour (ui/DESIGN.md: green Available, amber Busy, red Exiting, gray otherwise).

    A member that takes no new work (its process runs another message contract, it is still
    starting, or it winds down) is "Not accepting work" unless it is busy with work it holds.
    """

    if status.is_exiting:
        return {"key": "exiting", "label": "Exiting", "tone": "danger"}
    if status.is_paused:
        return {"key": "paused", "label": "Paused", "tone": "neutral"}
    if status.is_executing or status.has_execution_capacity is False:
        return {"key": "busy", "label": "Busy", "tone": "warning"}
    if status.accepting_work is False:
        return {"key": "not_accepting", "label": "Not accepting work", "tone": "neutral"}
    return {"key": "available", "label": "Available", "tone": "success"}


class GatewayTeamPresence:
    def __init__(
        self,
        *,
        members: Callable[[], Sequence[AgentStatus]],
        want_details: Callable[[], None],
        list_agent_classes: Callable[[], list[dict[str, str]]],
        logger: logging.Logger | None = None,
    ) -> None:
        # The team view's members (``GatewayTeamView.statuses``), none for a gateway without one.
        self._statuses = members
        self._want_details = want_details
        self._list_agent_classes = list_agent_classes
        self._logger = logger or logging.getLogger(__name__)

    def _members(self) -> list[dict[str, Any]]:
        members = [
            {
                "agent_id": status.self_id,
                "name": str(status.name or status.self_id),
                "named_agent_type": str(status.named_agent_type or "Unknown"),
                "is_team_lead": bool(status.is_team_lead),
                "status": _status_label(status),
                "available_skills": sorted(
                    {str(name).strip() for name in status.available_skills or [] if str(name).strip()},
                    key=str.casefold,
                ),
                "last_seen_at": status.last_seen_at,
            }
            for status in self._statuses()
        ]
        members.sort(
            key=lambda item: (
                not item["is_team_lead"],
                item["named_agent_type"].casefold(),
                item["name"].casefold(),
                item["agent_id"],
            )
        )
        return members

    def current_statuses(self) -> list[AgentStatus]:
        """Every member the records count live (the Status page)."""

        return [status for status in self._statuses() if not status.is_exiting]

    def list_web_chat_team_members(self, *, presence_only: bool = False) -> WebTeamMemberListV1:
        """Live members, plus the defined agent classes unless only presence is needed.

        A full read (the Team tab opened or refreshed) asks the members for their skills; the
        answers show with the next live update. One member that does not fit the page's
        model is logged and left out, so it cannot break the list everyone's pages read.
        """

        if not presence_only:
            self._want_details()
        members = []
        for item in self._members():
            try:
                members.append(WebTeamMemberV1.model_validate(item))
            except ValidationError as error:
                self._logger.warning(
                    "A team member could not be shown and was left out. event=gateway_team_member_invalid "
                    "agent_id=%s error=%s",
                    item["agent_id"],
                    error.errors(include_url=False, include_input=False),
                )
        return WebTeamMemberListV1(
            definitions=tuple(
                WebAgentClassV1.model_validate(item) for item in ([] if presence_only else self._list_agent_classes())
            ),
            members=tuple(members),
        )


__all__ = ["GatewayTeamPresence", "configured_agent_classes"]
