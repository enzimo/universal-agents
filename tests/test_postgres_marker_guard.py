"""The collection guard finds PostgreSQL cases that no test profile would run."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.postgres_marker_guard import unmarked_postgres_cases


def _item(nodeid: str, params: dict[str, object] | None, *, marked: bool) -> SimpleNamespace:
    callspec = None if params is None else SimpleNamespace(params=params)
    marker = pytest.mark.postgres.mark if marked else None
    return SimpleNamespace(nodeid=nodeid, callspec=callspec, get_closest_marker=lambda name: marker)


def test_an_unmarked_postgres_case_is_reported() -> None:
    items = [
        _item("t.py::test_a[sqlite]", {"agent": "sqlite"}, marked=False),
        _item("t.py::test_a[postgres]", {"agent": "postgres"}, marked=False),
    ]

    assert unmarked_postgres_cases(items) == ["t.py::test_a[postgres]"]


def test_marked_postgres_cases_and_other_tests_pass() -> None:
    items = [
        _item("t.py::test_b[postgres]", {"storage": "postgres"}, marked=True),
        _item("t.py::test_c", None, marked=False),
        _item("t.py::test_d[3]", {"count": 3}, marked=False),
        _item("t.py::test_e[postgres-url]", {"label": "postgresql://example.test/db"}, marked=False),
    ]

    assert unmarked_postgres_cases(items) == []
