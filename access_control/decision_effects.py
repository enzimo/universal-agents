"""Journal artifact ownership before writing data, and remove only the artifact recorded.

The initial recovery row precedes external work. Each newly opened, empty
file is recorded before any bytes are written, so a database outage cannot
leave delivered data or a program without a durable recovery record.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict

from access_control import safe_open
from storage.access_repository import AccessRepository, DecisionRecovery

logger = logging.getLogger(__name__)


def _read_bounded_sha256(descriptor: int, limit: int) -> tuple[int, str, bool]:
    """Read up to ``limit + 1`` bytes from ``descriptor``; return ``(count, sha256, exceeded)``."""

    digest = hashlib.sha256()
    remaining = limit + 1
    total = 0
    while remaining and (chunk := os.read(descriptor, min(remaining, 1024 * 1024))):
        digest.update(chunk)
        total += len(chunk)
        remaining -= len(chunk)
    return total, digest.hexdigest(), remaining == 0


class OwnedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parts: tuple[str, ...]
    device: int
    inode: int
    sha256: str
    size: int
    staging_parts: tuple[str, ...] | None = None

    @classmethod
    def prepared(
        cls, parts: tuple[str, ...], descriptor: int, data: bytes, staging_parts: tuple[str, ...] | None = None
    ) -> OwnedFile:
        info = os.fstat(descriptor)
        return cls(
            parts=parts, device=info.st_dev, inode=info.st_ino,
            sha256=hashlib.sha256(data).hexdigest(), size=len(data), staging_parts=staging_parts,
        )

    def remove(self, root: Path, remove: Callable[[], None] | None = None) -> None:
        """Missing or replaced files are finished work; access or I/O errors need another pass.

        Compare both the allocated file and its content. Inode numbers can
        be reused after a deletion, and a person can edit a file in place.
        An empty reservation can be removed after a failed preparation.
        """

        try:
            if self.staging_parts is not None:
                staging = safe_open.entry_status(root, self.staging_parts)
                if staging is not None and (staging.st_dev, staging.st_ino) == (self.device, self.inode):
                    safe_open.remove_file(root, self.staging_parts)
            with safe_open.opened_file(root, self.parts) as (descriptor, info):
                same = (info.st_dev, info.st_ino) == (self.device, self.inode) and info.st_size in (0, self.size)
                digest = hashlib.sha256()
                remaining = self.size + 1
                while same and remaining and (chunk := os.read(descriptor, min(remaining, 1024 * 1024))):
                    digest.update(chunk)
                    remaining -= len(chunk)
                if not same or not remaining or (info.st_size and digest.hexdigest() != self.sha256):
                    logger.warning(
                        "A decision's artifact was replaced or edited; the replacement is kept. "
                        "event=access_decision_artifact_changed path=%s", "/".join(self.parts),
                    )
                    return
                identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            if remove is None:
                safe_open.remove_file(root, self.parts, expected_identity=identity)
            else:
                remove()
        except safe_open.UnsafePath as exc:
            if exc.reason == safe_open.REASON_NOT_FOUND:
                return
            raise


class ReplacedFile(BaseModel):
    """A journaled atomic replacement of an existing file, storing the exact bytes to restore."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parts: tuple[str, ...]
    device: int
    inode: int
    sha256: str
    size: int
    previous_sha256: str
    previous_b64: str
    staging_parts: tuple[str, ...] | None = None

    @classmethod
    def prepared(
        cls,
        parts: tuple[str, ...],
        descriptor: int,
        data: bytes,
        previous_data: bytes,
        staging_parts: tuple[str, ...] | None = None,
    ) -> ReplacedFile:
        info = os.fstat(descriptor)
        return cls(
            parts=parts,
            device=info.st_dev,
            inode=info.st_ino,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            previous_sha256=hashlib.sha256(previous_data).hexdigest(),
            previous_b64=base64.b64encode(previous_data).decode("ascii"),
            staging_parts=staging_parts,
        )

    def previous_bytes(self) -> bytes:
        return base64.b64decode(self.previous_b64.encode("ascii"))

    def restore(self, root: Path, restore: Callable[[bytes], None] | None = None) -> None:
        """Restore the previous bytes if our replacement is on disk; clean up staging if never replaced.

        - If the process stopped before ``os.replace``, the target still holds
          ``previous_sha256`` and the staging file is removed quietly without
          logging ``access_decision_artifact_changed``.
        - If the target holds our exact replacement ``(device, inode, size, sha256)``,
          the previous bytes are restored atomically.
        - If the target was edited, replaced, or deleted externally, it is left
          untouched and ``access_decision_artifact_changed`` is logged.
        """

        if self.staging_parts is not None:
            staging = safe_open.entry_status(root, self.staging_parts)
            if staging is not None and (staging.st_dev, staging.st_ino) == (self.device, self.inode):
                safe_open.remove_file(root, self.staging_parts)
        previous = self.previous_bytes()
        try:
            with safe_open.opened_file(root, self.parts) as (descriptor, info):
                if info.st_size not in (self.size, len(previous)):
                    logger.warning(
                        "A decision's replaced artifact was modified or replaced externally; the current file is kept. "
                        "event=access_decision_artifact_changed path=%s",
                        "/".join(self.parts),
                    )
                    return
                count, digest, exceeded = _read_bounded_sha256(descriptor, max(self.size, len(previous)))
                if not exceeded and count == len(previous) and digest == self.previous_sha256:
                    # Never replaced (stopped before os.replace) or already restored.
                    return
                matches_replacement = (
                    not exceeded
                    and (info.st_dev, info.st_ino) == (self.device, self.inode)
                    and info.st_size == self.size
                    and count == self.size
                    and digest == self.sha256
                )
                if not matches_replacement:
                    logger.warning(
                        "A decision's replaced artifact was modified or replaced externally; the current file is kept. "
                        "event=access_decision_artifact_changed path=%s",
                        "/".join(self.parts),
                    )
                    return
        except safe_open.UnsafePath as exc:
            if exc.reason == safe_open.REASON_NOT_FOUND:
                logger.warning(
                    "A decision's replaced artifact was removed externally; it is not recreated. "
                    "event=access_decision_artifact_changed path=%s",
                    "/".join(self.parts),
                )
                return
            raise
        if restore is None:
            safe_open.write_bytes(root, self.parts, previous)
        else:
            restore(previous)


class RemovedFile(BaseModel):
    """A journaled removal of an existing file, storing the exact bytes to restore."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parts: tuple[str, ...]
    sha256: str
    size: int
    previous_b64: str

    @classmethod
    def prepared(cls, parts: tuple[str, ...], previous_data: bytes) -> RemovedFile:
        return cls(
            parts=parts,
            sha256=hashlib.sha256(previous_data).hexdigest(),
            size=len(previous_data),
            previous_b64=base64.b64encode(previous_data).decode("ascii"),
        )

    def previous_bytes(self) -> bytes:
        return base64.b64decode(self.previous_b64.encode("ascii"))

    def _existing_state(self, root: Path) -> str:
        """Classify ``root/parts`` as ``'matches'``, ``'changed'``, or ``'missing'`` without following links."""

        try:
            with safe_open.opened_file(root, self.parts) as (descriptor, info):
                if info.st_size != self.size:
                    return "changed"
                count, digest, exceeded = _read_bounded_sha256(descriptor, self.size)
                if not exceeded and count == self.size and digest == self.sha256:
                    return "matches"
                return "changed"
        except safe_open.UnsafePath as exc:
            if exc.reason == safe_open.REASON_NOT_FOUND:
                return "missing"
            if exc.reason in {
                safe_open.REASON_LINK,
                safe_open.REASON_NOT_REGULAR_FILE,
                safe_open.REASON_SEVERAL_LINKS,
            }:
                if safe_open.entry_status(root, self.parts) is not None:
                    return "changed"
                return "missing"
            raise

    def restore(self, root: Path, restore: Callable[[bytes], None] | None = None) -> None:
        """Restore the removed bytes atomically when missing; never overwrite another file or link.

        - If the path still holds the exact original bytes (stopped before unlink
          or already restored), return quietly.
        - If another file, symlink, directory, or multi-link entry now exists at
          the path, leave it untouched and log ``access_decision_artifact_changed``.
        - If missing, write ``previous_bytes()`` atomically via
          ``safe_open.write_bytes_exclusive`` (or ``restore``).
        """

        state = self._existing_state(root)
        if state == "matches":
            return
        if state == "changed":
            logger.warning(
                "A decision's removed artifact was replaced externally before it could be restored; "
                "the current file is kept. event=access_decision_artifact_changed path=%s",
                "/".join(self.parts),
            )
            return
        previous = self.previous_bytes()
        try:
            if restore is None:
                safe_open.write_bytes_exclusive(root, self.parts, previous)
            else:
                restore(previous)
        except safe_open.UnsafePath as exc:
            if exc.reason != safe_open.REASON_DESTINATION_EXISTS:
                raise
            after = self._existing_state(root)
            if after == "matches":
                return
            if after == "changed":
                logger.warning(
                    "A decision's removed artifact was replaced externally before it could be restored; "
                    "the current file is kept. event=access_decision_artifact_changed path=%s",
                    "/".join(self.parts),
                )
                return
            raise


class EffectJournal:
    def __init__(self, store: AccessRepository, recovery: DecisionRecovery) -> None:
        self.store = store
        self.recovery = recovery

    def prepare(
        self, category: str, root: Path, parts: tuple[str, ...], descriptor: int, data: bytes,
        *, staging_parts: tuple[str, ...] | None = None,
    ) -> None:
        owned = OwnedFile.prepared(parts, descriptor, data, staging_parts)
        self.recovery.effects[category].append(owned.model_dump(mode="json"))
        try:
            self.store.save_decision_recovery(self.recovery)
        except BaseException:
            # No data has been written yet. A failed journal update cannot
            # leave an unrecorded copy; a committed update just finds it gone.
            owned.remove(root)
            raise

    def prepare_replacement(
        self,
        category: str,
        root: Path,
        parts: tuple[str, ...],
        descriptor: int,
        data: bytes,
        previous_data: bytes,
        *,
        staging_parts: tuple[str, ...] | None = None,
    ) -> ReplacedFile:
        replaced = ReplacedFile.prepared(parts, descriptor, data, previous_data, staging_parts)
        self.recovery.effects[category].append(replaced.model_dump(mode="json"))
        try:
            self.store.save_decision_recovery(self.recovery)
        except BaseException:
            # No data has been written or replaced yet. Remove the empty
            # staging file; the target file still holds previous_data.
            replaced.restore(root)
            raise
        return replaced

    def prepare_removal(
        self,
        category: str,
        parts: tuple[str, ...],
        previous_data: bytes,
    ) -> RemovedFile:
        removed = RemovedFile.prepared(parts, previous_data)
        self.recovery.effects[category].append(removed.model_dump(mode="json"))
        try:
            self.store.save_decision_recovery(self.recovery)
        except BaseException:
            # No file has been unlinked yet. Drop the unsaved in-memory entry;
            # if the save committed before raising, recovery sees the original
            # bytes intact and treats it as a quiet no-op.
            self.recovery.effects[category].pop()
            raise
        return removed
