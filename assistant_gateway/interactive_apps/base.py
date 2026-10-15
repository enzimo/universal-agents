"""Base classes for gateway-backed interactive app adapters."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from typing import Awaitable, TypeVar

from assistant_gateway.application.access_reviews import (
    AccessReviewDecisionRequestV1,
    AccessReviewDecisionResultV1,
    AccessReviewOutcome,
    decode_review_control,
    encode_review_control,
    review_choice_label,
)
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.interactive_apps.contracts import InteractiveAppApplication
from assistant_gateway.translation import ChannelOutboundMessage


LOGGER = logging.getLogger(__name__)
_ResultT = TypeVar("_ResultT")
DEFAULT_ACTIVITY_MESSAGE_INTERVAL_SECONDS = 30.0
DEFAULT_TIMEOUT_NOTICE_TEMPLATE = (
    "I'm still waiting on that task, but it did not complete within the allotted "
    "{timeout_seconds:g}s timeout."
)
DEFAULT_PROGRESS_NOTICE = "Still working on that. I'll send the answer when it's ready."
REVIEW_DECISION_FAILED_TEXT = (
    "Your decision could not be saved because of an error. Try again, or decide in the Inbox."
)


@dataclass(frozen=True)
class ReviewControl:
    """One review button: its label and the data sent back when pressed."""

    label: str
    data: str
    choice: str


@dataclass(frozen=True)
class ActivityContext:
    chat_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    session_key: str | None = None
    conversation_id: str | None = None
    interaction: object | None = None


class BaseInteractiveApp(ABC):
    channel_name: str
    application: InteractiveAppApplication

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Return whether this app should be started."""

    @abstractmethod
    async def start(self) -> None:
        """Start the app runtime."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the app runtime."""

    @abstractmethod
    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        """Deliver a gateway event to the backing app."""

    @staticmethod
    def review_controls_for(outbound: ChannelOutboundMessage) -> tuple[ReviewControl, ...]:
        """The review buttons an access review announcement offers; empty for other messages."""

        request_id = getattr(outbound, "access_request_id", None)
        if not request_id:
            return ()
        return tuple(
            ReviewControl(
                label=review_choice_label(choice),
                data=encode_review_control(request_id, choice),
                choice=choice,
            )
            for choice in getattr(outbound, "review_choices", ())
        )

    async def decide_review_control(
        self,
        data: str,
        *,
        user_id: str,
        chat_id: str,
        thread_id: str | None,
    ) -> AccessReviewDecisionResultV1 | None:
        """Decide from a pressed review button as the chat's linked person.

        Returns None when the data is not a review button. Refusals come back
        as a result with plain text; an unexpected error is logged and
        reported as a failure, and the buttons stay.
        """

        decoded = decode_review_control(data)
        if decoded is None:
            return None
        request_id, choice = decoded
        try:
            return await self.application.decide_access_review(
                AccessReviewDecisionRequestV1(
                    request_id=request_id,
                    choice=choice,
                    channel_name=self.channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                )
            )
        except Exception:
            LOGGER.exception(
                "An access decision from a chat button failed. event=access_review_button_failed "
                "channel=%s chat_id=%s user_id=%s request_id=%s",
                self.channel_name,
                chat_id,
                user_id,
                request_id,
            )
            return AccessReviewDecisionResultV1(outcome=AccessReviewOutcome.REFUSED, text=REVIEW_DECISION_FAILED_TEXT)

    def uses_native_activity_indicator(self, _context: ActivityContext) -> bool:
        """Return whether this app should show a native activity indicator."""
        return False

    def native_activity_refresh_interval_seconds(self) -> float:
        """Return how often a native activity indicator should be refreshed."""
        return DEFAULT_ACTIVITY_MESSAGE_INTERVAL_SECONDS

    def progress_notice_interval_seconds(self) -> float:
        """Return how often a fallback textual progress notice should be sent."""
        return DEFAULT_ACTIVITY_MESSAGE_INTERVAL_SECONDS

    def progress_notice_text(self) -> str:
        """Return the fallback textual progress notice."""
        return DEFAULT_PROGRESS_NOTICE

    def timeout_notice_text(self, *, timeout_seconds: float) -> str:
        """Return the timeout notice shown when a request expires."""
        return DEFAULT_TIMEOUT_NOTICE_TEMPLATE.format(timeout_seconds=timeout_seconds)

    async def emit_native_activity_indicator(self, _context: ActivityContext) -> None:
        """Emit one native activity-indicator refresh."""
        return None

    @abstractmethod
    async def emit_text_activity_update(
        self, context: ActivityContext, text: str
    ) -> None:
        """Emit a textual activity update to the user."""

    async def run_with_activity_indicator(
        self,
        *,
        context: ActivityContext,
        awaitable: Awaitable[_ResultT],
    ) -> _ResultT:
        if self.uses_native_activity_indicator(context):
            await self.emit_native_activity_indicator(context)
        loop_task = asyncio.create_task(
            self._activity_loop(context=context),
            name=f"{self.channel_name}_activity_indicator",
        )
        try:
            return await awaitable
        finally:
            loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await loop_task

    async def send_timeout_notice(
        self, *, context: ActivityContext, timeout_seconds: float
    ) -> None:
        await self.emit_text_activity_update(
            context,
            self.timeout_notice_text(timeout_seconds=timeout_seconds),
        )

    async def _activity_loop(self, *, context: ActivityContext) -> None:
        if self.uses_native_activity_indicator(context):
            while True:
                await asyncio.sleep(self.native_activity_refresh_interval_seconds())
                await self.emit_native_activity_indicator(context)
        while True:
            await asyncio.sleep(self.progress_notice_interval_seconds())
            await self.emit_text_activity_update(context, self.progress_notice_text())

    def pauses_inbound_when_manager_unavailable(self) -> bool:
        """Return whether inbound delivery should pause while the manager is offline."""
        return False

    def is_registered_gateway_user(self, external_user_id: str) -> bool:
        """Return whether this channel identity is already bound to an active gateway user."""
        normalized = str(external_user_id or "").strip()
        if not normalized:
            return False
        application = getattr(self, "application", None)
        resolver = getattr(application, "is_registered_user", None)
        if not callable(resolver):
            return False
        try:
            return bool(
                resolver(
                    channel_name=self.channel_name,
                    external_user_id=normalized,
                    chat_scope="",
                )
            )
        except Exception:
            LOGGER.warning(
                "Failed to check registered gateway user for adapter allowlist. channel_name=%s external_user_id=%s",
                self.channel_name,
                normalized,
                exc_info=True,
            )
            return False

    async def pause_inbound(self) -> None:
        """Pause inbound delivery without stopping outbound delivery."""
        return None

    async def resume_inbound(self) -> None:
        """Resume inbound delivery after a manager outage."""
        return None
