"""Remove old files below an opened folder, never following a link.

The trusted cleanups that keep folders from growing forever share these walks:
people's and agents' scratch folders (``custom_agents.scratch_retention``),
the gateway's chat uploads (``assistant_gateway.upload_retention``), saved
tool outputs (``custom_agents.output_retention``) and the files of a removed
person's held conversation artifacts (``remove_listed_file``).

Agents' code can write scratch folders and may plant links in them, so no
walk follows a link: every folder is opened relative to the one before it
with ``O_NOFOLLOW``, and files are removed by name inside the folder that was
opened. Following a planted link could delete files anywhere the process can
write. Three walks:

- ``remove_old_files`` decides file by file by when each was last changed.
  Links, devices, sockets and other unusual entries are left alone and logged
  with the caller's event name (``OldFilesRemoved.SKIPPED_EVENT``).
- ``remove_folder_tree`` deletes one folder that is old as a whole (a saved
  tool result, a research result), with everything in it. A link inside is
  removed as a link; what it points at is never touched.
- ``remove_listed_file`` deletes one file a record names, only when it lies
  inside the given folder.
"""

from __future__ import annotations

import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from access_control import safe_open
from access_control.safe_open import UnsafePath

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 60 * 60
# Deeper folders are left alone (and logged); the cleaned trees are shallow.
MAX_DEPTH = 32
FOLDER_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW

SKIPPED_NOT_FILE_OR_FOLDER = "not_a_regular_file_or_folder"
SKIPPED_TOO_DEEP = "too_deep"
SKIPPED_CANNOT_OPEN = "cannot_open"
SKIPPED_CANNOT_REMOVE = "cannot_remove"
SKIPPED_OUTSIDE_FOLDER = "outside_cleaned_folder"


@dataclass
class OldFilesRemoved:
    """What a walk removed and what it left in place.

    With ``dry_run`` the walks remove nothing: they count the files (and
    their bytes) and the whole folders they would remove, with the same
    decisions, so a dry run tells what a real one would delete. Folders that
    a real walk removes only because it emptied them are not counted.
    """

    # Log event for entries left in place; each cleanup names its own.
    SKIPPED_EVENT: ClassVar[str] = "old_file_skipped"

    files_removed: int = 0
    bytes_removed: int = 0
    folders_removed: int = 0
    skipped: int = 0
    dry_run: bool = False


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
            remove_file_if_old(folder, name, info, shown, cutoff, counts)
        elif stat.S_ISDIR(info.st_mode):
            _remove_subfolder(folder, name, info, shown, cutoff, counts, depth=depth)
        else:
            note_skipped(counts, shown / name, SKIPPED_NOT_FILE_OR_FOLDER)


def remove_folder_tree(
    parent: int,
    name: str,
    shown: Path,
    counts: OldFilesRemoved,
    *,
    first: str | None = None,
    last: str | None = None,
    depth: int = 0,
) -> bool:
    """Delete the folder ``name`` in the open ``parent`` and everything in it; True when it is gone.

    ``shown`` is the folder's path, for logs only. ``first`` names an entry
    to delete before any other, so a reader that finds the folder half
    deleted finds that entry missing first (a saved tool result's
    ``manifest.json``). ``last`` names one to delete after all the others,
    whatever the listing order (a build folder's lock file, which keeps a new
    build out until the rest is gone). A link inside is removed as a link and never
    followed; a link in place of the folder itself is left alone. Regular
    files count as files removed (with their bytes), folders as folders
    removed. When something cannot be removed the folder stays, and the
    entry is logged.
    """

    if depth >= MAX_DEPTH:
        note_skipped(counts, shown, SKIPPED_TOO_DEEP)
        return False
    try:
        folder = os.open(name, FOLDER_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return True
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_OPEN)  # e.g. a link in place of the folder
        return False
    try:
        names = os.listdir(folder)
        if first in names:
            names.remove(first)
            names.insert(0, first)
        if last in names:
            names.remove(last)
            names.append(last)
        for entry in names:
            _remove_entry(folder, entry, shown / entry, counts, depth=depth)
    finally:
        os.close(folder)
    if counts.dry_run:
        counts.folders_removed += 1
        return True
    try:
        os.rmdir(name, dir_fd=parent)
    except FileNotFoundError:
        return True
    except OSError:
        return False  # something could not be removed (logged) or was added meanwhile
    counts.folders_removed += 1
    return True


def _remove_entry(folder: int, name: str, shown: Path, counts: OldFilesRemoved, *, depth: int) -> None:
    try:
        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        remove_folder_tree(folder, name, shown, counts, depth=depth + 1)
        return
    try:
        # unlink removes the entry itself: a link goes, its target stays.
        if not counts.dry_run:
            os.unlink(name, dir_fd=folder)
    except FileNotFoundError:
        return
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_REMOVE)
        return
    if stat.S_ISREG(info.st_mode):
        counts.files_removed += 1
        counts.bytes_removed += info.st_size


def remove_listed_file(root: Path, path: Path, counts: OldFilesRemoved) -> None:
    """Delete the regular file a record names, only inside ``root``, then its folder if that is now empty.

    ``root`` is the trusted folder the records' files are kept in (the
    gateway's uploads, conversation artifacts). A path outside it, or one
    through a link, is left alone and logged. A file already gone is not
    counted. ``root`` itself is never removed.
    """

    root = root.expanduser().resolve()
    try:
        parts = Path(os.path.abspath(path)).relative_to(root).parts
    except ValueError:
        note_skipped(counts, path, SKIPPED_OUTSIDE_FOLDER)
        return
    if not parts:
        note_skipped(counts, path, SKIPPED_OUTSIDE_FOLDER)
        return
    try:
        info = safe_open.entry_status(root, parts)
        if info is None:
            return  # already gone
        if counts.dry_run:
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePath(f"Not a regular file: {path}", reason=safe_open.REASON_NOT_REGULAR_FILE)
        else:
            safe_open.remove_file(root, parts)
    except UnsafePath as exc:
        if exc.reason != safe_open.REASON_NOT_FOUND:
            note_skipped(counts, path, exc.reason)
        return
    counts.files_removed += 1
    counts.bytes_removed += info.st_size
    if not counts.dry_run:
        _remove_emptied_folder(root, parts[:-1], counts)


def _remove_emptied_folder(root: Path, parts: tuple[str, ...], counts: OldFilesRemoved) -> None:
    """Remove the folder a listed file was in when that left it empty; ``root`` stays."""

    if not parts:
        return
    try:
        parent = safe_open.open_directory(root, parts[:-1])
    except UnsafePath:
        return
    try:
        os.rmdir(parts[-1], dir_fd=parent)  # only an empty folder, never through a link
    except OSError:
        return  # not empty, or no longer a folder: leave it
    finally:
        os.close(parent)
    counts.folders_removed += 1


def note_skipped(counts: OldFilesRemoved, path: Path, reason: str) -> None:
    counts.skipped += 1
    logger.info(
        "A cleanup left an entry in place. event=%s reason=%s path=%s", counts.SKIPPED_EVENT, reason, path
    )


def remove_file_if_old(
    folder: int, name: str, info: os.stat_result, shown: Path, cutoff: float, counts: OldFilesRemoved
) -> None:
    """Delete the regular file ``name`` in the open ``folder`` (path ``shown``) when ``info`` says it is old."""

    if info.st_mtime >= cutoff:
        return
    try:
        # unlink never follows a link: if the file was swapped for one, the
        # link itself is removed and its target is untouched.
        if not counts.dry_run:
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
    if counts.dry_run or not empty or not (emptied_now or info.st_mtime < cutoff):
        return
    try:
        os.rmdir(name, dir_fd=folder)
    except OSError:
        return  # something was added meanwhile, or it is no longer a folder: leave it
    counts.folders_removed += 1


__all__ = [
    "FOLDER_FLAGS",
    "MAX_DEPTH",
    "SECONDS_PER_DAY",
    "SKIPPED_CANNOT_OPEN",
    "SKIPPED_NOT_FILE_OR_FOLDER",
    "SKIPPED_OUTSIDE_FOLDER",
    "OldFilesRemoved",
    "note_skipped",
    "remove_file_if_old",
    "remove_folder_tree",
    "remove_listed_file",
    "remove_old_files",
    "retention_cutoff",
]
