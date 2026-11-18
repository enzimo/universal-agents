"""The bootstrap bundle a host starts from (README "Installation traffic", "Starting a host").

It holds what a host needs before it has a certificate of its own:
- the installation's id and its authority public keys, the root of every trust check;
- the installation's partition count (``transport.queues.partitions``, README
  "Partitions"), which the host's trust store takes, so every process computes agents'
  partitions with the same count;
- trust records: the certificates of the installation authority's hosts, so the host can
  check the authority's join reply, and the newest revocations record.

The launcher writes the authority keys when it creates them and the partition count from
the saved settings at every start, before any host starts; the installation authority
adds its own certificate and the current revocations record whenever it signs new ones
and keeps the count. A bundle written before the count joined it is read, so the launcher
can keep its records, but no trust store is built from it: a host that has no count
would have to guess one.

The authority keys are the root of trust: whoever can change the file can name other
keys, so it lives in the owner-only trust folder and only the launcher and the
installation authority write it. The records it carries are verified against
those keys like any other copy, so where a record is stored adds no trust. The NATS
credential of the pre-join permission set joins the bundle in phase 3, with per-host
NATS users.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from custom_messages.wire.encoding import b64url_decode
from custom_messages.wire.partitions import check_partition_count
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import AuthorityKey, TrustStore
from custom_transport.protocol1.private_files import write_private_file

_LOGGER = logging.getLogger(__name__)
_BUNDLE_VERSION = 1
# The order the trust store takes records in: a certificate before the revocations
# that may name it, and before the sessions it signs.
_RECORD_ORDER = ("agreement", "certificate", "revocations", "agent_session")


class BootstrapError(RuntimeError):
    """The bootstrap bundle is missing or damaged."""


@dataclass(frozen=True)
class BootstrapBundle:
    """``partitions`` is ``None`` only in a bundle written before the count joined it."""

    installation: str
    authority_keys: tuple[tuple[str, str], ...]
    records: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    partitions: Optional[int] = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.partitions is not None:
            check_partition_count(self.partitions)

    def anchors(self) -> list[AuthorityKey]:
        return [AuthorityKey(self.installation, key_id, Ed25519PublicKey.from_public_bytes(b64url_decode(public_key)))
                for key_id, public_key in self.authority_keys]

    def partition_count(self) -> int:
        """The installation's partition count; ``BootstrapError`` when the bundle holds none."""

        if self.partitions is None:
            raise BootstrapError("The bootstrap bundle holds no partition count. The launcher writes it before any "
                                 "host starts; restart the instance.")
        return self.partitions

    def empty_trust_store(self) -> TrustStore:
        """A trust store holding the bundle's authority keys and partition count, and no records yet."""

        return TrustStore(self.installation, self.anchors(), partition_count=self.partition_count())

    def trust_store(self, now: datetime) -> TrustStore:
        """A trust store holding the bundle's authority keys, its partition count and the records that verify at ``now``."""

        store = self.empty_trust_store()
        self.add_to(store, now)
        return store

    def add_to(self, store: TrustStore, now: datetime, *,
               record_types: Optional[Iterable[str]] = None) -> list[Mapping[str, Any]]:
        """Add the bundle's records of ``record_types`` (all by default) in the store's order; return those it took as new.

        ``PersistentTrustStore.open`` adds the agreements first, then the host's kept
        revocations records, then the rest, so a kept record comes before the bundle's.
        """

        wanted = set(_RECORD_ORDER if record_types is None else record_types)
        taken: list[Mapping[str, Any]] = []
        chosen = [record for record in self.records if record.get("record") in wanted]
        for record in sorted(chosen, key=lambda item: _RECORD_ORDER.index(str(item.get("record")))):
            try:
                if store.add(json.dumps(record).encode(), now=now):
                    taken.append(record)
            except FrameRefused as refused:
                # A stale record must not stop a host from starting; the authority writes newer ones.
                _LOGGER.warning("A bootstrap record does not verify and is skipped. event=bootstrap_record_refused "
                                "record=%s key=%s code=%s at=%s", record.get("record"), record.get("key"),
                                refused.code, refused.at)
        return taken

    def with_records(self, records: Iterable[Mapping[str, Any]]) -> "BootstrapBundle":
        """The bundle with ``records`` instead of its current ones (the authority's newest); the rest is kept."""

        return BootstrapBundle(self.installation, self.authority_keys, tuple(records), partitions=self.partitions)


def read_bundle(path: Path) -> BootstrapBundle:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BootstrapError(f"The bootstrap bundle {path} is missing; the launcher writes it before starting hosts.") from error
    except (OSError, ValueError) as error:
        raise BootstrapError(f"The bootstrap bundle {path} cannot be read: {error}") from error
    try:
        if document["version"] != _BUNDLE_VERSION:
            raise BootstrapError(f"The bootstrap bundle {path} has an unknown format.")
        keys = tuple((str(key["key_id"]), str(key["public_key"])) for key in document["authority_keys"])
        records = tuple(document.get("records", ()))
        if not keys or any(not isinstance(record, dict) or record.get("record") not in _RECORD_ORDER for record in records):
            raise BootstrapError(f"The bootstrap bundle {path} names no authority key or holds an unknown record.")
        return BootstrapBundle(str(document["installation"]), keys, records, partitions=document.get("partitions"))
    except (KeyError, TypeError, ValueError) as error:
        raise BootstrapError(f"The bootstrap bundle {path} is damaged: {error}") from error


def write_bundle(path: Path, bundle: BootstrapBundle) -> None:
    """Replace the bundle whole; readers see the old or the new file, never a part, also after a power loss.

    Every bundle written holds the partition count (``BootstrapError`` otherwise).
    """

    document = {
        "version": _BUNDLE_VERSION,
        "installation": bundle.installation,
        "partitions": bundle.partition_count(),
        "authority_keys": [{"key_id": key_id, "public_key": public_key} for key_id, public_key in bundle.authority_keys],
        "records": list(bundle.records),
    }
    write_private_file(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"))
