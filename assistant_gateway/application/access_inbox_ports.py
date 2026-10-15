"""What the web layer needs from the access Inbox (``assistant_gateway.access_inbox``).

The person id always comes from the signed-in browser session; roles are
looked up again by the implementation on every call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from access_control.entities import PersonPrincipal
from assistant_gateway.application.access_map_ports import AccessMapApplication

# How many notices one Mark-as-read request may name: a page, with room to spare.
MAX_NOTICE_IDS_PER_REQUEST = 100


@dataclass(frozen=True)
class NoticeQuery:
    """Which notices the Inbox shows: unread only (its default) or all, one page at a time."""

    unread_only: bool = True
    offset: int = 0
    limit: int = 20


class AccessInboxApplication(Protocol):
    @property
    def access_map(self) -> AccessMapApplication:
        """The Access page. It shares this Inbox's access service and membership
        lookup, so the gateway keeps one Cedar evaluator for people's decisions
        (and the website facades stay within their size budgets)."""

    def person(self, person_id: str) -> PersonPrincipal | None:
        """The person with their current team roles; None when they are not an active member."""

    async def inbox(self, person_id: str, notices: NoticeQuery = NoticeQuery()) -> dict[str, Any]:
        """Waiting reviews the person may see or decide, and one page of their notices with totals."""

    async def counts(self, person_id: str) -> dict[str, int]:
        """Requests the person can decide and their unread notices (the Settings badges)."""

    async def review(self, person_id: str, request_id: str) -> dict[str, Any] | None:
        """One request in any status, as the person may see it (the chat card); None when hidden or missing."""

    async def decide(
        self,
        person_id: str,
        request_id: str,
        choice: str,
        note: str = "",
        *,
        keep: bool | None = None,
        kept_item_ids: tuple[str, ...] = (),
        keep_hours: int | None = None,
        hand_on: bool | None = None,
    ) -> dict[str, Any]:
        """Approve, approve read only, approve for this task only, or deny, with the card's boxes."""

    async def mark_notices_read(self, person_id: str, notice_ids: tuple[str, ...] | None) -> dict[str, int]:
        """Mark this person's notices read (these ids, or all when None): ``marked`` and ``unread_notices``."""

    async def revoke_grant(self, person_id: str, grant_id: str) -> list[str]:
        """Revoke a grant the person gave or could have given (and what was handed down from it)."""


__all__ = ["MAX_NOTICE_IDS_PER_REQUEST", "AccessInboxApplication", "NoticeQuery"]
