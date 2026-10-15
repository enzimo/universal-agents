"""HTTP routes of the Web Chat Knowledge page (``/webchat?view=knowledge``).

Every route needs the signed-in browser session; changes also need the write
checks every Settings change uses (same origin, fetch metadata and the CSRF
header). Requests name the browser's own Web Chat thread (``session_key``),
which must belong to the signed-in person (``_permission_api_context``). The
person is always the session's user; the browser never names one.

Like the other Web Chat routes, these have no tool catalog entries: they are
people's own controls, protected by the session, the write checks and the
ownership rule (``access_control.knowledge_access``), not agent operations.

What a request may do is decided by the manager's knowledge service; a
refusal comes back with its stable code, plain words, and, for content held
for review or never stored, where it is (never the text itself).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from aiohttp import web
from pydantic import ValidationError

from assistant_gateway.application.knowledge_pages import GatewayKnowledgeRefused
from assistant_gateway.application.website_application import WebsiteGatewayApplication
from assistant_gateway.interactive_apps.website_handlers.operations import WebsiteKnowledgeHandlersOperations
from assistant_gateway.interactive_apps.website_support import WebChatSession
from custom_messages.contracts.control_v1 import (
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
)

LOGGER = logging.getLogger(__name__)

# HTTP status for each refusal code of ManagerKnowledgeCommand.
REFUSAL_STATUS = {
    "authorization_denied": 403,
    "conflict": 409,
    "page_exists": 409,
    "merged": 409,
    "review_required": 409,
    "sensitive_content": 422,
    "not_found": 404,
    "invalid_request": 400,
    "knowledge_off": 503,
    "runtime_error": 503,
}
# The browser's change routes (`.../pages/{space}/{page}/{change}`), as knowledge actions.
PAGE_CHANGES = {
    "save": KnowledgeCommandAction.WRITE,
    "notes": KnowledgeCommandAction.ADD_NOTE,
    "archive": KnowledgeCommandAction.ARCHIVE,
    "revert": KnowledgeCommandAction.REVERT,
}
CONTRIBUTION_DECISIONS = {
    "publish": KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
    "withdraw": KnowledgeCommandAction.WITHDRAW_CONTRIBUTION,
}
# What a change request's body may set; everything else comes from the route or the session.
CHANGE_FIELDS = ("title", "summary", "body", "tags", "note", "confirm_review", "base_revision", "revision", "edited")
NOT_VALID_TEXT = (
    "Nothing was saved because the request was not valid (a value is missing, too long or of the wrong kind). "
    "Check the fields and try again."
)
MANAGER_SILENT_TEXT = (
    "The manager did not answer in time, so the result is unknown. Refresh to see whether the change was saved."
)
NOT_ALLOWED_TEXT = "You cannot open or change this. Only the person whose pages or proposals these are can."


class WebsiteKnowledgeHandlers:
    """The Knowledge page's routes (module docstring)."""

    def __init__(
        self, *, application: WebsiteGatewayApplication, operations: WebsiteKnowledgeHandlersOperations
    ) -> None:
        self.application = application
        self.operations = operations

    async def access(self, request: web.Request) -> web.Response:
        """Whether the knowledge base is on, so the Settings rail shows the Knowledge link only then."""

        self._signed_in(request)
        return self.operations._permission_json_response(
            {"status": "ok", "enabled": self.application.knowledge.enabled}
        )

    async def list_pages(self, request: web.Request) -> web.Response:
        query = request.query
        return await self._read(
            request,
            {
                "action": KnowledgeCommandAction.LIST,
                "space": query.get("space", "team"),
                "status": query.get("status", "active"),
                "query": query.get("query", ""),
                "offset": query.get("offset", "0"),
            },
        )

    async def read_page(self, request: web.Request) -> web.Response:
        fields = {"action": KnowledgeCommandAction.READ, **_page_route(request)}
        if request.query.get("revision"):
            fields["revision"] = request.query["revision"]
        return await self._read(request, fields)

    async def history(self, request: web.Request) -> web.Response:
        return await self._read(request, {"action": KnowledgeCommandAction.HISTORY, **_page_route(request)})

    async def list_contributions(self, request: web.Request) -> web.Response:
        return await self._read(
            request,
            {"action": KnowledgeCommandAction.LIST_CONTRIBUTIONS, "offset": request.query.get("offset", "0")},
        )

    async def counts(self, request: web.Request) -> web.Response:
        """How many proposals wait for the signed-in person: the My proposals,
        Knowledge link and cog badges (ui/DESIGN.md, "Attention counts")."""

        web_session = self._signed_in(request)
        session_key = str(request.query.get("session_key") or "")
        fields = {"action": KnowledgeCommandAction.COUNT_CONTRIBUTIONS}
        result = await self._answer(request, web_session, session_key, fields)
        if isinstance(result, web.Response):
            return result
        return self.operations._permission_json_response({"status": "ok", "pending_proposals": result.total})

    async def read_contribution(self, request: web.Request) -> web.Response:
        return await self._read(
            request,
            {
                "action": KnowledgeCommandAction.READ_CONTRIBUTION,
                "contribution_id": request.match_info.get("contribution_id", ""),
            },
        )

    async def create_page(self, request: web.Request) -> web.Response:
        route = {"action": KnowledgeCommandAction.WRITE, "space": request.match_info.get("space", "")}
        return await self._change(request, route, new_page=True)

    async def change_page(self, request: web.Request) -> web.Response:
        action = PAGE_CHANGES.get(request.match_info.get("change", ""))
        if action is None:
            raise web.HTTPNotFound(text="There is no such change.")
        return await self._change(request, {"action": action, **_page_route(request)})

    async def decide_contribution(self, request: web.Request) -> web.Response:
        action = CONTRIBUTION_DECISIONS.get(request.match_info.get("decision", ""))
        if action is None:
            raise web.HTTPNotFound(text="There is no such decision.")
        route = {"action": action, "contribution_id": request.match_info.get("contribution_id", "")}
        return await self._change(request, route)

    # -- shared --------------------------------------------------------------------

    def _signed_in(self, request: web.Request) -> WebChatSession:
        return self.operations._require_permission_browser_session(request)

    async def _read(self, request: web.Request, fields: dict[str, Any]) -> web.Response:
        web_session = self._signed_in(request)
        return await self._run(request, web_session, str(request.query.get("session_key") or ""), fields)

    async def _change(
        self, request: web.Request, route: dict[str, Any], *, new_page: bool = False
    ) -> web.Response:
        web_session = self.operations._require_permission_write_authority(request)
        body = await self.operations._json_payload(request)
        fields = {name: body[name] for name in CHANGE_FIELDS if name in body}
        if new_page:
            # A new page is always created, never an edit of one with the same name.
            fields["base_revision"] = 0
        return await self._run(request, web_session, str(body.get("session_key") or ""), {**fields, **route})

    async def _run(
        self, request: web.Request, web_session: WebChatSession, session_key: str, fields: Mapping[str, Any]
    ) -> web.Response:
        result = await self._answer(request, web_session, session_key, fields)
        if isinstance(result, web.Response):
            return result
        return self.operations._permission_json_response({"status": "ok", **result.model_dump(mode="json")})

    async def _answer(
        self, request: web.Request, web_session: WebChatSession, session_key: str, fields: Mapping[str, Any]
    ) -> KnowledgeCommandResultPayload | web.Response:
        """The manager's result for one request, or the refusal to send the browser."""

        session_key = session_key.strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        # Checking that the thread is the person's reads the sessions (website_handlers.off_loop).
        context = await asyncio.to_thread(
            self.operations._permission_api_context, request, web_session, session_key=session_key
        )
        try:
            payload = KnowledgeCommandPayload.model_validate(dict(fields))
        except ValidationError as exc:
            LOGGER.info(
                "A Knowledge page request was not valid. event=web_knowledge_invalid person_id=%s errors=%s",
                context.canonical_user_id,
                [".".join(str(part) for part in error["loc"]) for error in exc.errors()],
            )
            return self._refused("invalid_request", NOT_VALID_TEXT)
        try:
            result = await self.application.knowledge.run(context, payload)
        except GatewayKnowledgeRefused as refused:
            LOGGER.info(
                "A Knowledge page request was refused. event=web_knowledge_refused person_id=%s action=%s code=%s",
                context.canonical_user_id,
                payload.action.value,
                refused.code,
            )
            return self._refused(
                refused.code,
                refused.message,
                review_flags=[flag.model_dump(mode="json") for flag in refused.review_flags],
                current_revision=refused.current_revision,
                merged_into=refused.merged_into,
            )
        except PermissionError:
            return self._refused("authorization_denied", NOT_ALLOWED_TEXT)
        except TimeoutError:
            LOGGER.warning(
                "The manager did not answer a Knowledge page request. event=web_knowledge_timeout "
                "person_id=%s action=%s",
                context.canonical_user_id,
                payload.action.value,
            )
            return self._refused("runtime_error", MANAGER_SILENT_TEXT)
        except RuntimeError as exc:
            # The manager is not running (GatewayManagerCommandService's unavailable text).
            return self._refused("runtime_error", str(exc))
        return result

    def _refused(self, code: str, message: str, **details: Any) -> web.Response:
        return self.operations._permission_json_response(
            {"status": "refused", "error": {"code": code, "message": message}, **details},
            status=REFUSAL_STATUS.get(code, 400),
        )


def _page_route(request: web.Request) -> dict[str, str]:
    return {"space": request.match_info.get("space", ""), "page": request.match_info.get("page", "")}


__all__ = ["WebsiteKnowledgeHandlers"]
