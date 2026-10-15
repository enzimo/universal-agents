"""Application-owned polling and delivery of user-visible activity notices."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from assistant_gateway.application.ports import GatewayActivitySource
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.ports import GatewayEventOutput
from assistant_gateway.application.proactive_delivery import (
    GatewayProactiveSessionResolver,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository


ResolveNoticeSession = Callable[[dict[str, Any]], SessionRecord]
EmitNotice = Callable[..., Awaitable[None]]


class GatewayActivityNoticeRoutingApplication:
    """Own activity-notice session resolution and event projection."""

    def __init__(
        self,
        *,
        sessions: SessionRepository,
        proactive_sessions: GatewayProactiveSessionResolver,
        event_output: GatewayEventOutput,
    ) -> None:
        self._sessions = sessions
        self._proactive_sessions = proactive_sessions
        self._event_output = event_output

    def resolve_session(self, item: dict[str, Any]) -> SessionRecord:
        session_key = str(item.get("session_key") or "").strip()
        if session_key:
            existing = self._sessions.resolve_session_reference(session_key)
            if existing is not None:
                return existing
        conversation_id = str(item.get("conversation_id") or "").strip()
        if conversation_id:
            existing = self._sessions.get_session_by_conversation_id(conversation_id)
            if existing is not None:
                return existing
        return self._proactive_sessions.resolve_session()

    async def emit_notice(self, *, text: str, session: SessionRecord) -> None:
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


class GatewayActivityNoticeCoordinator:
    """Own activity filtering, deduplication, polling, and source lifecycle."""

    def __init__(
        self,
        *,
        source: GatewayActivitySource,
        resolve_session: ResolveNoticeSession,
        emit_notice: EmitNotice,
        poll_interval_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self._source = source
        self._resolve_session = resolve_session
        self._emit_notice = emit_notice
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._logger = logger or logging.getLogger(__name__)
        self._seen_event_ids: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._source.enabled

    async def deliver(self, items: list[dict[str, Any]]) -> int:
        delivered = 0
        for item in items:
            if not self._is_plan_progress_notice(item):
                continue
            event_id = str(item.get("activity_event_id") or "").strip()
            if not event_id:
                event_id = f"{item.get('created_at') or ''}:{item.get('summary') or ''}"
            if event_id in self._seen_event_ids:
                continue
            self._seen_event_ids.add(event_id)
            text = str(item.get("summary") or item.get("detail") or "").strip()
            if not text:
                continue
            await self._emit_notice(
                text=text,
                session=self._resolve_session(item),
            )
            delivered += 1
        return delivered

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.deliver(await self._source.fetch_recent_activity())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.debug(
                    "Gateway activity notice polling skipped. "
                    "event=gateway_activity_notice_poll_skipped error=%s",
                    exc,
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        await self._source.close()

    @staticmethod
    def _is_plan_progress_notice(item: dict[str, Any]) -> bool:
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        return (
            bool(item.get("user_visible", True))
            and item.get("activity_type") == "plan_updated"
            and details.get("notice_type") == "plan_progress"
        )
