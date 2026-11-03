"""Signed records in the key-value buckets (README "State records", registry ``state_records``).

A host writes its own records (its liveness, its agents' states; the team services
write placements, sessions, the team view, the block list and their lock), each a
signed state document. Readers verify every record they read: trust records
(certificates, revocations, sessions, agreements) go into the host's trust store,
state reports are checked with ``TrustStore.verify``. Where a record is stored adds no
trust, so nothing read from a bucket is used before that check.

Writers take each sequence above the one they last wrote; records whose readers keep
the highest sequence (the block list, revocations) also take at least the record's
issue time in milliseconds, so a writer restored from a backup never writes a lower
one (registry write rules).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Mapping, Optional

from nats.js.errors import KeyNotFoundError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import PROTOCOL, STATE_RECORDS
from custom_messages.wire.trust import VerifiedRecord, sign_state
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.trust_state import PersistentTrustStore


@dataclass(frozen=True)
class StoredRecord:
    """A record as read from its bucket: the key-value revision and the verified record."""

    revision: int
    record: VerifiedRecord


class StateRecords:
    """One host's reads and writes of signed records in the key-value buckets."""

    def __init__(self, js: Any, identity: HostIdentity, trust: PersistentTrustStore) -> None:
        self._js = js
        self._identity = identity
        self._trust = trust

    def signed(self, record: str, key: str, body: Mapping[str, Any], *, sequence: int, issued_at: datetime,
               expires_at: Optional[datetime] = None) -> dict[str, Any]:
        """A state document of ``record`` under ``key``, signed by this host's key."""

        spec = STATE_RECORDS[record]
        spec.body_model.validate_python(dict(body))
        certificate = self._identity.certificate
        document: dict[str, Any] = {
            "record": record,
            "protocol": PROTOCOL,
            "key": key,
            "writer": {"type": "host", "installation": certificate.installation, "runtime": certificate.runtime,
                       "membership_epoch": certificate.membership_epoch, "key_id": self._identity.keys.key_id},
            "sequence": sequence,
            "issued_at": format_timestamp(issued_at),
        }
        if expires_at is not None:
            document["expires_at"] = format_timestamp(expires_at)
        document["body"] = dict(body)
        return sign_state(document, self._identity.keys.application_key)

    async def put(self, record: str, key: str, document: Mapping[str, Any], *,
                  last_revision: Optional[int] = None) -> int:
        """Store a signed document; with ``last_revision``, only if the key is still at that revision (0: absent).

        Returns the new revision. A compare-and-set that lost raises the client's
        ``KeyWrongLastSequenceError``, so a placement writer never overwrites another.
        """

        bucket = await self._js.key_value(STATE_RECORDS[record].bucket)
        data = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if last_revision is None:
            return int(await bucket.put(key, data))
        if last_revision == 0:
            return int(await bucket.create(key, data))
        return int(await bucket.update(key, data, last=last_revision))

    async def read_report(self, record: str, key: str, *, now: datetime,
                          held: Optional[tuple[int, str]] = None) -> Optional[StoredRecord]:
        """Read and verify one state report (host, agent, placement, team view, block list); ``None`` when absent."""

        entry = await self._get(record, key)
        if entry is None:
            return None
        return StoredRecord(int(entry.revision), self._trust.store.verify(entry.value, record=record, now=now, held=held))

    async def take_trust_record(self, record: str, key: str, *, now: datetime) -> bool:
        """Read one trust record (certificate, revocations, session, agreement) into the trust store.

        Returns whether the store took it as new; a missing key takes nothing.
        """

        entry = await self._get(record, key)
        if entry is None:
            return False
        return self._trust.add(entry.value, now=now)

    async def _get(self, record: str, key: str) -> Any:
        bucket = await self._js.key_value(STATE_RECORDS[record].bucket)
        try:
            entry = await bucket.get(key)
        except KeyNotFoundError:
            return None
        return entry if entry.value else None

    async def watch(self, record: str, keys: str = ">") -> AsyncIterator[tuple[str, Optional[bytes]]]:
        """Each change under ``keys`` as (key, raw record), with ``None`` when the key was deleted or expired.

        The caller verifies each record it uses, as ``read_report`` does, keeping the
        sequence it holds per key.
        """

        bucket = await self._js.key_value(STATE_RECORDS[record].bucket)
        watcher = await bucket.watch(keys)
        try:
            async for entry in watcher:
                if entry is None:
                    continue  # the end of the initial values
                yield entry.key, (entry.value or None)
        finally:
            await watcher.stop()
