"""Read-only view of the agents' workspaces area for signed-in team members.

The root is ``<instance_root>/workspaces``. Every member sees their own folder
under users/ (the owner asked for "their user folder and nothing else, for
now"); administrators also see the shared team/ and agents/ folders. Other
people's folders stay private even to administrators
(runtime_settings.instance_layout).
"""

from __future__ import annotations

import mimetypes
import os
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal

from assistant_gateway.application.ports import GatewayPrivateResourceAuthorizer
from assistant_gateway.application.website_application import (
    GatewayWorkspaceFileContent,
    WebWorkspaceDirectoryV1,
    WebWorkspaceEntryV1,
    WebWorkspaceFileV1,
)
from assistant_gateway.application.workspace_text_presentation import (
    is_workspace_text_file,
    present_workspace_text,
)
from access_control import safe_open
from access_control.people import is_administrator
from access_control.ownership import RESOURCE_FILE_PATH, WORKSPACE_FILE_READ
from access_control.safe_open import UnsafePath
from access_control.sensitive_paths import is_sensitive_path
from runtime_settings.instance_layout import USERS_DIRNAME, person_folder_name


WORKSPACE_PREVIEW_MAX_BYTES = 5 * 1024 * 1024
WORKSPACE_DIRECTORY_ENTRY_LIMIT = 2_000

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_HTML_SUFFIXES = frozenset({".htm", ".html"})
_PDF_SUFFIXES = frozenset({".pdf"})
_IMAGE_CONTENT_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# Version-control and dependency folders the browser leaves out. They are not
# secrets, so they are kept apart from credential names, which come from the
# list every file check shares (access_control.sensitive_paths).
_VCS_AND_DEPENDENCY_FOLDER_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)

WorkspacePreviewKind = Literal[
    "markdown",
    "html",
    "text",
    "image",
    "pdf",
    "unsupported",
    "too_large",
]


class WorkspaceBrowserError(RuntimeError):
    """Base error for a rejected workspace browser operation."""


class WorkspacePathRejectedError(WorkspaceBrowserError):
    """Raised when a requested path is unsafe or intentionally hidden."""


class WorkspacePathNotFoundError(WorkspaceBrowserError):
    """Raised when a requested workspace entry does not exist."""


class WorkspacePathTypeError(WorkspaceBrowserError):
    """Raised when a request targets the wrong filesystem entry type."""


class WorkspacePreviewUnavailableError(WorkspaceBrowserError):
    """Raised when raw content is not available for the requested file."""


@dataclass(frozen=True)
class GatewayWorkspaceAccess:
    """Trusted actor facts derived from the authenticated Website session."""

    canonical_user_id: str
    roles: tuple[str, ...]


class WebsiteWorkspaceBrowser:
    """List and read safe workspace entries without following symlinks."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        authorizer: GatewayPrivateResourceAuthorizer,
        team_id: str,
    ) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._authorizer = authorizer
        self._team_id = str(team_id or "").strip()

    def list_directory(
        self,
        *,
        access: GatewayWorkspaceAccess,
        path: str,
    ) -> WebWorkspaceDirectoryV1:
        parts = self._path_parts(path)
        self._require_own_person_folder(access, parts)
        relative_path = self._relative_path(parts)
        self._authorize(access=access, relative_path=relative_path or ".")
        with self._open_directory(parts) as directory_fd:
            entries = self._directory_entries(directory_fd, parts)
        if parts == (USERS_DIRNAME,):
            own = person_folder_name(access.canonical_user_id)
            entries = [entry for entry in entries if entry.name == own]
        elif parts == () and not is_administrator(access.roles):
            # Members see only their own folder, reached through users/.
            entries = [entry for entry in entries if entry.name == USERS_DIRNAME]
        truncated = len(entries) > WORKSPACE_DIRECTORY_ENTRY_LIMIT
        visible_entries = entries[:WORKSPACE_DIRECTORY_ENTRY_LIMIT]
        return WebWorkspaceDirectoryV1(
            path=relative_path,
            entries=tuple(visible_entries),
            truncated=truncated,
            entry_limit=WORKSPACE_DIRECTORY_ENTRY_LIMIT,
        )

    def inspect_file(
        self,
        *,
        access: GatewayWorkspaceAccess,
        path: str,
    ) -> WebWorkspaceFileV1:
        parts = self._path_parts(path, allow_root=False)
        self._require_own_person_folder(access, parts)
        relative_path = self._relative_path(parts)
        self._authorize(access=access, relative_path=relative_path)
        with self._open_file(parts) as file_fd:
            file_stat = os.fstat(file_fd)
            preview_kind, content_type, message = self._preview_metadata(
                name=parts[-1],
                size_bytes=file_stat.st_size,
            )
            content = None
            syntax_language = None
            highlighted_html = None
            formatted_content = None
            if preview_kind in {"markdown", "text"}:
                content = self._read_bytes(file_fd, file_stat.st_size).decode(
                    "utf-8-sig",
                    errors="replace",
                )
            if preview_kind == "text" and content is not None:
                presentation = present_workspace_text(
                    name=parts[-1],
                    content=content,
                    size_bytes=file_stat.st_size,
                )
                syntax_language = presentation.syntax_language
                highlighted_html = presentation.highlighted_html
                formatted_content = presentation.formatted_content
        return WebWorkspaceFileV1(
            path=relative_path,
            name=parts[-1],
            size_bytes=file_stat.st_size,
            modified_at_utc=datetime.fromtimestamp(
                file_stat.st_mtime,
                tz=timezone.utc,
            ),
            preview_kind=preview_kind,
            content_type=content_type,
            content=content,
            syntax_language=syntax_language,
            highlighted_html=highlighted_html,
            formatted_content=formatted_content,
            message=message,
        )

    def read_file_content(
        self,
        *,
        access: GatewayWorkspaceAccess,
        path: str,
    ) -> GatewayWorkspaceFileContent:
        parts = self._path_parts(path, allow_root=False)
        self._require_own_person_folder(access, parts)
        relative_path = self._relative_path(parts)
        self._authorize(access=access, relative_path=relative_path)
        with self._open_file(parts) as file_fd:
            file_stat = os.fstat(file_fd)
            preview_kind, content_type, _message = self._preview_metadata(
                name=parts[-1],
                size_bytes=file_stat.st_size,
            )
            if preview_kind not in {"html", "image", "pdf"} or not content_type:
                raise WorkspacePreviewUnavailableError(
                    "Raw preview content is unavailable for this file."
                )
            content = self._read_bytes(file_fd, file_stat.st_size)
        return GatewayWorkspaceFileContent(
            path=relative_path,
            preview_kind=preview_kind,
            content_type=content_type,
            content=content,
        )

    def _authorize(
        self,
        *,
        access: GatewayWorkspaceAccess,
        relative_path: str,
    ) -> None:
        allowed = self._authorizer.can_access(
            actor_canonical_user_id=access.canonical_user_id,
            owner_canonical_user_id=None,
            resource_type=RESOURCE_FILE_PATH,
            resource_id=f"workspace/{relative_path}",
            action=WORKSPACE_FILE_READ,
            team_id=self._team_id,
            roles=access.roles,
        )
        if not allowed:
            raise PermissionError(
                "You can browse only your own folder here; the team's shared folders are for administrators."
            )

    @staticmethod
    def _require_own_person_folder(
        access: GatewayWorkspaceAccess, parts: tuple[str, ...]
    ) -> None:
        """Only the signed-in person's own folder under users/ is visible."""

        if len(parts) >= 2 and parts[0] == USERS_DIRNAME:
            if parts[1] != person_folder_name(access.canonical_user_id):
                raise WorkspacePathRejectedError("Other people's folders are private.")

    @staticmethod
    def _relative_path(parts: tuple[str, ...]) -> str:
        return "/".join(parts)

    @staticmethod
    def _path_parts(path: str, *, allow_root: bool = True) -> tuple[str, ...]:
        raw_path = str(path or "")
        if raw_path == "" and allow_root:
            return ()
        if not raw_path or raw_path.startswith("/") or "\x00" in raw_path:
            raise WorkspacePathRejectedError("Workspace paths must be relative.")
        parts = tuple(raw_path.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise WorkspacePathRejectedError("Workspace path traversal is not allowed.")
        if any("\\" in part for part in parts):
            raise WorkspacePathRejectedError(
                "Workspace paths must use forward slashes."
            )
        if WebsiteWorkspaceBrowser._is_hidden_path(parts):
            raise WorkspacePathRejectedError("This workspace path is not available.")
        return parts

    @staticmethod
    def _is_hidden_path(parts: tuple[str, ...]) -> bool:
        """Whether the browser hides this path: tooling folders and credentials.

        Credential names come from the list every file check shares
        (access_control.sensitive_paths), so the browser never shows a file
        that agent tools and attachments refuse.
        """

        if any(part.casefold() in _VCS_AND_DEPENDENCY_FOLDER_NAMES for part in parts):
            return True
        return is_sensitive_path(PurePosixPath(*parts))

    @contextmanager
    def _open_directory(self, parts: tuple[str, ...]) -> Iterator[int]:
        with self._safely():
            directory_fd = safe_open.open_directory(self._workspace_root, parts)
        try:
            yield directory_fd
        finally:
            os.close(directory_fd)

    @contextmanager
    def _open_file(self, parts: tuple[str, ...]) -> Iterator[int]:
        """A one-link regular file below the workspace, opened without following links."""

        with ExitStack() as stack:
            with self._safely():
                file_fd, _info = stack.enter_context(safe_open.opened_file(self._workspace_root, parts))
            yield file_fd

    @staticmethod
    @contextmanager
    def _safely() -> Iterator[None]:
        """The browser's errors for the shared safe-open refusals (access_control.safe_open)."""

        try:
            yield
        except UnsafePath as exc:
            if exc.reason == safe_open.REASON_ROOT_UNAVAILABLE:
                raise WorkspacePathNotFoundError("The gateway workspace is unavailable.") from exc
            if exc.reason == safe_open.REASON_NOT_FOUND:
                raise WorkspacePathNotFoundError("Workspace path was not found.") from exc
            if exc.reason in {safe_open.REASON_NOT_DIRECTORY, safe_open.REASON_NOT_REGULAR_FILE}:
                raise WorkspacePathTypeError(
                    "Workspace path is not a directory."
                    if exc.reason == safe_open.REASON_NOT_DIRECTORY
                    else "Workspace preview requires a regular file."
                ) from exc
            if exc.reason == safe_open.REASON_SEVERAL_LINKS:
                raise WorkspacePathRejectedError("Multi-linked workspace files are not available.") from exc
            raise WorkspacePathRejectedError("Workspace path could not be opened safely.") from exc

    def _directory_entries(
        self,
        directory_fd: int,
        parent_parts: tuple[str, ...],
    ) -> list[WebWorkspaceEntryV1]:
        entries: list[WebWorkspaceEntryV1] = []
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise WorkspacePathRejectedError(
                "Workspace directory could not be listed safely."
            ) from exc
        for name in names:
            entry_parts = (*parent_parts, name)
            if self._is_hidden_path(entry_parts):
                continue
            try:
                entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if stat.S_ISLNK(entry_stat.st_mode):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                entry_kind: Literal["directory", "file"] = "directory"
                preview_kind = None
                content_type = None
            elif stat.S_ISREG(entry_stat.st_mode):
                if entry_stat.st_nlink != 1:
                    continue
                entry_kind = "file"
                preview_kind, content_type, _message = self._preview_metadata(
                    name=name,
                    size_bytes=entry_stat.st_size,
                )
            else:
                continue
            entries.append(
                WebWorkspaceEntryV1(
                    path=self._relative_path(entry_parts),
                    name=name,
                    kind=entry_kind,
                    size_bytes=(entry_stat.st_size if entry_kind == "file" else None),
                    modified_at_utc=datetime.fromtimestamp(
                        entry_stat.st_mtime,
                        tz=timezone.utc,
                    ),
                    preview_kind=preview_kind,
                    content_type=content_type,
                )
            )
        entries.sort(
            key=lambda entry: (entry.kind != "directory", entry.name.casefold())
        )
        return entries

    @staticmethod
    def _preview_metadata(
        *,
        name: str,
        size_bytes: int,
    ) -> tuple[WorkspacePreviewKind, str | None, str | None]:
        if size_bytes > WORKSPACE_PREVIEW_MAX_BYTES:
            return (
                "too_large",
                None,
                "This file is larger than the 5 MB preview limit.",
            )
        lowered_name = name.casefold()
        suffix = Path(lowered_name).suffix
        if suffix in _MARKDOWN_SUFFIXES:
            return "markdown", "text/markdown", None
        if suffix in _HTML_SUFFIXES:
            return "html", "text/html", None
        if suffix in _IMAGE_CONTENT_TYPES:
            return "image", _IMAGE_CONTENT_TYPES[suffix], None
        if suffix in _PDF_SUFFIXES:
            return "pdf", "application/pdf", None
        guessed_type, _encoding = mimetypes.guess_type(lowered_name)
        if is_workspace_text_file(name=lowered_name, content_type=guessed_type):
            return "text", guessed_type or "text/plain", None
        shown_type = suffix or "this file type"
        return (
            "unsupported",
            None,
            f"Preview is not available for {shown_type} files.",
        )

    @staticmethod
    def _read_bytes(file_fd: int, expected_size: int) -> bytes:
        if expected_size > WORKSPACE_PREVIEW_MAX_BYTES:
            raise WorkspacePreviewUnavailableError(
                "This file is larger than the 5 MB preview limit."
            )
        os.lseek(file_fd, 0, os.SEEK_SET)
        content = bytearray()
        while len(content) <= WORKSPACE_PREVIEW_MAX_BYTES:
            chunk = os.read(
                file_fd,
                min(64 * 1024, WORKSPACE_PREVIEW_MAX_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > WORKSPACE_PREVIEW_MAX_BYTES:
            raise WorkspacePreviewUnavailableError(
                "This file grew beyond the 5 MB preview limit while it was read."
            )
        return bytes(content)
