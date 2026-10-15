"""Trusted cleanup of people's and agents' scratch folders.

Agents keep temporary files in ``scratch/`` of their task folder
(``users/<person>/scratch/``, or ``agents/scratch/`` for work for no person),
and the gateway keeps its copies of files sent to a person in
``scratch/sent/``. On each run of the internal-improvement maintenance
schedule the manager deletes files there that were last changed more than
``tool_policy.scratch_retention_days`` ago (0 turns this off), before
InternalImprovementAgent's review, and puts the counts in that run's
instructions (``custom_agents.internal_improvement_runtime``).

Trusted code does this rather than the agent because InternalImprovementAgent
works for no person, and people's folders are closed to work for no person
(decision D6): the agent cannot open ``users/*/scratch`` at all.

Agents' code can write scratch folders and may plant links in them, so the
walk never follows a link: every folder is opened relative to the one before
it with ``O_NOFOLLOW``, and files are removed by name inside the folder that
was opened. Following a planted link could delete files anywhere the manager
can write. Links, devices, sockets and other unusual entries are left alone
and logged (``event=scratch_prune_skipped``).
"""

from __future__ import annotations

import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from access_control import safe_open
from access_control.safe_open import UnsafePath
from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    SCRATCH_DIRNAME,
    USERS_DIRNAME,
)

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 60 * 60
# Deeper folders are left alone (and logged); scratch trees are shallow.
MAX_SCRATCH_DEPTH = 32
_FOLDER_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW

SKIPPED_NOT_FILE_OR_FOLDER = "not_a_regular_file_or_folder"
SKIPPED_TOO_DEEP = "too_deep"
SKIPPED_CANNOT_OPEN = "cannot_open"
SKIPPED_CANNOT_REMOVE = "cannot_remove"


@dataclass
class ScratchPruneResult:
    """What one cleanup did."""

    retention_days: int
    scratch_folders: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    folders_removed: int = 0
    skipped: int = 0

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    def instruction_sentence(self) -> str:
        """One sentence for InternalImprovementAgent's run instructions."""

        if not self.enabled:
            return (
                "Scratch cleanup is turned off (tool_policy.scratch_retention_days is 0), "
                "so no temporary files were removed before this review."
            )
        return (
            f"Before this review, trusted maintenance removed {self.files_removed} files "
            f"({self.bytes_removed} bytes) and {self.folders_removed} emptied folders older than "
            f"{self.retention_days} days from {self.scratch_folders} scratch/ folders, and left "
            f"{self.skipped} links or other unusual entries in place. Include these counts in your "
            "run record; people's scratch folders are closed to you, so do not try to inspect them."
        )


def prune_scratch_folders(
    workspaces_dir: Path,
    *,
    retention_days: int,
    now: float | None = None,
) -> ScratchPruneResult:
    """Delete old files from every ``users/*/scratch`` and ``agents/scratch`` below ``workspaces_dir``.

    A regular file is deleted when it was last changed more than
    ``retention_days`` ago. A folder is removed when it is empty afterwards
    and either this run emptied it or it is itself that old; the ``scratch/``
    folders themselves stay. Nothing outside ``scratch/`` is touched and no
    link is followed.
    """

    result = ScratchPruneResult(retention_days=retention_days)
    if not result.enabled:
        logger.info("Scratch cleanup is turned off. event=scratch_prune_disabled retention_days=%s", retention_days)
        return result
    cutoff = (time.time() if now is None else now) - retention_days * SECONDS_PER_DAY
    for parts in _scratch_folders(workspaces_dir, result):
        shown = workspaces_dir.joinpath(*parts)
        try:
            folder = safe_open.open_directory(workspaces_dir, parts)
        except UnsafePath as exc:
            if exc.reason != safe_open.REASON_NOT_FOUND:
                _skip(result, shown, exc.reason)
            continue
        result.scratch_folders += 1
        try:
            _prune_folder(folder, shown, cutoff, result, depth=0)
        finally:
            os.close(folder)
    logger.info(
        "Old scratch files were removed. event=scratch_pruned retention_days=%s scratch_folders=%s "
        "files_removed=%s bytes_removed=%s folders_removed=%s skipped=%s",
        result.retention_days,
        result.scratch_folders,
        result.files_removed,
        result.bytes_removed,
        result.folders_removed,
        result.skipped,
    )
    return result


def _scratch_folders(workspaces_dir: Path, result: ScratchPruneResult) -> list[tuple[str, ...]]:
    """``agents/scratch`` and ``users/<folder>/scratch`` for every real person folder."""

    folders: list[tuple[str, ...]] = [(AGENTS_DIRNAME, SCRATCH_DIRNAME)]
    try:
        entries = safe_open.list_directory(workspaces_dir, (USERS_DIRNAME,))
    except UnsafePath as exc:
        if exc.reason != safe_open.REASON_NOT_FOUND:
            _skip(result, workspaces_dir / USERS_DIRNAME, exc.reason)
        return folders
    for name, info in sorted(entries):
        if stat.S_ISDIR(info.st_mode):
            folders.append((USERS_DIRNAME, name, SCRATCH_DIRNAME))
        else:
            _skip(result, workspaces_dir / USERS_DIRNAME / name, SKIPPED_NOT_FILE_OR_FOLDER)
    return folders


def _prune_folder(folder: int, shown: Path, cutoff: float, result: ScratchPruneResult, *, depth: int) -> None:
    for name in os.listdir(folder):
        try:
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed while we looked
        if stat.S_ISREG(info.st_mode):
            _prune_file(folder, name, info, shown, cutoff, result)
        elif stat.S_ISDIR(info.st_mode):
            _prune_subfolder(folder, name, info, shown, cutoff, result, depth=depth)
        else:
            _skip(result, shown / name, SKIPPED_NOT_FILE_OR_FOLDER)


def _prune_file(
    folder: int, name: str, info: os.stat_result, shown: Path, cutoff: float, result: ScratchPruneResult
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
        _skip(result, shown / name, SKIPPED_CANNOT_REMOVE)
        return
    result.files_removed += 1
    result.bytes_removed += info.st_size


def _prune_subfolder(
    folder: int,
    name: str,
    info: os.stat_result,
    shown: Path,
    cutoff: float,
    result: ScratchPruneResult,
    *,
    depth: int,
) -> None:
    path = shown / name
    if depth >= MAX_SCRATCH_DEPTH:
        _skip(result, path, SKIPPED_TOO_DEEP)
        return
    try:
        child = os.open(name, _FOLDER_FLAGS, dir_fd=folder)
    except OSError:
        _skip(result, path, SKIPPED_CANNOT_OPEN)  # e.g. replaced by a link since the listing
        return
    removed_before = result.files_removed + result.folders_removed
    try:
        _prune_folder(child, path, cutoff, result, depth=depth + 1)
        empty = not os.listdir(child)
    finally:
        os.close(child)
    emptied_now = result.files_removed + result.folders_removed > removed_before
    if not empty or not (emptied_now or info.st_mtime < cutoff):
        return
    try:
        os.rmdir(name, dir_fd=folder)
    except OSError:
        return  # something was added meanwhile, or it is no longer a folder: leave it
    result.folders_removed += 1


def _skip(result: ScratchPruneResult, path: Path, reason: str) -> None:
    result.skipped += 1
    logger.info("A scratch entry was left in place. event=scratch_prune_skipped reason=%s path=%s", reason, path)


__all__ = [
    "MAX_SCRATCH_DEPTH",
    "ScratchPruneResult",
    "prune_scratch_folders",
]
