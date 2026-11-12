"""Tools for inserting local media files into the Strands model context.

A file comes from one of two places: an upload the task may use, named by
``attachment_id`` and read from this host's upload folders
(``custom_tools.task_uploads``), or a file in the task's folders, named by
``path`` and read through the agent file library (``custom_tools.file_access``).
Results for an upload name it by id and file name, never by where it is stored.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from strands import tool

from custom_tools import file_access
from custom_tools.runtime_context import get_agent_ref
from custom_tools.task_uploads import TaskUpload, open_task_upload, read_failed


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


# Document block names. Bedrock's Converse API (provider "bedrock") accepts only
# letters, digits, single spaces, hyphens, parentheses and square brackets, at
# most 200 characters, and refuses a whole request in which two document blocks
# share a name, compared without regard to case, tool results and history included.
_DOCUMENT_NAME_DISALLOWED = re.compile(r"[^A-Za-z0-9()\[\]-]+")
DOCUMENT_NAME_STEM_MAX_CHARS = 80
DOCUMENT_NAME_MAX_CHARS = 200


def _document_name(name: Path, attachment_id: str = "") -> str:
    """The document block's name: the file name's stem, in characters every provider accepts.

    Other characters become single spaces. An upload's name ends with its
    attachment id in parentheses, so two uploads with the same file name (an
    invoice sent twice) load as different documents in one request. The same
    file loaded twice in one task still repeats its name.
    """

    stem = " ".join(_DOCUMENT_NAME_DISALLOWED.sub(" ", name.stem).split())
    stem = stem[:DOCUMENT_NAME_STEM_MAX_CHARS].rstrip() or "document"
    if not attachment_id:
        return stem
    return f"{stem} ({_DOCUMENT_NAME_DISALLOWED.sub('-', attachment_id)})"[:DOCUMENT_NAME_MAX_CHARS]


@dataclass(frozen=True)
class _Media:
    """The file to load: an upload the task may use (by id) or a file in the task's folders (by path).

    ``path`` is where the bytes are, and its suffix helps tell the format;
    ``name`` is the file name shown to the model. ``identity`` is how every
    result names the file: the path the caller gave, or the upload's id and
    file name, never where an upload is stored.
    """

    path: Path
    name: Path
    size_bytes: int
    identity: dict[str, Any]
    recorded_content_type: str = ""
    upload: Optional[TaskUpload] = None

    def read(self, *, max_bytes: int) -> bytes:
        """The bytes, read once: an upload from its upload folder, a file through the agent file library.

        Either way the bytes are those of the checked file, never a link swapped in afterwards.
        """

        if self.upload is not None:
            return self.upload.located.read_bytes(max_bytes=max_bytes)
        return file_access.read_bytes(self.path, max_bytes=max_bytes)[1]


def _metadata(media: _Media, *, content_type: str, media_kind: str, size_bytes: int, sha256: str) -> dict[str, Any]:
    return {
        **media.identity,
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
    path: str = "",
    attachment_id: str = "",
    purpose: str = "",
    content_type: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Dict[str, Any]:
    """
    Load an image or supported document into the model as native content blocks.

    Give exactly one of `attachment_id` or `path`. Name a file the person sent
    by its `attachment_id`: ids are listed in the prompt (this request's files
    and the files sent earlier in this conversation), in a guidance or answer
    that came with files, and per turn by get_conversation_context_slice. A
    task delegated to you can load only the files its delegation gave it. Use
    `path` for a file in this task's folders.

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

    When a file the person sent cannot be loaded, `result` says why, as for
    inspect_user_attachment: `unknown_attachment_id`, `upload_unavailable`
    (deleted; ask the person to send it again) or `remote_copy_required`.
    """
    media = _media_source(str(path or "").strip(), str(attachment_id or "").strip())
    if isinstance(media, dict):
        return media
    return _load(media, purpose=purpose, content_type=content_type, max_bytes=max_bytes)


def _media_source(path: str, attachment_id: str) -> _Media | Dict[str, Any]:
    """The file to load, or the tool result that explains why there is none."""

    if bool(path) == bool(attachment_id):
        return _tool_result(
            "error",
            [
                _json_text(
                    {
                        "error": (
                            "Give exactly one of attachment_id (a file the person sent) or path "
                            "(a file in this task's folders)."
                        )
                    }
                )
            ],
        )
    if attachment_id:
        upload = open_task_upload(attachment_id)
        if isinstance(upload, dict):
            return upload
        attachment, located = upload.attachment, upload.located
        return _Media(
            path=located.path,
            name=Path(Path(str(attachment.filename or "")).name or located.path.name),
            size_bytes=located.size_bytes,
            identity={"attachment_id": attachment.attachment_id, "filename": attachment.filename},
            recorded_content_type=attachment.content_type,
            upload=upload,
        )
    try:
        location, entry = file_access.status(path)
    except Exception as exc:
        return _tool_result("error", [_json_text({"error": str(exc), "path": path})])
    if entry is None or not stat.S_ISREG(entry.st_mode):
        return _tool_result(
            "error",
            [_json_text({"error": "Local file is unavailable.", "path": str(location.path)})],
        )
    return _Media(
        path=location.path,
        name=location.path,
        size_bytes=entry.st_size,
        identity={"path": str(location.path), "filename": location.path.name},
    )


def _load(media: _Media, *, purpose: str, content_type: str, max_bytes: int) -> Dict[str, Any]:
    """The file's native content block, after the model, size and media type checks."""

    requested_max_bytes = _safe_int(max_bytes, default=DEFAULT_MAX_BYTES, minimum=1, maximum=HARD_MAX_BYTES)
    resolved_content_type = _content_type(media.path, explicit_content_type=content_type or media.recorded_content_type)
    media_kind = _media_kind(media.path, resolved_content_type)
    refusal = _refusal(media, media_kind, resolved_content_type, requested_max_bytes)
    if refusal is not None:
        return _tool_result("error", [_json_text(refusal)])
    try:
        data = media.read(max_bytes=requested_max_bytes)
    except (OSError, file_access.PathPolicyError) as exc:
        return _tool_result("error", [_json_text(_read_error(media, exc))])
    metadata = _metadata(
        media,
        content_type=resolved_content_type,
        media_kind=media_kind,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    label = {
        "loaded_into_model_context": True,
        "message": (
            "The following native content block is included in this tool result. "
            "Use it directly for visual/document analysis."
        ),
        "purpose": str(purpose or "").strip() or None,
        **metadata,
    }
    block = _content_block(media, resolved_content_type, data)
    if block is not None:
        return _tool_result("success", [_json_text(label), block])
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


def _refusal(media: _Media, media_kind: str, content_type: str, max_bytes: int) -> Optional[dict[str, Any]]:
    """Why the file is not loaded, checked before reading it: the model, its size, or audio and video."""

    capability_error = _model_capability_error(media_kind)
    if capability_error is not None:
        return {**media.identity, **capability_error}
    if media.size_bytes > max_bytes:
        return {
            "error": "File is too large to insert into model context.",
            **media.identity,
            "size_bytes": media.size_bytes,
            "max_bytes": max_bytes,
            "content_type": content_type,
            "media_kind": media_kind,
        }
    if media_kind in {"audio", "video"}:
        return {
            "error": (
                "Audio and video files cannot currently be inserted through the standard "
                "Strands tool-result media path in this runtime."
            ),
            **media.identity,
            "content_type": content_type,
            "media_kind": media_kind,
            "recommended_next_step": (
                "Use a purpose-built transcription, frame-extraction, or media-analysis tool, "
                "or ask for a provider-specific media bridge to be added."
            ),
        }
    return None


def _read_error(media: _Media, exc: OSError) -> dict[str, Any]:
    """The error for a file that could not be read; an upload's never shows where it is stored."""

    if media.upload is None:
        return {"error": str(exc), **media.identity}
    return read_failed(media.upload.attachment, exc)


def _content_block(media: _Media, content_type: str, data: bytes) -> Optional[dict[str, Any]]:
    """The image or document block for ``data``, or None for a type the model cannot take directly."""

    image_format = _image_format(media.path, content_type)
    if image_format:
        return {"image": {"format": image_format, "source": {"bytes": data}}}
    document_format = _document_format(media.path, content_type)
    if document_format:
        attachment_id = media.upload.attachment.attachment_id if media.upload is not None else ""
        name = _document_name(media.name, attachment_id)
        return {"document": {"format": document_format, "name": name, "source": {"bytes": data}}}
    return None
