"""What the installation authority keeps on disk about what it signed: epochs, renewal answers, revocations.

The authority writes its certificates and revocations record to ``UA1_CERTS``, but that
bucket is not its own memory: any process with the shared NATS login can delete or
overwrite a key there, and a NATS server can lose its store while the instance's trust
folder survives. Three things must outlive that, so the authority keeps them in its own
trust folder (``trust/hosts/authority/issued.json``, owner-only, written whole by
``custom_transport.protocol1.private_files``) before the record reaches the bucket:

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
from custom_transport.protocol1.private_files import read_private_file, write_private_file

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
    partitions: tuple[int, ...] = ()
    connection: Optional[Mapping[str, Any]] = None


class IssueLedger:
    """One installation authority's record of the epochs, renewal answers, NATS key revocations, and revocations it signed."""

    def __init__(self, path: Path, installation: str, *, logger: logging.Logger = _LOGGER) -> None:
        self.path = path
        self.installation = installation
        self.logger = logger
        self._epochs: dict[str, int] = {}
        self._renewals: dict[str, StoredRenewal] = {}
        self._revocations: Optional[dict[str, Any]] = None
        # Permanent ban of revoked NATS user keys (CR-1/S1/C09): nats_public_key -> revoke_until (unix seconds).
        self._revoked_nats_keys: dict[str, int] = {}
        # NATS user keys whose revocation was confirmed on the server (C01/C08): nats_public_key -> revoke_until.
        self._confirmed_nats_revocations: dict[str, int] = {}
        # Permanent record of bootstrap bundle NATS user keys (S1): never accepted as a host's own key.
        self._bootstrap_nats_keys: set[str] = set()
        # Granted partitions -> verified partition_owner sequence recorded per issued certificate_id
        # (C11, C17): distinguishes pre-release renewals from post-recovery reassignments.
        self._certificate_partitions: dict[str, dict[int, int]] = {}
        # Latest issued NATS user JWT exp (unix seconds) per nats_public_key (F15/FA-9/SEC-08),
        # so revocation pruning follows the short JWT lifetime instead of the 30-day certificate end.
        self._nats_key_jwt_expires: dict[str, int] = {}

    @classmethod
    def open(cls, folder: Path, installation: str, *, logger: logging.Logger = _LOGGER) -> "IssueLedger":
        """The ledger kept in ``folder``; empty on the first start, for another installation, or when unreadable."""

        ledger = cls(folder / ISSUED_FILENAME, installation, logger=logger)
        try:
            document = json.loads(read_private_file(ledger.path))
        except FileNotFoundError:
            return ledger
        except (OSError, ValueError) as error:  # NotAPrivateFile is a ValueError
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
                str(key): StoredRenewal(
                    dict(held["certificate"]),
                    str(held["body_sha256"]),
                    parse_timestamp(str(held["stored_at"])),
                    partitions=tuple(int(p) for p in held.get("partitions", ())),
                    connection=dict(held["connection"]) if isinstance(held.get("connection"), dict) else None,
                )
                for key, held in document["renewals"].items()}
            revocations = document.get("revocations")
            ledger._revocations = dict(revocations) if isinstance(revocations, dict) else None
            raw_revoked_nats = document.get("revoked_nats_keys") or {}
            ledger._revoked_nats_keys = {str(k): int(v) for k, v in raw_revoked_nats.items()}
            raw_confirmed = document.get("confirmed_nats_revocations") or {}
            ledger._confirmed_nats_revocations = {str(k): int(v) for k, v in raw_confirmed.items()}
            raw_bootstrap = document.get("bootstrap_nats_keys") or []
            ledger._bootstrap_nats_keys = {str(k) for k in raw_bootstrap}
            raw_cert_parts = document.get("certificate_partitions") or {}
            ledger._certificate_partitions = {
                str(cert_id): {int(p): int(seq) for p, seq in parts.items()}
                for cert_id, parts in raw_cert_parts.items()
            }
            raw_jwt_exp = document.get("nats_key_jwt_expires") or {}
            ledger._nats_key_jwt_expires = {str(k): int(v) for k, v in raw_jwt_exp.items()}
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            ledger._epochs, ledger._renewals, ledger._revocations = {}, {}, None
            ledger._revoked_nats_keys, ledger._confirmed_nats_revocations, ledger._bootstrap_nats_keys = {}, {}, set()
            ledger._certificate_partitions = {}
            ledger._nats_key_jwt_expires = {}
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

    def store_renewal(
        self,
        retry_key: str,
        certificate: Mapping[str, Any],
        body_sha256: str,
        now: datetime,
        *,
        partitions: tuple[int, ...] = (),
        connection: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._renewals[retry_key] = StoredRenewal(
            dict(certificate),
            body_sha256,
            now,
            partitions=tuple(sorted(set(partitions))),
            connection=dict(connection) if connection is not None else None,
        )

    # -- revocations ------------------------------------------------------------------------------

    @property
    def revocations(self) -> Optional[dict[str, Any]]:
        """The newest revocations record this authority signed, as it signed it."""

        return self._revocations

    def keep_revocations(self, record: Mapping[str, Any]) -> None:
        self._revocations = dict(record)

    # -- NATS key lifecycle (CR-1, S1, C01, C09) --------------------------------------------------

    @property
    def revoked_nats_keys(self) -> dict[str, int]:
        """All revoked NATS user keys mapped to their ``revoke_until`` unix timestamp (permanent ban)."""

        return dict(self._revoked_nats_keys)

    def is_nats_key_revoked(self, nats_public_key: str) -> bool:
        return nats_public_key in self._revoked_nats_keys

    def record_revoked_nats_key(self, nats_public_key: str, revoke_until: int) -> None:
        self._revoked_nats_keys[nats_public_key] = max(
            self._revoked_nats_keys.get(nats_public_key, 0), int(revoke_until)
        )

    def is_nats_revocation_confirmed(
        self,
        nats_public_key: str,
        *,
        min_revoke_until: Optional[int] = None,
    ) -> bool:
        confirmed_until = self._confirmed_nats_revocations.get(nats_public_key)
        if confirmed_until is None:
            return False
        required = int(min_revoke_until) if min_revoke_until is not None else self._revoked_nats_keys.get(nats_public_key, 0)
        return confirmed_until >= required

    def record_confirmed_nats_revocation(self, nats_public_key: str, revoke_until: int) -> None:
        ts = int(revoke_until)
        self._revoked_nats_keys[nats_public_key] = max(self._revoked_nats_keys.get(nats_public_key, 0), ts)
        self._confirmed_nats_revocations[nats_public_key] = max(
            self._confirmed_nats_revocations.get(nats_public_key, 0), ts
        )

    def record_certificate_partitions(
        self,
        certificate_id: str,
        partition_sequences: Mapping[int, int],
    ) -> None:
        """Record ``{partition: owner_sequence}`` granted with ``certificate_id`` (empty dict on join, C11/C17)."""

        self._certificate_partitions[str(certificate_id)] = {
            int(p): int(seq) for p, seq in sorted(partition_sequences.items())
        }

    def certificate_partitions(self, certificate_id: str) -> Optional[dict[int, int]]:
        """The ``{partition: owner_sequence}`` map granted with ``certificate_id``, or ``None`` if not recorded."""

        recorded = self._certificate_partitions.get(str(certificate_id))
        return None if recorded is None else dict(recorded)

    def forget_certificate_partitions(self, certificate_id: str) -> None:
        self._certificate_partitions.pop(str(certificate_id), None)

    def record_nats_key_jwt_expiry(self, nats_public_key: str, exp_ts: int) -> None:
        """Record the latest issued NATS user JWT ``exp`` (unix seconds) for ``nats_public_key`` (F15)."""

        self._nats_key_jwt_expires[nats_public_key] = max(
            self._nats_key_jwt_expires.get(nats_public_key, 0), int(exp_ts)
        )

    def nats_key_jwt_expiry(self, nats_public_key: str) -> Optional[int]:
        """Latest recorded NATS user JWT ``exp`` (unix seconds) for ``nats_public_key``, or ``None``."""

        return self._nats_key_jwt_expires.get(nats_public_key)

    @property
    def bootstrap_nats_keys(self) -> frozenset[str]:
        return frozenset(self._bootstrap_nats_keys)

    def is_bootstrap_nats_key(self, nats_public_key: str) -> bool:
        return nats_public_key in self._bootstrap_nats_keys

    def record_bootstrap_nats_key(self, nats_public_key: str) -> None:
        self._bootstrap_nats_keys.add(nats_public_key)

    # -- saving -----------------------------------------------------------------------------------

    def save(self, now: datetime) -> None:
        """Write the ledger atomically and durably, dropping renewal answers older than the retention."""

        self._renewals = {key: held for key, held in self._renewals.items() if now - held.stored_at < OUTCOME_RETENTION}
        renewals_doc: dict[str, dict[str, Any]] = {}
        for key, held in sorted(self._renewals.items()):
            entry: dict[str, Any] = {
                "certificate": held.certificate,
                "body_sha256": held.body_sha256,
                "stored_at": format_timestamp(held.stored_at),
            }
            if held.partitions:
                entry["partitions"] = list(held.partitions)
            if held.connection is not None:
                entry["connection"] = dict(held.connection)
            renewals_doc[key] = entry
        document: dict[str, Any] = {
            "version": _VERSION,
            "installation": self.installation,
            "epochs": dict(sorted(self._epochs.items())),
            "renewals": renewals_doc,
            "revocations": self._revocations,
        }
        if self._revoked_nats_keys:
            document["revoked_nats_keys"] = dict(sorted(self._revoked_nats_keys.items()))
        if self._confirmed_nats_revocations:
            document["confirmed_nats_revocations"] = dict(sorted(self._confirmed_nats_revocations.items()))
        if self._bootstrap_nats_keys:
            document["bootstrap_nats_keys"] = sorted(self._bootstrap_nats_keys)
        if self._certificate_partitions:
            document["certificate_partitions"] = {
                cert_id: {str(p): seq for p, seq in sorted(parts.items())}
                for cert_id, parts in sorted(self._certificate_partitions.items())
            }
        if self._nats_key_jwt_expires:
            document["nats_key_jwt_expires"] = dict(sorted(self._nats_key_jwt_expires.items()))
        write_private_file(self.path, json.dumps(document, sort_keys=True).encode())
