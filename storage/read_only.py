"""Repository reads that must not initialize, repair, or change a database.

Transfer review uses the existing repositories to inspect the destination.
Their normal constructors initialize stores; this explicit scope suppresses
that initialization and opens database connections in read-only mode instead.
Missing or incompatible tables remain errors rather than being repaired.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_READ_ONLY = ContextVar("read_only_repositories", default=False)


def repositories_are_read_only() -> bool:
    return _READ_ONLY.get()


@contextmanager
def read_only_repositories():
    token = _READ_ONLY.set(True)
    try:
        yield
    finally:
        _READ_ONLY.reset(token)
