"""Application owner for manager availability transitions and presentation."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.manager_client import (
    GatewayManagerClient,
    GatewayManagerTransition,
)
from assistant_gateway.application.ports import GatewayEventOutput
from assistant_gateway.application.proactive_delivery import (
    GatewayProactiveSessionResolver,
    GatewayProactiveTargetProvider,
)
from assistant_gateway.application.response_coordinator import (
    GatewayResponseCoordinator,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.runtime_v1 import DELIVERY_CHANNEL_TELEGRAM


TransitionHook = Callable[[], Awaitable[None] | None]


class GatewayMaintenanceScheduleInstaller(Protocol):
    """Narrow maintenance operation required after manager replacement."""

    async def ensure_all(self) -> None: ...


@dataclass(frozen=True)
class GatewayManagerTransitionSettings:
    """Stable presentation and route settings for manager transitions."""

    assistant_display_name: str
    unavailable_message_template: str
    available_message_template: str
    unavailable_message_fallback: str
    available_message_fallback: str
    enabled_fallback_channels: tuple[str, ...]
    telegram_pause_polling: bool

    def unavailable_notice(self) -> str:
        return self._render(
            self.unavailable_message_template,
            fallback=self.unavailable_message_fallback,
        )

    def available_notice(self) -> str:
        return self._render(
            self.available_message_template,
            fallback=self.available_message_fallback,
        )

    def _render(self, template: str, *, fallback: str) -> str:
        try:
            rendered = template.format(
                assistant_name=self.assistant_display_name,
                agent_name=self.assistant_display_name,
            ).strip()
        except (IndexError, KeyError, ValueError):
            return fallback
        return rendered or fallback


class GatewayManagerTransitionApplication:
    """Own transition recovery, notices, hooks, and pending-request completion."""

    def __init__(
        self,
        *,
        settings: GatewayManagerTransitionSettings,
        sessions: SessionRepository,
        targets: GatewayProactiveTargetProvider,
        proactive_sessions: GatewayProactiveSessionResolver,
        maintenance: GatewayMaintenanceScheduleInstaller,
        manager: GatewayManagerClient,
        responses: GatewayResponseCoordinator,
        event_output: GatewayEventOutput,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._targets = targets
        self._proactive_sessions = proactive_sessions
        self._maintenance = maintenance
        self._manager = manager
        self._responses = responses
        self._event_output = event_output
        self._logger = logger or logging.getLogger(__name__)
        self._active = False
        self._notices_enabled = False
        self._unavailable_hook: TransitionHook | None = None
        self._available_hook: TransitionHook | None = None

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False
        self._notices_enabled = False

    def enable_notices(self) -> None:
        self._notices_enabled = True

    def set_hooks(
        self,
        *,
        on_manager_unavailable: TransitionHook | None = None,
        on_manager_available: TransitionHook | None = None,
    ) -> None:
        self._unavailable_hook = on_manager_unavailable
        self._available_hook = on_manager_available

    async def handle_available(self, transition: GatewayManagerTransition) -> None:
        if not self._active:
            return
        try:
            await self._maintenance.ensure_all()
        except Exception as exc:
            self._logger.warning(
                "Gateway failed to (re)install manager-owned maintenance schedules. "
                "event=gateway_manager_transition_schedule_install_failed "
                "manager_id=%s source=%s error_type=%s",
                transition.manager_id,
                transition.source,
                type(exc).__name__,
            )
        if not transition.became_available:
            return
        if self._notices_enabled:
            await self._emit_notice(available=True)
        await self._run_hook(self._available_hook, label="available")

    async def handle_unavailable(self, _transition: GatewayManagerTransition) -> None:
        if self._notices_enabled:
            await self._emit_notice(available=False)
        await self._run_hook(self._unavailable_hook, label="unavailable")
        for correlation_id in self._manager.pending_response_ids:
            await self._responses.queue_manager_unavailable(correlation_id)

    async def _run_hook(
        self,
        hook: TransitionHook | None,
        *,
        label: str,
    ) -> None:
        if hook is None:
            return
        result = hook()
        if inspect.isawaitable(result):
            await result
        self._logger.info("Gateway completed manager-%s transition hook.", label)

    def _resolve_notice_session(self) -> SessionRecord:
        target = self._targets.current_proactive_target()
        if not target.chat_id:
            fallback_channels = set(self._settings.enabled_fallback_channels)
            if (
                target.channel_name
                and target.channel_name != "cli"
                and target.channel_name in fallback_channels
            ):
                preferred = self._recent_session({target.channel_name})
                if preferred is not None:
                    return preferred
            fallback = self._recent_session(fallback_channels)
            if fallback is not None:
                return fallback
        return self._proactive_sessions.resolve_session()

    def _recent_session(self, channels: set[str]) -> SessionRecord | None:
        for session in self._sessions.list_sessions(limit=50, status="active"):
            if session.channel_name not in channels:
                continue
            if session.channel_name != "cli" and not session.chat_id:
                continue
            return session
        return None

    async def _emit_notice(self, *, available: bool) -> None:
        session = self._resolve_notice_session()
        text = self._notice_for_channel(
            available=available,
            channel_name=session.channel_name,
        )
        await self._event_output.emit(
            GatewayDeliveryEvent(
                text=text,
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

    def _notice_for_channel(
        self,
        *,
        available: bool,
        channel_name: str | None,
    ) -> str:
        notice = (
            self._settings.available_notice()
            if available
            else self._settings.unavailable_notice()
        )
        if (
            channel_name != DELIVERY_CHANNEL_TELEGRAM
            or not self._settings.telegram_pause_polling
        ):
            return notice
        if available:
            return (
                f"{notice} Telegram polling will resume now and the gateway will "
                "process missed Telegram messages that are still available from "
                "the last 24 hours."
            )
        return (
            f"{notice} Telegram polling will pause now, and Telegram only keeps "
            "missed bot messages for up to 24 hours while the manager is offline."
        )
