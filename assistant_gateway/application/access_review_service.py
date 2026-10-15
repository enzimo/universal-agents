"""Deciding an access review from a chat button as the linked person.

See ``assistant_gateway.application.access_reviews`` for the button contract.
The person is looked up from the pressing chat account on every press (the
same lookup the ``/access`` command uses); the access Inbox then decides with
that person's current team roles. A refusal keeps the buttons; a request that
was decided elsewhere or withdrawn closes them.
"""

from __future__ import annotations

import logging
from typing import Protocol

from assistant_gateway.application.access_reviews import (
    CLOSED_TEXT,
    DECIDED_WORDS,
    NOT_LINKED_TEXT,
    AccessReviewDecisionRequestV1,
    AccessReviewDecisionResultV1,
    AccessReviewOutcome,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)


class AccessReviewDecider(Protocol):
    """The gateway access Inbox, as the chat buttons need it."""

    async def decide_review(self, person_id: str, request_id: str, choice: str) -> str:
        """Decide as the person and return the request's new status; PermissionError when refused."""

    async def request_status(self, request_id: str) -> str | None:
        """The request's current status, or None when there is no such request."""


class GatewayAccessReviewControls:
    """Decide a review from a chat button as the linked person."""

    def __init__(
        self,
        *,
        request_context: GatewayRequestContextService,
        decider: AccessReviewDecider,
        logger: logging.Logger | None = None,
    ) -> None:
        self._request_context = request_context
        self._decider = decider
        self._logger = logger or logging.getLogger(__name__)

    async def decide(self, request: AccessReviewDecisionRequestV1) -> AccessReviewDecisionResultV1:
        resolved = self._request_context.resolve_canonical_user(
            GatewayRequestRoute(
                channel_name=request.channel_name,
                user_id=request.user_id,
                chat_id=request.chat_id,
                thread_id=request.thread_id,
            )
        )
        if resolved is None:
            self._refused(request, person_id=None, reason="not_linked")
            return AccessReviewDecisionResultV1(outcome=AccessReviewOutcome.REFUSED, text=NOT_LINKED_TEXT)
        person_id = resolved.user.canonical_user_id
        name = str(resolved.user.display_name or "").strip() or person_id
        try:
            status = await self._decider.decide_review(person_id, request.request_id, request.choice)
        except PermissionError as exc:
            current = await self._decider.request_status(request.request_id)
            if current in CLOSED_TEXT:
                return AccessReviewDecisionResultV1(outcome=AccessReviewOutcome.CLOSED, text=CLOSED_TEXT[current])
            self._refused(request, person_id=person_id, reason=str(exc))
            return AccessReviewDecisionResultV1(outcome=AccessReviewOutcome.REFUSED, text=str(exc) or "Not decided.")
        self._logger.info(
            "A person decided an access request from chat. event=access_review_decided_in_chat "
            "request_id=%s person_id=%s choice=%s status=%s channel=%s",
            request.request_id,
            person_id,
            request.choice,
            status,
            request.channel_name,
        )
        words = DECIDED_WORDS.get(request.choice, "Decided")
        return AccessReviewDecisionResultV1(outcome=AccessReviewOutcome.DECIDED, text=f"{words} by {name}.")

    def _refused(self, request: AccessReviewDecisionRequestV1, *, person_id: str | None, reason: str) -> None:
        self._logger.warning(
            "An access decision from chat was refused. event=access_review_chat_refused "
            "request_id=%s person_id=%s channel=%s chat_id=%s reason=%s",
            request.request_id,
            person_id or "-",
            request.channel_name,
            request.chat_id,
            reason,
        )


__all__ = ["AccessReviewDecider", "GatewayAccessReviewControls"]
