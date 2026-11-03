"""A host's trust store that keeps what the contract says must survive a restart.

README "Trust over time" and the registry's write rules: receivers keep the newest
revocations record they verified, "also across restarts, never forget it", and keep the
highest block-list sequence they have seen. Otherwise a host restarted from an older
bundle could trust a revoked key again, or accept an older block list written back to
its bucket. This wraps ``custom_messages.wire.trust.TrustStore`` and writes those two
things to the host's own trust folder whenever they change. Everything else in the
store (certificates, sessions, agreements) is read again from the buckets.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import TrustStore, VerifiedRecord
from custom_transport.protocol1.bootstrap import BootstrapBundle

TRUST_STATE_FILENAME = "trust-state.json"
_STATE_VERSION = 1
_REVOCATIONS, _BLOCK_LIST = "revocations", "block_list"
_LOGGER = logging.getLogger(__name__)


class PersistentTrustStore:
    """One host's trust store, with its revocations records and block-list sequences kept on disk."""

    def __init__(self, store: TrustStore, path: Path, revocations: dict[str, Any],
                 block_lists: dict[str, tuple[int, str]]) -> None:
        self.store = store
        self._path = path
        self._revocations = revocations
        self._block_lists = block_lists

    @classmethod
    def open(cls, bundle: BootstrapBundle, folder: Path, now: datetime) -> "PersistentTrustStore":
        """The bundle's trust, plus the newest records this host kept before it restarted."""

        path = folder / TRUST_STATE_FILENAME
        store = bundle.trust_store(now)
        revocations: dict[str, Any] = {}
        block_lists: dict[str, tuple[int, str]] = {}
        if path.exists():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                if document.get("version") != _STATE_VERSION:
                    raise ValueError("unknown format")
                revocations = dict(document.get(_REVOCATIONS, {}))
                block_lists = {team: (int(held[0]), str(held[1])) for team, held in document.get(_BLOCK_LIST, {}).items()}
            except (OSError, ValueError, TypeError, IndexError) as error:
                # Refusing to start would stop the host for good; logging it lets an operator act.
                _LOGGER.error("A host's kept trust records cannot be read; starting from the bundle alone. "
                              "event=trust_state_unreadable path=%s error=%s", path, error)
        persistent = cls(store, path, {}, block_lists)
        for installation, record in revocations.items():
            persistent._add_kept(installation, record, now)
        return persistent

    def _add_kept(self, installation: str, record: Any, now: datetime) -> None:
        try:
            added = self.store.add(json.dumps(record).encode(), now=now)
        except FrameRefused as refused:
            _LOGGER.error("A kept revocations record no longer verifies. event=kept_revocations_refused "
                          "installation=%s code=%s at=%s", installation, refused.code, refused.at)
            return
        if added:
            # Newer than the bundle's: keep it on disk. Otherwise the bundle's is newer and
            # the next record this host verifies replaces both.
            self._revocations[installation] = record

    def add(self, data: bytes, *, now: datetime) -> bool:
        """Verify and keep a trust record, as ``TrustStore.add``; a newer revocations record is also kept on disk."""

        added = self.store.add(data, now=now)
        if added:
            document = json.loads(data)
            if document.get("record") == _REVOCATIONS:
                self._revocations[str(document["body"]["installation"])] = document
                self._save()
        return added

    def verify_block_list(self, data: bytes, *, now: datetime) -> Optional[VerifiedRecord]:
        """Read a team's block list; ``None`` when this host already holds the same or a newer one.

        The highest sequence held for the team is passed to the reader, so an older
        list written back to the bucket is refused (``replayed``), also after a restart.
        """

        team = str(json.loads(data).get("key", ""))
        held = self._block_lists.get(team)
        record = self.store.verify(data, record=_BLOCK_LIST, now=now, held=held)
        if held is not None and record.document.sequence == held[0]:
            return None
        self._block_lists[team] = (record.document.sequence, record.digest)
        self._save()
        return record

    def _save(self) -> None:
        document = {"version": _STATE_VERSION, _REVOCATIONS: self._revocations,
                    _BLOCK_LIST: {team: [held[0], held[1]] for team, held in self._block_lists.items()}}
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._path.with_name(f".{self._path.name}.{secrets.token_hex(8)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)
