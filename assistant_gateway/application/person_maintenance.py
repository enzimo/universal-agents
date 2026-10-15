"""Who gets per-person maintenance schedules, and which chat reaches each person.

Per-person memory review (``custom_agents.memory_review_runtime``) installs one
pair of schedules for each active person, owned by that person. Each schedule's
context names a chat session, and the gateway maps the schedule id to it
(``GatewayDeliveryService.upsert_scheduled_target``), so anything the review
sends to its own thread reaches that person, never the team-wide proactive
session.

The session is a dedicated one per person (``person_maintenance_session_key``),
owned by them, with its own conversation, so review runs never add turns to
the person's own chats (the team-wide schedules use the proactive session the
same way). Its route (channel, chat, thread) is set at every install:

1. the person's default chat (``/user default``: ``default_channel_name``,
   ``default_chat_id``, ``default_thread_id``), when set;
2. otherwise the chat of their most recently used active session, which covers
   Web Chat and chat apps alike;
3. otherwise no route: the schedules are still installed, because reviews
   change memory without delivering anything; ``event=
   gateway_person_maintenance_unreachable`` is logged.

With gateway users off there is one person, the configured user, and their
route is the team's proactive delivery target.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from access_control.people import person_standing
from assistant_gateway.application.proactive_delivery import (
    GatewayProactiveSessionResolver,
)
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import IdentityRepository, SessionRepository
from custom_messages.contracts.access_v2 import PersonStanding

PERSON_MAINTENANCE_SESSION_PREFIX = "person_maintenance:"
# Channels that never reach a person on their own (local operator console).
_UNREACHABLE_CHANNELS = frozenset({"cli", "local"})
# Recent sessions read when looking for the person's latest chat.
_RECENT_SESSION_SCAN = 20
_ACTIVE_SESSION_STATUS = "active"


def person_maintenance_session_key(person_id: str) -> str:
    """The session that holds one person's maintenance schedule runs."""

    return f"{PERSON_MAINTENANCE_SESSION_PREFIX}{person_id}"


def is_person_maintenance_session_key(session_key: str) -> bool:
    """Whether a session is a person's maintenance session (``person_maintenance_session_key``)."""

    return session_key.startswith(PERSON_MAINTENANCE_SESSION_PREFIX)


class PersonRouteSource(StrEnum):
    """Where a person's maintenance route came from (logged with each install)."""

    DEFAULT_CHAT = "default_chat"
    RECENT_SESSION = "recent_session"
    TEAM_PROACTIVE = "team_proactive"
    NONE = "none"


@dataclass(frozen=True)
class GatewayPersonMaintenanceSettings:
    """Which team's people get per-person schedules, and single-user mode's person."""

    team_id: str
    users_enabled: bool
    default_owner_canonical_user_id: str


@dataclass(frozen=True)
class GatewayPersonMaintenanceTarget:
    """The session a person's maintenance schedules use, and where its route came from."""

    session: SessionRecord
    route_source: PersonRouteSource


class GatewayPersonMaintenanceSessions:
    """Lists active people and resolves the session that reaches each (module docstring)."""

    def __init__(
        self,
        *,
        settings: GatewayPersonMaintenanceSettings,
        identity: IdentityRepository,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        proactive_sessions: GatewayProactiveSessionResolver,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._identity = identity
        self._sessions = sessions
        self._session_repository = session_repository
        self._proactive_sessions = proactive_sessions
        self._logger = logger or logging.getLogger(__name__)

    def active_person_ids(self) -> tuple[str, ...]:
        """Everyone active in the team now; disabled and removed people are left out."""

        if not self._settings.users_enabled:
            return (self._settings.default_owner_canonical_user_id,)
        return tuple(
            row.canonical_user_id
            for row in self._identity.list_team_people(self._settings.team_id)
            if person_standing(membership_status=row.membership_status, person_status=row.person_status)
            is PersonStanding.ACTIVE
        )

    def is_active(self, person_id: str) -> bool:
        """Whether this person is active in the team now."""

        return person_id in self.active_person_ids()

    def resolve(self, person_id: str, *, manager_agent_id: str | None) -> GatewayPersonMaintenanceTarget:
        """The person's maintenance session, created if missing, with its route set now."""

        if not self._settings.users_enabled:
            return GatewayPersonMaintenanceTarget(
                session=self._proactive_sessions.resolve_session(),
                route_source=PersonRouteSource.TEAM_PROACTIVE,
            )
        route, source = self._route(person_id)
        if source is PersonRouteSource.NONE:
            self._logger.info(
                "No chat reaches this person yet; their maintenance schedules are installed without "
                "a delivery route. They get one at the next gateway start after the person chats "
                "or sets a default chat. event=gateway_person_maintenance_unreachable person_id=%s",
                person_id,
            )
        session = self._sessions.resolve_session(
            requested_session_key=person_maintenance_session_key(person_id),
            default_session_key=person_maintenance_session_key(person_id),
            manager_agent_id=manager_agent_id,
            owner_canonical_user_id=person_id,
            default_owner_canonical_user_id=person_id,
            route=route,
        )
        return GatewayPersonMaintenanceTarget(session=session, route_source=source)

    def _route(self, person_id: str) -> tuple[GatewaySessionRoute, PersonRouteSource]:
        person = self._identity.get_gateway_user(person_id)
        if person is not None and person.default_channel_name and person.default_chat_id:
            return (
                GatewaySessionRoute(
                    channel_name=person.default_channel_name,
                    user_id=person_id,
                    chat_id=person.default_chat_id,
                    thread_id=person.default_thread_id,
                ),
                PersonRouteSource.DEFAULT_CHAT,
            )
        recent = self._recent_chat_session(person_id)
        if recent is not None:
            return (
                GatewaySessionRoute(
                    channel_name=recent.channel_name,
                    user_id=recent.user_id,
                    chat_id=recent.chat_id,
                    thread_id=recent.thread_id,
                ),
                PersonRouteSource.RECENT_SESSION,
            )
        # user_id alone still rewrites the stored route, clearing an old chat.
        return GatewaySessionRoute(user_id=person_id), PersonRouteSource.NONE

    def _recent_chat_session(self, person_id: str) -> SessionRecord | None:
        own_key = person_maintenance_session_key(person_id)
        for session in self._session_repository.list_sessions(
            limit=_RECENT_SESSION_SCAN,
            status=_ACTIVE_SESSION_STATUS,
            owner_canonical_user_id=person_id,
        ):
            if session.session_key == own_key or not session.chat_id:
                continue
            if not session.channel_name or session.channel_name in _UNREACHABLE_CHANNELS:
                continue
            return session
        return None


__all__ = [
    "GatewayPersonMaintenanceSessions",
    "GatewayPersonMaintenanceSettings",
    "GatewayPersonMaintenanceTarget",
    "PERSON_MAINTENANCE_SESSION_PREFIX",
    "PersonRouteSource",
    "is_person_maintenance_session_key",
    "person_maintenance_session_key",
]
