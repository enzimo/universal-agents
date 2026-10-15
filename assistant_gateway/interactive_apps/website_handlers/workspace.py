from __future__ import annotations
import logging
from typing import NoReturn
from aiohttp import web
from assistant_gateway.application.workspace_browser import (
    WorkspaceBrowserError,
    WorkspacePathNotFoundError,
    WorkspacePathRejectedError,
    WorkspacePathTypeError,
    WorkspacePreviewUnavailableError,
)
from assistant_gateway.application.website_application import (
    WebsiteGatewayApplication,
)
from assistant_gateway.interactive_apps.website_support import (
    LOGGER,
    _WORKSPACE_HTML_PREVIEW_CSP,
)
from .operations import WebsiteWorkspaceHandlersOperations


class WebsiteWorkspaceHandlers:
    """Own workspace HTTP behavior with explicit state and bound operations."""

    def __init__(
        self, *, application: WebsiteGatewayApplication, logger: logging.Logger | None
    ) -> None:
        self.operations: WebsiteWorkspaceHandlersOperations
        self._logger = logger or LOGGER
        self.application = application

    def _workspace_access(
        self,
        request: web.Request,
    ) -> tuple[str, tuple[str, ...]]:
        """The signed-in person and their current team roles (never from the browser).

        Members browse their own folder; administrators also see team/ and
        agents/ (access_control.ownership decides each path).
        """

        web_session = self.operations._require_web_chat_session(request)
        canonical_user_id = str(web_session.canonical_user_id or "").strip()
        if not canonical_user_id:
            raise web.HTTPForbidden(text="Workspace files need a signed-in team member.")
        person = self.application.access_inbox.person(canonical_user_id)
        if person is None:
            raise web.HTTPForbidden(text="You are not an active member of this team.")
        return canonical_user_id, tuple(person.roles)

    def _raise_workspace_http_error(
        self,
        *,
        error: Exception,
        canonical_user_id: str,
        requested_path: str,
    ) -> NoReturn:
        if isinstance(error, PermissionError):
            self._logger.warning(
                "event=workspace_browser_access_denied actor=%s path=%r reason=%s",
                canonical_user_id,
                requested_path,
                error,
            )
            raise web.HTTPForbidden(text=str(error)) from error
        if isinstance(error, WorkspacePathNotFoundError):
            raise web.HTTPNotFound(text=str(error)) from error
        if isinstance(error, WorkspacePathTypeError):
            raise web.HTTPBadRequest(text=str(error)) from error
        if isinstance(error, WorkspacePreviewUnavailableError):
            raise web.HTTPUnsupportedMediaType(text=str(error)) from error
        if isinstance(error, WorkspacePathRejectedError):
            self._logger.warning(
                "event=workspace_browser_path_rejected actor=%s path=%r reason=%s",
                canonical_user_id,
                requested_path,
                error,
            )
            raise web.HTTPForbidden(text=str(error)) from error
        raise web.HTTPInternalServerError(
            text="Workspace preview failed safely."
        ) from error

    async def _handle_web_chat_workspace_directory(
        self,
        request: web.Request,
    ) -> web.Response:
        canonical_user_id, roles = self._workspace_access(request)
        requested_path = str(request.query.get("path") or "")
        try:
            directory = self.application.operations.list_web_chat_workspace_directory(
                canonical_user_id=canonical_user_id,
                roles=roles,
                path=requested_path,
            )
        except (PermissionError, WorkspaceBrowserError) as exc:
            self._raise_workspace_http_error(
                error=exc,
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
            )
        response = web.json_response(
            {"status": "ok", **directory.model_dump(mode="json")}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_web_chat_workspace_file(
        self,
        request: web.Request,
    ) -> web.Response:
        canonical_user_id, roles = self._workspace_access(request)
        requested_path = str(request.query.get("path") or "")
        try:
            file_view = self.application.operations.inspect_web_chat_workspace_file(
                canonical_user_id=canonical_user_id,
                roles=roles,
                path=requested_path,
            )
        except (PermissionError, WorkspaceBrowserError) as exc:
            self._raise_workspace_http_error(
                error=exc,
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
            )
        response = web.json_response(
            {"status": "ok", **file_view.model_dump(mode="json", exclude_none=True)}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _handle_web_chat_workspace_content(
        self,
        request: web.Request,
    ) -> web.Response:
        canonical_user_id, roles = self._workspace_access(request)
        requested_path = str(request.query.get("path") or "")
        try:
            content = self.application.operations.read_web_chat_workspace_content(
                canonical_user_id=canonical_user_id,
                roles=roles,
                path=requested_path,
            )
        except (PermissionError, WorkspaceBrowserError) as exc:
            self._raise_workspace_http_error(
                error=exc,
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
            )
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": "inline",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        if content.preview_kind == "html":
            headers.update(
                {
                    "Content-Security-Policy": _WORKSPACE_HTML_PREVIEW_CSP,
                    "X-Frame-Options": "SAMEORIGIN",
                }
            )
            return web.Response(
                body=content.content,
                content_type=content.content_type,
                charset="utf-8",
                headers=headers,
            )
        return web.Response(
            body=content.content,
            content_type=content.content_type,
            headers=headers,
        )
