"""Prevent every runtime entry point from reading a partially imported team.

Only the import writer, in its own execution context and for its exact root,
may load settings while the marker exists. Child processes receive no permit.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from team_transfer.errors import TransferError

_WRITING_ROOT: ContextVar[Path | None] = ContextVar(
    "team_transfer_writing_root", default=None
)


def require_finished_import(layout) -> None:
    if layout.team_import_marker_path.exists() and _WRITING_ROOT.get() != layout.root:
        raise TransferError(
            f"The team import into {layout.root} did not finish, so this instance must not start. "
            "For a failed later batch, restore the instance and databases from the backup taken before that batch. "
            "For a failed first import, use a fresh root and empty databases and import again."
        )


@contextmanager
def import_writer(root: Path) -> Iterator[None]:
    token = _WRITING_ROOT.set(root)
    try:
        yield
    finally:
        _WRITING_ROOT.reset(token)
