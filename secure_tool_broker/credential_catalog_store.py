"""Owner-scoped SQLite persistence for broker credential catalog metadata."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from storage import sqlite_connection
from secure_tool_broker.credential_expiry import format_utc_timestamp, is_expired

logger = logging.getLogger(__name__)


class CredentialCatalogReadError(RuntimeError):
    """Raised when an existing credential catalog database cannot be read."""


_SCOPE_KINDS = {"user", "team", "system"}
# How long a catalog statement waits for another writer's lock.
_LOCK_WAIT_MS = 10_000
DROPPED_DELETE_QUEUED_BY_FORM = "form"
DROPPED_DELETE_QUEUED_BY_TRANSFER_IMPORT = "transfer_import"
DROPPED_DELETE_QUEUED_BY_AGENT_CAPTURE = "agent_capture"
VALID_DROPPED_DELETE_QUEUED_BY = frozenset(
    {
        DROPPED_DELETE_QUEUED_BY_FORM,
        DROPPED_DELETE_QUEUED_BY_TRANSFER_IMPORT,
        DROPPED_DELETE_QUEUED_BY_AGENT_CAPTURE,
    }
)


@dataclass(frozen=True)
class CredentialOwnerScope:
    """Who owns a catalog row: a person, the team, or the broker itself.

    There is no default owner. Every catalog read and write names one, from the
    signed tool context or an account link, so one person's credentials can
    never land in another's rows by omission.
    """

    scope_kind: str
    owner_id: str

    def __post_init__(self) -> None:
        kind = str(self.scope_kind or "").strip().lower()
        owner = str(self.owner_id or "").strip()
        if kind not in _SCOPE_KINDS:
            raise ValueError(f"Unsupported credential scope: {kind or '<empty>'}")
        if not owner or len(owner) > 192 or any(ord(char) < 32 or ord(char) == 127 for char in owner):
            raise ValueError("credential owner_id must be a stable non-empty identifier without control characters")
        object.__setattr__(self, "scope_kind", kind)
        object.__setattr__(self, "owner_id", owner)

    @classmethod
    def user(cls, canonical_user_id: str) -> "CredentialOwnerScope":
        return cls("user", canonical_user_id)

    @classmethod
    def team(cls, team_id: str) -> "CredentialOwnerScope":
        return cls("team", team_id)

    @classmethod
    def system(cls, owner_id: str = "broker") -> "CredentialOwnerScope":
        return cls("system", owner_id)


@dataclass(frozen=True)
class SealboxTenantMapping:
    scope_kind: str
    owner_id: str
    tenant_id: str
    material_id: str
    created_at: str


@dataclass(frozen=True)
class CatalogEntryRow:
    """One stored catalog row across any owner scope, with its persisted update timestamp."""

    scope: CredentialOwnerScope
    credential_key: str
    credential: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UnreadableCatalogEntryRow:
    """Metadata for a catalog row whose JSON or owner scope could not be decoded."""

    scope_kind: str
    owner_id: str
    credential_key: str
    error: str


@dataclass(frozen=True)
class PendingDroppedSealboxDelete:
    """Broker-local retry queue entry for a Sealbox record dropped by a catalog overwrite."""

    scope: CredentialOwnerScope
    item_key: str
    item_kind: str
    credential_key: str
    created_at: str
    queued_by: str = DROPPED_DELETE_QUEUED_BY_FORM


def resolve_catalog_db_path() -> Path:
    """The catalog database: the launcher passes security_layer.credential_catalog.database_path."""

    instance_root = Path(os.getenv("TEAM_INSTANCE_ROOT") or Path.cwd()).expanduser()
    explicit = str(os.getenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH") or "").strip()
    candidate = Path(explicit).expanduser() if explicit else Path("credential_catalog.db")
    if not candidate.is_absolute():
        candidate = instance_root / candidate
    return candidate.resolve()


class CredentialCatalogStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        # Opened here rather than with connect_sqlite, which opens files read-only
        # during a transfer's dry run: this store keeps its own 10 s lock wait and
        # its behavior there.
        conn = sqlite3.connect(self.path, timeout=_LOCK_WAIT_MS / 1000)
        conn.row_factory = sqlite3.Row
        # The setup every store gets (WAL, FULL commits, macOS's full flush), so
        # a saved credential is on the disk before the broker reports it stored
        # and this store cannot drift from the others (storage.sqlite_connection).
        sqlite_connection.configure_sqlite_connection(conn, db_path=str(self.path), busy_timeout_ms=_LOCK_WAIT_MS)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS credential_catalog_entries (
                    scope_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    credential_key TEXT NOT NULL,
                    credential_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope_kind, owner_id, credential_key)
                );
                CREATE INDEX IF NOT EXISTS idx_credential_catalog_owner
                    ON credential_catalog_entries(scope_kind, owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS sealbox_tenant_mappings (
                    scope_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL UNIQUE,
                    material_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope_kind, owner_id)
                );
                CREATE TABLE IF NOT EXISTS pending_dropped_sealbox_deletes (
                    scope_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    item_kind TEXT NOT NULL,
                    credential_key TEXT NOT NULL,
                    queued_by TEXT NOT NULL DEFAULT 'form',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope_kind, owner_id, item_key)
                );
                CREATE INDEX IF NOT EXISTS idx_pending_dropped_sealbox_owner_cred
                    ON pending_dropped_sealbox_deletes(scope_kind, owner_id, credential_key);
                """
            )

    def load_scope(self, scope: CredentialOwnerScope) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT credential_key, credential_json
                FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ?
                ORDER BY credential_key
                """,
                (scope.scope_kind, scope.owner_id),
            ).fetchall()
        return {
            str(row["credential_key"]): dict(json.loads(str(row["credential_json"])))
            for row in rows
        }

    def put_credential(
        self,
        scope: CredentialOwnerScope,
        credential_key: str,
        credential: Mapping[str, Any],
        *,
        dropped_sealbox_targets: Sequence[tuple[str, str]] = (),
        queued_by: str = DROPPED_DELETE_QUEUED_BY_FORM,
    ) -> None:
        """Atomically insert or replace one credential without touching sibling keys.

        When ``dropped_sealbox_targets`` is provided, those Sealbox record keys
        are queued in ``pending_dropped_sealbox_deletes`` inside the same
        transaction so a crash or Sealbox deletion failure never orphans an
        older secret version.
        """
        now = datetime.now(timezone.utc).isoformat()
        rendered = json.dumps(dict(credential), sort_keys=True, separators=(",", ":"))
        normalized_queued_by = (
            str(queued_by or DROPPED_DELETE_QUEUED_BY_FORM).strip() or DROPPED_DELETE_QUEUED_BY_FORM
        )
        if normalized_queued_by not in VALID_DROPPED_DELETE_QUEUED_BY:
            raise ValueError(f"Unsupported dropped-delete queued_by value: {normalized_queued_by!r}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO credential_catalog_entries (
                    scope_kind, owner_id, credential_key, credential_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_kind, owner_id, credential_key) DO UPDATE SET
                    credential_json = excluded.credential_json,
                    updated_at = excluded.updated_at
                """,
                (
                    scope.scope_kind,
                    scope.owner_id,
                    str(credential_key),
                    rendered,
                    now,
                    now,
                ),
            )
            for item_key, item_kind in dropped_sealbox_targets:
                normalized_key = str(item_key).strip()
                normalized_kind = str(item_kind or "secret").strip() or "secret"
                if not normalized_key:
                    continue
                conn.execute(
                    """
                    INSERT INTO pending_dropped_sealbox_deletes (
                        scope_kind, owner_id, item_key, item_kind, credential_key, queued_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_kind, owner_id, item_key) DO UPDATE SET
                        item_kind = CASE
                            WHEN pending_dropped_sealbox_deletes.item_kind = 'credential'
                                OR excluded.item_kind = 'credential'
                            THEN 'credential'
                            ELSE excluded.item_kind
                        END,
                        credential_key = excluded.credential_key,
                        queued_by = excluded.queued_by,
                        created_at = excluded.created_at
                    """,
                    (
                        scope.scope_kind,
                        scope.owner_id,
                        normalized_key,
                        normalized_kind,
                        str(credential_key),
                        normalized_queued_by,
                        now,
                    ),
                )

    def load_credential(
        self,
        scope: CredentialOwnerScope,
        credential_key: str,
    ) -> CatalogEntryRow | None:
        """Read one catalog entry by primary key, including its persisted updated_at timestamp."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scope_kind, owner_id, credential_key, credential_json, created_at, updated_at
                FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ? AND credential_key = ?
                """,
                (scope.scope_kind, scope.owner_id, str(credential_key)),
            ).fetchone()
        if row is None:
            return None
        return CatalogEntryRow(
            scope=CredentialOwnerScope(str(row["scope_kind"]), str(row["owner_id"])),
            credential_key=str(row["credential_key"]),
            credential=dict(json.loads(str(row["credential_json"]))),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def scan_all_credentials(self) -> tuple[list[CatalogEntryRow], list[UnreadableCatalogEntryRow]]:
        """Decode every catalog entry row-by-row so one corrupt row does not block other owners."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scope_kind, owner_id, credential_key, credential_json, created_at, updated_at
                FROM credential_catalog_entries
                ORDER BY scope_kind, owner_id, credential_key
                """
            ).fetchall()
        decoded: list[CatalogEntryRow] = []
        unreadable: list[UnreadableCatalogEntryRow] = []
        for row in rows:
            scope_kind = str(row["scope_kind"])
            owner_id = str(row["owner_id"])
            credential_key = str(row["credential_key"])
            try:
                scope = CredentialOwnerScope(scope_kind, owner_id)
                payload = json.loads(str(row["credential_json"]))
                if not isinstance(payload, dict):
                    raise TypeError("credential_json must decode to a JSON object")
                decoded.append(
                    CatalogEntryRow(
                        scope=scope,
                        credential_key=credential_key,
                        credential=dict(payload),
                        created_at=str(row["created_at"]),
                        updated_at=str(row["updated_at"]),
                    )
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                unreadable.append(
                    UnreadableCatalogEntryRow(
                        scope_kind=scope_kind,
                        owner_id=owner_id,
                        credential_key=credential_key,
                        error=type(exc).__name__,
                    )
                )
        return decoded, unreadable

    def delete_credential(self, scope: CredentialOwnerScope, credential_key: str) -> bool:
        """Atomically delete one credential without touching sibling keys."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                DELETE FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ? AND credential_key = ?
                """,
                (scope.scope_kind, scope.owner_id, str(credential_key)),
            )
        return bool(cursor.rowcount)

    def delete_credential_if_unchanged(
        self,
        scope: CredentialOwnerScope,
        credential_key: str,
        *,
        expected_updated_at: str,
        expected_expires_at: str | None,
    ) -> bool:
        """Compare-and-delete one catalog entry only if its updated_at and expires_at still match.

        Prevents the background janitor from removing a temporary credential
        that was renewed or replaced concurrently.
        """

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT credential_json, updated_at
                FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ? AND credential_key = ?
                """,
                (scope.scope_kind, scope.owner_id, str(credential_key)),
            ).fetchone()
            if row is None:
                return False
            if str(row["updated_at"]) != expected_updated_at:
                return False
            payload = dict(json.loads(str(row["credential_json"])))
            if payload.get("expires_at") != expected_expires_at:
                return False
            cursor = conn.execute(
                """
                DELETE FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ? AND credential_key = ? AND updated_at = ?
                """,
                (scope.scope_kind, scope.owner_id, str(credential_key), expected_updated_at),
            )
        return bool(cursor.rowcount)

    def replace_scope(
        self,
        scope: CredentialOwnerScope,
        credentials: Mapping[str, Mapping[str, Any]],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._replace_scope_rows(conn, scope, credentials, now=now)

    @staticmethod
    def _replace_scope_rows(
        conn: sqlite3.Connection,
        scope: CredentialOwnerScope,
        credentials: Mapping[str, Mapping[str, Any]],
        *,
        now: str,
    ) -> None:
        existing = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT credential_key FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ?
                """,
                (scope.scope_kind, scope.owner_id),
            ).fetchall()
        }
        incoming = {str(key) for key in credentials}
        for credential_key, credential in credentials.items():
            rendered = json.dumps(dict(credential), sort_keys=True, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO credential_catalog_entries (
                    scope_kind, owner_id, credential_key, credential_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_kind, owner_id, credential_key) DO UPDATE SET
                    credential_json = excluded.credential_json,
                    updated_at = excluded.updated_at
                """,
                (
                    scope.scope_kind,
                    scope.owner_id,
                    str(credential_key),
                    rendered,
                    now,
                    now,
                ),
            )
        for stale_key in existing - incoming:
            conn.execute(
                """
                DELETE FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ? AND credential_key = ?
                """,
                (scope.scope_kind, scope.owner_id, stale_key),
            )

    def get_sealbox_tenant_mapping(
        self,
        scope: CredentialOwnerScope,
    ) -> SealboxTenantMapping | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scope_kind, owner_id, tenant_id, material_id, created_at
                FROM sealbox_tenant_mappings
                WHERE scope_kind = ? AND owner_id = ?
                """,
                (scope.scope_kind, scope.owner_id),
            ).fetchone()
        if row is None:
            return None
        return SealboxTenantMapping(
            scope_kind=str(row["scope_kind"]),
            owner_id=str(row["owner_id"]),
            tenant_id=str(row["tenant_id"]),
            material_id=str(row["material_id"]),
            created_at=str(row["created_at"]),
        )

    def list_sealbox_tenant_mappings(self) -> list[SealboxTenantMapping]:
        """List every Sealbox tenant mapping recorded in the catalog store."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scope_kind, owner_id, tenant_id, material_id, created_at
                FROM sealbox_tenant_mappings
                ORDER BY scope_kind, owner_id
                """
            ).fetchall()
        return [
            SealboxTenantMapping(
                scope_kind=str(row["scope_kind"]),
                owner_id=str(row["owner_id"]),
                tenant_id=str(row["tenant_id"]),
                material_id=str(row["material_id"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def put_sealbox_tenant_mapping(
        self,
        scope: CredentialOwnerScope,
        *,
        tenant_id: str,
        material_id: str,
    ) -> SealboxTenantMapping:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sealbox_tenant_mappings (
                    scope_kind, owner_id, tenant_id, material_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_kind, owner_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    material_id = excluded.material_id
                """,
                (scope.scope_kind, scope.owner_id, tenant_id, material_id, now),
            )
        mapping = self.get_sealbox_tenant_mapping(scope)
        if mapping is None:
            raise RuntimeError("Sealbox tenant mapping was not persisted")
        return mapping

    def list_pending_dropped_sealbox_deletes(
        self,
        *,
        scope: CredentialOwnerScope | None = None,
        credential_key: str | None = None,
    ) -> list[PendingDroppedSealboxDelete]:
        """List queued Sealbox record deletions awaiting retry."""

        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope_kind = ? AND owner_id = ?")
            params.extend([scope.scope_kind, scope.owner_id])
        if credential_key is not None:
            clauses.append("credential_key = ?")
            params.append(str(credential_key))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT scope_kind, owner_id, item_key, item_kind, credential_key, queued_by, created_at
                FROM pending_dropped_sealbox_deletes
                {where}
                ORDER BY scope_kind, owner_id, item_key
                """,
                tuple(params),
            ).fetchall()
        return [
            PendingDroppedSealboxDelete(
                scope=CredentialOwnerScope(str(row["scope_kind"]), str(row["owner_id"])),
                item_key=str(row["item_key"]),
                item_kind=str(row["item_kind"]),
                credential_key=str(row["credential_key"]),
                created_at=str(row["created_at"]),
                queued_by=str(row["queued_by"] or DROPPED_DELETE_QUEUED_BY_FORM),
            )
            for row in rows
        ]

    def pending_dropped_sealbox_keys(self, scope: CredentialOwnerScope) -> set[str]:
        """Return Sealbox item keys currently queued for deletion in ``scope``."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT item_key
                FROM pending_dropped_sealbox_deletes
                WHERE scope_kind = ? AND owner_id = ?
                """,
                (scope.scope_kind, scope.owner_id),
            ).fetchall()
        return {str(row["item_key"]) for row in rows}

    def remove_pending_dropped_sealbox_deletes(
        self,
        scope: CredentialOwnerScope,
        item_keys: Iterable[str],
    ) -> int:
        """Delete completed or superseded entries from ``pending_dropped_sealbox_deletes``."""

        keys = [str(key).strip() for key in item_keys if str(key).strip()]
        if not keys:
            return 0
        removed = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item_key in keys:
                cursor = conn.execute(
                    """
                    DELETE FROM pending_dropped_sealbox_deletes
                    WHERE scope_kind = ? AND owner_id = ? AND item_key = ?
                    """,
                    (scope.scope_kind, scope.owner_id, item_key),
                )
                removed += int(cursor.rowcount or 0)
        return removed


def _open_readonly_connection(
    path: str | os.PathLike[str],
) -> tuple[sqlite3.Connection, Path] | None:
    """Open the catalog database read-only without creating files, folders, or tables.

    Returns ``None`` only when the catalog file does not exist yet.
    """

    resolved = Path(path).expanduser().resolve()
    try:
        file_stat = os.stat(resolved)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "Credential catalog could not be accessed read-only. "
            "event=credential_catalog_readonly_failed path=%s error=%s",
            resolved,
            exc,
        )
        raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        logger.warning(
            "Credential catalog path is not a regular file. "
            "event=credential_catalog_readonly_failed path=%s",
            resolved,
        )
        raise CredentialCatalogReadError("The credential catalog could not be read.")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=_LOCK_WAIT_MS / 1000)
    except sqlite3.Error as exc:
        logger.warning(
            "Credential catalog could not be opened read-only. "
            "event=credential_catalog_readonly_failed path=%s error=%s",
            resolved,
            exc,
        )
        raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout={_LOCK_WAIT_MS}")
    return conn, resolved


def load_scope_readonly(
    path: str | os.PathLike[str],
    scope: CredentialOwnerScope,
) -> dict[str, dict[str, Any]]:
    """Read one owner's catalog rows read-only without creating files, folders, or tables.

    Returns ``{}`` only when the catalog file does not exist or has not yet
    created the ``credential_catalog_entries`` table. Any permission error,
    corrupt/locked database, non-file path, or unreadable JSON logs a warning
    with the path and raises ``CredentialCatalogReadError``.
    """

    opened = _open_readonly_connection(path)
    if opened is None:
        return {}
    conn, resolved = opened
    try:
        try:
            rows = conn.execute(
                """
                SELECT credential_key, credential_json
                FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ?
                ORDER BY credential_key
                """,
                (scope.scope_kind, scope.owner_id),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return {}
            logger.warning(
                "Credential catalog query failed in read-only mode. "
                "event=credential_catalog_readonly_failed path=%s error=%s",
                resolved,
                exc,
            )
            raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
        return {
            str(row["credential_key"]): dict(json.loads(str(row["credential_json"])))
            for row in rows
        }
    except CredentialCatalogReadError:
        raise
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "Credential catalog could not be decoded in read-only mode. "
            "event=credential_catalog_readonly_failed path=%s error=%s",
            resolved,
            exc,
        )
        raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
    finally:
        conn.close()


def load_credential_readonly(
    path: str | os.PathLike[str],
    scope: CredentialOwnerScope,
    credential_key: str,
) -> dict[str, Any] | None:
    """Read one catalog entry by key read-only without loading the owner's other rows."""

    opened = _open_readonly_connection(path)
    if opened is None:
        return None
    conn, resolved = opened
    try:
        try:
            row = conn.execute(
                """
                SELECT credential_json
                FROM credential_catalog_entries
                WHERE scope_kind = ? AND owner_id = ? AND credential_key = ?
                """,
                (scope.scope_kind, scope.owner_id, str(credential_key)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            logger.warning(
                "Credential catalog query failed in read-only mode. "
                "event=credential_catalog_readonly_failed path=%s error=%s",
                resolved,
                exc,
            )
            raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
        if row is None:
            return None
        return dict(json.loads(str(row["credential_json"])))
    except CredentialCatalogReadError:
        raise
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "Credential catalog could not be decoded in read-only mode. "
            "event=credential_catalog_readonly_failed path=%s error=%s",
            resolved,
            exc,
        )
        raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
    finally:
        conn.close()


def _sealbox_record_key_from_raw_ref(source_ref: Mapping[str, Any]) -> str | None:
    source = str(source_ref.get("source") or "").strip().lower()
    if source not in {"sealbox", "sealbox-cli", "sealbox_cli"}:
        return None
    key = str(source_ref.get("item_id") or source_ref.get("item_name") or "").strip()
    if not key:
        return None
    field_ref = str(source_ref.get("field_ref") or "password").strip()
    if field_ref in {"", "password", "value"}:
        return key
    return f"{key}:{field_ref}"


def expired_sealbox_record_keys(
    scope: CredentialOwnerScope,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Map canonical Sealbox record keys owned by expired catalog entries in ``scope`` to ``expires_at``."""

    rows = load_scope_readonly(resolve_catalog_db_path(), scope)
    expired: dict[str, str] = {}
    for raw in rows.values():
        if not isinstance(raw, Mapping):
            continue
        raw_expires = raw.get("expires_at")
        if raw_expires is None or (isinstance(raw_expires, str) and not raw_expires.strip()):
            continue
        try:
            if not is_expired(raw_expires, now=now):
                continue
            expires_iso = format_utc_timestamp(raw_expires)
        except Exception:
            continue
        raw_fields = raw.get("fields")
        if not isinstance(raw_fields, Mapping):
            continue
        for raw_field in raw_fields.values():
            if not isinstance(raw_field, Mapping):
                continue
            resolution_order = raw_field.get("resolution_order")
            if not isinstance(resolution_order, list):
                continue
            for source_ref in resolution_order:
                if not isinstance(source_ref, Mapping):
                    continue
                record_key = _sealbox_record_key_from_raw_ref(source_ref)
                if record_key:
                    expired[record_key] = expires_iso
    return expired


def pending_dropped_sealbox_keys_readonly(
    scope: CredentialOwnerScope,
) -> set[str]:
    """Read queued dropped-Sealbox record keys for ``scope`` without creating files or tables."""

    opened = _open_readonly_connection(resolve_catalog_db_path())
    if opened is None:
        return set()
    conn, resolved = opened
    try:
        try:
            rows = conn.execute(
                """
                SELECT item_key
                FROM pending_dropped_sealbox_deletes
                WHERE scope_kind = ? AND owner_id = ?
                """,
                (scope.scope_kind, scope.owner_id),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return set()
            logger.warning(
                "Pending dropped Sealbox query failed in read-only mode. "
                "event=credential_catalog_readonly_failed path=%s error=%s",
                resolved,
                exc,
            )
            raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
        return {str(row["item_key"]) for row in rows}
    except CredentialCatalogReadError:
        raise
    except sqlite3.Error as exc:
        logger.warning(
            "Pending dropped Sealbox rows could not be read in read-only mode. "
            "event=credential_catalog_readonly_failed path=%s error=%s",
            resolved,
            exc,
        )
        raise CredentialCatalogReadError("The credential catalog could not be read.") from exc
    finally:
        conn.close()


__all__ = [
    "CatalogEntryRow",
    "CredentialCatalogReadError",
    "CredentialCatalogStore",
    "CredentialOwnerScope",
    "DROPPED_DELETE_QUEUED_BY_AGENT_CAPTURE",
    "DROPPED_DELETE_QUEUED_BY_FORM",
    "DROPPED_DELETE_QUEUED_BY_TRANSFER_IMPORT",
    "PendingDroppedSealboxDelete",
    "SealboxTenantMapping",
    "UnreadableCatalogEntryRow",
    "VALID_DROPPED_DELETE_QUEUED_BY",
    "expired_sealbox_record_keys",
    "load_credential_readonly",
    "load_scope_readonly",
    "pending_dropped_sealbox_keys_readonly",
    "resolve_catalog_db_path",
]
