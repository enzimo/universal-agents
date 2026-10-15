"""The release this process runs, from the repository's ``VERSION`` file."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

_VERSION_PATH = Path(__file__).resolve().parent / "VERSION"
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def release_version() -> str:
    """``MAJOR.MINOR.PATCH`` of this release, or ``unknown`` when the file is missing.

    Every install ships ``VERSION``; a missing one is an install defect, logged
    once. Callers label records with it (set-aside messages), so they still
    work without it.
    """

    try:
        version = _VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            "The VERSION file could not be read; records name release 'unknown'. "
            "event=release_version_unreadable path=%s error_type=%s",
            _VERSION_PATH,
            type(exc).__name__,
        )
        return "unknown"
    return version or "unknown"
