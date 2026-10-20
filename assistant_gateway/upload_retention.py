"""The gateway's cleanup of old chat uploads.

Files people send in any chat app are saved in ``state/uploads`` (a folder per
conversation) and recorded in the gateway database (``gateway_attachments``).
Nothing else deletes them except removing their owner, so without this they
would pile up forever. The gateway deletes uploads older than
``interactive_apps.plugins.builtin.internal_website.settings.attachments.retention_days``
(90 by default; 0 keeps them) when it starts and every ``CLEANUP_INTERVAL`` after:

1. records created before the cutoff, oldest first and in batches, then their
   files. The record goes first, so a file that cannot be removed is left
   without a record, never a record without its file;
2. every file under ``state/uploads`` last changed before the cutoff, which
   also removes files left without a record (by a failed removal, or by
   earlier versions that saved files for sends they then refused), and the
   folders left empty.

The gateway is the only writer of ``state/uploads``. Agents copy an upload into
their own folder to work on it (``custom_tools.user_attachment_tools``), and
those copies are not touched. No link is followed (``old_file_cleanup``,
``access_control.safe_open``).

Administrators change the number of days in Settings > Team
(``assistant_gateway.file_retention``); the next run uses the new number.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, Protocol

from access_control import safe_open
from access_control.safe_open import UnsafePath
from old_file_cleanup import (
    SECONDS_PER_DAY,
    OldFilesRemoved,
    note_skipped,
    remove_old_files,
)

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL = timedelta(hours=6)
RECORD_BATCH = 500

SKIPPED_OUTSIDE_UPLOADS = "outside_uploads_folder"


class UploadRecords(Protocol):
    """The gateway database's upload records (``storage.gateway_state_repository``)."""

    def delete_attachments_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]: ...


@dataclass(kw_only=True)
class UploadPruneResult(OldFilesRemoved):
    """What one upload cleanup did."""

    SKIPPED_EVENT: ClassVar[str] = "upload_prune_skipped"

    retention_days: int
    records_removed: int = 0


def prune_uploads(
    *,
    records: UploadRecords,
    uploads_dir: Path,
    retention_days: int,
    now: datetime | None = None,
) -> UploadPruneResult:
    """Delete upload records and files older than ``retention_days``; 0 deletes nothing."""

    result = UploadPruneResult(retention_days=retention_days)
    if retention_days <= 0:
        logger.info("Upload cleanup is turned off. event=upload_prune_disabled retention_days=%s", retention_days)
        return result
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - retention_days * SECONDS_PER_DAY
    root = uploads_dir.expanduser().resolve()
    _remove_old_records(records, root, datetime.fromtimestamp(cutoff, timezone.utc).isoformat(), result)
    _remove_old_files(root, cutoff, result)
    logger.info(
        "Old chat uploads were removed. event=upload_pruned retention_days=%s records_removed=%s "
        "files_removed=%s bytes_removed=%s folders_removed=%s skipped=%s",
        result.retention_days,
        result.records_removed,
        result.files_removed,
        result.bytes_removed,
        result.folders_removed,
        result.skipped,
    )
    return result


def _remove_old_records(records: UploadRecords, root: Path, cutoff: str, result: UploadPruneResult) -> None:
    while True:
        paths = records.delete_attachments_created_before(cutoff, limit=RECORD_BATCH)
        result.records_removed += len(paths)
        for path in paths:
            if path:
                _remove_recorded_file(root, Path(path), result)
        if len(paths) < RECORD_BATCH:
            return


def _remove_recorded_file(root: Path, path: Path, result: UploadPruneResult) -> None:
    try:
        parts = Path(os.path.abspath(path)).relative_to(root).parts
    except ValueError:
        note_skipped(result, path, SKIPPED_OUTSIDE_UPLOADS)
        return
    try:
        info = safe_open.entry_status(root, parts)
        if info is None:
            return  # already gone
        safe_open.remove_file(root, parts)
    except UnsafePath as exc:
        if exc.reason != safe_open.REASON_NOT_FOUND:
            note_skipped(result, path, exc.reason)
        return
    result.files_removed += 1
    result.bytes_removed += info.st_size
    _remove_emptied_folder(root, parts[:-1], result)


def _remove_emptied_folder(root: Path, parts: tuple[str, ...], result: UploadPruneResult) -> None:
    """Remove a conversation folder this cleanup emptied, as the walk does; the uploads folder stays."""

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
    result.folders_removed += 1


def _remove_old_files(root: Path, cutoff: float, result: UploadPruneResult) -> None:
    try:
        folder = safe_open.open_root(root)
    except UnsafePath:
        return  # nothing uploaded yet
    try:
        remove_old_files(folder, root, cutoff, result)
    finally:
        os.close(folder)


class UploadRetention:
    """Runs the upload cleanup when the gateway starts and every ``CLEANUP_INTERVAL`` after.

    ``retention_days`` may change while it runs (Settings > Team); each run
    reads the current number.
    """

    def __init__(
        self,
        *,
        records: UploadRecords,
        uploads_dir: Path,
        retention_days: int,
        interval: timedelta = CLEANUP_INTERVAL,
    ) -> None:
        self._records = records
        self._uploads_dir = uploads_dir
        self._interval = interval
        self.retention_days = retention_days

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval.total_seconds())
            except TimeoutError:
                pass

    async def run_once(self) -> UploadPruneResult | None:
        try:
            return await asyncio.to_thread(
                prune_uploads,
                records=self._records,
                uploads_dir=self._uploads_dir,
                retention_days=self.retention_days,
            )
        except Exception:
            logger.exception(
                "The upload cleanup failed; it runs again later. event=upload_prune_failed uploads_dir=%s",
                self._uploads_dir,
            )
            return None


__all__ = [
    "CLEANUP_INTERVAL",
    "UploadPruneResult",
    "UploadRecords",
    "UploadRetention",
    "prune_uploads",
]
