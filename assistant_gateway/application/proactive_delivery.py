"""Application-owned proactive delivery target and session resolution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayEventOutput
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.domain.sessions import SessionRecord


@dataclass(frozen=True)
class GatewayProactiveTarget:
    """Reloadable operator-selected route for proactive gateway output."""

    channel_name: str
    chat_id: str
    thread_id: str | None = None


class GatewayProactiveTargetProvider(Protocol):
    """Present current proactive routing configuration to the application."""

    def current_proactive_target(self) -> GatewayProactiveTarget:
        """Return the latest validated proactive target."""


class GatewayProactiveSessionResolver(Protocol):
    """Resolve the gateway-owned session used for proactive work."""

    def resolve_session(self) -> SessionRecord:
        """Return the current proactive delivery session."""


def proactive_session_key(base_session_key: str, *, owner: str, default_owner: str) -> str:
    """The proactive session for whoever proactive work runs for now.

    The first user keeps the configured key (``heartbeat.session_key``);
    another owner gets their own, so the first user's stays theirs (sessions
    are owner-scoped) and is used again when they are active again.
    """

    return base_session_key if owner == default_owner else f"{base_session_key}:{owner}"


def is_proactive_session_key(session_key: str, *, base_session_key: str) -> bool:
    """Whether a session is a proactive session made by ``proactive_session_key``."""

    return session_key == base_session_key or session_key.startswith(f"{base_session_key}:")


@dataclass(frozen=True)
class GatewayProactiveDeliverySettings:
    """Stable identity settings for proactive delivery."""

    instance_name: str
    session_key: str
    default_owner_canonical_user_id: str
    users_enabled: bool


class GatewayProactiveDeliveryApplication:
    """Own proactive target selection, session resolution, and event emission."""

    def __init__(
        self,
        *,
        settings: GatewayProactiveDeliverySettings,
        targets: GatewayProactiveTargetProvider,
        sessions: GatewaySessionService,
        manager: GatewayManagerClient,
        event_output: GatewayEventOutput,
        team_work_owner: Callable[[str], str] | None = None,
    ) -> None:
        self._settings = settings
        self._targets = targets
        self._sessions = sessions
        self._manager = manager
        self._event_output = event_output
        # Who the proactive session, and so the runtime's team schedules, work
        # for: the first user while they are active, else another active
        # administrator (GatewayIdentityService.team_work_owner).
        self._team_work_owner = team_work_owner

    def owner(self) -> str:
        """The person proactive work runs for now (constructor comment)."""

        default = self._settings.default_owner_canonical_user_id
        if self._team_work_owner is None or not self._settings.users_enabled:
            return default
        return self._team_work_owner(default)

    def resolve_session(self) -> SessionRecord:
        """Resolve the explicit preferred route or the local CLI fallback."""

        owner = self.owner()
        target = self._targets.current_proactive_target()
        if target.channel_name != "cli" and target.chat_id:
            route = GatewaySessionRoute(
                channel_name=target.channel_name,
                user_id=owner,
                chat_id=target.chat_id,
                thread_id=target.thread_id,
            )
        else:
            route = GatewaySessionRoute(
                channel_name="cli",
                user_id=owner,
                chat_id=f"cli:{self._settings.instance_name}",
            )
        session_key = proactive_session_key(
            self._settings.session_key,
            owner=owner,
            default_owner=self._settings.default_owner_canonical_user_id,
        )
        return self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=session_key,
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=owner,
            default_owner_canonical_user_id=owner,
            route=route,
            enforce_owner_scope=self._settings.users_enabled,
        )

    async def emit_notice(self, text: str) -> None:
        """Emit one non-empty proactive notice through the selected session."""

        rendered = str(text or "").strip()
        if not rendered:
            return
        session = self.resolve_session()
        await self._event_output.emit(
            GatewayDeliveryEvent(
                text=rendered,
                session_key=session.session_key,
                channel_name=session.channel_name,
                user_id=session.user_id,
                chat_id=session.chat_id,
                thread_id=session.thread_id,
                conversation_id=session.conversation_id,
                scheduled_job_id=None,
                correlation_id=None,
                original_message_id=None,
            )
        )
