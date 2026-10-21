"""Revision-checked writes to the saved instance configuration.

All writers share the existing sidecar file lock, including workers in other
processes. The revision check and replacement happen under that lock; atomic
replacement alone cannot stop a stale reader from erasing another update.
The expected revision is the hash of the exact bytes the caller read, or
``None`` only when creating a file that must not already exist.
"""

from __future__ import annotations

from pathlib import Path

from custom_agents.governance.authorized_operations import atomic_write_bytes, sha256_bytes
from file_locking import exclusive_file_lock


class ConfigFileConflict(ValueError):
    """Another writer changed the configuration; the attempted update was refused."""


def config_revision(content: bytes | None) -> str | None:
    return None if content is None else sha256_bytes(content)


def replace_config_file(
    path: Path,
    content: bytes | None,
    *,
    expected_revision: str | None,
    backup_path: Path | None = None,
) -> str | None:
    """Replace or remove the file only if it still matches the caller's revision.

    A backup, when requested, records the checked bytes before replacement.
    ``content=None`` removes a newly created file during a failed operation's
    rollback. Rollbacks must name the revision they applied, so they cannot
    undo another writer's later change. Never acquire this lock around a call
    to this function: the shared file lock is deliberately not reentrant.
    """

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path, purpose="instance settings"):
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = None
        if config_revision(previous) != expected_revision:
            raise ConfigFileConflict(
                "The settings file changed while this was being saved, so nothing was saved. "
                "Reload the settings and try again."
            )
        if backup_path is not None:
            atomic_write_bytes(backup_path, previous or b"")
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(path, content)
    return config_revision(content)
