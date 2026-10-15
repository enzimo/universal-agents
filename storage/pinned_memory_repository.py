"""Backend-neutral repository for team/user pinned memory facts."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from id_generation import new_id
from custom_messages.contracts.control_v1 import PINNED_MEMORY_SORT_NEWEST

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import (
    DatabaseRow,
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite, ensure_sqlite_column

COREMEM_BANK = "coremem"
USERMEM_BANK = "usermem"
PinnedMemoryDatabaseRow = sqlite3.Row | DatabaseRow


class PinnedMemoryConflictError(ValueError):
    """The active memory changed after a caller read it."""


class PinnedMemoryCapacityError(ValueError):
    """The bank already holds its maximum number of facts for this team or person.

    A ``ValueError`` like the repository's other input errors, so existing
    callers keep working; callers that must report "no room" separately from
    bad input (the memory import) catch this type.
    """


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any, *, default: Any) -> str:
    return json.dumps(value if value is not None else default, sort_keys=True)


@dataclass(frozen=True)
class PinnedMemoryFactRow:
    bank: str
    team_id: str
    canonical_user_id: str
    memory_id: int
    fact_uuid: str
    fact_text: str
    valid_from_utc: str
    invalid_after_utc: Optional[str]
    tags_json: str
    metadata_json: str
    source_conversation_id: Optional[str]
    source_task_id: Optional[str]
    source_turn_index: Optional[int]
    source_gateway_session_key: Optional[str]
    source_channel_name: Optional[str]
    source_chat_id: Optional[str]
    source_thread_id: Optional[str]
    created_by_kind: str
    created_by_id: Optional[str]
    created_by_display_name: Optional[str]
    created_at_utc: str
    updated_at_utc: str
    source_key: Optional[str] = None

    @property
    def tags(self) -> list[str]:
        return list(json.loads(self.tags_json or "[]"))

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass(frozen=True)
class PinnedMemoryEventRow:
    event_id: str
    event_type: str
    bank: str
    team_id: str
    canonical_user_id: str
    memory_id: int
    fact_uuid: Optional[str]
    before_json: str
    after_json: str
    source_conversation_id: Optional[str]
    source_task_id: Optional[str]
    source_gateway_session_key: Optional[str]
    source_channel_name: Optional[str]
    source_chat_id: Optional[str]
    source_thread_id: Optional[str]
    actor_kind: str
    actor_id: Optional[str]
    actor_display_name: Optional[str]
    created_at_utc: str


class PinnedMemoryRepository(Protocol):
    def list_coremem(self, *, team_id: str, now_utc: Optional[str] = None, sort: str = "memory_id") -> list[PinnedMemoryFactRow]: ...

    def list_usermem(self, *, canonical_user_id: str, now_utc: Optional[str] = None, sort: str = "memory_id") -> list[PinnedMemoryFactRow]: ...

    def list_unexpired_facts(self, *, now_utc: Optional[str] = None) -> list[PinnedMemoryFactRow]: ...

    def create_coremem(self, *, team_id: str, fact_text: str, capacity: int, **kwargs: Any) -> PinnedMemoryFactRow: ...

    def create_usermem(self, *, canonical_user_id: str, fact_text: str, capacity: int, **kwargs: Any) -> PinnedMemoryFactRow: ...

    def replace_coremem(self, *, team_id: str, memory_id: int, **kwargs: Any) -> Optional[PinnedMemoryFactRow]: ...

    def replace_usermem(self, *, canonical_user_id: str, memory_id: int, **kwargs: Any) -> Optional[PinnedMemoryFactRow]: ...

    def delete_coremem(self, *, team_id: str, memory_id: int, **kwargs: Any) -> bool: ...

    def delete_usermem(self, *, canonical_user_id: str, memory_id: int, **kwargs: Any) -> bool: ...

    def prune_expired(self, *, now_utc: Optional[str] = None) -> int: ...

    def get_event(self, event_id: str) -> Optional[PinnedMemoryEventRow]: ...

    def get_latest_event(
        self,
        *,
        bank: str,
        team_id: str = "",
        canonical_user_id: str = "",
        memory_id: int,
        event_type: Optional[str] = None,
    ) -> Optional[PinnedMemoryEventRow]: ...

    def undo_memory_event(
        self,
        event_id: str,
        *,
        source: Optional[dict[str, Any]] = None,
        actor: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]: ...


class _RelationalPinnedMemoryRepository:
    _dialect: SqlDialect


    def _init_schema(self) -> None:
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(self._db_path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pinned_memory_facts_active (
                    bank TEXT NOT NULL,
                    team_id TEXT NOT NULL DEFAULT '',
                    canonical_user_id TEXT NOT NULL DEFAULT '',
                    memory_id INTEGER NOT NULL,
                    fact_uuid TEXT NOT NULL UNIQUE,
                    fact_text TEXT NOT NULL,
                    valid_from_utc TEXT NOT NULL,
                    invalid_after_utc TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    source_conversation_id TEXT,
                    source_task_id TEXT,
                    source_turn_index INTEGER,
                    source_gateway_session_key TEXT,
                    source_channel_name TEXT,
                    source_chat_id TEXT,
                    source_thread_id TEXT,
                    created_by_kind TEXT NOT NULL,
                    created_by_id TEXT,
                    created_by_display_name TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    source_key TEXT,
                    PRIMARY KEY (bank, team_id, canonical_user_id, memory_id)
                )
                """
            )
            self._ensure_column(conn, "pinned_memory_facts_active", "source_key", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pinned_memory_active_scope
                ON pinned_memory_facts_active(bank, team_id, canonical_user_id, valid_from_utc, memory_id)
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pinned_memory_active_source_key
                ON pinned_memory_facts_active(bank, team_id, canonical_user_id, source_key)
                WHERE source_key IS NOT NULL AND source_key != ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pinned_memory_fact_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    bank TEXT NOT NULL,
                    team_id TEXT NOT NULL DEFAULT '',
                    canonical_user_id TEXT NOT NULL DEFAULT '',
                    memory_id INTEGER NOT NULL,
                    fact_uuid TEXT,
                    before_json TEXT NOT NULL DEFAULT '{}',
                    after_json TEXT NOT NULL DEFAULT '{}',
                    source_conversation_id TEXT,
                    source_task_id TEXT,
                    source_gateway_session_key TEXT,
                    source_channel_name TEXT,
                    source_chat_id TEXT,
                    source_thread_id TEXT,
                    actor_kind TEXT NOT NULL,
                    actor_id TEXT,
                    actor_display_name TEXT,
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pinned_memory_events_scope
                ON pinned_memory_fact_events(bank, team_id, canonical_user_id, created_at_utc DESC)
                """
            )
            conn.commit()


    def list_coremem(self, *, team_id: str, now_utc: Optional[str] = None, sort: str = "memory_id") -> list[PinnedMemoryFactRow]:
        return self._list(bank=COREMEM_BANK, team_id=team_id, canonical_user_id="", now_utc=now_utc, sort=sort)

    def list_usermem(self, *, canonical_user_id: str, now_utc: Optional[str] = None, sort: str = "memory_id") -> list[PinnedMemoryFactRow]:
        return self._list(bank=USERMEM_BANK, team_id="", canonical_user_id=canonical_user_id, now_utc=now_utc, sort=sort)

    def list_unexpired_facts(self, *, now_utc: Optional[str] = None) -> list[PinnedMemoryFactRow]:
        """Every team and personal fact that has not expired, without changing anything.

        Unlike the ``list_*`` methods this does not prune expired facts (a
        write) and it includes facts that only take effect later, because the
        memory export must read a running instance without changing it and
        must carry scheduled facts too. Expired rows that the pruner has not
        removed yet are left out, as the next prune would remove them.
        """

        now = now_utc or utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM pinned_memory_facts_active
                WHERE invalid_after_utc IS NULL OR invalid_after_utc > {self._dialect.bind}
                ORDER BY bank ASC, team_id ASC, canonical_user_id ASC, memory_id ASC
                """,
                (now,),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def create_coremem(self, *, team_id: str, fact_text: str, capacity: int, **kwargs: Any) -> PinnedMemoryFactRow:
        return self._create(bank=COREMEM_BANK, team_id=team_id, canonical_user_id="", fact_text=fact_text, capacity=capacity, **kwargs)

    def create_usermem(self, *, canonical_user_id: str, fact_text: str, capacity: int, **kwargs: Any) -> PinnedMemoryFactRow:
        return self._create(bank=USERMEM_BANK, team_id="", canonical_user_id=canonical_user_id, fact_text=fact_text, capacity=capacity, **kwargs)

    def replace_coremem(self, *, team_id: str, memory_id: int, **kwargs: Any) -> Optional[PinnedMemoryFactRow]:
        return self._replace(bank=COREMEM_BANK, team_id=team_id, canonical_user_id="", memory_id=memory_id, **kwargs)

    def replace_usermem(self, *, canonical_user_id: str, memory_id: int, **kwargs: Any) -> Optional[PinnedMemoryFactRow]:
        return self._replace(bank=USERMEM_BANK, team_id="", canonical_user_id=canonical_user_id, memory_id=memory_id, **kwargs)

    def delete_coremem(self, *, team_id: str, memory_id: int, **kwargs: Any) -> bool:
        return self._delete(bank=COREMEM_BANK, team_id=team_id, canonical_user_id="", memory_id=memory_id, **kwargs)

    def delete_usermem(self, *, canonical_user_id: str, memory_id: int, **kwargs: Any) -> bool:
        return self._delete(bank=USERMEM_BANK, team_id="", canonical_user_id=canonical_user_id, memory_id=memory_id, **kwargs)

    def prune_expired(self, *, now_utc: Optional[str] = None) -> int:
        now = now_utc or utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM pinned_memory_facts_active
                WHERE invalid_after_utc IS NOT NULL AND invalid_after_utc <= {self._dialect.bind}
                """,
                (now,),
            ).fetchall()
            for row in rows:
                snapshot = dict(row)
                self._insert_event(
                    conn,
                    event_type="expire",
                    row=snapshot,
                    before=snapshot,
                    after={},
                    source={},
                    actor={"kind": "system", "id": "pinned_memory_pruner", "display_name": "Pinned Memory Pruner"},
                    created_at=now,
                )
                conn.execute(
                    f"""
                    DELETE FROM pinned_memory_facts_active
                    WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
                    """,
                    (row["bank"], row["team_id"], row["canonical_user_id"], row["memory_id"]),
                )
            conn.commit()
        return len(rows)

    def get_event(self, event_id: str) -> Optional[PinnedMemoryEventRow]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM pinned_memory_fact_events
                WHERE event_id = {self._dialect.bind}
                """,
                (event_id,),
            ).fetchone()
        return PinnedMemoryEventRow(**dict(row)) if row is not None else None

    def get_latest_event(
        self,
        *,
        bank: str,
        team_id: str = "",
        canonical_user_id: str = "",
        memory_id: int,
        event_type: Optional[str] = None,
    ) -> Optional[PinnedMemoryEventRow]:
        query = f"""
            SELECT *
            FROM pinned_memory_fact_events
            WHERE bank = {self._dialect.bind}
              AND team_id = {self._dialect.bind}
              AND canonical_user_id = {self._dialect.bind}
              AND memory_id = {self._dialect.bind}
        """
        params: list[Any] = [bank, team_id, canonical_user_id, memory_id]
        if event_type:
            query += f" AND event_type = {self._dialect.bind}"
            params.append(event_type)
        query += " ORDER BY created_at_utc DESC, event_id DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return PinnedMemoryEventRow(**dict(row)) if row is not None else None

    def undo_memory_event(
        self,
        event_id: str,
        *,
        source: Optional[dict[str, Any]] = None,
        actor: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        event = self.get_event(event_id)
        if event is None:
            raise ValueError(f"Memory event {event_id!r} was not found.")
        if event.event_type == "undo":
            raise ValueError("Cannot undo an undo event.")
        before = dict(json.loads(event.before_json or "{}"))
        now = utc_now_iso()
        source_payload = source or {}
        actor_payload = actor or {}
        with self._connect() as conn:
            current = self._get_row(
                conn,
                bank=event.bank,
                team_id=event.team_id,
                canonical_user_id=event.canonical_user_id,
                memory_id=event.memory_id,
            )
            if event.event_type == "create":
                if current is None:
                    raise ValueError("Cannot undo create because the active fact is already absent.")
                if event.fact_uuid and current["fact_uuid"] != event.fact_uuid:
                    raise ValueError("Cannot undo create because this memory ID has been reused.")
                current_snapshot = dict(current)
                undo_event_id = self._insert_event(
                    conn,
                    event_type="undo",
                    row=current_snapshot,
                    before=current_snapshot,
                    after={},
                    source=source_payload,
                    actor=actor_payload,
                    created_at=now,
                )
                conn.execute(
                    f"""
                    DELETE FROM pinned_memory_facts_active
                    WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
                    """,
                    (event.bank, event.team_id, event.canonical_user_id, event.memory_id),
                )
                conn.commit()
                return {
                    "undone_event_id": event_id,
                    "event_id": undo_event_id,
                    "event_type": event.event_type,
                    "bank": event.bank,
                    "memory_id": event.memory_id,
                    "deleted": True,
                }
            if event.event_type == "replace":
                if current is None:
                    raise ValueError("Cannot undo replace because the active fact is missing.")
                if event.fact_uuid and current["fact_uuid"] != event.fact_uuid:
                    raise ValueError("Cannot undo replace because this memory ID has been reused.")
                restored = dict(before)
                restored["updated_at_utc"] = now
                self._update_active(conn, restored)
                after_row = self._get_row(
                    conn,
                    bank=event.bank,
                    team_id=event.team_id,
                    canonical_user_id=event.canonical_user_id,
                    memory_id=event.memory_id,
                )
                after_row = self._require_row_after_mutation(
                    after_row,
                    operation="undo_replace",
                    bank=event.bank,
                    team_id=event.team_id,
                    canonical_user_id=event.canonical_user_id,
                    memory_id=event.memory_id,
                )
                undo_event_id = self._insert_event(
                    conn,
                    event_type="undo",
                    row=dict(after_row),
                    before=dict(current),
                    after=dict(after_row),
                    source=source_payload,
                    actor=actor_payload,
                    created_at=now,
                )
                conn.commit()
                return {
                    "undone_event_id": event_id,
                    "event_id": undo_event_id,
                    "event_type": event.event_type,
                    "bank": event.bank,
                    "memory_id": event.memory_id,
                    "fact": dict(after_row),
                }
            if event.event_type in {"delete", "expire"}:
                if current is not None:
                    raise ValueError("Cannot undo removal because this memory ID is already in use.")
                if not before:
                    raise ValueError("Cannot undo removal because the event has no prior fact snapshot.")
                restored = dict(before)
                restored["updated_at_utc"] = now
                self._insert_active(conn, restored)
                after_row = self._get_row(
                    conn,
                    bank=event.bank,
                    team_id=event.team_id,
                    canonical_user_id=event.canonical_user_id,
                    memory_id=event.memory_id,
                )
                after_row = self._require_row_after_mutation(
                    after_row,
                    operation="undo_removal",
                    bank=event.bank,
                    team_id=event.team_id,
                    canonical_user_id=event.canonical_user_id,
                    memory_id=event.memory_id,
                )
                undo_event_id = self._insert_event(
                    conn,
                    event_type="undo",
                    row=dict(after_row),
                    before={},
                    after=dict(after_row),
                    source=source_payload,
                    actor=actor_payload,
                    created_at=now,
                )
                conn.commit()
                return {
                    "undone_event_id": event_id,
                    "event_id": undo_event_id,
                    "event_type": event.event_type,
                    "bank": event.bank,
                    "memory_id": event.memory_id,
                    "fact": dict(after_row),
                }
        raise ValueError(f"Unsupported memory event type for undo: {event.event_type}")

    def _list(
        self,
        *,
        bank: str,
        team_id: str,
        canonical_user_id: str,
        now_utc: Optional[str],
        sort: str,
    ) -> list[PinnedMemoryFactRow]:
        now = now_utc or utc_now_iso()
        self.prune_expired(now_utc=now)
        order_by = {
            "memory_id": "memory_id ASC",
            PINNED_MEMORY_SORT_NEWEST: "updated_at_utc DESC, memory_id DESC",
            "valid_from_utc": "valid_from_utc ASC, memory_id ASC",
            "invalid_after_utc": "invalid_after_utc IS NULL ASC, invalid_after_utc ASC, memory_id ASC",
        }.get(sort, "memory_id ASC")
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM pinned_memory_facts_active
                WHERE bank = {self._dialect.bind}
                  AND team_id = {self._dialect.bind}
                  AND canonical_user_id = {self._dialect.bind}
                  AND valid_from_utc <= {self._dialect.bind}
                  AND (invalid_after_utc IS NULL OR invalid_after_utc > {self._dialect.bind})
                ORDER BY {order_by}
                """,
                (bank, team_id, canonical_user_id, now, now),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def _create(
        self,
        *,
        bank: str,
        team_id: str,
        canonical_user_id: str,
        fact_text: str,
        capacity: int,
        valid_from_utc: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        source_key: Optional[str] = None,
        source: Optional[dict[str, Any]] = None,
        actor: Optional[dict[str, Any]] = None,
    ) -> PinnedMemoryFactRow:
        fact = str(fact_text or "").strip()
        if not fact:
            raise ValueError("fact_text must be non-empty.")
        normalized_source_key = str(source_key or "").strip() or None
        if normalized_source_key is not None and len(normalized_source_key) > 240:
            raise ValueError("source_key must contain at most 240 characters.")
        now = utc_now_iso()
        with self._connect() as conn:
            self._prune_scope(conn, bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, now_utc=now)
            if normalized_source_key is not None:
                existing = conn.execute(
                    f"""
                    SELECT *
                    FROM pinned_memory_facts_active
                    WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND source_key = {self._dialect.bind}
                    """,
                    (bank, team_id, canonical_user_id, normalized_source_key),
                ).fetchone()
                if existing is not None:
                    return self._row_to_fact(existing)
            rows = conn.execute(
                f"""
                SELECT memory_id
                FROM pinned_memory_facts_active
                WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind}
                ORDER BY memory_id ASC
                """,
                (bank, team_id, canonical_user_id),
            ).fetchall()
            used = {int(row["memory_id"]) for row in rows}
            if len(used) >= int(capacity):
                raise PinnedMemoryCapacityError(f"{bank} capacity is full for this scope.")
            memory_id = next(idx for idx in range(1, int(capacity) + 1) if idx not in used)
            fact_uuid = new_id("pinned_memory")
            source_payload = source or {}
            actor_payload = actor or {}
            row_payload = {
                "bank": bank,
                "team_id": team_id,
                "canonical_user_id": canonical_user_id,
                "memory_id": memory_id,
                "fact_uuid": fact_uuid,
                "fact_text": fact,
                "valid_from_utc": valid_from_utc or now,
                "invalid_after_utc": invalid_after_utc,
                "tags_json": _json_dumps(tags or [], default=[]),
                "metadata_json": _json_dumps(metadata or {}, default={}),
                "source_conversation_id": source_payload.get("conversation_id"),
                "source_task_id": source_payload.get("task_id"),
                "source_turn_index": source_payload.get("turn_index"),
                "source_gateway_session_key": source_payload.get("gateway_session_key"),
                "source_channel_name": source_payload.get("channel_name"),
                "source_chat_id": source_payload.get("chat_id"),
                "source_thread_id": source_payload.get("thread_id"),
                "created_by_kind": actor_payload.get("kind", "user"),
                "created_by_id": actor_payload.get("id"),
                "created_by_display_name": actor_payload.get("display_name"),
                "created_at_utc": now,
                "updated_at_utc": now,
                "source_key": normalized_source_key,
            }
            self._insert_active(conn, row_payload)
            self._insert_event(
                conn,
                event_type="create",
                row=row_payload,
                before={},
                after=row_payload,
                source=source_payload,
                actor=actor_payload,
                created_at=now,
            )
            conn.commit()
        return self._get(bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, memory_id=memory_id)  # type: ignore[return-value]

    def _replace(
        self,
        *,
        bank: str,
        team_id: str,
        canonical_user_id: str,
        memory_id: int,
        fact_text: Optional[str] = None,
        valid_from_utc: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        clear_invalid_after_utc: bool = False,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        source: Optional[dict[str, Any]] = None,
        actor: Optional[dict[str, Any]] = None,
        expected_updated_at_utc: Optional[str] = None,
    ) -> Optional[PinnedMemoryFactRow]:
        now = utc_now_iso()
        with self._connect() as conn:
            row = self._get_row(conn, bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, memory_id=memory_id)
            if row is None:
                return None
            if (
                expected_updated_at_utc is not None
                and str(row["updated_at_utc"]) != expected_updated_at_utc
            ):
                raise PinnedMemoryConflictError(
                    f"{bank} #{memory_id} changed after it was loaded."
                )
            before = dict(row)
            next_fact = str(fact_text if fact_text is not None else row["fact_text"]).strip()
            if not next_fact:
                raise ValueError("fact_text must be non-empty.")
            next_tags = _json_dumps(tags if tags is not None else json.loads(row["tags_json"] or "[]"), default=[])
            next_metadata = _json_dumps(metadata if metadata is not None else json.loads(row["metadata_json"] or "{}"), default={})
            next_invalid_after_utc = (
                None
                if clear_invalid_after_utc
                else invalid_after_utc if invalid_after_utc is not None else row["invalid_after_utc"]
            )
            update_params: list[Any] = [
                next_fact,
                valid_from_utc or row["valid_from_utc"],
                next_invalid_after_utc,
                next_tags,
                next_metadata,
                now,
                bank,
                team_id,
                canonical_user_id,
                memory_id,
            ]
            expected_clause = ""
            if expected_updated_at_utc is not None:
                expected_clause = f" AND updated_at_utc = {self._dialect.bind}"
                update_params.append(expected_updated_at_utc)
            cursor = conn.execute(
                f"""
                UPDATE pinned_memory_facts_active
                SET fact_text = {self._dialect.bind},
                    valid_from_utc = {self._dialect.bind},
                    invalid_after_utc = {self._dialect.bind},
                    tags_json = {self._dialect.bind},
                    metadata_json = {self._dialect.bind},
                    updated_at_utc = {self._dialect.bind}
                WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
                """ + expected_clause,
                update_params,
            )
            if expected_updated_at_utc is not None and cursor.rowcount != 1:
                raise PinnedMemoryConflictError(
                    f"{bank} #{memory_id} changed while the update was being saved."
                )
            after_row = self._get_row(conn, bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, memory_id=memory_id)
            after_row = self._require_row_after_mutation(
                after_row,
                operation="replace",
                bank=bank,
                team_id=team_id,
                canonical_user_id=canonical_user_id,
                memory_id=memory_id,
            )
            self._insert_event(
                conn,
                event_type="replace",
                row=dict(after_row),
                before=before,
                after=dict(after_row),
                source=source or {},
                actor=actor or {},
                created_at=now,
            )
            conn.commit()
        return self._get(bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, memory_id=memory_id)

    def _delete(
        self,
        *,
        bank: str,
        team_id: str,
        canonical_user_id: str,
        memory_id: int,
        source: Optional[dict[str, Any]] = None,
        actor: Optional[dict[str, Any]] = None,
        expected_updated_at_utc: Optional[str] = None,
    ) -> bool:
        now = utc_now_iso()
        with self._connect() as conn:
            row = self._get_row(conn, bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, memory_id=memory_id)
            if row is None:
                return False
            if (
                expected_updated_at_utc is not None
                and str(row["updated_at_utc"]) != expected_updated_at_utc
            ):
                raise PinnedMemoryConflictError(
                    f"{bank} #{memory_id} changed after it was loaded."
                )
            snapshot = dict(row)
            delete_params: list[Any] = [
                bank,
                team_id,
                canonical_user_id,
                memory_id,
            ]
            expected_clause = ""
            if expected_updated_at_utc is not None:
                expected_clause = f" AND updated_at_utc = {self._dialect.bind}"
                delete_params.append(expected_updated_at_utc)
            cursor = conn.execute(
                f"""
                DELETE FROM pinned_memory_facts_active
                WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
                """ + expected_clause,
                delete_params,
            )
            if expected_updated_at_utc is not None and cursor.rowcount != 1:
                raise PinnedMemoryConflictError(
                    f"{bank} #{memory_id} changed while the delete was being saved."
                )
            self._insert_event(
                conn,
                event_type="delete",
                row=snapshot,
                before=snapshot,
                after={},
                source=source or {},
                actor=actor or {},
                created_at=now,
            )
            conn.commit()
        return True

    def _prune_scope(self, conn: sqlite3.Connection, *, bank: str, team_id: str, canonical_user_id: str, now_utc: str) -> None:
        rows = conn.execute(
            f"""
            SELECT *
            FROM pinned_memory_facts_active
            WHERE bank = {self._dialect.bind}
              AND team_id = {self._dialect.bind}
              AND canonical_user_id = {self._dialect.bind}
              AND invalid_after_utc IS NOT NULL
              AND invalid_after_utc <= {self._dialect.bind}
            """,
            (bank, team_id, canonical_user_id, now_utc),
        ).fetchall()
        for row in rows:
            snapshot = dict(row)
            self._insert_event(
                conn,
                event_type="expire",
                row=snapshot,
                before=snapshot,
                after={},
                source={},
                actor={"kind": "system", "id": "pinned_memory_pruner"},
                created_at=now_utc,
            )
            conn.execute(
                f"""
                DELETE FROM pinned_memory_facts_active
                WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
                """,
                (bank, team_id, canonical_user_id, row["memory_id"]),
            )

    def _insert_active(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        keys = [
            "bank",
            "team_id",
            "canonical_user_id",
            "memory_id",
            "fact_uuid",
            "fact_text",
            "valid_from_utc",
            "invalid_after_utc",
            "tags_json",
            "metadata_json",
            "source_conversation_id",
            "source_task_id",
            "source_turn_index",
            "source_gateway_session_key",
            "source_channel_name",
            "source_chat_id",
            "source_thread_id",
            "created_by_kind",
            "created_by_id",
            "created_by_display_name",
            "created_at_utc",
            "updated_at_utc",
            "source_key",
        ]
        placeholders = ", ".join(f"{self._dialect.bind}" for _ in keys)
        conn.execute(
            f"INSERT INTO pinned_memory_facts_active ({', '.join(keys)}) VALUES ({placeholders})",
            tuple(row.get(key) for key in keys),
        )

    def _update_active(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            f"""
            UPDATE pinned_memory_facts_active
            SET fact_uuid = {self._dialect.bind},
                fact_text = {self._dialect.bind},
                valid_from_utc = {self._dialect.bind},
                invalid_after_utc = {self._dialect.bind},
                tags_json = {self._dialect.bind},
                metadata_json = {self._dialect.bind},
                source_conversation_id = {self._dialect.bind},
                source_task_id = {self._dialect.bind},
                source_turn_index = {self._dialect.bind},
                source_gateway_session_key = {self._dialect.bind},
                source_channel_name = {self._dialect.bind},
                source_chat_id = {self._dialect.bind},
                source_thread_id = {self._dialect.bind},
                created_by_kind = {self._dialect.bind},
                created_by_id = {self._dialect.bind},
                created_by_display_name = {self._dialect.bind},
                created_at_utc = {self._dialect.bind},
                updated_at_utc = {self._dialect.bind},
                source_key = {self._dialect.bind}
            WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
            """,
            (
                row.get("fact_uuid"),
                row.get("fact_text"),
                row.get("valid_from_utc"),
                row.get("invalid_after_utc"),
                row.get("tags_json"),
                row.get("metadata_json"),
                row.get("source_conversation_id"),
                row.get("source_task_id"),
                row.get("source_turn_index"),
                row.get("source_gateway_session_key"),
                row.get("source_channel_name"),
                row.get("source_chat_id"),
                row.get("source_thread_id"),
                row.get("created_by_kind"),
                row.get("created_by_id"),
                row.get("created_by_display_name"),
                row.get("created_at_utc"),
                row.get("updated_at_utc"),
                row.get("source_key"),
                row.get("bank"),
                row.get("team_id", ""),
                row.get("canonical_user_id", ""),
                row.get("memory_id"),
            ),
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        row: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        source: dict[str, Any],
        actor: dict[str, Any],
        created_at: str,
    ) -> str:
        event_id = new_id("pin_mem_evt")
        conn.execute(
            f"""
            INSERT INTO pinned_memory_fact_events (
                event_id, event_type, bank, team_id, canonical_user_id, memory_id, fact_uuid,
                before_json, after_json, source_conversation_id, source_task_id,
                source_gateway_session_key, source_channel_name, source_chat_id, source_thread_id,
                actor_kind, actor_id, actor_display_name, created_at_utc
            ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
            """,
            (
                event_id,
                event_type,
                row.get("bank", ""),
                row.get("team_id", ""),
                row.get("canonical_user_id", ""),
                row.get("memory_id", 0),
                row.get("fact_uuid"),
                _json_dumps(before, default={}),
                _json_dumps(after, default={}),
                source.get("conversation_id"),
                source.get("task_id"),
                source.get("gateway_session_key"),
                source.get("channel_name"),
                source.get("chat_id"),
                source.get("thread_id"),
                actor.get("kind", "user"),
                actor.get("id"),
                actor.get("display_name"),
                created_at,
            ),
        )
        return event_id

    def _get(self, *, bank: str, team_id: str, canonical_user_id: str, memory_id: int) -> Optional[PinnedMemoryFactRow]:
        with self._connect() as conn:
            row = self._get_row(conn, bank=bank, team_id=team_id, canonical_user_id=canonical_user_id, memory_id=memory_id)
        return self._row_to_fact(row) if row is not None else None

    def _get_row(self, conn: sqlite3.Connection, *, bank: str, team_id: str, canonical_user_id: str, memory_id: int) -> PinnedMemoryDatabaseRow | None:
        row: PinnedMemoryDatabaseRow | None = conn.execute(
            f"""
            SELECT *
            FROM pinned_memory_facts_active
            WHERE bank = {self._dialect.bind} AND team_id = {self._dialect.bind} AND canonical_user_id = {self._dialect.bind} AND memory_id = {self._dialect.bind}
            """,
            (bank, team_id, canonical_user_id, memory_id),
        ).fetchone()
        return row

    def _require_row_after_mutation(
        self,
        row: PinnedMemoryDatabaseRow | None,
        *,
        operation: str,
        bank: str,
        team_id: str,
        canonical_user_id: str,
        memory_id: int,
    ) -> PinnedMemoryDatabaseRow:
        if row is not None:
            return row
        self._logger.error(
            "Pinned-memory mutation completed without a readable active row. "
            "event=pinned_memory_post_mutation_row_missing operation=%s bank=%s "
            "team_id=%s canonical_user_id=%s memory_id=%s",
            operation,
            bank,
            team_id,
            canonical_user_id,
            memory_id,
        )
        raise RuntimeError(
            f"Pinned-memory {operation} did not produce an active row for "
            f"{bank} memory_id={memory_id}."
        )

    @staticmethod
    def _row_to_fact(row: PinnedMemoryDatabaseRow) -> PinnedMemoryFactRow:
        return PinnedMemoryFactRow(**dict(row))


class SqlitePinnedMemoryRepository(_RelationalPinnedMemoryRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger, row_factory=sqlite3.Row)


    def _ensure_column(self, conn: Any, table_name: str, column_name: str, column_type: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=self._logger)


class PostgresPinnedMemoryRepository(_RelationalPinnedMemoryRepository):
    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)


    def _ensure_column(self, conn: Any, table_name: str, column_name: str, column_type: str) -> None:
        conn.execute(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}')
        return


def create_pinned_memory_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> PinnedMemoryRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="PinnedMemoryRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqlitePinnedMemoryRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresPinnedMemoryRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(f"PinnedMemoryRepository has no implementation for backend {storage_settings.backend!r}.")
