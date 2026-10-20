"""Remove files older than a cutoff below an opened folder, never following a link.

The trusted cleanups that keep folders from growing forever share this walk:
people's and agents' scratch folders (``custom_agents.scratch_retention``) and
the gateway's chat uploads (``assistant_gateway.upload_retention``).

Agents' code can write scratch folders and may plant links in them, so the
walk never follows a link: every folder is opened relative to the one before
it with ``O_NOFOLLOW``, and files are removed by name inside the folder that
was opened. Following a planted link could delete files anywhere the process
can write. Links, devices, sockets and other unusual entries are left alone
and logged with the caller's event name (``OldFilesRemoved.SKIPPED_EVENT``).
"""

from __future__ import annotations

import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 60 * 60
# Deeper folders are left alone (and logged); the cleaned trees are shallow.
MAX_DEPTH = 32
FOLDER_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW

SKIPPED_NOT_FILE_OR_FOLDER = "not_a_regular_file_or_folder"
SKIPPED_TOO_DEEP = "too_deep"
SKIPPED_CANNOT_OPEN = "cannot_open"
SKIPPED_CANNOT_REMOVE = "cannot_remove"


@dataclass
class OldFilesRemoved:
    """What a walk removed and what it left in place."""

    # Log event for entries left in place; each cleanup names its own.
    SKIPPED_EVENT: ClassVar[str] = "old_file_skipped"

    files_removed: int = 0
    bytes_removed: int = 0
    folders_removed: int = 0
    skipped: int = 0


def retention_cutoff(retention_days: int, now: float | None = None) -> float:
    """The time before which a file is older than ``retention_days``."""

    return (time.time() if now is None else now) - retention_days * SECONDS_PER_DAY


def remove_old_files(
    folder: int, shown: Path, cutoff: float, counts: OldFilesRemoved, *, depth: int = 0
) -> None:
    """Delete regular files last changed before ``cutoff`` in the open ``folder`` and below it.

    ``shown`` is the folder's path, for logs only. A folder below is removed
    when it is empty afterwards and either this walk emptied it or it is itself
    older than the cutoff; ``folder`` itself stays.
    """

    for name in os.listdir(folder):
        try:
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed while we looked
        if stat.S_ISREG(info.st_mode):
            _remove_file(folder, name, info, shown, cutoff, counts)
        elif stat.S_ISDIR(info.st_mode):
            _remove_subfolder(folder, name, info, shown, cutoff, counts, depth=depth)
        else:
            note_skipped(counts, shown / name, SKIPPED_NOT_FILE_OR_FOLDER)


def note_skipped(counts: OldFilesRemoved, path: Path, reason: str) -> None:
    counts.skipped += 1
    logger.info(
        "A cleanup left an entry in place. event=%s reason=%s path=%s", counts.SKIPPED_EVENT, reason, path
    )


def _remove_file(
    folder: int, name: str, info: os.stat_result, shown: Path, cutoff: float, counts: OldFilesRemoved
) -> None:
    if info.st_mtime >= cutoff:
        return
    try:
        # unlink never follows a link: if the file was swapped for one, the
        # link itself is removed and its target is untouched.
        os.unlink(name, dir_fd=folder)
    except FileNotFoundError:
        return
    except OSError:
        note_skipped(counts, shown / name, SKIPPED_CANNOT_REMOVE)
        return
    counts.files_removed += 1
    counts.bytes_removed += info.st_size


def _remove_subfolder(
    folder: int,
    name: str,
    info: os.stat_result,
    shown: Path,
    cutoff: float,
    counts: OldFilesRemoved,
    *,
    depth: int,
) -> None:
    path = shown / name
    if depth >= MAX_DEPTH:
        note_skipped(counts, path, SKIPPED_TOO_DEEP)
        return
    try:
        child = os.open(name, FOLDER_FLAGS, dir_fd=folder)
    except OSError:
        note_skipped(counts, path, SKIPPED_CANNOT_OPEN)  # e.g. replaced by a link since the listing
        return
    removed_before = counts.files_removed + counts.folders_removed
    try:
        remove_old_files(child, path, cutoff, counts, depth=depth + 1)
        empty = not os.listdir(child)
    finally:
        os.close(child)
    emptied_now = counts.files_removed + counts.folders_removed > removed_before
    if not empty or not (emptied_now or info.st_mtime < cutoff):
        return
    try:
        os.rmdir(name, dir_fd=folder)
    except OSError:
        return  # something was added meanwhile, or it is no longer a folder: leave it
    counts.folders_removed += 1


__all__ = [
    "MAX_DEPTH",
    "SECONDS_PER_DAY",
    "SKIPPED_CANNOT_OPEN",
    "SKIPPED_NOT_FILE_OR_FOLDER",
    "OldFilesRemoved",
    "note_skipped",
    "remove_old_files",
    "retention_cutoff",
]
