"""The bootstrap bundle a host starts from (README "Installation traffic", "Starting a host").

It holds what a host needs before it has a certificate of its own:
- the installation's id and its authority public keys, the root of every trust check;
- trust records: the certificates of the installation authority's hosts, so the host can
  check the authority's join reply, and the newest revocations record.

The launcher writes the authority keys when it creates them; the installation
authority adds its own certificate and the current revocations record whenever it
signs new ones. Readers verify every record against the authority keys, so the file
grants nothing by itself: where a record is stored adds no trust. The NATS credential
of the pre-join permission set joins the bundle in phase 3, with per-host NATS users.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from custom_messages.wire.encoding import b64url_decode
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import AuthorityKey, TrustStore

_LOGGER = logging.getLogger(__name__)
_BUNDLE_VERSION = 1
# The order the trust store takes records in: a certificate before the revocations
# that may name it, and before the sessions it signs.
_RECORD_ORDER = ("agreement", "certificate", "revocations", "agent_session")


class BootstrapError(RuntimeError):
    """The bootstrap bundle is missing or damaged."""


@dataclass(frozen=True)
class BootstrapBundle:
    installation: str
    authority_keys: tuple[tuple[str, str], ...]
    records: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def anchors(self) -> list[AuthorityKey]:
        return [AuthorityKey(self.installation, key_id, Ed25519PublicKey.from_public_bytes(b64url_decode(public_key)))
                for key_id, public_key in self.authority_keys]

    def trust_store(self, now: datetime) -> TrustStore:
        """A trust store holding the bundle's authority keys and the records that still verify at ``now``."""

        store = TrustStore(self.installation, self.anchors())
        for record in sorted(self.records, key=lambda item: _RECORD_ORDER.index(str(item.get("record")))):
            try:
                store.add(json.dumps(record).encode(), now=now)
            except FrameRefused as refused:
                # A stale record must not stop a host from starting; the authority writes newer ones.
                _LOGGER.warning("A bootstrap record does not verify and is skipped. event=bootstrap_record_refused "
                                "record=%s key=%s code=%s at=%s", record.get("record"), record.get("key"),
                                refused.code, refused.at)
        return store

    def with_records(self, records: Iterable[Mapping[str, Any]]) -> "BootstrapBundle":
        """The bundle with ``records`` instead of its current ones (the authority's newest)."""

        return BootstrapBundle(self.installation, self.authority_keys, tuple(records))


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
        return BootstrapBundle(str(document["installation"]), keys, records)
    except (KeyError, TypeError) as error:
        raise BootstrapError(f"The bootstrap bundle {path} is damaged: {error}") from error


def write_bundle(path: Path, bundle: BootstrapBundle) -> None:
    """Replace the bundle atomically; readers see the old or the new file, never a part."""

    document = {
        "version": _BUNDLE_VERSION,
        "installation": bundle.installation,
        "authority_keys": [{"key_id": key_id, "public_key": public_key} for key_id, public_key in bundle.authority_keys],
        "records": list(bundle.records),
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
