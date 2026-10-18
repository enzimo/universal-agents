"""Offline retirement of operator-selected, unsupported runtime snapshots.

The archive retains opaque storage rows, not executable or migrated task state.
Archival and removal share a transaction so a failed repair retains custody.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from storage.backends import STORAGE_BACKEND_POSTGRES
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from storage.runtime_snapshot_layout import assemble_snapshot
from storage.sqlite_connection import connect_sqlite
from storage.task_custody_repository import CUSTODY_TABLE, release_owners

LOGGER = logging.getLogger(__name__)
ARCHIVE_TABLE = "retired_task_runtime"
OWNER_TABLE = "active_task_runtime"
RECORD_TABLE = "active_task_runtime_records"
OWNER_COLUMNS = (
    "owner_agent_id",
    "owner_agent_name",
    "team_id",
    "owner_named_agent_type",
    "runtime_state_json",
    "updated_at",
)


@contextmanager
def _connection(storage: StorageSettings) -> Iterator[Any]:
    if storage.backend == STORAGE_BACKEND_POSTGRES:
        with connect_postgres(storage.postgres_dsn or "") as conn:
            yield conn
    else:
        path = Path(storage.sqlite_path or "").expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"SQLite manager database not found: {path}")
        with connect_sqlite(str(path), row_factory=sqlite3.Row) as conn:
            yield conn


def _has_table(conn: Any, table: str, *, postgres: bool) -> bool:
    if postgres:
        row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
        return row[0] is not None
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _unsupported_rows(
    conn: Any, team_id: str, *, has_records: bool
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(OWNER_COLUMNS[:-1])}, CAST(updated_at AS TEXT) AS updated_at "
        f"FROM {OWNER_TABLE} WHERE team_id = {conn.dialect.bind} ORDER BY owner_agent_id",
        (team_id,),
    ).fetchall()
    unsupported = []
    for row in rows:
        try:
            records = (
                conn.execute(
                    f"SELECT section, record_id, payload_json FROM {RECORD_TABLE} "
                    f"WHERE owner_agent_id = {conn.dialect.bind}",
                    (row["owner_agent_id"],),
                ).fetchall()
                if has_records
                else []
            )
            # A supported header does not establish that its saved records can
            # recover. Use the same assembly and strict decoder as the runtime.
            payload = assemble_snapshot(
                row["runtime_state_json"],
                {
                    (item["section"], item["record_id"]): item["payload_json"]
                    for item in records
                },
            )
            decode_runtime_snapshot(payload)
        except (ValueError, KeyError, TypeError):
            # Validation exceptions can contain task content. Preview only the
            # owner metadata; preserve the exact opaque rows for explicit archival.
            unsupported.append(dict(row))
    return unsupported


def inspect_runtime_snapshots(
    storage: StorageSettings, *, team_id: str
) -> list[dict[str, Any]]:
    """Return only owner metadata; task payloads must not enter CLI output."""
    with _connection(storage) as conn:
        if not _has_table(
            conn, OWNER_TABLE, postgres=storage.backend == STORAGE_BACKEND_POSTGRES
        ):
            return []
        return [
            {key: value for key, value in row.items() if key != "runtime_state_json"}
            for row in _unsupported_rows(
                conn,
                team_id,
                has_records=_has_table(
                    conn,
                    RECORD_TABLE,
                    postgres=storage.backend == STORAGE_BACKEND_POSTGRES,
                ),
            )
        ]


def _archive_row(
    conn: Any, row: dict[str, Any], *, has_records: bool, reason: str
) -> str:
    bind = conn.dialect.bind
    records = []
    if has_records:
        records = [
            dict(record)
            for record in conn.execute(
                f"SELECT * FROM {RECORD_TABLE} WHERE owner_agent_id = {bind} ORDER BY section, record_id",
                (row["owner_agent_id"],),
            ).fetchall()
        ]
    archive_id = f"runtime_retirement_{uuid4().hex}"
    archived = {
        "archive_id": archive_id,
        **row,
        "records_json": json.dumps(records, ensure_ascii=False, allow_nan=False),
        "retired_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    conn.execute(
        f"INSERT INTO {ARCHIVE_TABLE} ({', '.join(archived)}) "
        f"VALUES ({', '.join(bind for _ in archived)})",
        tuple(archived.values()),
    )
    stored = conn.execute(
        f"SELECT * FROM {ARCHIVE_TABLE} WHERE archive_id = {bind}", (archive_id,)
    ).fetchone()
    if stored is None or dict(stored) != archived:
        raise RuntimeError(
            "Runtime snapshot archive verification failed; active snapshots were retained."
        )
    if has_records:
        conn.execute(
            f"DELETE FROM {RECORD_TABLE} WHERE owner_agent_id = {bind}",
            (row["owner_agent_id"],),
        )
    deleted = conn.execute(
        f"DELETE FROM {OWNER_TABLE} WHERE owner_agent_id = {bind} AND team_id = {bind}",
        (row["owner_agent_id"], row["team_id"]),
    )
    if deleted.rowcount != 1:
        raise RuntimeError(
            "Runtime snapshot retirement failed; transaction rolled back."
        )
    return archive_id


def retire_runtime_snapshots(
    storage: StorageSettings,
    *,
    team_id: str,
    owner_agent_ids: list[str],
    reason: str,
) -> list[str]:
    """Archive and retire exact unsupported owners while affected runtimes are stopped.

    This is operator maintenance, never a startup fallback or an agent tool.
    Fully recoverable owners and owners outside the selected team are rejected.
    """
    if (
        not team_id.strip()
        or not reason.strip()
        or not owner_agent_ids
        or any(not owner.strip() for owner in owner_agent_ids)
    ):
        raise ValueError(
            "Retirement requires a team, explicit owner ids, and a reason."
        )
    postgres = storage.backend == STORAGE_BACKEND_POSTGRES
    with _connection(storage) as conn:
        if not postgres:
            conn.execute("BEGIN IMMEDIATE")
        if not _has_table(conn, OWNER_TABLE, postgres=postgres):
            raise ValueError("No active runtime snapshot table exists.")
        has_records = _has_table(conn, RECORD_TABLE, postgres=postgres)
        if postgres:
            # Block concurrent inserts as well as writes to the existing owners.
            tables = f"{OWNER_TABLE}, {RECORD_TABLE}" if has_records else OWNER_TABLE
            conn.execute(f"LOCK TABLE {tables} IN EXCLUSIVE MODE")
        candidates = {
            row["owner_agent_id"]: row
            for row in _unsupported_rows(conn, team_id, has_records=has_records)
        }
        selected = sorted(set(owner_agent_ids))
        if any(owner not in candidates for owner in selected):
            raise ValueError(
                "Every selected owner must have an unsupported snapshot in the selected team. Run --dry-run again."
            )
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (
            archive_id TEXT PRIMARY KEY,
            owner_agent_id TEXT NOT NULL, owner_agent_name TEXT, team_id TEXT,
            owner_named_agent_type TEXT, runtime_state_json TEXT NOT NULL,
            updated_at TEXT, records_json TEXT NOT NULL, retired_at TEXT NOT NULL,
            reason TEXT NOT NULL
        )""")
        archive_ids = [
            _archive_row(
                conn, candidates[owner], has_records=has_records, reason=reason
            )
            for owner in selected
        ]
        if _has_table(conn, CUSTODY_TABLE, postgres=postgres):
            # Retired work is abandoned: its tasks no longer have a holder.
            release_owners(conn, conn.dialect, selected)
    LOGGER.warning(
        "event=runtime_snapshots_retired team_id=%s owner_count=%d archive_ids=%s",
        team_id,
        len(archive_ids),
        ",".join(archive_ids),
    )
    return archive_ids
