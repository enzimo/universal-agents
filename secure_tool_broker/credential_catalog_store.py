"""Owner-scoped SQLite persistence for broker credential catalog metadata."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_SCOPE_KINDS = {"user", "team", "system"}


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
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
    ) -> None:
        """Atomically insert or replace one credential without touching sibling keys."""
        now = datetime.now(timezone.utc).isoformat()
        rendered = json.dumps(dict(credential), sort_keys=True, separators=(",", ":"))
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


__all__ = [
    "CredentialCatalogStore",
    "CredentialOwnerScope",
    "SealboxTenantMapping",
    "resolve_catalog_db_path",
]
