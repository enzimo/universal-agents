"""Bounded inspection tools for user-provided task attachments.

Uploads are named by ``attachment_id``: the runtime decides which files the
task may use (its own and, for a top-level task, its conversation's earlier
files) and reads them only from this host's upload folders
(``custom_tools.task_uploads``); results by id never show where they are
stored. A file named by path is checked and read once through the agent file
library (``custom_tools.file_access``), and the inspection works on those
bytes, so a link swapped in afterwards changes nothing.
"""

from __future__ import annotations

import io
import shutil
import mimetypes
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from strands import tool

from custom_tools import file_access
from custom_tools.task_uploads import (
    TaskUpload,
    described,
    open_task_upload,
    read_failed,
    remote_copy_required,
    tool_result,
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


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _path_summary(path: Path, size_bytes: Optional[int] = None) -> Dict[str, Any]:
    """How a by-path result names the file: the workspace path the caller gave, resolved."""

    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return {
        "attachment_id": None,
        "filename": path.name,
        "content_type": content_type,
        "kind": _kind_for_content_type(content_type),
        "size_bytes": size_bytes,
        "sha256": None,
        "storage_refs": [{"storage_type": "local", "path": str(path)}],
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


def _copy_into_task_folder(upload: TaskUpload) -> Path:
    """Copy an upload the task may use into the task's own folder, where shell and Python can reach it."""

    attachment, located = upload.attachment, upload.located
    home = current_workspace_scope().home
    name = Path(str(attachment.filename or located.path.name)).name or located.path.name
    target = home / PERSON_UPLOADS_DIRNAME / f"{attachment.attachment_id}_{name}"
    with located.reader() as reader, file_access.writable(target) as (location, writer):
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
    return location.path


@tool
def inspect_user_attachment(
    attachment_id: str = "",
    path: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
    copy_to_workspace: bool = False,
) -> Dict[str, Any]:
    """
    Inspect a file the person sent, or a file in this task's folders, without returning raw binary bytes.

    Name a file the person sent by its `attachment_id`. Ids are listed in the
    prompt (this request's files and the files sent earlier in this
    conversation), in a guidance or answer that came with files, and per turn
    by get_conversation_context_slice. A task delegated to you can open only
    the files its delegation gave it. Use `path` only for a file in this
    task's folders.

    Text-like documents return bounded line excerpts. Images, audio, video,
    Office files, and other binary files return metadata only.

    When a file cannot be opened, `result` says why: `unknown_attachment_id`
    (this task may not use that id; `available_attachments` lists the files
    it may use), `upload_unavailable` (the file was deleted; ask the person to
    send it again), or `remote_copy_required` (it is stored on another host;
    report this to the team lead instead of pasting bytes or base64 into task
    messages).

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
    summary = source.summary()
    content_type = str(summary.get("content_type") or mimetypes.guess_type(source.path.name)[0] or "")
    payload: Dict[str, Any] = {"attachment": summary, "bytes_returned_inline": 0, "binary_bytes_included": False}
    if source.upload is None:
        if copy_to_workspace:
            return tool_result("error", {"error": "copy_to_workspace needs an attachment_id."})
        payload["local_path"] = str(source.path)  # the path the caller named, in the task's folders
    try:
        if copy_to_workspace and source.upload is not None:
            payload["workspace_path"] = str(_copy_into_task_folder(source.upload))
        payload["inspection"] = _inspection(source, content_type, max_chars=requested_chars, max_lines=requested_lines)
    except (OSError, file_access.PathPolicyError) as exc:
        return _read_failed(source, exc)
    return tool_result("success", payload)


@dataclass(frozen=True)
class _Source:
    """The file to inspect: an upload the task may use (by id) or a file in the task's folders (by path).

    ``path`` is where the bytes are; it is shown only for a file named by path.
    """

    path: Path
    size_bytes: int
    upload: Optional[TaskUpload] = None

    def summary(self) -> Dict[str, Any]:
        if self.upload is not None:
            return described(self.upload.attachment)
        return _path_summary(self.path, self.size_bytes)

    def read(self, *, max_bytes: int) -> bytes:
        """The bytes, read once: an upload from its upload folder, a file through the agent file library."""

        if self.upload is not None:
            return self.upload.located.read_bytes(max_bytes=max_bytes)
        return file_access.read_bytes(self.path, max_bytes=max_bytes)[1]


def _source(attachment_id: str, path: str) -> _Source | Dict[str, Any]:
    """The file to inspect, or the tool result that explains why there is none."""

    if attachment_id:
        upload = open_task_upload(attachment_id)
        if isinstance(upload, dict):
            return upload
        return _Source(upload.located.path, upload.located.size_bytes, upload)
    if not path:
        return tool_result("error", {"error": "Provide attachment_id or path."})
    try:
        location, entry = file_access.status(path)
    except Exception as exc:
        return tool_result("error", {"error": str(exc), "path": path})
    if entry is None or not stat.S_ISREG(entry.st_mode):
        return remote_copy_required(
            {
                "attachment": _path_summary(location.path),
                "local_path": str(location.path),
                "reason": "local attachment path is unavailable to this agent",
            }
        )
    return _Source(location.path, entry.st_size)


def _read_failed(source: _Source, exc: OSError) -> Dict[str, Any]:
    """The result when the file could not be read or copied; an upload's error never shows where it is stored."""

    if source.upload is None:
        return tool_result("error", {"error": str(exc), "path": str(source.path)})
    return tool_result("error", read_failed(source.upload.attachment, exc))


def _inspection(source: _Source, content_type: str, *, max_chars: int, max_lines: int) -> Dict[str, Any]:
    """A bounded text or PDF excerpt, read once; metadata only for anything else."""

    text_like = _is_text_like(content_type=content_type, path=source.path)
    pdf = source.path.suffix.lower() == ".pdf" or content_type == "application/pdf"
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
    data = source.read(max_bytes=MAX_INSPECTED_BYTES)
    if text_like:
        return {"kind": "text_excerpt", **_bounded_text(data, max_chars=max_chars, max_lines=max_lines)}
    return {"kind": "pdf_text", **_pdf_excerpt(data, max_chars=max_chars)}
