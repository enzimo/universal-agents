from __future__ import annotations
from assistant_gateway.interactive_apps.website_support import require_website_runtime
import logging
import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from aiohttp import web
from assistant_gateway.application.authorization_errors import (
    present_authorization_error,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_application import (
    WebsiteGatewayApplication,
)
from assistant_gateway.interactive_apps.host_ports import (
    InteractiveAppRuntimeContext,
    InternalWebsiteHostEnvironment,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_WEBSITE,
)
from assistant_gateway.interactive_apps.website_support import (
    LOGGER,
    WebChatSession,
    _WEB_CHAT_SESSION_COOKIE,
    _WEB_CHAT_SESSION_TTL_SECONDS,
)
from .off_loop import off_loop, take_checked_person
from .operations import WebsiteAccessHandlersOperations


class WebsiteAccessHandlers:
    """Own access HTTP behavior with explicit state and bound operations."""

    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        environment: InternalWebsiteHostEnvironment,
        logger: logging.Logger | None,
        runtime: InteractiveAppRuntimeContext | None,
    ) -> None:
        self.operations: WebsiteAccessHandlersOperations
        self._logger = logger or LOGGER
        self._web_chat_sessions: dict[str, WebChatSession] = {}
        self.application = application
        self.channel_name = DELIVERY_CHANNEL_WEBSITE
        self.environment = environment
        self.runtime = runtime

    async def _handle_web_chat_auth(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        token = str(payload.get("token") or "").strip()
        identity = self.application.identity.authenticate_web_chat_token(token)
        if identity is None:
            raise web.HTTPUnauthorized(
                text="Invalid Web Chat token or invitation code."
            )
        canonical_user_id = identity.canonical_user_id
        display_name = identity.display_name
        session_token, expires_at = self._create_web_chat_session(
            auth_kind=identity.auth_kind,
            canonical_user_id=canonical_user_id,
            display_name=display_name,
            credential_kind=identity.credential_kind,
            enrollment_required=identity.enrollment_required,
            enrollment_label=identity.enrollment_label,
        )
        user_bound = canonical_user_id is not None
        is_bootstrap_user = identity.credential_kind == "bootstrap_token"
        response = web.json_response(
            {
                "status": "ok",
                "assistant_name": self.environment.assistant_display_name,
                "session_expires_at": expires_at,
                "user_bound": user_bound,
                "canonical_user_id": canonical_user_id,
                "display_name": display_name,
                "auth_kind": identity.auth_kind,
                "credential_kind": identity.credential_kind,
                "is_bootstrap_user": is_bootstrap_user,
                "permanent_token_required": identity.enrollment_required,
                "permanent_token_status": (
                    "required" if identity.enrollment_required else "complete"
                ),
                "enrollment_label": identity.enrollment_label,
                "permission_csrf_token": self._web_chat_sessions[
                    session_token
                ].permission_csrf_token,
            }
        )
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            _WEB_CHAT_SESSION_COOKIE,
            session_token,
            max_age=_WEB_CHAT_SESSION_TTL_SECONDS,
            httponly=True,
            secure=self._web_chat_cookie_secure(),
            samesite="Strict",
            path="/",
        )
        return response

    @off_loop
    def _handle_web_chat_session(self, request: web.Request) -> web.Response:
        session = self._require_web_chat_session(
            request,
            allow_enrollment_pending=True,
        )
        response = web.json_response(self._web_chat_session_payload(session))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_web_chat_enrollment_token(
        self,
        request: web.Request,
    ) -> web.Response:
        session_token = self._web_chat_session_token(request)
        session = self._require_web_chat_session(
            request,
            allow_enrollment_pending=True,
        )
        if (
            not session.enrollment_required
            or session.credential_kind != "invite"
            or not session.canonical_user_id
        ):
            raise web.HTTPForbidden(
                text=(
                    "This Web Chat session does not require permanent token enrollment."
                )
            )
        pending_expires_at_utc = datetime.fromtimestamp(
            session.expires_at,
            tz=timezone.utc,
        ).isoformat()
        issued = self.application.identity.issue_web_user_enrollment_token(
            canonical_user_id=session.canonical_user_id,
            label=session.enrollment_label,
            pending_expires_at_utc=pending_expires_at_utc,
            replacement_token_id=session.pending_token_id,
        )
        if issued is None:
            raise web.HTTPConflict(
                text=("The pending permanent token could not be generated or replaced.")
            )
        updated_session = replace(
            session,
            pending_token_id=issued.token_id,
        )
        self._web_chat_sessions[session_token] = updated_session
        response = web.json_response(
            {
                "status": "ok",
                "token": issued.raw_token,
                "label": issued.label,
                "permanent_token_status": "pending_confirmation",
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_web_chat_enrollment_complete(
        self,
        request: web.Request,
    ) -> web.Response:
        session_token = self._web_chat_session_token(request)
        session = self._require_web_chat_session(
            request,
            allow_enrollment_pending=True,
        )
        if (
            not session.enrollment_required
            or session.credential_kind != "invite"
            or not session.canonical_user_id
        ):
            raise web.HTTPForbidden(
                text=(
                    "This Web Chat session does not require permanent token enrollment."
                )
            )
        if not session.pending_token_id:
            raise web.HTTPConflict(
                text=(
                    "Generate and save a permanent token before completing enrollment."
                )
            )
        activated = self.application.identity.activate_web_user_enrollment_token(
            canonical_user_id=session.canonical_user_id,
            token_id=session.pending_token_id,
        )
        if activated is None:
            raise web.HTTPConflict(
                text=("The pending permanent token expired or could not be activated.")
            )
        updated_session = replace(
            session,
            credential_kind="user_token",
            enrollment_required=False,
            pending_token_id=None,
        )
        self._web_chat_sessions[session_token] = updated_session
        response = web.json_response(self._web_chat_session_payload(updated_session))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_web_chat_logout(self, request: web.Request) -> web.Response:
        session_token = self._web_chat_session_token(request)
        if session_token:
            session = self._web_chat_sessions.pop(session_token, None)
            if session and session.canonical_user_id and session.pending_token_id:
                self.application.identity.revoke_pending_web_user_enrollment_token(
                    canonical_user_id=session.canonical_user_id,
                    token_id=session.pending_token_id,
                )
        response = web.json_response({"status": "ok"})
        response.headers["Cache-Control"] = "no-store"
        response.del_cookie(
            _WEB_CHAT_SESSION_COOKIE,
            path="/",
            secure=self._web_chat_cookie_secure(),
            httponly=True,
            samesite="Strict",
        )
        return response

    def _web_chat_cookie_secure(self) -> bool:
        public_origin = str(self.environment.public_origin or "").strip()
        return urlparse(public_origin).scheme.lower() == "https"

    async def _handle_web_chat_session_context(self, request: web.Request) -> web.Response:
        """Create or resolve the browser's thread as a gateway session before a Settings change.

        MCP and procedure controls act as the person whose gateway session
        this is (``SignedInPerson``); a thread that has not sent a message yet
        has no session until this call.
        """

        web_session = self._require_permission_write_authority(request)
        payload = await self._json_payload(request)
        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(text="session_key is required.")
        if not web_session.canonical_user_id:
            raise web.HTTPForbidden(text="Sign in with a registered user account to change settings.")
        existing = self.application.sessions.get_session_details(session_key)
        if existing is not None and not self._web_session_can_access_session(web_session, session_key):
            raise web.HTTPNotFound(text="Thread not found.")
        try:
            session = self.application.sessions.ensure_web_chat_session(
                session_key=session_key,
                canonical_user_id=web_session.canonical_user_id,
            )
        except PermissionError as exc:
            raise web.HTTPNotFound(text="Thread not found.") from exc
        if session.session_key != session_key or not self._web_session_can_access_session(web_session, session_key):
            raise web.HTTPNotFound(text="Thread not found.")
        return self._permission_json_response({"status": "ok", "session_key": session.session_key})

    def _permission_api_context(
        self,
        request: web.Request,
        web_session: WebChatSession,
        *,
        session_key: str,
    ) -> GatewayRouteContextV1:
        if not web_session.canonical_user_id:
            raise web.HTTPForbidden(
                text="Sign in with a registered user account to use this setting."
            )
        if not self._web_session_can_access_session(web_session, session_key):
            raise web.HTTPNotFound(text="Thread not found.")
        return GatewayRouteContextV1(
            session_key=session_key,
            channel_name=self.channel_name,
            user_id=web_session.canonical_user_id,
            chat_id="web-chat",
            canonical_user_id=web_session.canonical_user_id,
        )

    def _require_permission_browser_session(
        self, request: web.Request
    ) -> WebChatSession:
        """Require the browser-only cookie channel for permission state."""

        if request.headers.get("Authorization") or request.headers.get(
            "X-Web-Chat-Session"
        ):
            raise web.HTTPForbidden(
                text="Open Settings from the signed-in Web Chat page to review access requests."
            )
        if not str(request.cookies.get(_WEB_CHAT_SESSION_COOKIE) or "").strip():
            raise web.HTTPUnauthorized(
                text="Your sign-in session expired. Sign in again and retry."
            )
        return self._require_web_chat_session(request)

    @staticmethod
    def _normalized_origin(value: str) -> str:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    def _require_permission_write_authority(
        self,
        request: web.Request,
    ) -> WebChatSession:
        """Bind a browser mutation to its cookie, origin, and in-memory CSRF token."""

        session = self._require_permission_browser_session(request)
        origin = self._normalized_origin(str(request.headers.get("Origin") or ""))
        configured_origins = {
            self._normalized_origin(self.environment.base_url),
            self._normalized_origin(str(self.environment.public_origin or "")),
            self._normalized_origin(f"{request.scheme}://{request.host}"),
        }
        configured_origins.discard("")
        if not origin or origin not in configured_origins:
            raise web.HTTPForbidden(
                text="This page cannot submit access decisions. Reopen the signed-in Web Chat page."
            )
        fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site and fetch_site != "same-origin":
            raise web.HTTPForbidden(
                text="This page cannot submit access decisions. Reopen the signed-in Web Chat page."
            )
        csrf_token = str(request.headers.get("X-UA-Permission-CSRF") or "").strip()
        if not csrf_token or not secrets.compare_digest(
            csrf_token,
            session.permission_csrf_token,
        ):
            raise web.HTTPForbidden(
                text="Your sign-in session expired. Sign in again and retry."
            )
        return session

    @staticmethod
    def _permission_json_response(
        payload: dict[str, Any],
        *,
        status: int = 200,
    ) -> web.Response:
        response = web.json_response(payload, status=status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    def _authorization_error_response(
        self,
        error: Exception,
        *,
        operation: str,
    ) -> web.Response:
        presentation = present_authorization_error(
            error,
            operation=operation,
            presentation_mode=require_website_runtime(
                self.runtime
            ).authorization_presentation_mode,
        )
        self._logger.warning(
            "Authorization browser action failed. "
            "event=web_chat_authorization_action_failed operation=%s "
            "error_code=%s internal_error=%s",
            operation,
            presentation.code,
            str(error),
            exc_info=True,
        )
        return self._permission_json_response(
            presentation.payload(),
            status=presentation.status_code,
        )

    async def _json_payload(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="Request body must be JSON.") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Request body must be a JSON object.")
        return payload

    def _web_session_can_access_session(
        self, web_session: WebChatSession, session_key: str
    ) -> bool:
        return self.application.sessions.can_access_web_chat_session(
            session_key=session_key,
            owner_canonical_user_id=self._web_session_owner_scope(web_session),
            allow_missing=web_session.auth_kind != "user",
        )

    def _web_session_owner_scope(self, web_session: WebChatSession) -> str | None:
        return str(web_session.canonical_user_id or "").strip() or None

    def _required_web_chat_owner(self, web_session: WebChatSession) -> str:
        owner = self._web_session_owner_scope(web_session)
        if owner is None:
            raise web.HTTPForbidden(
                text="Web Chat transcript access requires a canonical user."
            )
        return owner

    def _create_web_chat_session(
        self,
        *,
        auth_kind: str,
        canonical_user_id: str | None = None,
        display_name: str | None = None,
        credential_kind: str = "bootstrap_token",
        enrollment_required: bool = False,
        enrollment_label: str = "",
    ) -> tuple[str, int]:
        self._prune_web_chat_sessions()
        session_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + _WEB_CHAT_SESSION_TTL_SECONDS
        self._web_chat_sessions[session_token] = WebChatSession(
            expires_at=float(expires_at),
            auth_kind=auth_kind,
            permission_csrf_token=secrets.token_urlsafe(32),
            canonical_user_id=canonical_user_id,
            display_name=display_name,
            credential_kind=credential_kind,
            enrollment_required=enrollment_required,
            enrollment_label=enrollment_label,
        )
        return session_token, expires_at

    def _prune_web_chat_sessions(self) -> None:
        now = time.time()
        # A copy: checks also run in worker threads (website_handlers.off_loop).
        expired = [
            token
            for token, session in tuple(self._web_chat_sessions.items())
            if session.expires_at <= now
        ]
        for token in expired:
            self._web_chat_sessions.pop(token, None)

    def _session_person(self, request: web.Request) -> str | None:
        """The person of this request's live session, read from memory only (website_handlers.off_loop)."""

        session = self._web_chat_sessions.get(self._web_chat_session_token(request))
        if session is None or session.expires_at <= time.time():
            return None
        return session.canonical_user_id

    def _may_use_web_chat(self, request: web.Request, person_id: str) -> bool:
        """Whether the person may still use Web Chat: the answer the request's middleware read off the
        loop, once, else the database now (website_handlers.off_loop)."""

        checked = take_checked_person(request, person_id)
        if checked is not None:
            return checked
        return self.application.identity.is_web_chat_user_active(person_id)

    def _require_web_chat_session(
        self,
        request: web.Request,
        *,
        allow_enrollment_pending: bool = False,
    ) -> WebChatSession:
        self._prune_web_chat_sessions()
        session_token = self._web_chat_session_token(request)
        session = self._web_chat_sessions.get(session_token)
        if session is None or session.expires_at <= time.time():
            raise web.HTTPUnauthorized(text="Web Chat session is invalid or expired.")
        if session.auth_kind == "user" and not session.canonical_user_id:
            self._web_chat_sessions.pop(session_token, None)
            raise web.HTTPUnauthorized(
                text="Web Chat user session has no canonical identity."
            )
        if session.canonical_user_id and not self._may_use_web_chat(request, session.canonical_user_id):
            self._web_chat_sessions.pop(session_token, None)
            raise web.HTTPUnauthorized(
                text="Web Chat user is disabled or no longer has team access."
            )
        if session.enrollment_required and not allow_enrollment_pending:
            raise web.HTTPForbidden(
                text=("Complete permanent token enrollment before using Web Chat.")
            )
        return session

    def _web_chat_session_payload(
        self,
        session: WebChatSession,
    ) -> dict[str, Any]:
        permanent_token_status = "complete"
        if session.enrollment_required:
            permanent_token_status = (
                "pending_confirmation" if session.pending_token_id else "required"
            )
        return {
            "status": "ok",
            "assistant_name": self.environment.assistant_display_name,
            "session_expires_at": int(session.expires_at),
            "user_bound": session.canonical_user_id is not None,
            "canonical_user_id": session.canonical_user_id,
            "display_name": session.display_name,
            "auth_kind": session.auth_kind,
            "credential_kind": session.credential_kind,
            "is_bootstrap_user": (session.credential_kind == "bootstrap_token"),
            "permanent_token_required": session.enrollment_required,
            "permanent_token_status": permanent_token_status,
            "enrollment_label": session.enrollment_label,
            "permission_csrf_token": session.permission_csrf_token,
        }

    @staticmethod
    def _web_chat_session_token(request: web.Request) -> str:
        auth_header = str(request.headers.get("Authorization") or "")
        session_token = ""
        if auth_header.lower().startswith("bearer "):
            session_token = auth_header[7:].strip()
        session_token = (
            session_token
            or str(request.headers.get("X-Web-Chat-Session") or "").strip()
            or str(request.cookies.get(_WEB_CHAT_SESSION_COOKIE) or "").strip()
        )
        return session_token

    def _require_bootstrap_web_chat_session(
        self, request: web.Request
    ) -> WebChatSession:
        session = self._require_web_chat_session(request)
        if session.auth_kind != "bootstrap":
            raise web.HTTPForbidden(
                text="This Web Chat action requires a bootstrap/admin session."
            )
        return session
