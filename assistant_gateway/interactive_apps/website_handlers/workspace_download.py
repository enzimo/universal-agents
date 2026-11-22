"""Pure HTTP helpers for authenticated streaming workspace file downloads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from assistant_gateway.interactive_apps.website_support import _WEB_CHAT_ROUTE


DOWNLOAD_CHUNK_BYTES = 64 * 1024

_MAX_RANGE_DIGITS = 20
_RFC5987_SAFE = "!#$&+-.^_`|~"
_URL_PATH_QUERY_SAFE = "-_.~"


@dataclass(frozen=True, slots=True)
class ByteRange:
    """An inclusive, validated byte range within a file."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class ByteRangeResult:
    """Outcome of parsing an HTTP Range header for a single-range download."""

    status: Literal["none", "satisfiable", "unsatisfiable"]
    byte_range: ByteRange | None = None


def _parse_ascii_uint(value: str) -> int | None:
    if (
        not value
        or len(value) > _MAX_RANGE_DIGITS
        or not value.isascii()
        or not value.isdigit()
    ):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _resolve_byte_range(
    start_text: str,
    end_text: str,
    size_bytes: int,
) -> ByteRangeResult:
    if not start_text and not end_text:
        return ByteRangeResult(status="none")
    if not start_text:
        suffix_length = _parse_ascii_uint(end_text)
        if suffix_length is None:
            return ByteRangeResult(status="none")
        if suffix_length == 0 or size_bytes <= 0:
            return ByteRangeResult(status="unsatisfiable")
        suffix_start = max(0, size_bytes - suffix_length)
        return ByteRangeResult(
            status="satisfiable",
            byte_range=ByteRange(start=suffix_start, end=size_bytes - 1),
        )
    start = _parse_ascii_uint(start_text)
    if start is None:
        return ByteRangeResult(status="none")
    if not end_text:
        if size_bytes <= 0 or start >= size_bytes:
            return ByteRangeResult(status="unsatisfiable")
        return ByteRangeResult(
            status="satisfiable",
            byte_range=ByteRange(start=start, end=size_bytes - 1),
        )
    end = _parse_ascii_uint(end_text)
    if end is None:
        return ByteRangeResult(status="none")
    if size_bytes <= 0 or start > end or start >= size_bytes:
        return ByteRangeResult(status="unsatisfiable")
    return ByteRangeResult(
        status="satisfiable",
        byte_range=ByteRange(start=start, end=min(end, size_bytes - 1)),
    )


def parse_byte_range(header_value: str | None, size_bytes: int) -> ByteRangeResult:
    """Parse a single HTTP ``bytes=`` range header per RFC 9110.

    Malformed syntax, non-``bytes`` units, and multi-range headers are ignored
    (``status="none"``) so the caller serves the full file with HTTP 200.
    Well-formed single ranges that cannot be satisfied against ``size_bytes``
    return ``status="unsatisfiable"`` so the caller responds with HTTP 416.
    """

    raw = str(header_value or "").strip()
    if not raw or "=" not in raw:
        return ByteRangeResult(status="none")
    unit, range_set = raw.split("=", 1)
    if unit.strip().lower() != "bytes":
        return ByteRangeResult(status="none")
    spec = range_set.strip()
    if not spec or "," in spec or "-" not in spec:
        return ByteRangeResult(status="none")
    start_text, end_text = spec.split("-", 1)
    return _resolve_byte_range(start_text, end_text, size_bytes)


def build_attachment_disposition(filename: str) -> str:
    """Build a safe ``Content-Disposition: attachment`` header with UTF-8 filename*."""

    cleaned_chars = [
        ch
        for ch in str(filename or "")
        if ord(ch) >= 0x20 and ord(ch) != 0x7F and ch not in {"/", "\\"}
    ]
    sanitized = "".join(cleaned_chars).strip()
    if not sanitized:
        sanitized = "download"

    ascii_chars = [
        ch if (0x20 <= ord(ch) <= 0x7E and ch not in {'"', "\\"}) else "_"
        for ch in sanitized
    ]
    ascii_fallback = "".join(ascii_chars).strip()
    if not ascii_fallback or set(ascii_fallback) <= {"."}:
        ascii_fallback = "download"

    encoded_utf8 = quote(sanitized, safe=_RFC5987_SAFE, encoding="utf-8")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_utf8}'


def _media_range_quality(params: Sequence[str]) -> float:
    for param in params:
        if "=" not in param:
            continue
        key, raw_value = param.split("=", 1)
        if key.strip().lower() != "q":
            continue
        try:
            return float(raw_value.strip())
        except ValueError:
            return 0.0
    return 1.0


def accepts_html_navigation(accept_header: str | None) -> bool:
    """Return True when the HTTP Accept header explicitly accepts ``text/html``."""

    raw = str(accept_header or "").strip()
    if not raw:
        return False
    for entry in raw.split(","):
        parts = entry.split(";")
        media_type = parts[0].strip().lower()
        if media_type != "text/html":
            continue
        if _media_range_quality(parts[1:]) > 0.0:
            return True
    return False


def unauthenticated_download_redirect_location(requested_path: str) -> str:
    """Return a same-origin relative Web Chat URL for an unauthenticated download click."""

    cleaned = str(requested_path or "").strip()
    if not cleaned:
        return f"{_WEB_CHAT_ROUTE}?view=files"
    encoded = quote(cleaned, safe=_URL_PATH_QUERY_SAFE, encoding="utf-8")
    return f"{_WEB_CHAT_ROUTE}?view=files&path={encoded}&download=1"


def download_security_headers() -> dict[str, str]:
    """Security headers attached to every workspace file download response."""

    return {
        "Cache-Control": "no-store",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
