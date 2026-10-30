"""Native statement syntax selected when a repository constructs its query.

Connections execute SQL verbatim. Binding markers are deliberately part of
query construction, never a search-and-replace pass over executable SQL.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SqlDialect:
    bind: str
    name: str
    # The size of a text value in bytes (UTF-8), with ``{}`` for the column.
    # SQLite's LENGTH counts characters for text, so the value is read as a
    # blob; PostgreSQL's octet_length reads a stored value's size without
    # decompressing it.
    text_bytes_template: str

    def text_bytes(self, column: str) -> str:
        """The byte size of ``column``'s text, 0 when it is NULL."""

        return f"COALESCE({self.text_bytes_template.format(column)}, 0)"


SQLITE = SqlDialect(bind="?", name="SQLite", text_bytes_template="LENGTH(CAST({} AS BLOB))")
POSTGRES = SqlDialect(bind="%s", name="Postgres", text_bytes_template="octet_length({})")
