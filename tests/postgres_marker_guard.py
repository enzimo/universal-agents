"""Find PostgreSQL test cases that the PostgreSQL CI job would never select.

The hermetic profile skips a PostgreSQL case when no server is configured, and
the PostgreSQL job selects tests with ``-m postgres``. A case parametrized with
a plain ``"postgres"`` backend value but without the ``postgres`` marker is
therefore never run anywhere. ``tests/conftest.py`` refuses such a collection.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

POSTGRES_BACKEND_VALUES = frozenset({"postgres", "postgresql"})


def runs_postgres_case(item: Any) -> bool:
    """True when one of the item's parameters selects the PostgreSQL backend."""

    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return False
    return any(
        isinstance(value, str) and value.lower() in POSTGRES_BACKEND_VALUES
        for value in callspec.params.values()
    )


def unmarked_postgres_cases(items: Iterable[Any]) -> list[str]:
    """Node ids of PostgreSQL cases that lack the ``postgres`` marker."""

    return [
        item.nodeid
        for item in items
        if runs_postgres_case(item) and item.get_closest_marker("postgres") is None
    ]
