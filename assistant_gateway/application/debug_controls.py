"""Shared validation for gateway debug-control requests."""

from __future__ import annotations

from typing import Any


CONTEXT_DUMP_MODES = frozenset({"always", "on_error", "on_budget_warning"})


def normalize_context_dump_mode(value: Any) -> str:
    """Return a supported context-dump mode or raise a stable validation error."""
    normalized = str(value or "").strip().lower()
    if normalized not in CONTEXT_DUMP_MODES:
        raise ValueError(
            "Context dump mode must be one of: always, on_error, on_budget_warning."
        )
    return normalized
