"""Canonical storage backend identifiers.

The storage layer owns these values so repository implementations do not
depend on agent-runtime modules merely to select a backend.
"""

STORAGE_BACKEND_SQLITE = "sqlite"
STORAGE_BACKEND_POSTGRES = "postgres"
STORAGE_BACKEND_POSTGRESQL_ALIAS = "postgresql"
SUPPORTED_STORAGE_BACKENDS = frozenset(
    {STORAGE_BACKEND_SQLITE, STORAGE_BACKEND_POSTGRES}
)
