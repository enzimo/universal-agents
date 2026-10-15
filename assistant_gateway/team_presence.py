"""Live team presence for Settings → Team and the Tasks view.

Agents report their status on the manager's presence and response routes; the
gateway keeps the latest report per agent of its own team in memory (nothing
is stored) and shows a redacted view: name, type, whether it leads the team,
a status label, the skills it can activate, and when it was last seen. Older
reports never replace newer ones, and agents that stop reporting drop out
after twice the stale time.

The Team view also lists the agent classes defined for the team (from the
agent catalog, without prompts), so people can see who could be started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from assistant_gateway.application.ports import GatewayClock
from assistant_gateway.application.website_application import (
    WebAgentClassV1,
    WebTeamMemberListV1,
    WebTeamMemberV1,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import MessageMetaType


@dataclass(frozen=True)
class _PresenceObservation:
    status: AgentStatus
    reported_at: datetime
    source: str


def configured_agent_classes(config: Any) -> list[dict[str, str]]:
    """The team's defined agent classes (name, display name, description; never prompts)."""

    from custom_agents.agent_catalog import (
        default_team_agent_definition_paths,
        load_agent_catalog,
        resolve_agent_definition_paths,
    )

    root = str(config.instance_root_path)
    catalog = load_agent_catalog(
        [
            *default_team_agent_definition_paths(instance_root=root),
            *resolve_agent_definition_paths(config.agent.agent_definition_paths, instance_root=root),
        ],
        include_configured=False,
    )
    return [
        {
            "named_agent_type": item.named_agent_type,
            "display_name": item.display_name,
            "description": item.description,
        }
        for _, item in sorted(catalog.agent_types.items())
    ]


def _status_label(status: AgentStatus, *, age_seconds: float, stale_after: float) -> dict[str, str]:
    if status.is_exiting:
        return {"key": "exiting", "label": "Exiting", "tone": "danger"}
    if age_seconds > stale_after:
        return {"key": "stale", "label": "Stale", "tone": "neutral"}
    if status.is_paused:
        return {"key": "paused", "label": "Paused", "tone": "neutral"}
    if status.is_executing or status.has_execution_capacity is False:
        return {"key": "busy", "label": "Busy", "tone": "warning"}
    return {"key": "available", "label": "Available", "tone": "success"}


class GatewayTeamPresence:
    def __init__(
        self,
        *,
        team_id: str,
        clock: GatewayClock,
        stale_after_seconds: float,
        list_agent_classes: Callable[[], list[dict[str, str]]],
        logger: logging.Logger | None = None,
    ) -> None:
        self._team_id = team_id
        self._clock = clock
        self._stale_after = float(stale_after_seconds)
        self._list_agent_classes = list_agent_classes
        self._logger = logger or logging.getLogger(__name__)
        self._presence: dict[str, _PresenceObservation] = {}

    def record_agent_presence(self, status: AgentStatus, reported_at: datetime | None, source: str) -> None:
        """Record a same-team status report without keeping diagnostic payloads for the browser."""

        if status.status_type != MessageMetaType.RESPONSE or status.team_id != self._team_id or not status.self_id:
            return
        timestamp = reported_at or self._clock.now_utc()
        existing = self._presence.get(status.self_id)
        if existing is not None and timestamp < existing.reported_at:
            self._logger.debug(
                "Gateway ignored older team presence. agent_id=%s source=%s timestamp=%s",
                status.self_id,
                source,
                timestamp.isoformat(),
            )
            return
        self._presence[status.self_id] = _PresenceObservation(
            status=status.model_copy(deep=True), reported_at=timestamp, source=source
        )

    def _members(self) -> list[dict[str, Any]]:
        now = self._clock.now_utc()
        retention_seconds = max(self._stale_after * 2.0, 60.0)
        members: list[dict[str, Any]] = []
        for agent_id, observation in list(self._presence.items()):
            age_seconds = max(0.0, (now - observation.reported_at).total_seconds())
            if age_seconds > retention_seconds:
                self._presence.pop(agent_id, None)
                continue
            status = observation.status
            members.append(
                {
                    "agent_id": agent_id,
                    "name": str(status.name or agent_id),
                    "named_agent_type": str(status.named_agent_type or "Unknown"),
                    "is_team_lead": bool(status.is_team_lead),
                    "status": _status_label(status, age_seconds=age_seconds, stale_after=self._stale_after),
                    "available_skills": sorted(
                        {str(name).strip() for name in status.available_skills or [] if str(name).strip()},
                        key=str.casefold,
                    ),
                    "last_seen_at": observation.reported_at.isoformat(),
                }
            )
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
        """The latest heartbeat of every agent that reported within the stale limit (Status page)."""

        now = self._clock.now_utc()
        return [
            observation.status
            for observation in list(self._presence.values())
            if (now - observation.reported_at).total_seconds() <= self._stale_after
            and not observation.status.is_exiting
        ]

    def list_web_chat_team_members(self, *, presence_only: bool = False) -> WebTeamMemberListV1:
        """Live members, plus the defined agent classes unless only presence is needed."""

        return WebTeamMemberListV1(
            definitions=tuple(
                WebAgentClassV1.model_validate(item) for item in ([] if presence_only else self._list_agent_classes())
            ),
            members=tuple(WebTeamMemberV1.model_validate(item) for item in self._members()),
        )


__all__ = ["GatewayTeamPresence", "configured_agent_classes"]
