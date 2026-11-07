"""What the installation authority keeps on disk about what it signed: epochs, renewal answers, revocations.

The authority writes its certificates and revocations record to ``UA1_CERTS``, but that
bucket is not its own memory: any process with the shared NATS login can delete or
overwrite a key there, and a NATS server can lose its store while the instance's trust
folder survives. Three things must outlive that, so the authority keeps them in its own
trust folder (``trust/hosts/authority/issued.json``, owner-only), written before the record
reaches the bucket:

- **The highest membership epoch it issued, per runtime.** An epoch names one certificate
  only (README "Trust over time"), and hosts, the bootstrap bundle and the authority's own
  trust store may hold a certificate the bucket no longer has. Issuing that epoch again
  with other content would be refused as ``replayed`` by everyone holding the first one.
- **Each renewal's answer, for 7 days** (README "Stored outcomes": the longest authority
  token lifetime). A host whose reply was lost retries with the same renewal id and gets
  the certificate it was issued, not another one it would refuse.
- **The newest revocations record it signed.** The next record is signed from the newest
  one the authority verifies; this copy keeps a lost bundle and an older record written
  back to the bucket from lifting a revocation.

A file that cannot be read is moved aside (logged) and the authority starts from the
bucket and the bundle, as it would on the first start: it still verifies every record it
reads there, so a damaged file loses no trust, only the protection above for records the
bucket also lost.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS
from custom_messages.wire.values import parse_timestamp

ISSUED_FILENAME = "issued.json"
# README "Stored outcomes": kept at least as long as an authority token can last.
OUTCOME_RETENTION = timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS)
_VERSION = 1
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredRenewal:
    """A renewal's answer: the certificate issued, the digest of the body that asked, and when."""

    certificate: Mapping[str, Any]
    body_sha256: str
    stored_at: datetime


class IssueLedger:
    """One installation authority's record of the epochs, renewal answers and revocations it signed."""

    def __init__(self, path: Path, installation: str, *, logger: logging.Logger = _LOGGER) -> None:
        self.path = path
        self.installation = installation
        self.logger = logger
        self._epochs: dict[str, int] = {}
        self._renewals: dict[str, StoredRenewal] = {}
        self._revocations: Optional[dict[str, Any]] = None

    @classmethod
    def open(cls, folder: Path, installation: str, *, logger: logging.Logger = _LOGGER) -> "IssueLedger":
        """The ledger kept in ``folder``; empty on the first start, for another installation, or when unreadable."""

        ledger = cls(folder / ISSUED_FILENAME, installation, logger=logger)
        try:
            document = json.loads(ledger.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ledger
        except (OSError, ValueError) as error:
            ledger._set_aside(error)
            return ledger
        try:
            if document["version"] != _VERSION:
                raise ValueError(f"unknown format {document['version']!r}")
            if document["installation"] != installation:
                # A new installation id: nothing of the old installation's applies.
                logger.warning("The authority's issue record belongs to another installation and is replaced. "
                               "event=authority_ledger_other_installation path=%s kept=%s installation=%s",
                               ledger.path, document["installation"], installation)
                return ledger
            ledger._epochs = {str(runtime): int(epoch) for runtime, epoch in document["epochs"].items()}
            ledger._renewals = {
                str(key): StoredRenewal(dict(held["certificate"]), str(held["body_sha256"]),
                                        parse_timestamp(str(held["stored_at"])))
                for key, held in document["renewals"].items()}
            revocations = document.get("revocations")
            ledger._revocations = dict(revocations) if isinstance(revocations, dict) else None
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            ledger._epochs, ledger._renewals, ledger._revocations = {}, {}, None
            ledger._set_aside(error)
        return ledger

    def _set_aside(self, error: Exception) -> None:
        """Move an unreadable file aside, so the next save does not overwrite what an operator may want to read."""

        aside = self.path.with_name(f"{self.path.name}.unreadable-{secrets.token_hex(4)}")
        try:
            os.replace(self.path, aside)
        except OSError:
            aside = self.path
        self.logger.error("The authority's issue record cannot be read; it starts from the bucket and the bundle. "
                          "event=authority_ledger_unreadable path=%s kept_at=%s error=%s", self.path, aside, error)

    # -- epochs -----------------------------------------------------------------------------------

    def highest_epoch(self, runtime: str) -> int:
        """The highest membership epoch this authority issued to ``runtime``; 0 when none."""

        return self._epochs.get(runtime, 0)

    def take_epoch(self, runtime: str, epoch: int) -> None:
        """Note that ``epoch`` is used, before anything holds it; ``save`` makes it last."""

        self._epochs[runtime] = max(self._epochs.get(runtime, 0), epoch)

    # -- renewal answers --------------------------------------------------------------------------

    def renewal(self, retry_key: str, now: datetime) -> Optional[StoredRenewal]:
        """The answer stored for a renewal's retry key, while it is kept."""

        held = self._renewals.get(retry_key)
        return held if held is not None and now - held.stored_at < OUTCOME_RETENTION else None

    def store_renewal(self, retry_key: str, certificate: Mapping[str, Any], body_sha256: str, now: datetime) -> None:
        self._renewals[retry_key] = StoredRenewal(dict(certificate), body_sha256, now)

    # -- revocations ------------------------------------------------------------------------------

    @property
    def revocations(self) -> Optional[dict[str, Any]]:
        """The newest revocations record this authority signed, as it signed it."""

        return self._revocations

    def keep_revocations(self, record: Mapping[str, Any]) -> None:
        self._revocations = dict(record)

    # -- saving -----------------------------------------------------------------------------------

    def save(self, now: datetime) -> None:
        """Write the ledger atomically and durably, dropping renewal answers older than the retention."""

        self._renewals = {key: held for key, held in self._renewals.items() if now - held.stored_at < OUTCOME_RETENTION}
        document = {
            "version": _VERSION,
            "installation": self.installation,
            "epochs": dict(sorted(self._epochs.items())),
            "renewals": {key: {"certificate": held.certificate, "body_sha256": held.body_sha256,
                               "stored_at": format_timestamp(held.stored_at)}
                         for key, held in sorted(self._renewals.items())},
            "revocations": self._revocations,
        }
        _write_private_json(self.path, document)


def _write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    """Replace ``path`` with ``document``, owner-only; readers see the old or the new file, never a part."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)
