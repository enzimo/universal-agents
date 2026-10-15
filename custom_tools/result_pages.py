"""Small inspection responses with explicit pagination and stable text identity."""

import hashlib
from typing import Any

MAX_PAGE_SIZE = 20
MAX_EXCERPT_CHARS = 4000


def page_size(limit: int, offset: int) -> int:
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset must be non-negative")
    return min(limit, MAX_PAGE_SIZE)


def text_page(
    text: str, *, offset: int = 0, max_chars: int = MAX_EXCERPT_CHARS
) -> dict[str, Any]:
    if offset < 0 or max_chars < 1 or offset > len(text):
        raise ValueError("Choose an offset within the text and a positive max_chars")
    end = min(len(text), offset + min(max_chars, MAX_EXCERPT_CHARS))
    return {
        "text": text[offset:end],
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
