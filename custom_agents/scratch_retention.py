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
walk (``old_file_cleanup``, shared with the gateway's upload cleanup) never
follows a link. Links, devices, sockets and other unusual entries are left
alone and logged (``event=scratch_prune_skipped``).
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from access_control import safe_open
from access_control.safe_open import UnsafePath
from old_file_cleanup import (
    SECONDS_PER_DAY,
    SKIPPED_NOT_FILE_OR_FOLDER,
    OldFilesRemoved,
    note_skipped,
    remove_old_files,
    retention_cutoff,
)
from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    SCRATCH_DIRNAME,
    USERS_DIRNAME,
)

logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class ScratchPruneResult(OldFilesRemoved):
    """What one cleanup did."""

    SKIPPED_EVENT: ClassVar[str] = "scratch_prune_skipped"

    retention_days: int
    scratch_folders: int = 0

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
    cutoff = retention_cutoff(retention_days, now)
    for parts in _scratch_folders(workspaces_dir, result):
        shown = workspaces_dir.joinpath(*parts)
        try:
            folder = safe_open.open_directory(workspaces_dir, parts)
        except UnsafePath as exc:
            if exc.reason != safe_open.REASON_NOT_FOUND:
                note_skipped(result, shown, exc.reason)
            continue
        result.scratch_folders += 1
        try:
            remove_old_files(folder, shown, cutoff, result)
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
            note_skipped(result, workspaces_dir / USERS_DIRNAME, exc.reason)
        return folders
    for name, info in sorted(entries):
        if stat.S_ISDIR(info.st_mode):
            folders.append((USERS_DIRNAME, name, SCRATCH_DIRNAME))
        else:
            note_skipped(result, workspaces_dir / USERS_DIRNAME / name, SKIPPED_NOT_FILE_OR_FOLDER)
    return folders


__all__ = [
    "SECONDS_PER_DAY",
    "ScratchPruneResult",
    "prune_scratch_folders",
]
