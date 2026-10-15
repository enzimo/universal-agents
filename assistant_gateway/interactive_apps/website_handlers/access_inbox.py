"""Authenticated HTTP routes for the access Inbox (reviews and notices).

Reads need the signed-in browser session; changes also need the write checks
every permission action uses (same origin, fetch metadata and the CSRF
header). The person is always the session's user. Refusals from the access
service are written for people, so their text is shown as is.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from access_control.decisions import HandDownRefused
from assistant_gateway.application.access_inbox_ports import MAX_NOTICE_IDS_PER_REQUEST, NoticeQuery
from assistant_gateway.application.website_application import WebsiteGatewayApplication
from assistant_gateway.interactive_apps.website_handlers.pagination import list_page_query
from assistant_gateway.interactive_apps.website_handlers.operations import (
    WebsiteSignedInHandlersOperations,
)

LOGGER = logging.getLogger(__name__)
SIGN_IN_TEXT = "Sign in with a registered user account to see or change access."
# The Notices list's two views (`?notices=`), as the Inbox page sends them.
NOTICES_UNREAD = "unread"
NOTICES_ALL = "all"
NOT_SHOWN_TEXT = "This request is not available to you. Only people who can decide it, or whose work it is for, can see it."


class SignedInAccessHandlers:
    """Shared by the access Inbox and the Access page: the signed-in person and plain refusals."""

    refusal_event = "web_access_inbox_refused"

    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        operations: WebsiteSignedInHandlersOperations,
    ) -> None:
        self.application = application
        self.operations = operations

    def _person(self, request: web.Request, *, write: bool) -> str:
        session = (
            self.operations._require_permission_write_authority(request)
            if write
            else self.operations._require_permission_browser_session(request)
        )
        person_id = str(session.canonical_user_id or "").strip()
        if not person_id:
            raise web.HTTPForbidden(text=SIGN_IN_TEXT)
        return person_id

    def _refused(self, exc: Exception, *, action: str, person_id: str) -> web.Response:
        LOGGER.warning(
            "Access action refused. event=%s action=%s person_id=%s reason=%s",
            self.refusal_event,
            action,
            person_id,
            exc,
        )
        return self.operations._permission_json_response({"status": "refused", "error": str(exc)}, status=409)


def _notice_query(request: web.Request) -> NoticeQuery:
    """Which notices to list: ``notices=unread`` (default) or ``all``, paged by ``offset`` and ``limit``."""

    shown = str(request.query.get("notices") or NOTICES_UNREAD).strip()
    if shown not in {NOTICES_UNREAD, NOTICES_ALL}:
        raise web.HTTPBadRequest(text="notices must be unread or all.")
    return NoticeQuery(unread_only=shown == NOTICES_UNREAD, **list_page_query(request))


def _notice_ids(payload: dict[str, Any]) -> tuple[str, ...] | None:
    """The notices a Mark-as-read request names: ``{"all": true}`` (None) or ``{"notice_ids": [...]}``."""

    if payload.get("all") is True:
        return None
    ids = payload.get("notice_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item.strip() for item in ids):
        raise web.HTTPBadRequest(text="Choose at least one notice to mark as read.")
    if len(ids) > MAX_NOTICE_IDS_PER_REQUEST:
        raise web.HTTPBadRequest(text=f"Mark at most {MAX_NOTICE_IDS_PER_REQUEST} notices at a time.")
    return tuple(item.strip() for item in ids)


class WebsiteAccessInboxHandlers(SignedInAccessHandlers):
    async def list(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=False)
        inbox = await self.application.access_inbox.inbox(person_id, _notice_query(request))
        return self.operations._permission_json_response({"status": "ok", **inbox})

    async def counts(self, request: web.Request) -> web.Response:
        """The Settings badges: requests this person can decide and their unread notices."""

        person_id = self._person(request, write=False)
        counts = await self.application.access_inbox.counts(person_id)
        return self.operations._permission_json_response({"status": "ok", **counts})

    async def review(self, request: web.Request) -> web.Response:
        """One request in any status, for the Web Chat card (so it shows a stored decision too)."""

        person_id = self._person(request, write=False)
        request_id = str(request.match_info.get("request_id") or "").strip()
        review = await self.application.access_inbox.review(person_id, request_id)
        if review is None:
            # Missing and hidden look the same, so ids reveal nothing.
            LOGGER.info(
                "An access request was not shown. event=web_access_review_not_shown request_id=%s person_id=%s",
                request_id,
                person_id,
            )
            return self.operations._permission_json_response(
                {"status": "not_found", "error": NOT_SHOWN_TEXT}, status=404
            )
        return self.operations._permission_json_response({"status": "ok", "review": review})

    async def decide(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        payload = await self.operations._json_payload(request)
        request_id = str(request.match_info.get("request_id") or "").strip()
        try:
            keep = payload.get("keep")
            kept = payload.get("kept_item_ids") or []
            hours = payload.get("keep_hours")
            review = await self.application.access_inbox.decide(
                person_id,
                request_id,
                str(payload.get("choice") or ""),
                str(payload.get("note") or ""),
                keep=keep if isinstance(keep, bool) else None,
                kept_item_ids=tuple(str(item) for item in kept if isinstance(item, str))[:80],
                keep_hours=hours if isinstance(hours, int) and not isinstance(hours, bool) else None,
                # The card sends its "may hand it on" box; absent means no box (yes).
                hand_on=payload["hand_on"] is True if "hand_on" in payload else None,
            )
        except HandDownRefused as exc:
            return self._refused(exc, action="decide", person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", "review": review})

    async def mark_notices_read(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        notice_ids = _notice_ids(await self.operations._json_payload(request))
        result = await self.application.access_inbox.mark_notices_read(person_id, notice_ids)
        LOGGER.info(
            "Notices marked as read. event=web_access_notices_read person_id=%s all=%s requested=%s marked=%s",
            person_id,
            notice_ids is None,
            0 if notice_ids is None else len(notice_ids),
            result["marked"],
        )
        return self.operations._permission_json_response({"status": "ok", **result})

    async def revoke_grant(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        grant_id = str(request.match_info.get("grant_id") or "").strip()
        try:
            revoked = await self.application.access_inbox.revoke_grant(person_id, grant_id)
        except HandDownRefused as exc:
            return self._refused(exc, action="revoke", person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", "revoked_grant_ids": revoked})


__all__ = ["SignedInAccessHandlers", "WebsiteAccessInboxHandlers"]
