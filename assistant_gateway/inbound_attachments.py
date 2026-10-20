"""Inbound user attachment storage and validation for interactive chat channels."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from runtime_settings.instance_layout import InstanceLayout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from access_control.sensitive_paths import is_sensitive_path
from custom_messages.contracts.attachments_v1 import AttachmentOrigin, AttachmentStorageRef, UserTaskAttachment
from id_generation import new_id

DEFAULT_MAX_FILE_BYTES = 30 * 1024 * 1024
DEFAULT_ALLOWED_KINDS = {"image", "document", "audio", "video"}
# Executable and script file types an upload may not have. Credential file
# names are refused separately, by the list every file check shares
# (access_control.sensitive_paths).
EXECUTABLE_SUFFIXES = {
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".dmg",
    ".exe",
    ".js",
    ".msi",
    ".pif",
    ".ps1",
    ".scr",
    ".sh",
    ".vbs",
}
DOCUMENT_CONTENT_TYPES = {
    "application/json",
    "application/msword",
    "application/pdf",
    "application/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/xml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
}
DOCUMENT_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".json",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".yaml",
    ".yml",
}
FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._ -]+")


class AttachmentValidationError(ValueError):
    """Raised when an inbound attachment cannot be accepted."""


@dataclass(frozen=True)
class StoredInboundAttachment:
    attachment: UserTaskAttachment
    local_path: Path
    expires_at: str | None


@dataclass(frozen=True)
class InboundAttachmentStoragePolicy:
    """Explicit attachment limits and cache location supplied to adapter hosts."""

    cache_dir: Path
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    allowed_kinds: frozenset[str] = frozenset(DEFAULT_ALLOWED_KINDS)


def resolve_inbound_attachment_cache_dir(config: Any) -> Path:
    """state/uploads: agents read an upload by id from their task's attachments."""

    return InstanceLayout.at(config.instance_root_path).uploads_dir


def resolve_web_chat_attachment_cache_dir(config: Any) -> Path:
    return resolve_inbound_attachment_cache_dir(config)


def safe_attachment_session_dir_name(value: str | None) -> str:
    rendered = str(value or "unknown").replace("/", "_").replace(":", "_").strip()
    return rendered or "unknown"


def sanitize_attachment_filename(raw_filename: str | None) -> str:
    filename = Path(str(raw_filename or "attachment").replace("\\", "/")).name.strip()
    filename = FILENAME_SAFE_PATTERN.sub("_", filename)
    filename = re.sub(r"\s+", " ", filename).strip()
    filename = filename.rstrip(".")
    if not filename:
        filename = "attachment"
    if filename.startswith("."):
        filename = "attachment" + filename
    return filename[:160]


def validate_raw_attachment_filename(raw_filename: str | None) -> None:
    rendered = str(raw_filename or "").strip()
    if not rendered:
        raise AttachmentValidationError("Attachment filename is required.")
    normalized = rendered.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) != 1 or any(part == ".." for part in parts):
        raise AttachmentValidationError(f"{rendered} is not an allowed attachment filename.")
    base_name = parts[0].strip()
    # A credential file (an env file, an SSH key, a .pem) is refused by the
    # same names agent tools and the workspace browser refuse.
    if is_sensitive_path(PurePosixPath(base_name)):
        raise AttachmentValidationError(f"{base_name} is a sensitive filename and cannot be attached.")


def infer_attachment_kind(*, content_type: str, filename: str) -> str:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename).suffix.lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("text/") or normalized in DOCUMENT_CONTENT_TYPES or suffix in DOCUMENT_SUFFIXES:
        return "document"
    return "other"


def _allowed_kinds(raw_value: Any) -> set[str]:
    if isinstance(raw_value, str):
        values = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, Iterable):
        values = [str(item).strip() for item in raw_value]
    else:
        values = []
    allowed = {value for value in values if value}
    return allowed or set(DEFAULT_ALLOWED_KINDS)


def _settings(config: Any) -> Any:
    return getattr(getattr(config.interactive_apps, "internal_website", None), "attachments", None)


def _max_file_bytes(config: Any) -> int:
    settings = _settings(config)
    value = getattr(settings, "max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_FILE_BYTES
    return max(1, parsed)


def _new_attachment_id() -> str:
    return new_id("att")


def _expires_at(created_at: datetime, expires_after_seconds: Optional[float]) -> str | None:
    return (
        (created_at + timedelta(seconds=float(expires_after_seconds))).isoformat()
        if expires_after_seconds
        else None
    )


def _stored_attachment(
    *,
    attachment_id: str,
    filename: str,
    content_type: str,
    kind: str,
    size_bytes: int,
    sha256: str,
    local_path: Path,
    origin: AttachmentOrigin,
    caption: Optional[str],
    expires_after_seconds: Optional[float],
) -> StoredInboundAttachment:
    created_at = datetime.now(timezone.utc)
    attachment = UserTaskAttachment(
        attachment_id=attachment_id,
        filename=filename,
        content_type=content_type,
        kind=kind,  # type: ignore[arg-type]
        size_bytes=size_bytes,
        sha256=sha256,
        created_at=created_at.isoformat(),
        origin=origin,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(local_path))],
        caption=caption,
    )
    return StoredInboundAttachment(
        attachment=attachment,
        local_path=local_path,
        expires_at=_expires_at(created_at, expires_after_seconds),
    )


def store_inbound_attachment_bytes(
    *,
    data: bytes,
    filename: str,
    content_type: str = "",
    config: Any,
    session_key: str,
    conversation_id: Optional[str],
    correlation_id: Optional[str] = None,
    expires_after_seconds: Optional[float] = None,
    origin: AttachmentOrigin = "other",
    caption: Optional[str] = None,
) -> StoredInboundAttachment:
    return store_inbound_attachment_bytes_with_policy(
        data=data,
        filename=filename,
        content_type=content_type,
        policy=InboundAttachmentStoragePolicy(
            cache_dir=resolve_inbound_attachment_cache_dir(config),
            max_file_bytes=_max_file_bytes(config),
            allowed_kinds=frozenset(
                _allowed_kinds(getattr(_settings(config), "allowed_kinds", None))
            ),
        ),
        session_key=session_key,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        expires_after_seconds=expires_after_seconds,
        origin=origin,
        caption=caption,
    )


@dataclass(frozen=True)
class CheckedInboundAttachment:
    """An upload that passed the checks, with the name, type and kind it is saved under."""

    filename: str
    content_type: str
    kind: str


def check_inbound_attachment_bytes(
    *,
    data: bytes,
    filename: str,
    content_type: str = "",
    policy: InboundAttachmentStoragePolicy,
) -> CheckedInboundAttachment:
    """Check an upload against the policy without saving anything.

    Saving checks the same way, first; a channel that receives several files
    checks them all before saving any, so a refused file leaves none behind.
    """

    validate_raw_attachment_filename(filename)
    safe_filename = sanitize_attachment_filename(filename)
    size_bytes = len(data)
    if size_bytes > max(1, int(policy.max_file_bytes)):
        raise AttachmentValidationError(f"{safe_filename} exceeds the configured upload limit.")
    if size_bytes <= 0:
        raise AttachmentValidationError(f"{safe_filename} is empty.")
    if Path(safe_filename).suffix.lower() in EXECUTABLE_SUFFIXES:
        raise AttachmentValidationError(
            f"{safe_filename} has a blocked executable or script file type."
        )
    resolved_content_type = (
        str(content_type or "").split(";", 1)[0].strip().lower()
        or mimetypes.guess_type(safe_filename)[0]
        or "application/octet-stream"
    )
    kind = infer_attachment_kind(content_type=resolved_content_type, filename=safe_filename)
    allowed_kinds = set(policy.allowed_kinds) or set(DEFAULT_ALLOWED_KINDS)
    if kind not in allowed_kinds:
        raise AttachmentValidationError(
            f"{safe_filename} has unsupported attachment kind {kind!r}."
        )
    return CheckedInboundAttachment(
        filename=safe_filename, content_type=resolved_content_type, kind=kind
    )


def store_inbound_attachment_bytes_with_policy(
    *,
    data: bytes,
    filename: str,
    content_type: str = "",
    policy: InboundAttachmentStoragePolicy,
    session_key: str,
    conversation_id: Optional[str],
    correlation_id: Optional[str] = None,
    expires_after_seconds: Optional[float] = None,
    origin: AttachmentOrigin = "other",
    caption: Optional[str] = None,
) -> StoredInboundAttachment:
    del conversation_id, correlation_id
    checked = check_inbound_attachment_bytes(
        data=data, filename=filename, content_type=content_type, policy=policy
    )
    attachment_id = _new_attachment_id()
    cache_dir = policy.cache_dir.expanduser().resolve()
    session_dir = cache_dir / safe_attachment_session_dir_name(session_key)
    session_dir.mkdir(parents=True, exist_ok=True)
    local_path = (
        session_dir / f"{attachment_id}{Path(checked.filename).suffix}"
    ).resolve()
    local_path.write_bytes(data)
    return _stored_attachment(
        attachment_id=attachment_id,
        filename=checked.filename,
        content_type=checked.content_type,
        kind=checked.kind,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        local_path=local_path,
        origin=origin,
        caption=caption,
        expires_after_seconds=expires_after_seconds,
    )


def user_task_attachment_from_record(record: Any) -> UserTaskAttachment:
    refs = [AttachmentStorageRef(storage_type="local", path=str(record.local_path))]
    if getattr(record, "nats_bucket", None) and getattr(record, "nats_object_name", None):
        refs.append(
            AttachmentStorageRef(
                storage_type="nats_object",
                bucket=str(record.nats_bucket),
                object_name=str(record.nats_object_name),
            )
        )
    return UserTaskAttachment(
        attachment_id=str(record.attachment_id),
        filename=str(record.filename),
        content_type=str(record.content_type),
        kind=str(record.kind),  # type: ignore[arg-type]
        size_bytes=int(record.size_bytes),
        sha256=str(record.sha256),
        created_at=str(record.created_at),
        storage_refs=refs,
    )


def attachment_public_payload(attachment: UserTaskAttachment, *, url: str | None = None) -> dict[str, Any]:
    return {
        "attachment_id": attachment.attachment_id,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "url": url,
    }


def attachment_debug_summary(attachments: Iterable[UserTaskAttachment] | None, *, max_items: int = 6) -> list[dict[str, Any]]:
    """Return a bounded, byte-free attachment summary for runtime diagnostics."""

    summary: list[dict[str, Any]] = []
    values = list(attachments or [])
    for attachment in values[:max_items]:
        local_path = attachment.local_path()
        local_path_text = str(local_path) if local_path else None
        local_exists = None
        local_size_bytes = None
        if local_path:
            path = Path(str(local_path)).expanduser()
            try:
                local_exists = path.exists() and path.is_file()
                local_size_bytes = path.stat().st_size if local_exists else None
            except OSError:
                local_exists = False
                local_size_bytes = None
        summary.append(
            {
                "attachment_id": attachment.attachment_id,
                "filename": attachment.filename,
                "origin": attachment.origin,
                "kind": attachment.kind,
                "content_type": attachment.content_type,
                "size_bytes": attachment.size_bytes,
                "sha256": attachment.sha256,
                "caption_present": bool(attachment.caption),
                "storage_types": [ref.storage_type for ref in list(attachment.storage_refs or [])],
                "local_path": local_path_text,
                "local_exists": local_exists,
                "local_size_bytes": local_size_bytes,
            }
        )
    if len(values) > max_items:
        summary.append({"omitted_attachment_count": len(values) - max_items})
    return summary
