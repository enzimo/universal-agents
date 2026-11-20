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
    # Where a bound text first occurs in a column's text (1-based, 0 when it
    # does not), with ``{0}`` for the column and ``{1}`` for the bind marker.
    # Both functions match the text literally and with case, unlike LIKE,
    # which needs its wildcards escaped and ignores ASCII case in SQLite only.
    position_template: str

    def text_bytes(self, column: str) -> str:
        """The byte size of ``column``'s text, 0 when it is NULL."""

        return f"COALESCE({self.text_bytes_template.format(column)}, 0)"

    def contains(self, column: str) -> str:
        """A condition true when ``column``'s text holds the bound text, matched literally; false when NULL."""

        return f"{self.position_template.format(column, self.bind)} > 0"


SQLITE = SqlDialect(
    bind="?", name="SQLite", text_bytes_template="LENGTH(CAST({} AS BLOB))", position_template="instr({0}, {1})"
)
POSTGRES = SqlDialect(
    bind="%s", name="Postgres", text_bytes_template="octet_length({})", position_template="strpos({0}, {1})"
)
