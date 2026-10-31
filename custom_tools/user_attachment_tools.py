"""Bounded inspection tools for user-provided task attachments.

Uploads by id are stored in the runtime's own records, out of agents' reach.
A file named by path is checked and read once through the agent file library
(``custom_tools.file_access``), and the inspection works on those bytes, so a
link swapped in afterwards changes nothing.
"""

from __future__ import annotations

import io
import json
import shutil
import mimetypes
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from strands import tool

from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_tools import file_access
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import PERSON_UPLOADS_DIRNAME
from custom_tools.runtime_context import bound_task_id, get_agent_ref


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


def _current_task_attachments() -> list[UserTaskAttachment]:
    agent = get_agent_ref()
    if agent is None:
        return []
    task_id = bound_task_id()
    task_info = None
    task_manager = getattr(agent, "task_manager", None)
    if task_id and task_manager is not None and hasattr(task_manager, "get_task_info"):
        task_info = task_manager.get_task_info(task_id)

    attachments = [
        attachment
        for attachment in list(getattr(getattr(task_info, "task", None), "attachments", None) or [])
        if isinstance(attachment, UserTaskAttachment)
    ]
    for steering in list(getattr(agent, "_pending_proactive_task_steering", {}).get(task_id, []) or []):
        attachments.extend(
            attachment
            for attachment in list(getattr(steering, "attachments", None) or [])
            if isinstance(attachment, UserTaskAttachment)
        )
    return attachments


def _attachment_local_path(attachment: UserTaskAttachment) -> Optional[str]:
    for ref in list(attachment.storage_refs or []):
        if ref.storage_type == "local" and ref.path:
            return ref.path
    return None


def _find_attachment(attachment_id: str) -> Optional[UserTaskAttachment]:
    normalized = str(attachment_id or "").strip()
    for attachment in _current_task_attachments():
        if attachment.attachment_id == normalized:
            return attachment
    return None


def _attachment_summary(
    attachment: Optional[UserTaskAttachment], path: Optional[Path], size_bytes: Optional[int] = None
) -> Dict[str, Any]:
    if attachment is None:
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
    return {
        "attachment_id": attachment.attachment_id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "created_at": attachment.created_at,
        "storage_refs": [ref.model_dump(exclude_none=True) for ref in list(attachment.storage_refs or [])],
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


def _copy_into_task_folder(attachment: UserTaskAttachment, source: Path) -> Path:
    """Copy one of this task's uploads (a runtime record) into the task's own folder."""

    home = current_workspace_scope().home
    name = Path(str(attachment.filename or source.name)).name or source.name
    target = home / PERSON_UPLOADS_DIRNAME / f"{attachment.attachment_id}_{name}"
    with source.open("rb") as reader, file_access.writable(target) as (location, writer):
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
    Inspect a user-provided task attachment without returning raw binary bytes.

    Prefer passing an `attachment_id` from the current task's attachment list.
    Text-like documents return bounded line excerpts. Images, audio, video,
    Office files, and other binary files return metadata only. If the attachment
    reference has no readable local file, report `remote_copy_required` to the
    team lead instead of pasting bytes or base64 into task messages.

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
    attachment, resolved_path, size_bytes = source.attachment, source.path, source.size_bytes
    summary = _attachment_summary(attachment, resolved_path, size_bytes)
    content_type = str(summary.get("content_type") or mimetypes.guess_type(resolved_path.name)[0] or "")
    payload: Dict[str, Any] = {
        "attachment": summary,
        "local_path": str(resolved_path),
        "bytes_returned_inline": 0,
        "binary_bytes_included": False,
    }
    if copy_to_workspace:
        if attachment is None:
            return _json_result(
                "error", {"error": "copy_to_workspace needs an attachment_id."}
            )
        payload["workspace_path"] = str(_copy_into_task_folder(attachment, resolved_path))
    try:
        payload["inspection"] = _inspection(source, content_type, max_chars=requested_chars, max_lines=requested_lines)
    except (OSError, file_access.PathPolicyError) as exc:
        return _json_result("error", {"error": str(exc), "path": str(resolved_path)})
    return _json_result("success", payload)


@dataclass(frozen=True)
class _Source:
    """The file to inspect: an upload record (by id) or a file in the task's folders (by path)."""

    attachment: Optional[UserTaskAttachment]
    path: Path
    size_bytes: int


def _remote_copy_required(attachment: Optional[UserTaskAttachment], path: Optional[Path], reason: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"attachment": _attachment_summary(attachment, path)}
    if path is not None:
        payload["local_path"] = str(path)
    payload["reason"] = reason
    payload["team_lead_action"] = "Request a gateway-backed NATS Object Store mirror for this attachment."
    return _json_result("remote_copy_required", payload)


def _source(attachment_id: str, path: str) -> _Source | Dict[str, Any]:
    """The file to inspect, or the tool result that explains why there is none."""

    if attachment_id:
        attachment = _find_attachment(attachment_id)
        if attachment is None:
            return _json_result(
                "error",
                {
                    "error": "attachment_id was not found on the current task",
                    "attachment_id": attachment_id,
                    "available_attachment_ids": [item.attachment_id for item in _current_task_attachments()],
                },
            )
        local_path = _attachment_local_path(attachment)
        if not local_path:
            return _remote_copy_required(attachment, None, "attachment does not include a local storage reference")
        # Uploads are runtime records, out of agents' reach, so the path is trusted.
        resolved = Path(local_path).expanduser().resolve()
        if not resolved.is_file():
            return _remote_copy_required(attachment, resolved, "local attachment path is unavailable to this agent")
        return _Source(attachment, resolved, resolved.stat().st_size)
    if not path:
        return _json_result("error", {"error": "Provide attachment_id or path."})
    try:
        location, entry = file_access.status(path)
    except Exception as exc:
        return _json_result("error", {"error": str(exc), "path": path})
    if entry is None or not stat.S_ISREG(entry.st_mode):
        return _remote_copy_required(None, location.path, "local attachment path is unavailable to this agent")
    return _Source(None, location.path, entry.st_size)


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
    data = (
        source.path.read_bytes()
        if source.attachment is not None
        else file_access.read_bytes(source.path, max_bytes=MAX_INSPECTED_BYTES)[1]
    )
    if text_like:
        return {"kind": "text_excerpt", **_bounded_text(data, max_chars=max_chars, max_lines=max_lines)}
    return {"kind": "pdf_text", **_pdf_excerpt(data, max_chars=max_chars)}
