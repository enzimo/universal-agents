from __future__ import annotations
import asyncio
import functools
import logging
import threading
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
    GatewayWorkspaceFileDownload,
    WebsiteGatewayApplication,
)
from assistant_gateway.interactive_apps.website_support import (
    LOGGER,
    _WORKSPACE_HTML_PREVIEW_CSP,
)
from .operations import WebsiteWorkspaceHandlersOperations
from .workspace_download import (
    DOWNLOAD_CHUNK_BYTES,
    accepts_html_navigation,
    build_attachment_disposition,
    download_security_headers,
    parse_byte_range,
    unauthenticated_download_redirect_location,
)


class _InactiveWorkspaceMemberError(PermissionError):
    """Raised when a signed-in session no longer maps to an active team member."""


class _DownloadOpenCancelledError(Exception):
    """Internal signal that a download open was cancelled before opening a descriptor."""


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

        Members browse their own folder and team/; administrators also see
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

    def _open_workspace_download_sync(
        self,
        *,
        canonical_user_id: str,
        requested_path: str,
        cancelled: threading.Event | None = None,
    ) -> GatewayWorkspaceFileDownload:
        """Resolve member roles and open the download descriptor off the event loop."""

        if cancelled is not None and cancelled.is_set():
            raise _DownloadOpenCancelledError()
        person = self.application.access_inbox.person(canonical_user_id)
        if person is None:
            raise _InactiveWorkspaceMemberError(
                "You are not an active member of this team."
            )
        if cancelled is not None and cancelled.is_set():
            raise _DownloadOpenCancelledError()
        download = self.application.operations.open_web_chat_workspace_download(
            canonical_user_id=canonical_user_id,
            roles=tuple(person.roles),
            path=requested_path,
        )
        if cancelled is not None and cancelled.is_set():
            download.close()
        return download

    @staticmethod
    def _schedule_download_close(
        loop: asyncio.AbstractEventLoop,
        download: GatewayWorkspaceFileDownload,
        logger: logging.Logger,
    ) -> asyncio.Future[None] | None:
        """Schedule download.close() off-loop and consume any background error."""

        try:
            close_future = loop.run_in_executor(None, download.close)
        except RuntimeError:
            download.close()
            return None

        def _consume_close_error(future: asyncio.Future[None]) -> None:
            if future.cancelled():
                return
            exc = future.exception()
            if exc is not None:
                logger.warning(
                    "event=workspace_download_close_failed path=%r error=%s",
                    download.path,
                    exc,
                )

        close_future.add_done_callback(_consume_close_error)
        return close_future

    @classmethod
    async def _close_download_safely(
        cls,
        download: GatewayWorkspaceFileDownload,
        logger: logging.Logger | None = None,
    ) -> None:
        """Close the download descriptor off-loop without blocking on cancellation."""

        loop = asyncio.get_running_loop()
        close_future = cls._schedule_download_close(
            loop,
            download,
            logger or LOGGER,
        )
        if close_future is not None:
            await asyncio.shield(close_future)

    async def _open_download_off_loop(
        self,
        *,
        canonical_user_id: str,
        requested_path: str,
    ) -> GatewayWorkspaceFileDownload:
        """Open a download descriptor off-loop and close it immediately if cancelled."""

        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        open_future = loop.run_in_executor(
            None,
            functools.partial(
                self._open_workspace_download_sync,
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
                cancelled=cancelled,
            ),
        )
        try:
            return await asyncio.shield(open_future)
        except asyncio.CancelledError:
            cancelled.set()

            def _close_orphaned_open(
                future: asyncio.Future[GatewayWorkspaceFileDownload],
            ) -> None:
                if future.cancelled() or future.exception() is not None:
                    return
                orphaned = future.result()
                if not orphaned.closed:
                    self._schedule_download_close(loop, orphaned, self._logger)

            open_future.add_done_callback(_close_orphaned_open)
            raise

    def _unauthenticated_download_response(
        self,
        request: web.Request,
        requested_path: str,
    ) -> web.Response | None:
        if request.method != "GET" or not accepts_html_navigation(
            request.headers.get("Accept")
        ):
            return None
        location = unauthenticated_download_redirect_location(requested_path)
        self._logger.info(
            "event=workspace_download_auth_redirect path=%r location=%r",
            requested_path,
            location,
        )
        return web.Response(
            status=302,
            headers={
                "Location": location,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @staticmethod
    def _unsatisfiable_range_response(
        request: web.Request,
        size_bytes: int,
    ) -> web.Response:
        headers = {
            **download_security_headers(),
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes */{size_bytes}",
        }
        if request.method == "HEAD":
            return web.Response(status=416, body=b"", headers=headers)
        return web.Response(
            status=416,
            text="Requested byte range is not satisfiable.",
            headers=headers,
        )

    async def _stream_download_body(
        self,
        *,
        request: web.Request,
        response: web.StreamResponse,
        download: GatewayWorkspaceFileDownload,
        start: int,
        length: int,
        canonical_user_id: str,
        requested_path: str,
    ) -> web.StreamResponse:
        offset = start
        remaining = length
        try:
            while remaining > 0:
                chunk = await asyncio.to_thread(
                    download.read_at,
                    offset,
                    min(DOWNLOAD_CHUNK_BYTES, remaining),
                )
                if not chunk:
                    self._logger.warning(
                        "event=workspace_download_truncated actor=%s path=%r "
                        "offset=%d remaining_bytes=%d expected_size=%d",
                        canonical_user_id,
                        requested_path,
                        offset,
                        remaining,
                        download.size_bytes,
                    )
                    response.force_close()
                    if request.transport is not None:
                        request.transport.close()
                    return response
                await response.write(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            await response.write_eof()
        except ConnectionResetError:
            self._logger.info(
                "event=workspace_download_client_disconnected actor=%s path=%r "
                "offset=%d remaining_bytes=%d",
                canonical_user_id,
                requested_path,
                offset,
                remaining,
            )
        except OSError as exc:
            self._logger.warning(
                "event=workspace_download_read_failed actor=%s path=%r "
                "offset=%d remaining_bytes=%d error=%s",
                canonical_user_id,
                requested_path,
                offset,
                remaining,
                exc,
            )
            response.force_close()
            if request.transport is not None:
                request.transport.close()
        return response

    async def _handle_web_chat_workspace_download(
        self,
        request: web.Request,
    ) -> web.StreamResponse:
        """Stream an authenticated workspace file download with byte-range support."""

        requested_path = str(request.query.get("path") or "")
        try:
            web_session = self.operations._require_web_chat_session(request)
        except web.HTTPUnauthorized:
            redirect = self._unauthenticated_download_response(request, requested_path)
            if redirect is not None:
                return redirect
            raise
        canonical_user_id = str(web_session.canonical_user_id or "").strip()
        if not canonical_user_id:
            raise web.HTTPForbidden(
                text="Workspace files need a signed-in team member."
            )
        try:
            download = await self._open_download_off_loop(
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
            )
        except _InactiveWorkspaceMemberError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except (PermissionError, WorkspaceBrowserError) as exc:
            self._raise_workspace_http_error(
                error=exc,
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
            )
        try:
            range_headers = request.headers.getall("Range", ())
            combined_range = ", ".join(range_headers) if range_headers else None
            range_result = parse_byte_range(
                combined_range,
                download.size_bytes,
            )
            if range_result.status == "unsatisfiable":
                return self._unsatisfiable_range_response(request, download.size_bytes)

            headers = {
                **download_security_headers(),
                "Accept-Ranges": "bytes",
                "Content-Disposition": build_attachment_disposition(download.name),
            }
            if range_result.status == "satisfiable" and range_result.byte_range is not None:
                start = range_result.byte_range.start
                length = range_result.byte_range.length
                status = 206
                headers["Content-Range"] = (
                    f"bytes {range_result.byte_range.start}-"
                    f"{range_result.byte_range.end}/{download.size_bytes}"
                )
            else:
                start = 0
                length = download.size_bytes
                status = 200

            response = web.StreamResponse(status=status, headers=headers)
            response.content_type = download.content_type
            response.content_length = length
            await response.prepare(request)
            if request.method == "HEAD":
                await response.write_eof()
                return response
            return await self._stream_download_body(
                request=request,
                response=response,
                download=download,
                start=start,
                length=length,
                canonical_user_id=canonical_user_id,
                requested_path=requested_path,
            )
        finally:
            await self._close_download_safely(download, self._logger)
