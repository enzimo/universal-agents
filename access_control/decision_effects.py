"""Journal artifact ownership before writing data, and remove only the artifact recorded.

The initial recovery row precedes external work. Each newly opened, empty
file is recorded before any bytes are written, so a database outage cannot
leave delivered data or a program without a durable recovery record.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict

from access_control import safe_open
from storage.access_repository import AccessRepository, DecisionRecovery

logger = logging.getLogger(__name__)


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
