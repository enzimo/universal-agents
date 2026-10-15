"""Tools for inserting local media files into the Strands model context."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from strands import tool

from custom_tools import file_access
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


def _metadata(path: Path, *, content_type: str, media_kind: str, size_bytes: int, sha256: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "filename": path.name,
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


@tool
def load_local_media_for_model_context(
    path: str,
    purpose: str = "",
    content_type: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Load an image or supported document from this task's folders into the model as native content blocks.

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
    try:
        location, entry = file_access.status(path)
    except Exception as exc:
        return _tool_result("error", [_json_text({"error": str(exc), "path": path})])
    resolved_path = location.path

    if entry is None or not stat.S_ISREG(entry.st_mode):
        return _tool_result(
            "error",
            [_json_text({"error": "Local file is unavailable.", "path": str(resolved_path)})],
        )

    requested_max_bytes = _safe_int(max_bytes, default=DEFAULT_MAX_BYTES, minimum=1, maximum=HARD_MAX_BYTES)
    size_bytes = entry.st_size
    resolved_content_type = _content_type(resolved_path, explicit_content_type=content_type)
    media_kind = _media_kind(resolved_path, resolved_content_type)
    capability_error = _model_capability_error(media_kind)
    if capability_error is not None:
        return _tool_result(
            "error",
            [_json_text({"path": str(resolved_path), **capability_error})],
        )
    if size_bytes > requested_max_bytes:
        return _tool_result(
            "error",
            [
                _json_text(
                    {
                        "error": "File is too large to insert into model context.",
                        "path": str(resolved_path),
                        "size_bytes": size_bytes,
                        "max_bytes": requested_max_bytes,
                        "content_type": resolved_content_type,
                        "media_kind": media_kind,
                    }
                )
            ],
        )

    if media_kind in {"audio", "video"}:
        return _tool_result(
            "error",
            [
                _json_text(
                    {
                        "error": (
                            "Audio and video files cannot currently be inserted through the standard "
                            "Strands tool-result media path in this runtime."
                        ),
                        "path": str(resolved_path),
                        "content_type": resolved_content_type,
                        "media_kind": media_kind,
                        "recommended_next_step": (
                            "Use a purpose-built transcription, frame-extraction, or media-analysis tool, "
                            "or ask for a provider-specific media bridge to be added."
                        ),
                    }
                )
            ],
        )

    try:
        # Read through the library: the bytes are those of the checked file, never a swapped link.
        _location, data = file_access.read_bytes(resolved_path, max_bytes=requested_max_bytes)
    except (OSError, file_access.PathPolicyError) as exc:
        return _tool_result("error", [_json_text({"error": str(exc), "path": str(resolved_path)})])
    size_bytes = len(data)
    sha256 = hashlib.sha256(data).hexdigest()
    metadata = _metadata(
        resolved_path,
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

    image_format = _image_format(resolved_path, resolved_content_type)
    if image_format:
        return _tool_result(
            "success",
            [
                _json_text(label),
                {"image": {"format": image_format, "source": {"bytes": data}}},
            ],
        )

    document_format = _document_format(resolved_path, resolved_content_type)
    if document_format:
        return _tool_result(
            "success",
            [
                _json_text(label),
                {
                    "document": {
                        "format": document_format,
                        "name": _document_name(resolved_path),
                        "source": {"bytes": data},
                    }
                },
            ],
        )

    return _tool_result(
        "error",
        [
            _json_text(
                {
                    "error": "Unsupported file type for direct model-context insertion.",
                    "supported_images": sorted(set(IMAGE_FORMATS_BY_SUFFIX)),
                    "supported_documents": sorted(set(DOCUMENT_FORMATS_BY_SUFFIX)),
                    **metadata,
                }
            )
        ],
    )
