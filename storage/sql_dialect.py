"""Native statement syntax selected when a repository constructs its query.

Connections execute SQL verbatim. Binding markers are deliberately part of
query construction, never a search-and-replace pass over executable SQL.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SqlDialect:
    bind: str
    name: str


SQLITE = SqlDialect(bind="?", name="SQLite")
POSTGRES = SqlDialect(bind="%s", name="Postgres")
