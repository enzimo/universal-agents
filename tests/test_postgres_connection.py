from __future__ import annotations

from storage.postgres_connection import PostgresCursor


class _MappingCursor:
    description = [("first",), ("second",)]
    rowcount = 1

    def fetchone(self):
        return {"first": "a", "second": "b"}

    def fetchall(self):
        return [{"first": "a", "second": "b"}]


def test_postgres_connection_cursor_supports_mapping_rows() -> None:
    cursor = PostgresCursor(_MappingCursor())
    row = cursor.fetchone()
    assert row is not None
    assert row["first"] == "a"
    assert row[1] == "b"
