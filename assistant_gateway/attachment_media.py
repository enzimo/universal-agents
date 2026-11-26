"""Shared media-type policy for Web Chat and outgoing notification attachments.

This module is the single source of truth for which media types may render
inside Web Chat (a narrow raster image allowlist plus non-XML audio and video)
and how attachment ``kind``, HTTP ``Content-Disposition``, and safe response
``Content-Type`` are derived from a normalized media type. Only raster images
are served with ``Content-Disposition: inline``; audio and video keep their
normalized media type so Web Chat's media players play them, while using
``Content-Disposition: attachment`` so direct navigation downloads the file
instead of opening an empty sandbox-blocked player page. It uses only the
Python standard library so gateway application services, HTTP handlers, and
notification validation can share it without cycles.
"""

from __future__ import annotations

import mimetypes
import re
from typing import Literal

ATTACHMENT_SANDBOX_CSP = "sandbox; default-src 'none'"
DEFAULT_ATTACHMENT_CONTENT_TYPE = "application/octet-stream"

# Only raster image formats that cannot carry active scripts are rendered
# inline on the Web Chat origin. SVG and other image/* subtypes download.
INLINE_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)

_MEDIA_TYPE_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
}

# Strict RFC 6838 type/subtype token syntax without parameters or whitespace.
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$"
)

AttachmentKind = Literal["image", "audio", "video", "document"]
AttachmentDisposition = Literal["inline", "attachment"]


def parse_media_type(content_type: str | None) -> str | None:
    """Parse and normalize a MIME ``type/subtype``, or return None if empty/malformed."""

    raw = str(content_type or "")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    candidate = raw.split(";", 1)[0].strip().lower()
    if not candidate or not _MEDIA_TYPE_PATTERN.match(candidate):
        return None
    return _MEDIA_TYPE_ALIASES.get(candidate, candidate)


def normalize_media_type(content_type: str | None) -> str:
    """Normalize a MIME media type to lowercase ``type/subtype`` or octet-stream."""

    return parse_media_type(content_type) or DEFAULT_ATTACHMENT_CONTENT_TYPE


def resolve_attachment_content_type(
    content_type: str | None,
    *,
    filename: str | None = None,
) -> str:
    """Resolve and normalize an attachment's media type.

    An explicit ``content_type`` always takes precedence over ``filename`` so a
    misleading suffix (such as ``.png`` on a ``text/html`` file) can never
    promote a non-raster file to an inline image. When ``content_type`` is
    omitted, fall back to ``mimetypes.guess_type`` and then
    ``application/octet-stream``.
    """

    explicit = str(content_type or "").strip()
    if explicit:
        return normalize_media_type(explicit)
    name = str(filename or "").strip()
    if name:
        guessed = mimetypes.guess_type(name)[0]
        if guessed:
            return normalize_media_type(guessed)
    return DEFAULT_ATTACHMENT_CONTENT_TYPE


def is_inline_image_content_type(content_type: str | None) -> bool:
    """Return True only for allowlisted raster image media types."""

    return normalize_media_type(content_type) in INLINE_IMAGE_CONTENT_TYPES


def is_inline_audio_content_type(content_type: str | None) -> bool:
    """Return True for valid non-XML ``audio/*`` media types."""

    normalized = normalize_media_type(content_type)
    return normalized.startswith("audio/") and not normalized.endswith("+xml")


def is_inline_video_content_type(content_type: str | None) -> bool:
    """Return True for valid non-XML ``video/*`` media types."""

    normalized = normalize_media_type(content_type)
    return normalized.startswith("video/") and not normalized.endswith("+xml")


def is_inline_attachment_content_type(content_type: str | None) -> bool:
    """Return True when ``content_type`` may be displayed inline on Web Chat."""

    return (
        is_inline_image_content_type(content_type)
        or is_inline_audio_content_type(content_type)
        or is_inline_video_content_type(content_type)
    )


def attachment_kind_for_content_type(content_type: str | None) -> AttachmentKind:
    """Derive attachment ``kind`` strictly from the normalized media type."""

    normalized = normalize_media_type(content_type)
    if normalized in INLINE_IMAGE_CONTENT_TYPES:
        return "image"
    if is_inline_audio_content_type(normalized):
        return "audio"
    if is_inline_video_content_type(normalized):
        return "video"
    return "document"


def attachment_disposition_for_content_type(
    content_type: str | None,
) -> AttachmentDisposition:
    """Return ``inline`` only for allowlisted raster images, ``attachment`` otherwise.

    Audio and video attachments keep their normalized ``Content-Type`` so
    ``<audio>`` and ``<video>`` elements on the Web Chat page play them (media
    elements ignore ``Content-Disposition``), while using ``attachment`` so
    opening the raw URL in its own tab downloads the file cleanly rather than
    rendering a Chromium player page blocked by ``sandbox; default-src 'none'``.
    """

    if is_inline_image_content_type(content_type):
        return "inline"
    return "attachment"


def attachment_response_content_type(content_type: str | None) -> str:
    """Return the HTTP Content-Type safe to send from the Web Chat origin.

    Allowlisted inline media keeps its normalized type so browsers render
    thumbnails and media players. Everything else is forced to
    ``application/octet-stream`` so a downloaded attachment (such as
    ``text/javascript``, ``text/css``, ``text/html``, or ``image/svg+xml``)
    cannot be loaded as a same-origin script or stylesheet under ``nosniff``.
    """

    normalized = normalize_media_type(content_type)
    if is_inline_attachment_content_type(normalized):
        return normalized
    return DEFAULT_ATTACHMENT_CONTENT_TYPE
