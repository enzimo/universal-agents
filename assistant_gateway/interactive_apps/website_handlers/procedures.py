"""Authenticated HTTP routes for installed-procedure reviews (Settings → Team).

Lists need the signed-in browser session; adding or rejecting a proposal also
needs the write checks every Settings change uses (same origin, fetch
metadata and the CSRF header). Every call acts as the session's person on the
browser's current thread, and the service refuses anyone who may not make the
``installed_procedure`` change (``assistant_gateway.installed_procedures``).
Refusals and failures are returned as plain error bodies
(``authorization_errors``).
"""

from __future__ import annotations

from aiohttp import web

from assistant_gateway.application.procedures import ProcedureActionRequestV1, ProcedureListRequestV1
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_application import WebsiteGatewayApplication
from assistant_gateway.interactive_apps.website_support import WebChatSession

from .operations import WebsiteProceduresHandlersOperations
from .pagination import list_page_query

_CONTROL_ERRORS = (PermissionError, RuntimeError, ValueError)


class WebsiteProcedureHandlers:
    def __init__(self, *, application: WebsiteGatewayApplication) -> None:
        self.operations: WebsiteProceduresHandlersOperations
        self.application = application

    def _context(self, request: web.Request, web_session: WebChatSession, session_key: str) -> GatewayRouteContextV1:
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        return self.operations._permission_api_context(request, web_session, session_key=session_key)

    def _read_request(self, request: web.Request) -> ProcedureListRequestV1:
        web_session = self.operations._require_permission_browser_session(request)
        session_key = str(request.query.get("session_key") or "").strip()
        return ProcedureListRequestV1(
            context=self._context(request, web_session, session_key), **list_page_query(request)
        )

    async def _action_request(self, request: web.Request, *, default_reason: str | None) -> ProcedureActionRequestV1:
        web_session = self.operations._require_permission_write_authority(request)
        payload = await self.operations._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        return ProcedureActionRequestV1(
            context=self._context(request, web_session, session_key),
            proposal_id=str(request.match_info.get("proposal_id") or "").strip(),
            explanation=str(payload.get("explanation") or "").strip() or None,
            reason=str(payload.get("reason") or default_reason or "").strip() or None,
        )

    async def list_proposals(self, request: web.Request) -> web.Response:
        read = self._read_request(request)
        try:
            result = self.application.procedures.list_proposals(read)
        except _CONTROL_ERRORS as exc:
            return self.operations._authorization_error_response(exc, operation="load procedure proposals")
        return self.operations._permission_json_response(
            {
                "status": "ok",
                "proposals": result.model_dump(mode="json")["proposals"],
                "has_more": result.has_more,
                "total": result.total,
            }
        )

    async def list_installed(self, request: web.Request) -> web.Response:
        read = self._read_request(request)
        try:
            result = self.application.procedures.list_installed(read)
        except _CONTROL_ERRORS as exc:
            return self.operations._authorization_error_response(exc, operation="load installed procedures")
        return self.operations._permission_json_response(
            {
                "status": "ok",
                "definitions": result.model_dump(mode="json")["definitions"],
                "has_more": result.has_more,
                "total": result.total,
            }
        )

    async def install(self, request: web.Request) -> web.Response:
        action = await self._action_request(request, default_reason=None)
        try:
            result = await self.application.procedures.install(action)
        except _CONTROL_ERRORS as exc:
            return self.operations._authorization_error_response(exc, operation="add this procedure")
        return self.operations._permission_json_response(
            {"status": "ok", **result.model_dump(mode="json", exclude_none=True)}
        )

    async def reject(self, request: web.Request) -> web.Response:
        action = await self._action_request(request, default_reason="administrator_rejected")
        try:
            proposal = self.application.procedures.reject(action)
        except _CONTROL_ERRORS as exc:
            return self.operations._authorization_error_response(exc, operation="reject this procedure proposal")
        return self.operations._permission_json_response(
            {"status": "ok", "proposal": proposal.model_dump(mode="json", exclude_none=True)}
        )


__all__ = ["WebsiteProcedureHandlers"]
