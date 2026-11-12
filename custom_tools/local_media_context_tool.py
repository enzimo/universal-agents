"""Tools for inserting local media files into the Strands model context.

A file comes either from the task's folders, named by path and read through
the agent file library (``custom_tools.file_access``), or is a file the person
sent, named by ``attachment_id`` and found through
``custom_tools.attachment_uploads`` (the current request's files, and for the
team lead's own request its conversation's earlier files). Both go through the
same model capability, size and type checks.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from strands import tool

from custom_tools import file_access
from custom_tools.attachment_uploads import OpenedUpload, upload_by_id
from custom_tools.runtime_context import get_agent_ref


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
HARD_MAX_BYTES = 20 * 1024 * 1024

IMAGE_FORMATS_BY_MIME = {
    "image/gif": "gif",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
}
IMAGE_FORMATS_BY_SUFFIX = {
    ".gif": "gif",
    ".jpeg": "jpeg",
    ".jpg": "jpeg",
    ".png": "png",
    ".webp": "webp",
}

DOCUMENT_FORMATS_BY_MIME = {
    "application/msword": "doc",
    "application/pdf": "pdf",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/csv": "csv",
    "text/html": "html",
    "text/markdown": "md",
    "text/plain": "txt",
}
DOCUMENT_FORMATS_BY_SUFFIX = {
    ".csv": "csv",
    ".doc": "doc",
    ".docx": "docx",
    ".htm": "html",
    ".html": "html",
    ".md": "md",
    ".pdf": "pdf",
    ".txt": "txt",
    ".xls": "xls",
    ".xlsx": "xlsx",
}

AUDIO_FORMATS_BY_SUFFIX = {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".wav", ".webm"}
VIDEO_FORMATS_BY_SUFFIX = {".3gp", ".flv", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm", ".wmv"}


def _tool_result(status: str, content: list[dict[str, Any]]) -> Dict[str, Any]:
    return {"status": status, "content": content}


def _json_text(payload: dict[str, Any]) -> dict[str, str]:
    return {"text": json.dumps(payload, indent=2, sort_keys=True)}


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _content_type(path: Path, explicit_content_type: str = "") -> str:
    normalized = str(explicit_content_type or "").split(";", 1)[0].strip().lower()
    if normalized:
        return normalized
    guessed = mimetypes.guess_type(str(path))[0]
    return str(guessed or "application/octet-stream").split(";", 1)[0].strip().lower()


def _image_format(path: Path, content_type: str) -> Optional[str]:
    return IMAGE_FORMATS_BY_MIME.get(content_type) or IMAGE_FORMATS_BY_SUFFIX.get(path.suffix.lower())


def _document_format(path: Path, content_type: str) -> Optional[str]:
    return DOCUMENT_FORMATS_BY_MIME.get(content_type) or DOCUMENT_FORMATS_BY_SUFFIX.get(path.suffix.lower())


def _media_kind(path: Path, content_type: str) -> str:
    if _image_format(path, content_type):
        return "image"
    if _document_format(path, content_type):
        return "document"
    if content_type.startswith("audio/") or path.suffix.lower() in AUDIO_FORMATS_BY_SUFFIX:
        return "audio"
    if content_type.startswith("video/") or path.suffix.lower() in VIDEO_FORMATS_BY_SUFFIX:
        return "video"
    return "unsupported"


def _document_name(path: Path) -> str:
    stem = path.stem or "document"
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return (sanitized or "document")[:80]


def _metadata(media: "_Media", *, content_type: str, media_kind: str, size_bytes: int, sha256: str) -> dict[str, Any]:
    return {
        **media.named,
        "filename": media.name.name,
        "content_type": content_type,
        "media_kind": media_kind,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


def _model_capability_error(media_kind: str) -> Optional[dict[str, Any]]:
    capability_by_kind = {
        "image": "supports_image_input",
        "document": "supports_file_input",
        "audio": "supports_audio_input",
    }
    capability = capability_by_kind.get(media_kind)
    agent = get_agent_ref()
    if capability is None or agent is None:
        return None
    from custom_agents.person_model_runtime import active_bundle, model_supports

    # The model in use: a person's own model while their turn runs on it.
    supported = model_supports(agent, capability.removeprefix("supports_").removesuffix("_input"))
    if supported is True:
        return None
    bundle = active_bundle(agent)
    if bundle is not None:
        return {
            "error": (
                f"The model this person chose ({bundle.describe}) is not set up for {media_kind} input. "
                "They can turn it on with /auth model me if the model accepts it."
            ),
            "capability_field": capability,
            "capability_value": supported,
            "media_kind": media_kind,
            "provider": bundle.provider,
            "model_id": bundle.profile.model_id,
        }
    return {
        "error": (
            f"The active model profile does not enable {media_kind} input. "
            f"Set current_agent.llm.{capability}=true only if the selected model "
            "accepts this native input type."
        ),
        "capability_field": capability,
        "capability_value": getattr(agent, capability, None),
        "media_kind": media_kind,
        "provider": getattr(agent, "model_provider", None),
        "model_id": getattr(agent, "model_id", None),
    }


@dataclass(frozen=True)
class _Media:
    """The file to load: a file in the task's folders (by path) or a file the person sent (by id).

    ``name`` is the file's name (the path itself, or the name a file the
    person sent came with): its suffix decides the file's type, and it names a
    document block. ``named`` is how results name the file: its path, or the
    attachment_id of a file the person sent, never where that is stored.
    ``read`` returns at most ``max_bytes`` bytes of the file that was checked.
    """

    name: Path
    content_type: str
    size_bytes: int
    named: dict[str, Any]
    read: Callable[[int], bytes]


def _error(payload: dict[str, Any]) -> Dict[str, Any]:
    return _tool_result("error", [_json_text(payload)])


def _media_by_path(path: str, content_type: str) -> _Media | Dict[str, Any]:
    try:
        location, entry = file_access.status(path)
    except Exception as exc:
        return _error({"error": str(exc), "path": path})
    resolved_path = location.path
    if entry is None or not stat.S_ISREG(entry.st_mode):
        return _error({"error": "Local file is unavailable.", "path": str(resolved_path)})

    def read(max_bytes: int) -> bytes:
        # Read through the library: the bytes are those of the checked file, never a swapped link.
        return file_access.read_bytes(resolved_path, max_bytes=max_bytes)[1]

    return _Media(
        name=resolved_path,
        content_type=_content_type(resolved_path, explicit_content_type=content_type),
        size_bytes=entry.st_size,
        named={"path": str(resolved_path)},
        read=read,
    )


def _media_by_id(attachment_id: str, content_type: str) -> _Media | Dict[str, Any]:
    upload = upload_by_id(attachment_id)
    if isinstance(upload, dict):
        return upload
    name = Path(Path(str(upload.attachment.filename or "")).name or upload.path.name)
    return _Media(
        name=name,
        content_type=_content_type(name, explicit_content_type=content_type or upload.attachment.content_type),
        size_bytes=upload.size_bytes,
        named={"attachment_id": upload.attachment.attachment_id},
        read=lambda max_bytes: _read_upload(upload, max_bytes),
    )


def _read_upload(upload: OpenedUpload, max_bytes: int) -> bytes:
    """At most ``max_bytes`` bytes of an upload (a runtime record out of agents' reach)."""

    with upload.path.open("rb") as reader:
        data = reader.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"The file is larger than {max_bytes} bytes.")
    return data


@tool
def load_local_media_for_model_context(
    path: str = "",
    attachment_id: str = "",
    purpose: str = "",
    content_type: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Load an image or supported document into the model as native content blocks.

    Name exactly one file: `path` for a file in this task's folders, or
    `attachment_id` for a file the person sent (with the current request, with
    guidance or an answer while it ran, or earlier in this conversation; the
    prompt lists them).

    Use this only when you need the model to directly examine the file contents,
    such as a screenshot, diagram, chart, PDF, spreadsheet, or small document.
    The tool result inserts media/document blocks into the next inner-loop LLM
    input for this same Strands turn. Prefer file_read or inspect_user_attachment
    when metadata or bounded text is enough.

    Supported direct content blocks:
    - images: png, jpeg, gif, webp
    - documents: pdf, csv, doc, docx, xls, xlsx, html, txt, md

    Audio and video files are detected but not inserted because this runtime's
    standard Strands tool-result path does not reliably pass them through all
    configured providers.
    """
    if bool(str(path or "").strip()) == bool(str(attachment_id or "").strip()):
        return _error(
            {
                "error": (
                    "Name exactly one file: attachment_id for a file the person sent, "
                    "or path for a file in this task's folders."
                )
            }
        )
    media = _media_by_id(attachment_id, content_type) if attachment_id.strip() else _media_by_path(path, content_type)
    if isinstance(media, dict):
        return media

    requested_max_bytes = _safe_int(max_bytes, default=DEFAULT_MAX_BYTES, minimum=1, maximum=HARD_MAX_BYTES)
    size_bytes = media.size_bytes
    resolved_content_type = media.content_type
    media_kind = _media_kind(media.name, resolved_content_type)
    capability_error = _model_capability_error(media_kind)
    if capability_error is not None:
        return _error({**media.named, **capability_error})
    if size_bytes > requested_max_bytes:
        return _error(
            {
                "error": "File is too large to insert into model context.",
                **media.named,
                "size_bytes": size_bytes,
                "max_bytes": requested_max_bytes,
                "content_type": resolved_content_type,
                "media_kind": media_kind,
            }
        )

    if media_kind in {"audio", "video"}:
        return _error(
            {
                "error": (
                    "Audio and video files cannot currently be inserted through the standard "
                    "Strands tool-result media path in this runtime."
                ),
                **media.named,
                "content_type": resolved_content_type,
                "media_kind": media_kind,
                "recommended_next_step": (
                    "Use a purpose-built transcription, frame-extraction, or media-analysis tool, "
                    "or ask for a provider-specific media bridge to be added."
                ),
            }
        )

    try:
        data = media.read(requested_max_bytes)
    except (OSError, file_access.PathPolicyError) as exc:
        return _error({"error": str(exc), **media.named})
    size_bytes = len(data)
    sha256 = hashlib.sha256(data).hexdigest()
    metadata = _metadata(
        media,
        content_type=resolved_content_type,
        media_kind=media_kind,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    purpose_text = str(purpose or "").strip()
    label = {
        "loaded_into_model_context": True,
        "message": (
            "The following native content block is included in this tool result. "
            "Use it directly for visual/document analysis."
        ),
        "purpose": purpose_text or None,
        **metadata,
    }

    image_format = _image_format(media.name, resolved_content_type)
    if image_format:
        return _tool_result(
            "success",
            [
                _json_text(label),
                {"image": {"format": image_format, "source": {"bytes": data}}},
            ],
        )

    document_format = _document_format(media.name, resolved_content_type)
    if document_format:
        return _tool_result(
            "success",
            [
                _json_text(label),
                {
                    "document": {
                        "format": document_format,
                        "name": _document_name(media.name),
                        "source": {"bytes": data},
                    }
                },
            ],
        )

    return _error(
        {
            "error": "Unsupported file type for direct model-context insertion.",
            "supported_images": sorted(set(IMAGE_FORMATS_BY_SUFFIX)),
            "supported_documents": sorted(set(DOCUMENT_FORMATS_BY_SUFFIX)),
            **metadata,
        }
    )
