"""Persistent SQLite and PostgreSQL index of protocol 1 ``UA1_BLOBS`` owners and pending deletes.

The team services own deletion for their team's objects in ``OBJ_UA1_BLOBS``
(``contracts/messages/v1/README.md`` "Blobs"). Because NATS object headers record only
the first writer, two requests or agents in the same team can share the same
``{team}~{sha256}`` bytes. This repository keeps every active owner of each blob and
durably queues pending deletes inside the same database transaction as removing the last
owner, so:

- a blob is deleted only when no owner remains (either its last owner's retention window
  expires, or its last owning person departs);
- crash recovery across restarts retries every pending delete before ``svc.team`` starts
  serving new ``blobs.register`` calls (C01);
- recording ``target_nuid`` on a pending delete lets retries and concurrent registrations
  purge the exact departed/deleted chunk subject without touching newer live metadata
  uploaded under a fresh ``nuid``.

Both SQLite (``BEGIN IMMEDIATE``) and PostgreSQL (``BLOB_INDEX_WRITE_LOCK_KEY``
transaction advisory lock) serialize writes identically through ``conn.begin_write()``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from storage.config import StorageSettings
from storage.postgres_connection import (
    BLOB_INDEX_WRITE_LOCK_KEY,
    connect_postgres,
    ensure_storage_parent_dir,
)
from storage.schema_setup import begin_schema_setup
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

BlobOwnerKind = Literal["request", "agent"]
PendingDeleteReason = Literal["expired", "person_departed"]

_VALID_OWNER_KINDS = frozenset({"request", "agent"})
_VALID_DELETE_REASONS = frozenset({"expired", "person_departed"})


class BlobSizeMismatch(ValueError):
    """Raised when registering an owner with a byte size conflicting with indexed owners."""


@dataclass(frozen=True)
class BlobOwnerRecord:
    """One active owner row in ``protocol1_blob_owners``."""

    team_id: str
    sha256: str
    owner_kind: BlobOwnerKind
    owner_id: str
    person_ref: Optional[str]
    size_bytes: int
    registered_at_utc: datetime
    expires_at_utc: datetime


@dataclass(frozen=True)
class PendingBlobDelete:
    """One durable pending NATS delete in ``protocol1_blob_pending_deletes``."""

    team_id: str
    sha256: str
    target_nuid: Optional[str]
    requested_at_utc: datetime
    reason: str
    person_ref: Optional[str] = None


@dataclass(frozen=True)
class PersonOwnerRemoval:
    """Outcome of atomically removing one departed person's blob ownership rows."""

    owners_removed: int
    unowned_sha256s: tuple[str, ...]
    kept_sha256s: tuple[str, ...]


def _iso_utc(moment: datetime) -> str:
    """Fixed-width UTC ISO-8601 timestamp so SQL string comparisons match chronological order."""

    utc = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)


def _row_to_owner(row: Any) -> BlobOwnerRecord:
    return BlobOwnerRecord(
        team_id=str(row["team_id"]),
        sha256=str(row["sha256"]),
        owner_kind=row["owner_kind"],
        owner_id=str(row["owner_id"]),
        person_ref=None if row["person_ref"] is None else str(row["person_ref"]),
        size_bytes=int(row["size_bytes"]),
        registered_at_utc=_parse_utc(row["registered_at_utc"]),
        expires_at_utc=_parse_utc(row["expires_at_utc"]),
    )


def _row_to_pending(row: Any) -> PendingBlobDelete:
    return PendingBlobDelete(
        team_id=str(row["team_id"]),
        sha256=str(row["sha256"]),
        target_nuid=None if row["target_nuid"] is None else str(row["target_nuid"]),
        requested_at_utc=_parse_utc(row["requested_at_utc"]),
        reason=str(row["reason"]),
        person_ref=None if row["person_ref"] is None else str(row["person_ref"]),
    )


class BlobIndexRepository:
    """SQLite and PostgreSQL storage for ``protocol1_blob_owners`` and ``protocol1_blob_pending_deletes``."""

    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("The blob index requires configured storage.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            begin_schema_setup(conn, __name__)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS protocol1_blob_owners (
                team_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                owner_kind TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                person_ref TEXT,
                size_bytes INTEGER NOT NULL,
                registered_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                PRIMARY KEY (team_id, sha256, owner_kind, owner_id)
            )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_protocol1_blob_owners_expires
                ON protocol1_blob_owners(team_id, expires_at_utc)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_protocol1_blob_owners_person
                ON protocol1_blob_owners(team_id, person_ref)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS protocol1_blob_pending_deletes (
                team_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                target_nuid TEXT,
                requested_at_utc TEXT NOT NULL,
                reason TEXT NOT NULL,
                person_ref TEXT,
                PRIMARY KEY (team_id, sha256)
            )"""
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn, write_lock_key=BLOB_INDEX_WRITE_LOCK_KEY)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def close(self) -> None:
        """No persistent connection is held open; provided for OperationFamily.close symmetry."""

    def register_owner(
        self,
        *,
        team_id: str,
        sha256: str,
        owner_kind: BlobOwnerKind,
        owner_id: str,
        person_ref: Optional[str],
        size_bytes: int,
        registered_at_utc: datetime,
        expires_at_utc: datetime,
        clear_pending_delete: bool = True,
    ) -> BlobOwnerRecord:
        """Upsert one owner row (keeping the later expiry) and optionally clear pending delete atomically."""

        if owner_kind not in _VALID_OWNER_KINDS:
            raise ValueError(f"invalid blob owner_kind: {owner_kind!r}")
        if owner_kind == "agent" and person_ref is not None:
            raise ValueError("system agent blob owners cannot carry person_ref")
        if size_bytes < 0:
            raise ValueError(f"size_bytes must be non-negative, got {size_bytes}")

        p = self.dialect.bind
        reg_text = _iso_utc(registered_at_utc)
        exp_text = _iso_utc(expires_at_utc)

        with self._connect() as conn:
            conn.begin_write()
            existing_size = conn.execute(
                f"SELECT size_bytes FROM protocol1_blob_owners WHERE team_id = {p} AND sha256 = {p} LIMIT 1",
                (team_id, sha256),
            ).fetchone()
            if existing_size is not None and int(existing_size["size_bytes"]) != size_bytes:
                raise BlobSizeMismatch(
                    f"Blob {team_id}~{sha256} is indexed with size {existing_size['size_bytes']}, not {size_bytes}."
                )
            conn.execute(
                f"""INSERT INTO protocol1_blob_owners (
                    team_id, sha256, owner_kind, owner_id, person_ref, size_bytes, registered_at_utc, expires_at_utc
                ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT(team_id, sha256, owner_kind, owner_id) DO UPDATE SET
                    person_ref = COALESCE(protocol1_blob_owners.person_ref, excluded.person_ref),
                    expires_at_utc = CASE
                        WHEN excluded.expires_at_utc > protocol1_blob_owners.expires_at_utc
                        THEN excluded.expires_at_utc
                        ELSE protocol1_blob_owners.expires_at_utc
                    END""",
                (team_id, sha256, owner_kind, owner_id, person_ref, size_bytes, reg_text, exp_text),
            )
            if clear_pending_delete:
                conn.execute(
                    f"DELETE FROM protocol1_blob_pending_deletes WHERE team_id = {p} AND sha256 = {p}",
                    (team_id, sha256),
                )
            row = conn.execute(
                f"""SELECT team_id, sha256, owner_kind, owner_id, person_ref, size_bytes,
                           registered_at_utc, expires_at_utc
                    FROM protocol1_blob_owners
                    WHERE team_id = {p} AND sha256 = {p} AND owner_kind = {p} AND owner_id = {p}""",
                (team_id, sha256, owner_kind, owner_id),
            ).fetchone()
            conn.commit()
        assert row is not None
        return _row_to_owner(row)

    def has_owners(self, team_id: str, sha256: str) -> bool:
        """Whether at least one owner row remains for ``(team_id, sha256)``."""

        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM protocol1_blob_owners WHERE team_id = {p} AND sha256 = {p} LIMIT 1",
                (team_id, sha256),
            ).fetchone()
        return row is not None

    def list_owners(self, team_id: str, sha256: str) -> tuple[BlobOwnerRecord, ...]:
        """All active owner rows for ``(team_id, sha256)`` ordered deterministically."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT team_id, sha256, owner_kind, owner_id, person_ref, size_bytes,
                           registered_at_utc, expires_at_utc
                    FROM protocol1_blob_owners
                    WHERE team_id = {p} AND sha256 = {p}
                    ORDER BY owner_kind, owner_id""",
                (team_id, sha256),
            ).fetchall()
        return tuple(_row_to_owner(row) for row in rows)

    def list_indexed_person_refs(self, team_id: str) -> tuple[str, ...]:
        """All distinct ``person_ref`` values currently holding at least one blob owner row in ``team_id``."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT person_ref
                    FROM protocol1_blob_owners
                    WHERE team_id = {p} AND person_ref IS NOT NULL
                    ORDER BY person_ref""",
                (team_id,),
            ).fetchall()
        return tuple(str(row["person_ref"]) for row in rows)

    def get_pending_delete(self, team_id: str, sha256: str) -> Optional[PendingBlobDelete]:
        """Read the pending delete row for ``(team_id, sha256)``, or ``None``."""

        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                    FROM protocol1_blob_pending_deletes
                    WHERE team_id = {p} AND sha256 = {p}""",
                (team_id, sha256),
            ).fetchone()
        return None if row is None else _row_to_pending(row)

    def list_pending_deletes(
        self, team_id: str, *, person_ref: Optional[str] = None
    ) -> tuple[PendingBlobDelete, ...]:
        """List all pending delete rows for ``team_id`` (optionally filtered to one departed ``person_ref``)."""

        p = self.dialect.bind
        with self._connect() as conn:
            if person_ref is None:
                rows = conn.execute(
                    f"""SELECT team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                        FROM protocol1_blob_pending_deletes
                        WHERE team_id = {p}
                        ORDER BY requested_at_utc, sha256""",
                    (team_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                        FROM protocol1_blob_pending_deletes
                        WHERE team_id = {p} AND person_ref = {p}
                        ORDER BY requested_at_utc, sha256""",
                    (team_id, person_ref),
                ).fetchall()
        return tuple(_row_to_pending(row) for row in rows)

    def queue_pending_delete(
        self,
        team_id: str,
        sha256: str,
        *,
        requested_at_utc: datetime,
        reason: PendingDeleteReason,
        target_nuid: Optional[str] = None,
        person_ref: Optional[str] = None,
    ) -> PendingBlobDelete:
        """Record a pending delete durably, keeping any previously captured ``target_nuid``."""

        if reason not in _VALID_DELETE_REASONS:
            raise ValueError(f"invalid pending delete reason: {reason!r}")
        p = self.dialect.bind
        req_text = _iso_utc(requested_at_utc)
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"""INSERT INTO protocol1_blob_pending_deletes (
                    team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                ) VALUES ({p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT(team_id, sha256) DO UPDATE SET
                    target_nuid = COALESCE(protocol1_blob_pending_deletes.target_nuid, excluded.target_nuid),
                    reason = excluded.reason,
                    person_ref = COALESCE(excluded.person_ref, protocol1_blob_pending_deletes.person_ref)""",
                (team_id, sha256, target_nuid, req_text, reason, person_ref),
            )
            row = conn.execute(
                f"""SELECT team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                    FROM protocol1_blob_pending_deletes
                    WHERE team_id = {p} AND sha256 = {p}""",
                (team_id, sha256),
            ).fetchone()
            conn.commit()
        assert row is not None
        return _row_to_pending(row)

    def set_pending_delete_nuid(self, team_id: str, sha256: str, target_nuid: str) -> bool:
        """Persist the exact chunk ``nuid`` being deleted so retries never touch newer uploads."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            cursor = conn.execute(
                f"""UPDATE protocol1_blob_pending_deletes
                    SET target_nuid = {p}
                    WHERE team_id = {p} AND sha256 = {p}""",
                (target_nuid, team_id, sha256),
            )
            conn.commit()
            return int(cursor.rowcount) > 0

    def complete_pending_delete(
        self, team_id: str, sha256: str, *, expected_nuid: Optional[str] = None
    ) -> bool:
        """Remove a finished pending delete row (optionally matching ``expected_nuid`` or NULL)."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            if expected_nuid is None:
                cursor = conn.execute(
                    f"DELETE FROM protocol1_blob_pending_deletes WHERE team_id = {p} AND sha256 = {p}",
                    (team_id, sha256),
                )
            else:
                cursor = conn.execute(
                    f"""DELETE FROM protocol1_blob_pending_deletes
                        WHERE team_id = {p} AND sha256 = {p}
                          AND (target_nuid = {p} OR target_nuid IS NULL)""",
                    (team_id, sha256, expected_nuid),
                )
            conn.commit()
            return int(cursor.rowcount) > 0

    def remove_expired_owners(self, team_id: str, now_utc: datetime) -> tuple[int, tuple[str, ...]]:
        """Delete expired owner rows and atomically queue pending deletes for blobs left with 0 owners."""

        p = self.dialect.bind
        now_text = _iso_utc(now_utc)
        with self._connect() as conn:
            conn.begin_write()
            affected_rows = conn.execute(
                f"""SELECT DISTINCT sha256
                    FROM protocol1_blob_owners
                    WHERE team_id = {p} AND expires_at_utc <= {p}
                    ORDER BY sha256""",
                (team_id, now_text),
            ).fetchall()
            if not affected_rows:
                conn.commit()
                return 0, ()

            affected = [str(row["sha256"]) for row in affected_rows]
            cursor = conn.execute(
                f"DELETE FROM protocol1_blob_owners WHERE team_id = {p} AND expires_at_utc <= {p}",
                (team_id, now_text),
            )
            owners_removed = int(cursor.rowcount)

            unowned: list[str] = []
            for sha256 in affected:
                remaining = conn.execute(
                    f"SELECT 1 FROM protocol1_blob_owners WHERE team_id = {p} AND sha256 = {p} LIMIT 1",
                    (team_id, sha256),
                ).fetchone()
                if remaining is None:
                    unowned.append(sha256)
                    conn.execute(
                        f"""INSERT INTO protocol1_blob_pending_deletes (
                            team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                        ) VALUES ({p}, {p}, NULL, {p}, {p}, NULL)
                        ON CONFLICT(team_id, sha256) DO NOTHING""",
                        (team_id, sha256, now_text, "expired"),
                    )
            conn.commit()
        return owners_removed, tuple(unowned)

    def remove_person_owners(
        self, team_id: str, person_ref: str, now_utc: datetime
    ) -> PersonOwnerRemoval:
        """Delete a departed person's owner rows and atomically queue pending deletes for unowned blobs."""

        p = self.dialect.bind
        now_text = _iso_utc(now_utc)
        with self._connect() as conn:
            conn.begin_write()
            affected_rows = conn.execute(
                f"""SELECT DISTINCT sha256
                    FROM protocol1_blob_owners
                    WHERE team_id = {p} AND person_ref = {p}
                    ORDER BY sha256""",
                (team_id, person_ref),
            ).fetchall()
            if not affected_rows:
                conn.commit()
                return PersonOwnerRemoval(owners_removed=0, unowned_sha256s=(), kept_sha256s=())

            affected = [str(row["sha256"]) for row in affected_rows]
            cursor = conn.execute(
                f"DELETE FROM protocol1_blob_owners WHERE team_id = {p} AND person_ref = {p}",
                (team_id, person_ref),
            )
            owners_removed = int(cursor.rowcount)

            unowned: list[str] = []
            kept: list[str] = []
            for sha256 in affected:
                remaining = conn.execute(
                    f"SELECT 1 FROM protocol1_blob_owners WHERE team_id = {p} AND sha256 = {p} LIMIT 1",
                    (team_id, sha256),
                ).fetchone()
                if remaining is None:
                    unowned.append(sha256)
                    conn.execute(
                        f"""INSERT INTO protocol1_blob_pending_deletes (
                            team_id, sha256, target_nuid, requested_at_utc, reason, person_ref
                        ) VALUES ({p}, {p}, NULL, {p}, {p}, {p})
                        ON CONFLICT(team_id, sha256) DO UPDATE SET
                            reason = 'person_departed',
                            person_ref = excluded.person_ref""",
                        (team_id, sha256, now_text, "person_departed", person_ref),
                    )
                else:
                    kept.append(sha256)
            conn.commit()
        return PersonOwnerRemoval(
            owners_removed=owners_removed,
            unowned_sha256s=tuple(unowned),
            kept_sha256s=tuple(kept),
        )


__all__ = [
    "BlobIndexRepository",
    "BlobOwnerKind",
    "BlobOwnerRecord",
    "BlobSizeMismatch",
    "PendingBlobDelete",
    "PendingDeleteReason",
    "PersonOwnerRemoval",
]
