"""Bounded inspection tools for user-provided task attachments.

Uploads by id are stored in the runtime's own records, out of agents' reach,
and found, read and copied through ``custom_tools.attachment_uploads``: the
current request's files, and for the team lead's own request the files of its
conversation's earlier turns. A file named by path is checked and read once
through the agent file library (``custom_tools.file_access``), and the
inspection works on those bytes, so a link swapped in afterwards changes
nothing.
"""

from __future__ import annotations

import io
import json
import mimetypes
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from strands import tool

from custom_tools import file_access
from custom_tools.attachment_uploads import (
    REMOTE_COPY_ACTION,
    STATUS_REMOTE_COPY_REQUIRED,
    OpenedUpload,
    UploadUnreadable,
    copy_upload,
    read_upload,
    upload_by_id,
)
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import PERSON_UPLOADS_DIRNAME


DEFAULT_MAX_CHARS = 6000
HARD_MAX_CHARS = 12000
DEFAULT_MAX_LINES = 80
HARD_MAX_LINES = 200
TEXT_CONTENT_TYPES = {
    "application/json",
    "application/xml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
}
TEXT_SUFFIXES = {".csv", ".html", ".json", ".md", ".txt", ".xml", ".yaml", ".yml"}
# Inspection reads the whole file once; larger files return metadata only.
MAX_INSPECTED_BYTES = 64 * 1024 * 1024


def _json_result(status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": status, "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}]}


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _path_summary(path: Optional[Path], size_bytes: Optional[int] = None) -> Dict[str, Any]:
    """How results name a file in the task's folders (named by path)."""

    content_type = mimetypes.guess_type(str(path or ""))[0] or "application/octet-stream"
    return {
        "attachment_id": None,
        "filename": path.name if path else None,
        "content_type": content_type,
        "kind": _kind_for_content_type(content_type),
        "size_bytes": size_bytes,
        "sha256": None,
        "storage_refs": [{"storage_type": "local", "path": str(path)}] if path else [],
    }


def _kind_for_content_type(content_type: str) -> str:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("text/") or normalized in TEXT_CONTENT_TYPES:
        return "document"
    return "other"


def _is_text_like(*, content_type: str, path: Path) -> bool:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in TEXT_CONTENT_TYPES or path.suffix.lower() in TEXT_SUFFIXES


def _bounded_text(data: bytes, *, max_chars: int, max_lines: int) -> Dict[str, Any]:
    lines: list[dict[str, Any]] = []
    chars = 0
    truncated = False
    text = data.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        if len(lines) >= max_lines or chars >= max_chars:
            truncated = True
            break
        remaining = max_chars - chars
        chunk = line[:remaining]
        lines.append({"line": line_number, "text": chunk.rstrip("\n")})
        chars += len(chunk)
        if len(chunk) < len(line):
            truncated = True
            break
    return {
        "line_count_returned": len(lines),
        "max_lines": max_lines,
        "max_chars": max_chars,
        "truncated": truncated,
        "lines": lines,
    }


def _pdf_excerpt(data: bytes, *, max_chars: int) -> Dict[str, Any]:
    try:
        import pypdf  # type: ignore[import-not-found]
    except Exception:
        return {
            "parser": "unsupported",
            "message": "PDF text extraction requires pypdf, which is not available in this runtime.",
        }
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        chunks: list[str] = []
        chars = 0
        pages_returned = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            if not text:
                pages_returned += 1
                continue
            remaining = max_chars - chars
            if remaining <= 0:
                break
            chunks.append(text[:remaining])
            chars += min(len(text), remaining)
            pages_returned += 1
            if len(text) > remaining:
                break
        return {
            "parser": "pypdf",
            "page_count": len(reader.pages),
            "pages_returned": pages_returned,
            "max_chars": max_chars,
            "truncated": chars >= max_chars,
            "text": "\n".join(chunks),
        }
    except Exception as exc:
        return {"parser": "pypdf", "error": str(exc)}


def _copy_into_task_folder(upload: OpenedUpload) -> Path:
    """Copy one of this task's uploads (a runtime record) into the task's own folder."""

    home = current_workspace_scope().home
    attachment = upload.attachment
    name = Path(str(attachment.filename or upload.path.name)).name or upload.path.name
    return copy_upload(upload, home / PERSON_UPLOADS_DIRNAME / f"{attachment.attachment_id}_{name}")


@tool
def inspect_user_attachment(
    attachment_id: str = "",
    path: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
    copy_to_workspace: bool = False,
) -> Dict[str, Any]:
    """
    Inspect a user-provided task attachment without returning raw binary bytes.

    Prefer passing an `attachment_id`: a file sent with the current request,
    with guidance or an answer while it ran, or earlier in this conversation
    (the prompt lists them). Text-like documents return bounded line excerpts.
    Images, audio, video, Office files, and other binary files return metadata
    only. If the attachment has no readable local file, report
    `remote_copy_required` to the team lead instead of pasting bytes or base64
    into task messages. A file past the team's retention period is no longer
    stored; ask the person to send it again.

    Uploads are stored where shell and Python cannot reach them. Set
    `copy_to_workspace=true` with an `attachment_id` to copy the file into this
    task's own folder (uploads/ inside it); `workspace_path` in the result is the
    copy to process with shell or Python.
    """
    requested_chars = _safe_int(max_chars, default=DEFAULT_MAX_CHARS, minimum=1, maximum=HARD_MAX_CHARS)
    requested_lines = _safe_int(max_lines, default=DEFAULT_MAX_LINES, minimum=1, maximum=HARD_MAX_LINES)
    source = _source(attachment_id, path)
    if isinstance(source, dict):
        return source
    content_type = str(source.summary.get("content_type") or mimetypes.guess_type(source.name.name)[0] or "")
    payload: Dict[str, Any] = {
        "attachment": source.summary,
        "bytes_returned_inline": 0,
        "binary_bytes_included": False,
    }
    if source.upload is None:
        payload["local_path"] = str(source.path)
    upload_to_copy = source.upload if copy_to_workspace else None
    if copy_to_workspace and upload_to_copy is None:
        return _json_result("error", {"error": "copy_to_workspace needs an attachment_id."})
    try:
        payload["inspection"] = _inspection(source, content_type, max_chars=requested_chars, max_lines=requested_lines)
    except UploadUnreadable as exc:
        # A file the person sent, removed or unreadable after it was found: a result that names no path.
        return exc.result
    except (OSError, file_access.PathPolicyError) as exc:
        # Only a file named by path fails here: an upload's read failures are UploadUnreadable.
        return _json_result("error", {"error": str(exc), "path": str(source.path)})
    if upload_to_copy is not None:
        # The copy is the last read of the upload. An upload removed before the copy opens it gives an
        # error and leaves no copy; once the copy has opened it, the copy completes even if the file is
        # removed, so a result with workspace_path always means the copy was made. Only reading the
        # upload is handled: a failure writing the task's own folder is raised as before.
        try:
            payload["workspace_path"] = str(_copy_into_task_folder(upload_to_copy))
        except UploadUnreadable as exc:
            return exc.result
    return _json_result("success", payload)


@dataclass(frozen=True)
class _Source:
    """The file to inspect: an upload found by id, or a file in the task's folders named by path.

    ``upload`` is set for a file the person sent, ``path`` for a file named by
    path. ``name`` decides the file's type by its suffix: the name it was sent
    with for an upload, the path itself otherwise. ``summary`` is how the
    result names the file; an upload's never says where it is stored.
    ``read`` returns at most ``max_bytes`` bytes of the file that was checked
    (``UploadUnreadable`` for an upload that cannot be read).
    """

    upload: Optional[OpenedUpload]
    path: Optional[Path]
    size_bytes: int
    name: Path
    summary: Dict[str, Any]
    read: Callable[[int], bytes]


def _source(attachment_id: str, path: str) -> _Source | Dict[str, Any]:
    """The file to inspect, or the tool result that explains why there is none."""

    if attachment_id:
        upload = upload_by_id(attachment_id)
        if isinstance(upload, dict):
            return upload
        name = Path(Path(str(upload.attachment.filename or "")).name or upload.path.name)
        return _Source(
            upload, None, upload.size_bytes, name, upload.summary(), lambda max_bytes: read_upload(upload, max_bytes)
        )
    if not path:
        return _json_result("error", {"error": "Provide attachment_id or path."})
    try:
        location, entry = file_access.status(path)
    except Exception as exc:
        return _json_result("error", {"error": str(exc), "path": path})
    if entry is None or not stat.S_ISREG(entry.st_mode):
        return _json_result(
            STATUS_REMOTE_COPY_REQUIRED,
            {
                "attachment": _path_summary(location.path),
                "local_path": str(location.path),
                "reason": "local attachment path is unavailable to this agent",
                "team_lead_action": REMOTE_COPY_ACTION,
            },
        )
    resolved_path = location.path

    def read(max_bytes: int) -> bytes:
        # Read through the library: the bytes are those of the checked file, never a swapped link.
        return file_access.read_bytes(resolved_path, max_bytes=max_bytes)[1]

    return _Source(None, resolved_path, entry.st_size, resolved_path, _path_summary(resolved_path, entry.st_size), read)


def _inspection(source: _Source, content_type: str, *, max_chars: int, max_lines: int) -> Dict[str, Any]:
    """A bounded text or PDF excerpt, read once; metadata only for anything else."""

    text_like = _is_text_like(content_type=content_type, path=source.name)
    pdf = source.name.suffix.lower() == ".pdf" or content_type == "application/pdf"
    if not (text_like or pdf):
        return {
            "kind": "metadata_only",
            "message": "Binary attachment metadata returned. Raw bytes and base64 are intentionally omitted.",
        }
    if source.size_bytes > MAX_INSPECTED_BYTES:
        return {
            "kind": "metadata_only",
            "message": f"The file is larger than {MAX_INSPECTED_BYTES} bytes, so only its metadata is returned.",
        }
    data = source.read(MAX_INSPECTED_BYTES)
    if text_like:
        return {"kind": "text_excerpt", **_bounded_text(data, max_chars=max_chars, max_lines=max_lines)}
    return {"kind": "pdf_text", **_pdf_excerpt(data, max_chars=max_chars)}
