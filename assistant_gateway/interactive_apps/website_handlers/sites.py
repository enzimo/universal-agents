"""Chat on a published page, only for the person the page was made for.

Anyone with a page link can open the page (people open pages from chat
messages, often without signing in), but the page's chat continues its
owner's conversation with the agents. So the chat runs only in the signed-in
Web Chat session of the person the page's trusted record names
(``user_docs.published_sites``), every message is sent as that person, and
only their own sessions are used or created. Anyone else gets a JSON answer
the page turns into a sign-in link.

Before this rule the page carried its owner's session key, the chat needed
no sign-in, and the gateway attributed such a message to the instance's
default person, so anyone holding a link could talk to the agents in the
owner's thread and read the replies.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewayDeliveryEvent,
)
from assistant_gateway.application.website_application import (
    WebsiteGatewayApplication,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_WEBSITE,
)
from id_generation import new_id
from user_docs.published_sites import (
    SITE_CHAT_SIGN_IN_PATH,
    SITE_ID_PATTERN,
    PublishedSiteRecordV1,
    PublishedSiteStore,
)
from assistant_gateway.interactive_apps.website_support import (
    LOGGER,
    _payload_bool,
    _safe_client_id,
)
from .operations import WebsiteSitesHandlersOperations

SITE_CHAT_SIGN_IN_REQUIRED = "sign_in_required"
SITE_CHAT_NOT_PAGE_OWNER = "not_page_owner"


def _chat_refusal(status: type[web.HTTPException], code: str, message: str) -> web.HTTPException:
    """A JSON refusal the page's script shows with a sign-in link."""

    return status(
        text=json.dumps(
            {"status": code, "reply": message, "sign_in_url": SITE_CHAT_SIGN_IN_PATH}
        ),
        content_type="application/json",
    )


class WebsiteSitesHandlers:
    """Own sites HTTP behavior with explicit state and bound operations."""

    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        logger: logging.Logger | None,
        published_sites: PublishedSiteStore,
    ) -> None:
        self.operations: WebsiteSitesHandlersOperations
        self._logger = logger or LOGGER
        self.application = application
        self.channel_name = DELIVERY_CHANNEL_WEBSITE
        self.published_sites = published_sites

    async def _handle_site_chat(self, request: web.Request) -> web.Response:
        site_id = self._validated_site_id(request)
        record = self._record_or_404(site_id)
        person = self._page_owner(request, record)
        payload = await self._json_object(request)
        text = str(payload.get("message") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="message is required.")

        client_id = _safe_client_id(payload.get("client_id"))
        conversation_id = str(payload.get("conversation_id") or "").strip() or None
        try:
            session_resolution = self.application.sessions.resolve_generated_site_session(
                site_id=site_id,
                client_id=client_id,
                requested_session_key=str(payload.get("session_key") or "").strip(),
                conversation_id=conversation_id,
                owner_canonical_user_id=person,
            )
        except PermissionError as exc:
            self._logger.warning(
                "A page chat named a thread of another person. event=generated_site_chat_thread_refused "
                "site_id=%s person_id=%s",
                site_id,
                person,
            )
            raise web.HTTPNotFound(text="Thread not found.") from exc
        session_key = session_resolution.session_key

        if text.lower() == "/new":
            session = self.application.start_new_session(
                channel_name=self.channel_name,
                user_id=client_id,
                chat_id=site_id,
                thread_id=site_id,
                canonical_user_id=person,
            )
            return web.json_response(
                {
                    "status": "ok",
                    "reply": f"Started new website chat session: {session.session_key}",
                    "session_key": session.session_key,
                    "conversation_id": session.conversation_id,
                }
            )

        response = await self._submit(
            text,
            site_id=site_id,
            client_id=client_id,
            person=person,
            session_key=session_key,
            conversation_id=conversation_id,
            preserve_session_binding=session_resolution.preserve_channel_binding,
            message_id=str(payload.get("message_id") or new_id("web_message")),
        )

        mirrored = False
        if _payload_bool(payload.get("mirror_to_origin")):
            mirrored = await self._mirror_chat_exchange_to_origin(
                site_id=site_id,
                session_key=session_key,
                conversation_id=response.conversation_id,
                user_text=text,
                assistant_text=response.text,
                correlation_id=response.correlation_id,
            )

        return web.json_response(
            {
                "status": "ok",
                "reply": response.text,
                "session_key": session_key,
                "conversation_id": response.conversation_id,
                "correlation_id": response.correlation_id,
                "task_id": response.task_id,
                "mirrored": mirrored,
            }
        )

    async def _submit(
        self,
        text: str,
        *,
        site_id: str,
        client_id: str,
        person: str,
        session_key: str,
        conversation_id: str | None,
        preserve_session_binding: bool,
        message_id: str,
    ) -> GatewayApplicationResponse:
        """Answer a pending question in the session, or send a new message as ``person``."""

        if not self.application.is_task_progress_status_request(
            text
        ) and await self.application.has_pending_query(session_key):
            return await self.application.reply_to_pending_query(
                session_key=session_key,
                response_text=text,
            )
        route = {
            "channel_name": None if preserve_session_binding else self.channel_name,
            "user_id": None if preserve_session_binding else client_id,
            "chat_id": None if preserve_session_binding else site_id,
            "thread_id": None if preserve_session_binding else site_id,
        }
        try:
            return await self.application.send_user_message(
                text,
                session_key=session_key,
                conversation_id=conversation_id,
                original_message_id=message_id,
                canonical_user_id=person,
                **route,
            )
        except ValueError as exc:
            if conversation_id is None:
                raise web.HTTPBadRequest(text=str(exc)) from exc
            self._logger.warning(
                "Website chat could not use supplied conversation_id=%s for session_key=%s; "
                "falling back to the session's active conversation.",
                conversation_id,
                session_key,
            )
            return await self.application.send_user_message(
                text,
                session_key=session_key,
                conversation_id=None,
                original_message_id=message_id,
                canonical_user_id=person,
                **route,
            )
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc

    def _page_owner(self, request: web.Request, record: PublishedSiteRecordV1) -> str:
        """The signed-in person, who must be the person the page was made for."""

        try:
            web_session = self.operations._require_web_chat_session(request)
        except (web.HTTPUnauthorized, web.HTTPForbidden) as exc:
            raise _chat_refusal(
                web.HTTPUnauthorized,
                SITE_CHAT_SIGN_IN_REQUIRED,
                "Sign in to Web Chat to use this page's chat.",
            ) from exc
        person = self.operations._web_session_owner_scope(web_session)
        if person and person == record.owner_person_id:
            return person
        self._logger.info(
            "A page chat message was refused because the sender is not the page's owner. "
            "event=generated_site_chat_refused site_id=%s person_id=%s owner_person_id=%s",
            record.site_id,
            person or "-",
            record.owner_person_id or "-",
        )
        message = (
            "Only the person this page was made for can use its chat."
            if record.owner_person_id
            else "This page was made by maintenance work for no person, so it has no chat. Use Web Chat instead."
        )
        raise _chat_refusal(web.HTTPForbidden, SITE_CHAT_NOT_PAGE_OWNER, message)

    @staticmethod
    async def _json_object(request: web.Request) -> dict:
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="Request body must be JSON.") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Request body must be a JSON object.")
        return payload

    async def _mirror_chat_exchange_to_origin(
        self,
        *,
        site_id: str,
        session_key: str,
        conversation_id: str | None,
        user_text: str,
        assistant_text: str,
        correlation_id: str | None,
    ) -> bool:
        session = self.application.sessions.get_session_details(session_key)
        if session is None:
            self._logger.info(
                "Website chat mirror skipped because session_key=%s was not found.",
                session_key,
            )
            return False
        mirror_text = (
            f"Web follow-up on {site_id}\n\n"
            f"User: {user_text}\n\n"
            f"Assistant: {assistant_text}"
        )
        if (
            session.channel_name == self.channel_name
            and str(session.chat_id or "") == "web-chat"
        ):
            return await self.application.operations.emit_delivery_event(
                GatewayDeliveryEvent(
                    text=mirror_text,
                    session_key=session.session_key,
                    channel_name=self.channel_name,
                    user_id=session.user_id,
                    chat_id="web-chat",
                    thread_id=session.thread_id,
                    conversation_id=conversation_id,
                    scheduled_job_id=None,
                    correlation_id=correlation_id,
                    original_message_id=None,
                    message_type="generated_site_mirror",
                )
            )
        if not session.channel_name or session.channel_name == self.channel_name:
            return False
        event = GatewayDeliveryEvent(
            text=mirror_text,
            session_key=session.session_key,
            channel_name=session.channel_name,
            user_id=session.user_id,
            chat_id=session.chat_id,
            thread_id=session.thread_id,
            conversation_id=conversation_id,
            scheduled_job_id=None,
            correlation_id=correlation_id,
            original_message_id=None,
        )
        try:
            emitted = await self.application.operations.emit_delivery_event(event)
        except Exception:
            self._logger.warning(
                "Website chat mirror failed for session_key=%s channel=%s.",
                session.session_key,
                session.channel_name,
                exc_info=True,
            )
            return False
        if not emitted:
            self._logger.info(
                "Website chat mirror skipped because no gateway event output is configured "
                "for session_key=%s.",
                session_key,
            )
        return emitted

    def _validated_site_id(self, request: web.Request) -> str:
        site_id = str(request.match_info.get("site_id") or "").strip()
        if not SITE_ID_PATTERN.match(site_id):
            raise web.HTTPNotFound(text="Generated site not found.")
        return site_id

    def _record_or_404(self, site_id: str) -> PublishedSiteRecordV1:
        record = self.published_sites.record(site_id)
        if record is None:
            raise web.HTTPNotFound(text="Generated site not found.")
        return record
