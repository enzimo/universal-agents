"""Readable file sizes for people: ``812 bytes``, ``13.7 KB``, ``3.2 MB``, ``1.4 GB``.

One wording for every message and page that names a size (file sharing,
``/upgrade``'s export report), so the same file never reads differently in two
places. Binary units (1 KB = 1024 bytes), one decimal above a kilobyte.
"""

from __future__ import annotations

_UNITS = (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024))


def size_words(size_bytes: int) -> str:
    """How big a file is, in the largest unit it reaches."""

    value = max(0, int(size_bytes))
    for unit, scale in _UNITS:
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{value} bytes"


__all__ = ["size_words"]
