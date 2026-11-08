"""A host's trust store that keeps what the contract says must survive a restart.

README "Trust over time" and the registry's write rules: receivers keep the newest
revocations record they verified, "also across restarts, never forget it", and keep the
highest block-list sequence they have seen. Otherwise a host restarted from an older
bundle could trust a revoked key again, or accept an older block list written back to
its bucket. This wraps ``custom_messages.wire.trust.TrustStore`` and keeps, in
``trust-state.json`` in the host's own trust folder:

- for each installation, the revocations record the store holds, whether it came from
  the bundle, a bucket or this file, plus any older record that still lists an entry the
  newer one left out early. ``TrustStore`` keeps such an entry until nothing its key
  signed can be accepted (README "Trust over time"); adding the kept records oldest
  first after a restart rebuilds the same list;
- for each team, the highest block-list sequence read and its digest.

Order at start: the bundle's agreements, then the kept revocations records, oldest
first, then the bundle's other records. A kept record therefore comes before the
bundle's and before anything read from the buckets, as the README requires ("adds its
stored record before any it reads"). A kept record this store cannot check yet
(another installation's, before its agreement is read; a clock that is behind) stays
in the file and is tried again before each record this store adds, and right after an
agreement, so it still comes first. Kept records are never trusted as stored: each is
verified again when it is added.

Saving takes an exclusive lock on the file (``file_locking``), reads the file again,
merges what another store object on the same folder kept (a second copy of a host
starting while the first still runs), and replaces the file whole
(``private_files``). Records another object kept are verified by this one before it
uses them. A save that fails is logged (``trust_state_save_failed``) and tried again at
the next change or block-list read; the store keeps using what it verified.

A file that cannot be read (damaged, written by another format version, not private
to the host) is moved aside as ``trust-state.json.damaged-<time>-<random>`` and logged
(``trust_state_unreadable``), and the host continues from the bundle. Refusing to
start would stop the host until someone removed the file, while the loss is small: a
host reads the current revocations record from UA1_CERTS when it starts, and the
installation authority rewrites the bundle with every revocations record it signs.

Certificates, sessions and agreements are read again from the buckets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import RETENTION, SKEW, TrustStore, VerifiedRecord
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.bootstrap import BootstrapBundle
from custom_transport.protocol1.private_files import (
    ensure_private_folder,
    read_private_file,
    sync_folder,
    write_private_file,
)
from file_locking import FileLockUnavailable, exclusive_file_lock

TRUST_STATE_FILENAME = "trust-state.json"
# A file that cannot be read is renamed to its name plus this mark, the time and a random part.
DAMAGED_MARK = ".damaged-"
_STATE_VERSION = 2
_REVOCATIONS, _AGREEMENT, _BLOCK_LIST = "revocations", "agreement", "block_list"
_BLOCK_LISTS = "block_lists"
_REVOCATIONS_KEY_PREFIX = "revocations."
# The bundle's records that come after the kept revocations records.
_AFTER_KEPT = ("certificate", _REVOCATIONS, "agent_session")
_LOGGER = logging.getLogger(__name__)

Record = dict[str, Any]


@dataclass
class _KeptState:
    """What a trust-state file holds: revocations records by installation, oldest first, and block-list sequences."""

    revocations: dict[str, list[Record]] = field(default_factory=dict)
    block_lists: dict[str, tuple[int, str]] = field(default_factory=dict)


class PersistentTrustStore:
    """One host's trust store, with the revocations records and block-list sequences it must keep on disk."""

    def __init__(self, store: TrustStore, path: Path) -> None:
        self.store = store
        self._path = path
        # installation -> records this store verified, oldest first; the newest is the one it holds.
        self._kept: dict[str, list[Record]] = {}
        # Kept records this store has not verified yet; written back and tried again.
        self._pending: list[Record] = []
        # Fingerprints of records this store let go (pruned or never verifiable); a merge does not bring them back.
        self._dropped: set[str] = set()
        self._block_lists: dict[str, tuple[int, str]] = {}
        self._unsaved = False

    @classmethod
    def open(cls, bundle: BootstrapBundle, folder: Path, now: datetime) -> "PersistentTrustStore":
        """The bundle's trust, with the records this host kept before it restarted added before the bundle's."""

        persistent = cls(TrustStore(bundle.installation, bundle.anchors()), folder / TRUST_STATE_FILENAME)
        kept = persistent._read_file()
        persistent._block_lists = dict(kept.block_lists)
        bundle.add_to(persistent.store, now, record_types=(_AGREEMENT,))
        for records in kept.revocations.values():
            for record in records:
                persistent._add_kept(record, now)
        for taken in bundle.add_to(persistent.store, now, record_types=_AFTER_KEPT):
            if taken.get("record") == _REVOCATIONS:
                persistent._kept.setdefault(_installation(taken), []).append(dict(taken))
        for installation in list(persistent._kept):
            persistent._prune(installation, now)
        if _fingerprints(persistent._all_records()) != _fingerprints(_flatten(kept.revocations)):
            persistent._save()
        return persistent

    # -- trust records ------------------------------------------------------------------------------

    def add(self, data: bytes, *, now: datetime) -> bool:
        """Verify and keep a trust record, as ``TrustStore.add``; a revocations record the store takes is also kept on disk.

        Kept records still waiting to be checked are tried first, so they come before this one.
        """

        changed = self._retry_pending(now) or self._unsaved
        added = False
        try:
            added = self.store.add(data, now=now)
            if added:
                document = json.loads(bytes(data))
                if document.get("record") == _REVOCATIONS:
                    self._keep(document, now)
                    changed = True
                elif document.get("record") == _AGREEMENT:
                    # Another installation's kept record may be checkable now.
                    changed = self._retry_pending(now) or changed
        finally:
            if changed:
                self._save()
        return added

    def kept_revocations(self, installation: str) -> list[Record]:
        """The revocations records this store verified and keeps for ``installation``, oldest first.

        The last is the one the store holds. The installation authority signs its next
        record from the newest one it verified, these included (``installation_authority``).
        """

        return [dict(record) for record in self._kept.get(installation, [])]

    def _add_kept(self, record: Record, now: datetime) -> None:
        try:
            self.store.add(_encoded(record), now=now)
        except FrameRefused as refused:
            # Usual for another installation's record, whose agreement is read after start.
            _LOGGER.warning("A kept revocations record cannot be checked yet; it stays kept and is tried again before each "
                            "record this host adds. event=kept_revocations_waiting installation=%s sequence=%s code=%s at=%s",
                            _installation(record), record.get("sequence"), refused.code, refused.known_at)
            self._pending.append(record)
            return
        # Verified, also when the store already held it or a newer one: pruning decides what stays.
        self._kept.setdefault(_installation(record), []).append(record)

    def _keep(self, record: Record, now: datetime) -> None:
        installation = _installation(record)
        self._kept.setdefault(installation, []).append(record)
        self._prune(installation, now)

    def _prune(self, installation: str, now: datetime) -> None:
        """Keep the newest record and the older ones that still list an entry the newer ones left out early."""

        records = sorted(_unique(self._kept.get(installation, [])), key=_sequence)
        if not records:
            return
        newest = records[-1]
        covered = {_entry_id(entry) for entry in _entries(newest)}
        keep = [newest]
        for record in reversed(records[:-1]):
            needed = {_entry_id(entry) for entry in _entries(record) if _needed(entry, now)} - covered
            if needed:
                keep.append(record)
                covered |= needed
        self._dropped |= _fingerprints(records) - _fingerprints(keep)
        self._kept[installation] = sorted(keep, key=_sequence)

    def _retry_pending(self, now: datetime) -> bool:
        """Try the kept records not checked yet; return whether any was taken or dropped."""

        changed = False
        for record in sorted(self._pending, key=_sequence):
            try:
                self.store.add(_encoded(record), now=now)
            except FrameRefused as refused:
                held = self._kept.get(_installation(record))
                if held and _sequence(record) <= _sequence(held[-1]):
                    # The store holds a newer checked record, so this one can never be taken.
                    self._pending.remove(record)
                    self._dropped.add(_fingerprint(record))
                    changed = True
                    _LOGGER.warning("A kept revocations record that never verified is dropped; a newer one is held. "
                                    "event=kept_revocations_dropped installation=%s sequence=%s code=%s",
                                    _installation(record), record.get("sequence"), refused.code)
                continue
            self._pending.remove(record)
            _LOGGER.info("A kept revocations record was checked and taken. event=kept_revocations_taken "
                         "installation=%s sequence=%s", _installation(record), record.get("sequence"))
            self._keep(record, now)
            changed = True
        return changed

    # -- block lists --------------------------------------------------------------------------------

    def verify_block_list(self, data: bytes, *, team: str, now: datetime) -> VerifiedRecord:
        """Read ``team``'s block list: the verified list, also when it is the one already read.

        The highest sequence held for the team is passed to the reader, so an older
        list written back to the bucket is refused (``replayed``), also after a restart.
        A list stored under another team's key is refused (``invalid_body`` at ``/key``).
        """

        held = self._block_lists.get(team)
        record = self.store.verify(data, record=_BLOCK_LIST, now=now, held=held)
        if record.document.key != team:
            raise FrameRefused("invalid_body", f"the block list of {record.document.key} is read as {team}'s", at="/key")
        if held is None or record.document.sequence > held[0]:
            self._block_lists[team] = (record.document.sequence, record.digest)
            self._save()
        elif self._unsaved:
            self._save()
        return record

    # -- the file -----------------------------------------------------------------------------------

    def _all_records(self) -> list[Record]:
        return [*_flatten(self._kept), *self._pending]

    def _save(self) -> None:
        """Write the kept records, merged under the file's lock with what another store object on this folder kept."""

        try:
            ensure_private_folder(self._path.parent)
            with exclusive_file_lock(self._path, purpose="kept trust records"):
                self._merge(self._read_file())
                write_private_file(self._path, self._content())
        except (OSError, FileLockUnavailable) as error:
            self._unsaved = True
            _LOGGER.error("A host's kept trust records could not be saved; the next change or block-list read tries again. "
                          "event=trust_state_save_failed path=%s error=%s", self._path, error)
            return
        self._unsaved = False

    def _merge(self, on_disk: _KeptState) -> None:
        """Take what another store object kept: its records to verify before use, the higher block-list sequences."""

        known = _fingerprints(self._all_records()) | self._dropped
        for record in _flatten(on_disk.revocations):
            if _fingerprint(record) not in known:
                self._pending.append(record)
        for team, (sequence, digest) in on_disk.block_lists.items():
            held = self._block_lists.get(team)
            if held is None or sequence > held[0]:
                self._block_lists[team] = (sequence, digest)
            elif sequence == held[0] and digest != held[1]:
                _LOGGER.warning("Two block lists of one team were kept under one sequence; this store's is kept. "
                                "event=trust_state_block_list_conflict team=%s sequence=%s", team, sequence)

    def _content(self) -> bytes:
        revocations: dict[str, list[Record]] = {}
        for record in self._all_records():
            revocations.setdefault(_installation(record), []).append(record)
        document = {
            "version": _STATE_VERSION,
            _REVOCATIONS: {installation: sorted(_unique(records), key=_sequence)
                           for installation, records in sorted(revocations.items())},
            _BLOCK_LISTS: {team: [held[0], held[1]] for team, held in sorted(self._block_lists.items())},
        }
        return (json.dumps(document, sort_keys=True, indent=1) + "\n").encode("utf-8")

    def _read_file(self) -> _KeptState:
        """The kept state on disk; nothing when there is no file, and nothing, with the file set aside, when it cannot be read."""

        try:
            return _parse_state(read_private_file(self._path))
        except FileNotFoundError:
            return _KeptState()
        except (OSError, ValueError, TypeError) as error:  # NotAPrivateFile is a ValueError
            self._set_aside(error)
            return _KeptState()

    def _set_aside(self, error: Exception) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        aside = self._path.with_name(f"{self._path.name}{DAMAGED_MARK}{stamp}-{secrets.token_hex(3)}")
        try:
            os.replace(self._path, aside)
            sync_folder(self._path.parent)
        except OSError as move_error:
            _LOGGER.error("A host's kept trust records cannot be read or set aside; the host continues from the bundle. "
                          "event=trust_state_unreadable path=%s moved_to=none error=%s move_error=%s",
                          self._path, error, move_error)
            return
        _LOGGER.error("A host's kept trust records cannot be read; the file is set aside and the host continues from the "
                      "bundle and the records it reads from the buckets. event=trust_state_unreadable path=%s moved_to=%s "
                      "error=%s", self._path, aside, error)


# -- the file's shape -----------------------------------------------------------------------------------

def _parse_state(raw: bytes) -> _KeptState:
    """A trust-state file's content; ``ValueError`` for anything but this version's exact shape."""

    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("version") != _STATE_VERSION:
        raise ValueError(f"not a version {_STATE_VERSION} trust-state file")
    revocations, block_lists = document.get(_REVOCATIONS), document.get(_BLOCK_LISTS)
    if not isinstance(revocations, dict) or not isinstance(block_lists, dict):
        raise ValueError("its revocations and block lists are not maps")
    kept: dict[str, list[Record]] = {}
    for installation, records in revocations.items():
        if not isinstance(records, list) or not all(_looks_like_revocations(record) for record in records):
            raise ValueError(f"the records kept for {installation} are not revocations records")
        kept[str(installation)] = sorted(records, key=_sequence)
    held: dict[str, tuple[int, str]] = {}
    for team, value in block_lists.items():
        if not (isinstance(value, list) and len(value) == 2 and type(value[0]) is int and isinstance(value[1], str)):
            raise ValueError(f"the block-list sequence kept for {team} is not [sequence, digest]")
        held[str(team)] = (value[0], value[1])
    return _KeptState(kept, held)


def _looks_like_revocations(record: Any) -> bool:
    return (isinstance(record, dict) and record.get("record") == _REVOCATIONS and type(record.get("sequence")) is int
            and str(record.get("key", "")).startswith(_REVOCATIONS_KEY_PREFIX))


# -- records -------------------------------------------------------------------------------------------

def _encoded(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint(record: Mapping[str, Any]) -> str:
    """The same for the same record however it was encoded: the digest of its canonical JSON."""

    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprints(records: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(_fingerprint(record) for record in records)


def _unique(records: Iterable[Record]) -> list[Record]:
    seen: dict[str, Record] = {}
    for record in records:
        seen.setdefault(_fingerprint(record), record)
    return list(seen.values())


def _flatten(by_installation: Mapping[str, list[Record]]) -> list[Record]:
    return [record for records in by_installation.values() for record in records]


def _sequence(record: Mapping[str, Any]) -> int:
    return int(record["sequence"])


def _installation(record: Mapping[str, Any]) -> str:
    """The installation a revocations record covers: its key is ``revocations.<installation>`` (checked when verified)."""

    return str(record.get("key", "")).removeprefix(_REVOCATIONS_KEY_PREFIX)


def _entries(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    body = record.get("body")
    entries = body.get("entries", []) if isinstance(body, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_id(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return str(entry.get("certificate_id")), str(entry.get("runtime")), str(entry.get("key_id"))


def _needed(entry: Mapping[str, Any], now: datetime) -> bool:
    """Whether a frame the entry's key signed could still be accepted at ``now`` (``TrustStore``'s rule for early entries)."""

    return now - parse_timestamp(str(entry["key_expires_at"])) < RETENTION + SKEW
