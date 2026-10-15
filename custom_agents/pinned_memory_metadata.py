"""Separate stored import provenance from metadata agents use."""

from typing import Any

IMPORTED_FROM_KEY = "imported_from"


def public_pinned_memory_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep skill metadata while excluding the original author's import record."""
    return {key: value for key, value in metadata.items() if key != IMPORTED_FROM_KEY}
