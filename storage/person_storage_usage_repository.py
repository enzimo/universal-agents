"""Each person's stored-data totals, measured by the gateway and the manager, and the steps announced.

Chat transcripts and completed tasks are kept indefinitely, so each person
hears as their stored data grows (``assistant_gateway.storage_usage``). That
data is split between two processes that do not read each other's records:
the gateway keeps chat transcripts, requests, replies and uploads in its own
database, and the manager keeps conversation records, completed tasks and the
files saved beside conversations in its database, and walks people's folders.
So each side measures only its own data and writes one row per person here:

- ``side`` is ``gateway`` or ``manager``; ``bytes_json`` holds the bytes by
  kind (``SIDE_KINDS``) and ``total_bytes`` their sum; ``measured_at`` says
  when, so the gateway can refuse to add a total that is out of date;
- the gateway's row also keeps the highest step announced to the person and
  how many announcements there were (``announced_step_bytes``,
  ``announcements``), so a step is announced once per crossing, also across
  restarts.

A side replaces all its rows at each measurement (``record_side``): people
who are no longer active lose their rows, so the table holds at most two rows
per active person and does not grow with time.

The table is in the manager's database, beside the access store and the
departure records, which both processes already open. This module reads and
writes only this table and imports no other store.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.read_only import repositories_are_read_only
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

logger = logging.getLogger(__name__)

PERSON_STORAGE_USAGE_TABLE = "person_storage_usage"
PERSON_STORAGE_USAGE_SCHEMA_VERSION = 1


class StorageSide(StrEnum):
    """Which process measured a row."""

    GATEWAY = "gateway"
    MANAGER = "manager"


# What each side measures, in bytes, by kind.
CHATS = "chats"  # chat transcripts, requests and replies (gateway database)
UPLOADS = "uploads"  # files the person sent in chat that are still kept (state/uploads)
TASKS = "tasks"  # conversation turns and summaries, and completed tasks (manager database)
CONVERSATION_FILES = "conversation_files"  # long texts saved beside their conversations
FOLDER = "folder"  # the person's own folder (workspaces/users/<folder>)
SIDE_KINDS: dict[StorageSide, tuple[str, ...]] = {
    StorageSide.GATEWAY: (CHATS, UPLOADS),
    StorageSide.MANAGER: (TASKS, CONVERSATION_FILES, FOLDER),
}


@dataclass(frozen=True)
class SideUsage:
    """One side's measurement of one person's stored data."""

    person_id: str
    side: StorageSide
    bytes_by_kind: Mapping[str, int]
    measured_at: datetime

    @property
    def total(self) -> int:
        return sum(self.bytes_by_kind.values())


@dataclass(frozen=True)
class Announcement:
    """The highest step announced to a person (bytes; 0 for none) and how many announcements there were."""

    step_bytes: int = 0
    count: int = 0


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _checked(side: StorageSide, bytes_by_kind: Mapping[str, int]) -> dict[str, int]:
    unknown = set(bytes_by_kind) - set(SIDE_KINDS[side])
    if unknown:
        raise ValueError(f"The {side.value} side does not measure {sorted(unknown)}.")
    if any(int(size) < 0 for size in bytes_by_kind.values()):
        raise ValueError("A stored size cannot be negative.")
    return {kind: int(bytes_by_kind.get(kind, 0)) for kind in SIDE_KINDS[side]}


class PersonStorageUsageRepository:
    """Per-person stored-data totals by side, and the steps announced (module docstring)."""

    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            # BIGINT: sizes pass 2 GB, the limit of PostgreSQL's INTEGER.
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {PERSON_STORAGE_USAGE_TABLE} (
                schema_version INTEGER NOT NULL CHECK (schema_version = {PERSON_STORAGE_USAGE_SCHEMA_VERSION}),
                team_id TEXT NOT NULL, person_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('gateway', 'manager')),
                bytes_json TEXT NOT NULL, total_bytes BIGINT NOT NULL, measured_at TEXT NOT NULL,
                announced_step_bytes BIGINT NOT NULL DEFAULT 0, announcements INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (team_id, person_id, side)
            )""")
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{PERSON_STORAGE_USAGE_TABLE}_side "
                f"ON {PERSON_STORAGE_USAGE_TABLE}(team_id, side, measured_at)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def record_side(
        self, team_id: str, side: StorageSide, usage: Mapping[str, Mapping[str, int]], *, measured_at: datetime
    ) -> None:
        """Replace this side's rows with ``usage`` (person id -> bytes by kind), in one transaction.

        Rows of people not in ``usage`` are deleted; the gateway's row keeps
        its announcements while the person stays.
        """

        p = self.dialect.bind
        rows = [(person, _checked(side, sizes)) for person, sizes in usage.items()]
        with self._connect() as conn:
            conn.begin_write()
            for person, sizes in rows:
                conn.execute(
                    f"""INSERT INTO {PERSON_STORAGE_USAGE_TABLE}
                        (schema_version, team_id, person_id, side, bytes_json, total_bytes, measured_at)
                        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
                        ON CONFLICT (team_id, person_id, side) DO UPDATE SET
                        bytes_json = excluded.bytes_json, total_bytes = excluded.total_bytes,
                        measured_at = excluded.measured_at""",
                    (
                        PERSON_STORAGE_USAGE_SCHEMA_VERSION,
                        team_id,
                        person,
                        side.value,
                        json.dumps(sizes, sort_keys=True),
                        sum(sizes.values()),
                        _utc(measured_at),
                    ),
                )
            kept = [person for person, _ in rows]
            others = f" AND person_id NOT IN ({', '.join(p for _ in kept)})" if kept else ""
            conn.execute(
                f"DELETE FROM {PERSON_STORAGE_USAGE_TABLE} WHERE team_id={p} AND side={p}{others}",
                (team_id, side.value, *kept),
            )
            conn.commit()

    def side_usage(self, team_id: str, side: StorageSide) -> dict[str, SideUsage]:
        """This side's latest measurement of each person, by person id."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT person_id, bytes_json, measured_at FROM {PERSON_STORAGE_USAGE_TABLE} "
                f"WHERE team_id={p} AND side={p}",
                (team_id, side.value),
            ).fetchall()
        return {
            str(row[0]): SideUsage(
                person_id=str(row[0]),
                side=side,
                bytes_by_kind={str(kind): int(size) for kind, size in json.loads(row[1]).items()},
                measured_at=datetime.fromisoformat(str(row[2])),
            )
            for row in rows
        }

    def announcement(self, team_id: str, person_id: str) -> Announcement:
        """The highest step announced to the person and how many announcements there were; none without a row."""

        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT announced_step_bytes, announcements FROM {PERSON_STORAGE_USAGE_TABLE} "
                f"WHERE team_id={p} AND person_id={p} AND side={p}",
                (team_id, person_id, StorageSide.GATEWAY.value),
            ).fetchone()
        return Announcement(int(row[0]), int(row[1])) if row else Announcement()

    def record_announcement(self, team_id: str, person_id: str, announcement: Announcement) -> bool:
        """Keep the person's announced step on their gateway row; False when the gateway has no row for them."""

        p = self.dialect.bind
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {PERSON_STORAGE_USAGE_TABLE} SET announced_step_bytes={p}, announcements={p} "
                f"WHERE team_id={p} AND person_id={p} AND side={p}",
                (announcement.step_bytes, announcement.count, team_id, person_id, StorageSide.GATEWAY.value),
            )
            changed = int(getattr(cursor, "rowcount", 0) or 0)
            conn.commit()
        return changed > 0


__all__ = [
    "CHATS",
    "CONVERSATION_FILES",
    "FOLDER",
    "PERSON_STORAGE_USAGE_SCHEMA_VERSION",
    "PERSON_STORAGE_USAGE_TABLE",
    "SIDE_KINDS",
    "TASKS",
    "UPLOADS",
    "Announcement",
    "PersonStorageUsageRepository",
    "SideUsage",
    "StorageSide",
]
