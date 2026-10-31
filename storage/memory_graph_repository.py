"""Backend-neutral repository boundary for scoped memory graph persistence."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import hashlib
import heapq
import json
import logging
import math
from dataclasses import dataclass
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence

from id_generation import new_id

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

try:  # macOS Python builds often omit loadable SQLite extension support.
    import pysqlite3 as sqlite3
except ImportError:  # pragma: no cover - depends on platform packaging
    import sqlite3

from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    redact_postgres_dsn,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite
from .sqlite_vec_support import load_sqlite_vec, serialize_float32


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Optional[Dict[str, Any] | List[Any]]) -> str:
    if value is None:
        value = {}
    return json.dumps(value, sort_keys=True)


def _normalize_name(value: str) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def _append_claim_effective_time_filter(
    where_parts: List[str],
    params: List[Any],
    active_at_utc: Optional[str],
    *, bind: str,
) -> None:
    if not active_at_utc:
        return
    where_parts.append(f"(valid_from_utc IS NULL OR valid_from_utc <= {bind})")
    where_parts.append(f"(invalid_after_utc IS NULL OR invalid_after_utc > {bind})")
    params.extend([active_at_utc, active_at_utc])


SQLITE_VEC_PROVIDER = "sqlite_vec"
MEMORY_GRAPH_VECTOR_TABLE = "memory_graph_vectors"
MEMORY_GRAPH_VECTOR_RECORDS_TABLE = "memory_graph_vector_records"
MEMORY_GRAPH_VECTOR_STATE_TABLE = "memory_graph_vector_index_state"
MEMORY_GRAPH_VECTOR_STATE_KEY = "default"


@dataclass(frozen=True)
class MemoryEpisodeRow:
    episode_id: str
    subject_id: str
    conversation_id: Optional[str]
    task_id: Optional[str]
    turn_index: Optional[int]
    source_kind: str
    request_text: str
    clarification_summary_text: str
    turn_summary_text: str
    final_answer_text: str
    execution_history_text: str
    event_time: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryEntityRow:
    entity_id: str
    subject_id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    aliases_json: str
    summary_text: str
    embedding: Optional[str]
    embedding_profile_id: Optional[str]
    metadata_json: str
    confidence: float
    source_episode_id: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str

    @property
    def aliases(self) -> List[str]:
        return list(json.loads(self.aliases_json or "[]"))

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass(frozen=True)
class MemoryClaimRow:
    claim_id: str
    subject_id: str
    subject_entity_id: Optional[str]
    predicate: str
    object_entity_id: Optional[str]
    object_literal: Optional[str]
    fact_text: str
    claim_type: str
    confidence: float
    status: str
    valid_from_utc: Optional[str]
    invalid_after_utc: Optional[str]
    is_inferred: bool
    inference_rule: Optional[str]
    embedding: Optional[str]
    embedding_profile_id: Optional[str]
    metadata_json: str
    created_at: str
    updated_at: str

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass(frozen=True)
class MemoryClaimLinkRow:
    claim_link_id: str
    subject_id: str
    from_claim_id: str
    to_claim_id: str
    link_type: str
    confidence: float
    rationale_json: str
    created_at: str

    @property
    def rationale(self) -> Dict[str, Any]:
        return dict(json.loads(self.rationale_json or "{}"))


@dataclass(frozen=True)
class MemoryClaimEvidenceRow:
    claim_evidence_id: str
    claim_id: str
    episode_id: str
    evidence_text: str
    evidence_kind: str
    span_json: str
    extraction_confidence: float
    created_at: str

    @property
    def span(self) -> Dict[str, Any]:
        return dict(json.loads(self.span_json or "{}"))


@dataclass(frozen=True)
class TaskTurnDeltaRow:
    delta_id: str
    task_id: str
    conversation_id: Optional[str]
    turn_id: Optional[str]
    turn_index: Optional[int]
    source_kind: str
    actor_id: Optional[str]
    actor_name: Optional[str]
    actor_type: Optional[str]
    turn_kind: str
    delta_json: str
    delta_summary: str
    extracted_at: str
    extractor_version: Optional[str]
    batch_id: Optional[str]

    @property
    def delta(self) -> Dict[str, Any]:
        return dict(json.loads(self.delta_json or "{}"))


@dataclass(frozen=True)
class TaskStateSnapshotRow:
    snapshot_id: str
    task_id: str
    conversation_id: Optional[str]
    through_turn_index: Optional[int]
    status: Optional[str]
    assignee_id: Optional[str]
    priority: Optional[str]
    due_at: Optional[str]
    active_blockers_json: str
    active_dependencies_json: str
    open_questions_json: str
    next_actions_json: str
    current_summary: str
    state_json: str
    created_at: str

    @property
    def active_blockers(self) -> List[Any]:
        return list(json.loads(self.active_blockers_json or "[]"))

    @property
    def active_dependencies(self) -> List[Any]:
        return list(json.loads(self.active_dependencies_json or "[]"))

    @property
    def open_questions(self) -> List[Any]:
        return list(json.loads(self.open_questions_json or "[]"))

    @property
    def next_actions(self) -> List[Any]:
        return list(json.loads(self.next_actions_json or "[]"))

    @property
    def state(self) -> Dict[str, Any]:
        return dict(json.loads(self.state_json or "{}"))


@dataclass(frozen=True)
class TaskHandoffEventRow:
    handoff_event_id: str
    task_id: str
    conversation_id: Optional[str]
    turn_id: Optional[str]
    assigner_id: Optional[str]
    assigner_name: Optional[str]
    assignee_id: Optional[str]
    assignee_name: Optional[str]
    handoff_kind: str
    rationale: Optional[str]
    handoff_notes: Optional[str]
    created_at: str


@dataclass(frozen=True)
class CaseMemoryRow:
    case_memory_id: str
    task_id: str
    conversation_id: Optional[str]
    project_key: Optional[str]
    case_type: str
    summary: str
    resolution: str
    outcome_quality: Optional[str]
    key_blockers_json: str
    dependencies_json: str
    tools_used_json: str
    lessons_learned_json: str
    embedding: Optional[str]
    embedding_profile_id: Optional[str]
    created_at: str

    @property
    def key_blockers(self) -> List[Any]:
        return list(json.loads(self.key_blockers_json or "[]"))

    @property
    def dependencies(self) -> List[Any]:
        return list(json.loads(self.dependencies_json or "[]"))

    @property
    def tools_used(self) -> List[Any]:
        return list(json.loads(self.tools_used_json or "[]"))

    @property
    def lessons_learned(self) -> List[Any]:
        return list(json.loads(self.lessons_learned_json or "[]"))


@dataclass(frozen=True)
class MemoryEmbeddingProfileRow:
    embedding_profile_id: str
    profile_hash: str
    provider: str
    model_id: Optional[str]
    api_base: Optional[str]
    dimensions: Optional[int]
    status: str
    created_at: str
    archived_at: Optional[str]
    metadata_json: str

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


class MemoryGraphRepository(Protocol):
    def get_active_embedding_profile(self) -> Optional[MemoryEmbeddingProfileRow]: ...

    def list_embedding_profiles(self, *, status: Optional[str] = None) -> List[MemoryEmbeddingProfileRow]: ...

    def activate_embedding_profile(
        self,
        *,
        provider: str,
        model_id: Optional[str],
        api_base: Optional[str],
        dimensions: Optional[int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEmbeddingProfileRow: ...

    def archive_active_embedding_profile(
        self,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryEmbeddingProfileRow]: ...

    def purge_embedding_profiles(self) -> None: ...

    def purge_graph_embeddings(self) -> Dict[str, int]: ...

    def count_graph_embedding_rows(self) -> Dict[str, int]: ...

    def upsert_task_turn_delta(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        turn_index: Optional[int],
        source_kind: str,
        actor_id: Optional[str],
        actor_name: Optional[str],
        actor_type: Optional[str],
        turn_kind: str,
        delta: Dict[str, Any],
        delta_summary: str,
        extractor_version: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> TaskTurnDeltaRow: ...

    def list_task_turn_deltas(self, *, task_id: str, limit: int = 50) -> List[TaskTurnDeltaRow]: ...

    def create_task_state_snapshot(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        through_turn_index: Optional[int],
        status: Optional[str],
        assignee_id: Optional[str],
        priority: Optional[str],
        due_at: Optional[str],
        active_blockers: Optional[List[Any]],
        active_dependencies: Optional[List[Any]],
        open_questions: Optional[List[Any]],
        next_actions: Optional[List[Any]],
        current_summary: str,
        state: Optional[Dict[str, Any]],
    ) -> TaskStateSnapshotRow: ...

    def get_latest_task_state_snapshot(self, *, task_id: str) -> Optional[TaskStateSnapshotRow]: ...

    def add_task_handoff_event(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        assigner_id: Optional[str],
        assigner_name: Optional[str],
        assignee_id: Optional[str],
        assignee_name: Optional[str],
        handoff_kind: str,
        rationale: Optional[str],
        handoff_notes: Optional[str],
    ) -> TaskHandoffEventRow: ...

    def list_task_handoff_events(self, *, task_id: str, limit: int = 20) -> List[TaskHandoffEventRow]: ...

    def upsert_case_memory(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        project_key: Optional[str],
        case_type: str,
        summary: str,
        resolution: str,
        outcome_quality: Optional[str],
        key_blockers: Optional[List[Any]],
        dependencies: Optional[List[Any]],
        tools_used: Optional[List[Any]],
        lessons_learned: Optional[List[Any]],
        embedding: Optional[str] = None,
    ) -> CaseMemoryRow: ...

    def list_case_memories(
        self,
        *,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[CaseMemoryRow]: ...

    def ensure_episode(
        self,
        *,
        subject_id: str,
        conversation_id: Optional[str],
        task_id: Optional[str],
        turn_index: Optional[int],
        source_kind: str,
        request_text: str = "",
        clarification_summary_text: str = "",
        turn_summary_text: str = "",
        final_answer_text: str = "",
        execution_history_text: str = "",
        event_time: Optional[str] = None,
    ) -> MemoryEpisodeRow: ...

    def get_episodes(self, episode_ids: Sequence[str]) -> List[MemoryEpisodeRow]: ...

    def ensure_entity(
        self,
        *,
        subject_id: str,
        entity_type: str,
        canonical_name: str,
        aliases: Optional[List[str]] = None,
        summary_text: str = "",
        embedding: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        confidence: float = 1.0,
        source_episode_id: Optional[str] = None,
        is_active: bool = True,
    ) -> MemoryEntityRow: ...

    def search_entities(self, *, subject_id: str, query_text: str, limit: int = 10) -> List[MemoryEntityRow]: ...

    def search_entities_by_embedding(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
    ) -> List[MemoryEntityRow]: ...

    def create_claim(
        self,
        *,
        subject_id: str,
        subject_entity_id: Optional[str],
        predicate: str,
        object_entity_id: Optional[str],
        object_literal: Optional[str],
        fact_text: str,
        claim_type: str,
        confidence: float,
        status: str = "active",
        valid_from_utc: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        is_inferred: bool = False,
        inference_rule: Optional[str] = None,
        embedding: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryClaimRow: ...

    def update_claim(
        self,
        *,
        claim_id: str,
        status: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryClaimRow]: ...

    def list_claims_for_subject(
        self,
        *,
        subject_id: str,
        limit: int = 100,
        status: Optional[str] = None,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]: ...

    def search_claims(
        self,
        *,
        subject_id: str,
        query_text: str,
        limit: int = 10,
        active_only: bool = True,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]: ...

    def search_claims_by_embedding(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
        active_only: bool = True,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]: ...

    def ensure_claim_link(
        self,
        *,
        subject_id: str,
        from_claim_id: str,
        to_claim_id: str,
        link_type: str,
        confidence: float,
        rationale: Optional[Dict[str, Any]] = None,
    ) -> MemoryClaimLinkRow: ...

    def list_claim_links(
        self,
        *,
        subject_id: str,
        claim_ids: Optional[Sequence[str]] = None,
        limit: int = 50,
    ) -> List[MemoryClaimLinkRow]: ...

    def add_claim_evidence(
        self,
        *,
        claim_id: str,
        episode_id: str,
        evidence_text: str,
        evidence_kind: str,
        span: Optional[Dict[str, Any]] = None,
        extraction_confidence: float = 1.0,
    ) -> MemoryClaimEvidenceRow: ...

    def list_evidence_for_claims(self, claim_ids: Sequence[str]) -> List[MemoryClaimEvidenceRow]: ...


class _RelationalMemoryGraphRepository:
    """Shared scoped graph records; adapters own vector retrieval and schema operations."""

    _dialect: SqlDialect


    def _migrate_claim_validity_columns(self, conn: sqlite3.Connection) -> None:
        self._rename_or_copy_column(conn, "memory_claims", "valid_from", "valid_from_utc", "TEXT")
        self._rename_or_copy_column(conn, "memory_claims", "invalid_after", "invalid_after_utc", "TEXT")

    def _rename_or_copy_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        old_column: str,
        new_column: str,
        definition: str,
    ) -> None:
        columns = self._table_columns(conn, table)
        if old_column not in columns:
            return
        if new_column in columns:
            conn.execute(
                f"""
                UPDATE {table}
                SET {new_column} = COALESCE({new_column}, {old_column})
                WHERE {new_column} IS NULL AND {old_column} IS NOT NULL
                """
            )
            return
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_column} TO {new_column}")

    def _init_sqlite_vector_support(self, conn: sqlite3.Connection) -> None:
        if not self._sqlite_vector_enabled:
            return
        if self._sqlite_vector_provider != SQLITE_VEC_PROVIDER:
            raise ValueError(
                f"Unsupported SQLite vector provider {self._sqlite_vector_provider!r}. "
                f"Expected {SQLITE_VEC_PROVIDER!r}."
            )
        should_log_load = self._sqlite_vec_version is None
        loaded_version = load_sqlite_vec(
            conn,
            logger=self._logger if should_log_load else None,
            storage_target=self._db_path,
            require_extension=self._sqlite_vector_require_extension,
        )
        self._sqlite_vec_version = loaded_version or self._sqlite_vec_version
        if self._sqlite_vec_version is None:
            return
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MEMORY_GRAPH_VECTOR_RECORDS_TABLE} (
                vec_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                record_kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                embedding_profile_id TEXT,
                embedding_dimension INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(record_kind, record_id)
            ) STRICT
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MEMORY_GRAPH_VECTOR_STATE_TABLE} (
                singleton_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                dimensions INTEGER,
                sqlite_vec_version TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_memory_graph_vector_records_kind_subject "
            f"ON {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}(record_kind, subject_id, embedding_profile_id)"
        )

    def _ensure_sqlite_vector_table(self, conn: sqlite3.Connection, dimension: int) -> bool:
        if not self._sqlite_vector_enabled:
            return False
        if dimension <= 0:
            return False
        self._init_sqlite_vector_support(conn)
        if self._sqlite_vec_version is None:
            return False
        now = utc_now_iso()
        row = conn.execute(
            f"""
            SELECT dimensions
            FROM {MEMORY_GRAPH_VECTOR_STATE_TABLE}
            WHERE singleton_key = {self._dialect.bind}
            """,
            (MEMORY_GRAPH_VECTOR_STATE_KEY,),
        ).fetchone()
        if row is None:
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {MEMORY_GRAPH_VECTOR_TABLE}
                USING vec0(
                    record_kind TEXT,
                    record_id TEXT,
                    subject_id TEXT,
                    embedding_profile_id TEXT,
                    embedding float[{dimension}]
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {MEMORY_GRAPH_VECTOR_STATE_TABLE} (
                    singleton_key, provider, dimensions, sqlite_vec_version, created_at, updated_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    MEMORY_GRAPH_VECTOR_STATE_KEY,
                    SQLITE_VEC_PROVIDER,
                    dimension,
                    self._sqlite_vec_version,
                    now,
                    now,
                ),
            )
            return True

        existing_dimension = int(row["dimensions"] if isinstance(row, sqlite3.Row) else row[0])
        if existing_dimension != dimension:
            raise RuntimeError(
                "SQLite memory-graph vector dimension mismatch: "
                f"existing sqlite-vec table uses dimension {existing_dimension}, "
                f"but the current embedding has dimension {dimension}. "
                "Fresh installs should keep one embedding dimension; recreate the SQLite database "
                "or purge graph embedding state before changing embedding dimensions."
            )
        conn.execute(
            f"""
            UPDATE {MEMORY_GRAPH_VECTOR_STATE_TABLE}
            SET sqlite_vec_version = {self._dialect.bind}, updated_at = {self._dialect.bind}
            WHERE singleton_key = {self._dialect.bind}
            """,
            (self._sqlite_vec_version, now, MEMORY_GRAPH_VECTOR_STATE_KEY),
        )
        return True

    def _upsert_memory_graph_vector(
        self,
        conn: sqlite3.Connection,
        *,
        record_kind: str,
        record_id: str,
        subject_id: str,
        embedding_profile_id: Optional[str],
        embedding: Optional[str],
        updated_at: str,
    ) -> None:
        if not self._sqlite_vector_enabled or not embedding:
            return
        values = _parse_embedding_values(embedding)
        if not values:
            return
        dimension = len(values)
        active_dimension = self._resolve_active_embedding_dimension(conn)
        if active_dimension is not None and active_dimension != dimension:
            raise RuntimeError(
                "Memory graph embedding dimension mismatch: "
                f"active profile expects dimension {active_dimension}, "
                f"but {record_kind} {record_id!r} has dimension {dimension}."
            )
        if not self._ensure_sqlite_vector_table(conn, dimension):
            return
        vector_embedding_profile_id = embedding_profile_id or ""
        row = conn.execute(
            f"""
            SELECT vec_rowid
            FROM {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}
            WHERE record_kind = {self._dialect.bind} AND record_id = {self._dialect.bind}
            """,
            (record_kind, record_id),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                f"""
                INSERT INTO {MEMORY_GRAPH_VECTOR_RECORDS_TABLE} (
                    record_kind, record_id, subject_id, embedding_profile_id, embedding_dimension, updated_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (record_kind, record_id, subject_id, embedding_profile_id, dimension, updated_at),
            )
            vec_rowid = int(cursor.lastrowid)
        else:
            vec_rowid = int(row["vec_rowid"] if isinstance(row, sqlite3.Row) else row[0])
            conn.execute(
                f"""
                UPDATE {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}
                SET subject_id = {self._dialect.bind},
                    embedding_profile_id = {self._dialect.bind},
                    embedding_dimension = {self._dialect.bind},
                    updated_at = {self._dialect.bind}
                WHERE vec_rowid = {self._dialect.bind}
                """,
                (subject_id, embedding_profile_id, dimension, updated_at, vec_rowid),
            )
        conn.execute(f"DELETE FROM {MEMORY_GRAPH_VECTOR_TABLE} WHERE rowid = {self._dialect.bind}", (vec_rowid,))
        conn.execute(
            f"""
            INSERT INTO {MEMORY_GRAPH_VECTOR_TABLE} (
                rowid, record_kind, record_id, subject_id, embedding_profile_id, embedding
            ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
            """,
            (
                vec_rowid,
                record_kind,
                record_id,
                subject_id,
                vector_embedding_profile_id,
                serialize_float32(values),
            ),
        )

    def _delete_memory_graph_vector(
        self,
        conn: sqlite3.Connection,
        *,
        record_kind: Optional[str] = None,
        record_ids: Optional[Sequence[str]] = None,
    ) -> None:
        if not self._sqlite_vector_enabled:
            return
        if record_ids is not None and not record_ids:
            return
        self._init_sqlite_vector_support(conn)
        if self._sqlite_vec_version is None:
            return
        state = conn.execute(
            f"""
            SELECT 1
            FROM {MEMORY_GRAPH_VECTOR_STATE_TABLE}
            WHERE singleton_key = {self._dialect.bind}
            """,
            (MEMORY_GRAPH_VECTOR_STATE_KEY,),
        ).fetchone()
        if state is None:
            return
        where: List[str] = []
        params: List[Any] = []
        if record_kind:
            where.append(f"record_kind = {self._dialect.bind}")
            params.append(record_kind)
        if record_ids:
            where.append(f"record_id IN ({','.join(f'{self._dialect.bind}' for _ in record_ids)})")
            params.extend(list(record_ids))
        query = f"SELECT vec_rowid FROM {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}"
        if where:
            query += " WHERE " + " AND ".join(where)
        rows = conn.execute(query, params).fetchall()
        vec_rowids = [int(row["vec_rowid"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]
        for vec_rowid in vec_rowids:
            conn.execute(f"DELETE FROM {MEMORY_GRAPH_VECTOR_TABLE} WHERE rowid = {self._dialect.bind}", (vec_rowid,))
        delete_query = f"DELETE FROM {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}"
        if where:
            delete_query += " WHERE " + " AND ".join(where)
        conn.execute(delete_query, params)

    def _init_schema(self) -> None:
        backend_name = self._dialect.name
        storage_target = redact_postgres_dsn(self._dsn) if backend_name == "Postgres" else self._db_path
        ensure_storage_parent_dir(self._db_path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_turn_deltas (
                    delta_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT,
                    turn_id TEXT,
                    turn_index INTEGER,
                    source_kind TEXT NOT NULL,
                    actor_id TEXT,
                    actor_name TEXT,
                    actor_type TEXT,
                    turn_kind TEXT NOT NULL,
                    delta_json TEXT NOT NULL DEFAULT '{}',
                    delta_summary TEXT NOT NULL DEFAULT '',
                    extracted_at TEXT NOT NULL,
                    extractor_version TEXT,
                    batch_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_state_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT,
                    through_turn_index INTEGER,
                    status TEXT,
                    assignee_id TEXT,
                    priority TEXT,
                    due_at TEXT,
                    active_blockers_json TEXT NOT NULL DEFAULT '[]',
                    active_dependencies_json TEXT NOT NULL DEFAULT '[]',
                    open_questions_json TEXT NOT NULL DEFAULT '[]',
                    next_actions_json TEXT NOT NULL DEFAULT '[]',
                    current_summary TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_handoff_events (
                    handoff_event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT,
                    turn_id TEXT,
                    assigner_id TEXT,
                    assigner_name TEXT,
                    assignee_id TEXT,
                    assignee_name TEXT,
                    handoff_kind TEXT NOT NULL,
                    rationale TEXT,
                    handoff_notes TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS case_memories (
                    case_memory_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    conversation_id TEXT,
                    project_key TEXT,
                    case_type TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    resolution TEXT NOT NULL DEFAULT '',
                    outcome_quality TEXT,
                    key_blockers_json TEXT NOT NULL DEFAULT '[]',
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    tools_used_json TEXT NOT NULL DEFAULT '[]',
                    lessons_learned_json TEXT NOT NULL DEFAULT '[]',
                    embedding TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embedding_profiles (
                    embedding_profile_id TEXT PRIMARY KEY,
                    profile_hash TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    model_id TEXT,
                    api_base TEXT,
                    dimensions INTEGER,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    archived_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_embedding_profile_state (
                    singleton_key TEXT PRIMARY KEY,
                    active_embedding_profile_id TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_episodes (
                    episode_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    conversation_id TEXT,
                    task_id TEXT,
                    turn_index INTEGER,
                    source_kind TEXT NOT NULL,
                    request_text TEXT NOT NULL DEFAULT '',
                    clarification_summary_text TEXT NOT NULL DEFAULT '',
                    turn_summary_text TEXT NOT NULL DEFAULT '',
                    final_answer_text TEXT NOT NULL DEFAULT '',
                    execution_history_text TEXT NOT NULL DEFAULT '',
                    event_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(subject_id, source_kind, conversation_id, task_id, turn_index)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entities (
                    entity_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    summary_text TEXT NOT NULL DEFAULT '',
                    embedding TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_episode_id TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(subject_id, normalized_name, entity_type)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_claims (
                    claim_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    subject_entity_id TEXT,
                    predicate TEXT NOT NULL,
                    object_entity_id TEXT,
                    object_literal TEXT,
                    fact_text TEXT NOT NULL,
                    claim_type TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    status TEXT NOT NULL DEFAULT 'active',
                    valid_from_utc TEXT,
                    invalid_after_utc TEXT,
                    is_inferred INTEGER NOT NULL DEFAULT 0,
                    inference_rule TEXT,
                    embedding TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_claim_links (
                    claim_link_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    from_claim_id TEXT NOT NULL,
                    to_claim_id TEXT NOT NULL,
                    link_type TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    rationale_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(subject_id, from_claim_id, to_claim_id, link_type)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_claim_evidence (
                    claim_evidence_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    evidence_text TEXT NOT NULL DEFAULT '',
                    evidence_kind TEXT NOT NULL,
                    span_json TEXT NOT NULL DEFAULT '{}',
                    extraction_confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id, episode_id, evidence_kind, evidence_text)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_episodes_subject_event ON memory_episodes(subject_id, event_time DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_entities_subject_name ON memory_entities(subject_id, normalized_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_claims_subject_status ON memory_claims(subject_id, status, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_claims_subject_predicate ON memory_claims(subject_id, predicate, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_claim_links_subject_from ON memory_claim_links(subject_id, from_claim_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_claim_links_subject_to ON memory_claim_links(subject_id, to_claim_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_claim_evidence_claim ON memory_claim_evidence(claim_id, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_embedding_profiles_status_created ON memory_embedding_profiles(status, created_at DESC)"
            )
            self._ensure_column(conn, "case_memories", "embedding_profile_id", "TEXT")
            self._ensure_column(conn, "memory_entities", "embedding_profile_id", "TEXT")
            self._migrate_claim_validity_columns(conn)
            self._ensure_column(conn, "memory_claims", "embedding_profile_id", "TEXT")
            if backend_name == "SQLite":
                self._init_sqlite_vector_support(conn)
            conn.commit()
        self._logger.info("%s memory-graph repository initialized at %s", backend_name, storage_target)

    def get_active_embedding_profile(self) -> Optional[MemoryEmbeddingProfileRow]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.embedding_profile_id, p.profile_hash, p.provider, p.model_id, p.api_base,
                       p.dimensions, p.status, p.created_at, p.archived_at, p.metadata_json
                FROM memory_embedding_profile_state s
                JOIN memory_embedding_profiles p
                  ON p.embedding_profile_id = s.active_embedding_profile_id
                WHERE s.singleton_key = 'default'
                """,
            ).fetchone()
        return self._row_to_embedding_profile(row) if row is not None else None

    def list_embedding_profiles(self, *, status: Optional[str] = None) -> List[MemoryEmbeddingProfileRow]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    f"""
                    SELECT embedding_profile_id, profile_hash, provider, model_id, api_base,
                           dimensions, status, created_at, archived_at, metadata_json
                    FROM memory_embedding_profiles
                    WHERE status = {self._dialect.bind}
                    ORDER BY created_at DESC
                    """,
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT embedding_profile_id, profile_hash, provider, model_id, api_base,
                           dimensions, status, created_at, archived_at, metadata_json
                    FROM memory_embedding_profiles
                    ORDER BY created_at DESC
                    """
                ).fetchall()
        return [self._row_to_embedding_profile(row) for row in rows]

    def activate_embedding_profile(
        self,
        *,
        provider: str,
        model_id: Optional[str],
        api_base: Optional[str],
        dimensions: Optional[int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEmbeddingProfileRow:
        now = utc_now_iso()
        normalized_provider = str(provider or "").strip().lower()
        normalized_model_id = str(model_id or "").strip() or None
        normalized_api_base = str(api_base or "").strip().rstrip("/") or None
        normalized_dimensions = int(dimensions) if dimensions is not None else None
        profile_hash = build_embedding_profile_hash(
            provider=normalized_provider,
            model_id=normalized_model_id,
            api_base=normalized_api_base,
            dimensions=normalized_dimensions,
        )
        with self._connect() as conn:
            existing = conn.execute(
                f"""
                SELECT embedding_profile_id, profile_hash, provider, model_id, api_base,
                       dimensions, status, created_at, archived_at, metadata_json
                FROM memory_embedding_profiles
                WHERE profile_hash = {self._dialect.bind}
                """,
                (profile_hash,),
            ).fetchone()
            if existing is None:
                embedding_profile_id = new_id("embed_prof")
                conn.execute(
                    f"""
                    INSERT INTO memory_embedding_profiles (
                        embedding_profile_id, profile_hash, provider, model_id, api_base,
                        dimensions, status, created_at, archived_at, metadata_json
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        embedding_profile_id,
                        profile_hash,
                        normalized_provider,
                        normalized_model_id,
                        normalized_api_base,
                        normalized_dimensions,
                        "active",
                        now,
                        None,
                        _json_dumps(metadata),
                    ),
                )
            else:
                embedding_profile_id = str(existing["embedding_profile_id"])
                merged_metadata = dict(json.loads(existing["metadata_json"] or "{}"))
                merged_metadata.update(metadata or {})
                conn.execute(
                    f"""
                    UPDATE memory_embedding_profiles
                    SET provider = {self._dialect.bind},
                        model_id = {self._dialect.bind},
                        api_base = {self._dialect.bind},
                        dimensions = {self._dialect.bind},
                        status = 'active',
                        archived_at = NULL,
                        metadata_json = {self._dialect.bind}
                    WHERE embedding_profile_id = {self._dialect.bind}
                    """,
                    (
                        normalized_provider,
                        normalized_model_id,
                        normalized_api_base,
                        normalized_dimensions,
                        _json_dumps(merged_metadata),
                        embedding_profile_id,
                    ),
                )
            conn.execute(
                f"UPDATE memory_embedding_profiles SET status = 'archived', archived_at = COALESCE(archived_at, {self._dialect.bind}) WHERE embedding_profile_id != {self._dialect.bind} AND status = 'active'",
                (now, embedding_profile_id),
            )
            conn.execute(
                f"""
                INSERT INTO memory_embedding_profile_state (singleton_key, active_embedding_profile_id, updated_at)
                VALUES ('default', {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(singleton_key) DO UPDATE SET
                    active_embedding_profile_id = excluded.active_embedding_profile_id,
                    updated_at = excluded.updated_at
                """,
                (embedding_profile_id, now),
            )
            conn.execute(
                f"UPDATE case_memories SET embedding_profile_id = {self._dialect.bind} WHERE embedding IS NOT NULL AND embedding_profile_id IS NULL",
                (embedding_profile_id,),
            )
            conn.execute(
                f"UPDATE memory_entities SET embedding_profile_id = {self._dialect.bind} WHERE embedding IS NOT NULL AND embedding_profile_id IS NULL",
                (embedding_profile_id,),
            )
            conn.execute(
                f"UPDATE memory_claims SET embedding_profile_id = {self._dialect.bind} WHERE embedding IS NOT NULL AND embedding_profile_id IS NULL",
                (embedding_profile_id,),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT embedding_profile_id, profile_hash, provider, model_id, api_base,
                       dimensions, status, created_at, archived_at, metadata_json
                FROM memory_embedding_profiles
                WHERE embedding_profile_id = {self._dialect.bind}
                """,
                (embedding_profile_id,),
            ).fetchone()
        return self._row_to_embedding_profile(row)

    def archive_active_embedding_profile(
        self,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryEmbeddingProfileRow]:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.embedding_profile_id, p.profile_hash, p.provider, p.model_id, p.api_base,
                       p.dimensions, p.status, p.created_at, p.archived_at, p.metadata_json
                FROM memory_embedding_profile_state s
                JOIN memory_embedding_profiles p
                  ON p.embedding_profile_id = s.active_embedding_profile_id
                WHERE s.singleton_key = 'default'
                """
            ).fetchone()
            if row is None:
                return None
            embedding_profile_id = str(row["embedding_profile_id"])
            merged_metadata = dict(json.loads(row["metadata_json"] or "{}"))
            merged_metadata.update(metadata or {})
            conn.execute(
                f"""
                UPDATE memory_embedding_profiles
                SET status = 'archived',
                    archived_at = {self._dialect.bind},
                    metadata_json = {self._dialect.bind}
                WHERE embedding_profile_id = {self._dialect.bind}
                """,
                (now, _json_dumps(merged_metadata), embedding_profile_id),
            )
            conn.execute(
                f"""
                INSERT INTO memory_embedding_profile_state (singleton_key, active_embedding_profile_id, updated_at)
                VALUES ('default', NULL, {self._dialect.bind})
                ON CONFLICT(singleton_key) DO UPDATE SET
                    active_embedding_profile_id = NULL,
                    updated_at = excluded.updated_at
                """,
                (now,),
            )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT embedding_profile_id, profile_hash, provider, model_id, api_base,
                       dimensions, status, created_at, archived_at, metadata_json
                FROM memory_embedding_profiles
                WHERE embedding_profile_id = {self._dialect.bind}
                """,
                (embedding_profile_id,),
            ).fetchone()
        return self._row_to_embedding_profile(current)

    def purge_embedding_profiles(self) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_embedding_profile_state WHERE singleton_key = 'default'")
            conn.execute("DELETE FROM memory_embedding_profiles")
            conn.commit()
        self._logger.info("Purged memory-graph embedding profiles at %s.", now)

    def forget_case_memory_vectors(self, case_memory_ids: Sequence[str]) -> None:
        """Delete the vectors of case memories a person's history clean-up deleted.

        The case memories' rows go in the clean-up's own transaction
        (``storage.history_cleanup_repository``); their vectors live in the
        sqlite-vec table, which only this repository's connection can open.
        """

        if not case_memory_ids:
            return
        with self._connect() as conn:
            self._delete_memory_graph_vector(conn, record_kind="case_memory", record_ids=list(case_memory_ids))
            conn.commit()

    def purge_graph_embeddings(self) -> Dict[str, int]:
        counts = self.count_graph_embedding_rows()
        with self._connect() as conn:
            self._delete_memory_graph_vector(conn)
            conn.execute("DELETE FROM memory_claim_links")
            conn.execute("DELETE FROM memory_claim_evidence")
            conn.execute("DELETE FROM memory_claims")
            conn.execute("DELETE FROM memory_entities")
            conn.execute("DELETE FROM case_memories")
            conn.commit()
        return counts

    def count_graph_embedding_rows(self) -> Dict[str, int]:
        with self._connect() as conn:
            return {
                "memory_entities": int(conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0]),
                "memory_claims": int(conn.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0]),
                "memory_claim_links": int(conn.execute("SELECT COUNT(*) FROM memory_claim_links").fetchone()[0]),
                "memory_claim_evidence": int(conn.execute("SELECT COUNT(*) FROM memory_claim_evidence").fetchone()[0]),
                "case_memories": int(conn.execute("SELECT COUNT(*) FROM case_memories").fetchone()[0]),
            }

    def _resolve_active_embedding_profile_id(self, conn: sqlite3.Connection) -> Optional[str]:
        row = conn.execute(
            """
            SELECT active_embedding_profile_id
            FROM memory_embedding_profile_state
            WHERE singleton_key = 'default'
            """
        ).fetchone()
        if row is None:
            return None
        value = row[0] if not isinstance(row, sqlite3.Row) else row["active_embedding_profile_id"]
        return str(value) if value else None

    def _resolve_active_embedding_dimension(self, conn: sqlite3.Connection) -> Optional[int]:
        row = conn.execute(
            """
            SELECT p.dimensions
            FROM memory_embedding_profile_state s
            JOIN memory_embedding_profiles p
              ON p.embedding_profile_id = s.active_embedding_profile_id
            WHERE s.singleton_key = 'default'
            """
        ).fetchone()
        if row is None:
            return None
        value = row[0] if not isinstance(row, sqlite3.Row) else row["dimensions"]
        return int(value) if value is not None else None


    def upsert_task_turn_delta(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        turn_index: Optional[int],
        source_kind: str,
        actor_id: Optional[str],
        actor_name: Optional[str],
        actor_type: Optional[str],
        turn_kind: str,
        delta: Dict[str, Any],
        delta_summary: str,
        extractor_version: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> TaskTurnDeltaRow:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT delta_id, task_id, conversation_id, turn_id, turn_index, source_kind,
                       actor_id, actor_name, actor_type, turn_kind, delta_json, delta_summary,
                       extracted_at, extractor_version, batch_id
                FROM task_turn_deltas
                WHERE task_id = {self._dialect.bind}
                  AND COALESCE(turn_index, -1) = COALESCE({self._dialect.bind}, -1)
                  AND turn_kind = {self._dialect.bind}
                ORDER BY extracted_at DESC
                LIMIT 1
                """,
                (task_id, turn_index, turn_kind),
            ).fetchone()
            if row is None:
                delta_id = new_id("task_delta")
                conn.execute(
                    f"""
                    INSERT INTO task_turn_deltas (
                        delta_id, task_id, conversation_id, turn_id, turn_index, source_kind,
                        actor_id, actor_name, actor_type, turn_kind, delta_json, delta_summary,
                        extracted_at, extractor_version, batch_id
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        delta_id,
                        task_id,
                        conversation_id,
                        turn_id,
                        turn_index,
                        source_kind,
                        actor_id,
                        actor_name,
                        actor_type,
                        turn_kind,
                        _json_dumps(delta),
                        delta_summary,
                        now,
                        extractor_version,
                        batch_id,
                    ),
                )
            else:
                delta_id = str(row["delta_id"])
                conn.execute(
                    f"""
                    UPDATE task_turn_deltas
                    SET conversation_id = {self._dialect.bind},
                        turn_id = {self._dialect.bind},
                        source_kind = {self._dialect.bind},
                        actor_id = {self._dialect.bind},
                        actor_name = {self._dialect.bind},
                        actor_type = {self._dialect.bind},
                        turn_kind = {self._dialect.bind},
                        delta_json = {self._dialect.bind},
                        delta_summary = {self._dialect.bind},
                        extracted_at = {self._dialect.bind},
                        extractor_version = {self._dialect.bind},
                        batch_id = {self._dialect.bind}
                    WHERE delta_id = {self._dialect.bind}
                    """,
                    (
                        conversation_id,
                        turn_id,
                        source_kind,
                        actor_id,
                        actor_name,
                        actor_type,
                        turn_kind,
                        _json_dumps(delta),
                        delta_summary,
                        now,
                        extractor_version,
                        batch_id,
                        delta_id,
                    ),
                )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT delta_id, task_id, conversation_id, turn_id, turn_index, source_kind,
                       actor_id, actor_name, actor_type, turn_kind, delta_json, delta_summary,
                       extracted_at, extractor_version, batch_id
                FROM task_turn_deltas
                WHERE delta_id = {self._dialect.bind}
                """,
                (delta_id,),
            ).fetchone()
        return self._row_to_task_turn_delta(current)

    def list_task_turn_deltas(self, *, task_id: str, limit: int = 50) -> List[TaskTurnDeltaRow]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT delta_id, task_id, conversation_id, turn_id, turn_index, source_kind,
                       actor_id, actor_name, actor_type, turn_kind, delta_json, delta_summary,
                       extracted_at, extractor_version, batch_id
                FROM task_turn_deltas
                WHERE task_id = {self._dialect.bind}
                ORDER BY COALESCE(turn_index, -1) ASC, extracted_at ASC
                LIMIT {self._dialect.bind}
                """,
                (task_id, max(1, limit)),
            ).fetchall()
        return [self._row_to_task_turn_delta(row) for row in rows]

    def create_task_state_snapshot(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        through_turn_index: Optional[int],
        status: Optional[str],
        assignee_id: Optional[str],
        priority: Optional[str],
        due_at: Optional[str],
        active_blockers: Optional[List[Any]],
        active_dependencies: Optional[List[Any]],
        open_questions: Optional[List[Any]],
        next_actions: Optional[List[Any]],
        current_summary: str,
        state: Optional[Dict[str, Any]],
    ) -> TaskStateSnapshotRow:
        snapshot_id = new_id("task_snapshot")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO task_state_snapshots (
                    snapshot_id, task_id, conversation_id, through_turn_index, status, assignee_id, priority, due_at,
                    active_blockers_json, active_dependencies_json, open_questions_json, next_actions_json,
                    current_summary, state_json, created_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    snapshot_id,
                    task_id,
                    conversation_id,
                    through_turn_index,
                    status,
                    assignee_id,
                    priority,
                    due_at,
                    json.dumps(active_blockers or []),
                    json.dumps(active_dependencies or []),
                    json.dumps(open_questions or []),
                    json.dumps(next_actions or []),
                    current_summary or "",
                    _json_dumps(state),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT snapshot_id, task_id, conversation_id, through_turn_index, status, assignee_id, priority, due_at,
                       active_blockers_json, active_dependencies_json, open_questions_json, next_actions_json,
                       current_summary, state_json, created_at
                FROM task_state_snapshots
                WHERE snapshot_id = {self._dialect.bind}
                """,
                (snapshot_id,),
            ).fetchone()
        return self._row_to_task_state_snapshot(row)

    def get_latest_task_state_snapshot(self, *, task_id: str) -> Optional[TaskStateSnapshotRow]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT snapshot_id, task_id, conversation_id, through_turn_index, status, assignee_id, priority, due_at,
                       active_blockers_json, active_dependencies_json, open_questions_json, next_actions_json,
                       current_summary, state_json, created_at
                FROM task_state_snapshots
                WHERE task_id = {self._dialect.bind}
                ORDER BY COALESCE(through_turn_index, -1) DESC, created_at DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_task_state_snapshot(row) if row is not None else None

    def add_task_handoff_event(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        turn_id: Optional[str],
        assigner_id: Optional[str],
        assigner_name: Optional[str],
        assignee_id: Optional[str],
        assignee_name: Optional[str],
        handoff_kind: str,
        rationale: Optional[str],
        handoff_notes: Optional[str],
    ) -> TaskHandoffEventRow:
        handoff_event_id = new_id("task_handoff")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO task_handoff_events (
                    handoff_event_id, task_id, conversation_id, turn_id, assigner_id, assigner_name,
                    assignee_id, assignee_name, handoff_kind, rationale, handoff_notes, created_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    handoff_event_id,
                    task_id,
                    conversation_id,
                    turn_id,
                    assigner_id,
                    assigner_name,
                    assignee_id,
                    assignee_name,
                    handoff_kind,
                    rationale,
                    handoff_notes,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT handoff_event_id, task_id, conversation_id, turn_id, assigner_id, assigner_name,
                       assignee_id, assignee_name, handoff_kind, rationale, handoff_notes, created_at
                FROM task_handoff_events
                WHERE handoff_event_id = {self._dialect.bind}
                """,
                (handoff_event_id,),
            ).fetchone()
        return self._row_to_task_handoff_event(row)

    def list_task_handoff_events(self, *, task_id: str, limit: int = 20) -> List[TaskHandoffEventRow]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT handoff_event_id, task_id, conversation_id, turn_id, assigner_id, assigner_name,
                       assignee_id, assignee_name, handoff_kind, rationale, handoff_notes, created_at
                FROM task_handoff_events
                WHERE task_id = {self._dialect.bind}
                ORDER BY created_at ASC
                LIMIT {self._dialect.bind}
                """,
                (task_id, max(1, limit)),
            ).fetchall()
        return [self._row_to_task_handoff_event(row) for row in rows]

    def upsert_case_memory(
        self,
        *,
        task_id: str,
        conversation_id: Optional[str],
        project_key: Optional[str],
        case_type: str,
        summary: str,
        resolution: str,
        outcome_quality: Optional[str],
        key_blockers: Optional[List[Any]],
        dependencies: Optional[List[Any]],
        tools_used: Optional[List[Any]],
        lessons_learned: Optional[List[Any]],
        embedding: Optional[str] = None,
    ) -> CaseMemoryRow:
        now = utc_now_iso()
        with self._connect() as conn:
            active_profile = self._resolve_active_embedding_profile_id(conn)
            row = conn.execute(
                f"""
                SELECT case_memory_id, task_id, conversation_id, project_key, case_type, summary, resolution,
                       outcome_quality, key_blockers_json, dependencies_json, tools_used_json,
                       lessons_learned_json, embedding, embedding_profile_id, created_at
                FROM case_memories
                WHERE task_id = {self._dialect.bind} AND case_type = {self._dialect.bind}
                LIMIT 1
                """,
                (task_id, case_type),
            ).fetchone()
            if row is None:
                case_memory_id = new_id("case_memory")
                conn.execute(
                    f"""
                    INSERT INTO case_memories (
                        case_memory_id, task_id, conversation_id, project_key, case_type, summary, resolution,
                        outcome_quality, key_blockers_json, dependencies_json, tools_used_json,
                        lessons_learned_json, embedding, embedding_profile_id, created_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        case_memory_id,
                        task_id,
                        conversation_id,
                        project_key,
                        case_type,
                        summary,
                        resolution,
                        outcome_quality,
                        json.dumps(key_blockers or []),
                        json.dumps(dependencies or []),
                        json.dumps(tools_used or []),
                        json.dumps(lessons_learned or []),
                        embedding,
                        active_profile,
                        now,
                    ),
                )
            else:
                case_memory_id = str(row["case_memory_id"])
                conn.execute(
                    f"""
                    UPDATE case_memories
                    SET conversation_id = {self._dialect.bind},
                        project_key = {self._dialect.bind},
                        summary = {self._dialect.bind},
                        resolution = {self._dialect.bind},
                        outcome_quality = {self._dialect.bind},
                        key_blockers_json = {self._dialect.bind},
                        dependencies_json = {self._dialect.bind},
                        tools_used_json = {self._dialect.bind},
                        lessons_learned_json = {self._dialect.bind},
                        embedding = COALESCE({self._dialect.bind}, embedding),
                        embedding_profile_id = COALESCE({self._dialect.bind}, embedding_profile_id)
                    WHERE case_memory_id = {self._dialect.bind}
                    """,
                    (
                        conversation_id,
                        project_key,
                        summary,
                        resolution,
                        outcome_quality,
                        json.dumps(key_blockers or []),
                        json.dumps(dependencies or []),
                        json.dumps(tools_used or []),
                        json.dumps(lessons_learned or []),
                        embedding,
                        active_profile,
                        case_memory_id,
                    ),
                )
            self._upsert_memory_graph_vector(
                conn,
                record_kind="case_memory",
                record_id=case_memory_id,
                subject_id=str(conversation_id or task_id or project_key or ""),
                embedding_profile_id=active_profile,
                embedding=embedding,
                updated_at=now,
            )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT case_memory_id, task_id, conversation_id, project_key, case_type, summary, resolution,
                       outcome_quality, key_blockers_json, dependencies_json, tools_used_json,
                       lessons_learned_json, embedding, embedding_profile_id, created_at
                FROM case_memories
                WHERE case_memory_id = {self._dialect.bind}
                """,
                (case_memory_id,),
            ).fetchone()
        return self._row_to_case_memory(current)

    def list_case_memories(
        self,
        *,
        conversation_id: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[CaseMemoryRow]:
        params: List[Any] = []
        where: List[str] = []
        with self._connect() as conn:
            active_profile = self._resolve_active_embedding_profile_id(conn)
            if conversation_id:
                where.append(f"conversation_id = {self._dialect.bind}")
                params.append(conversation_id)
            if task_id:
                where.append(f"task_id = {self._dialect.bind}")
                params.append(task_id)
            if active_profile:
                where.append(f"COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}")
                params.extend([active_profile, active_profile])
            query = """
                SELECT case_memory_id, task_id, conversation_id, project_key, case_type, summary, resolution,
                       outcome_quality, key_blockers_json, dependencies_json, tools_used_json,
                       lessons_learned_json, embedding, embedding_profile_id, created_at
                FROM case_memories
            """
            if where:
                query += " WHERE " + " AND ".join(where)
            query += f" ORDER BY created_at DESC LIMIT {self._dialect.bind}"
            params.append(max(1, limit))
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_case_memory(row) for row in rows]

    def ensure_episode(
        self,
        *,
        subject_id: str,
        conversation_id: Optional[str],
        task_id: Optional[str],
        turn_index: Optional[int],
        source_kind: str,
        request_text: str = "",
        clarification_summary_text: str = "",
        turn_summary_text: str = "",
        final_answer_text: str = "",
        execution_history_text: str = "",
        event_time: Optional[str] = None,
    ) -> MemoryEpisodeRow:
        now = utc_now_iso()
        event_at = event_time or now
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT episode_id, subject_id, conversation_id, task_id, turn_index, source_kind,
                       request_text, clarification_summary_text, turn_summary_text, final_answer_text,
                       execution_history_text, event_time, created_at, updated_at
                FROM memory_episodes
                WHERE subject_id = {self._dialect.bind} AND source_kind = {self._dialect.bind}
                  AND COALESCE(conversation_id, '') = COALESCE({self._dialect.bind}, '')
                  AND COALESCE(task_id, '') = COALESCE({self._dialect.bind}, '')
                  AND COALESCE(turn_index, -1) = COALESCE({self._dialect.bind}, -1)
                """,
                (subject_id, source_kind, conversation_id, task_id, turn_index),
            ).fetchone()
            if row is None:
                episode_id = new_id("memory_episode")
                conn.execute(
                    f"""
                    INSERT INTO memory_episodes (
                        episode_id, subject_id, conversation_id, task_id, turn_index, source_kind,
                        request_text, clarification_summary_text, turn_summary_text, final_answer_text,
                        execution_history_text, event_time, created_at, updated_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        episode_id,
                        subject_id,
                        conversation_id,
                        task_id,
                        turn_index,
                        source_kind,
                        request_text,
                        clarification_summary_text,
                        turn_summary_text,
                        final_answer_text,
                        execution_history_text,
                        event_at,
                        now,
                        now,
                    ),
                )
            else:
                episode_id = str(row["episode_id"])
                conn.execute(
                    f"""
                    UPDATE memory_episodes
                    SET request_text = {self._dialect.bind},
                        clarification_summary_text = {self._dialect.bind},
                        turn_summary_text = {self._dialect.bind},
                        final_answer_text = {self._dialect.bind},
                        execution_history_text = {self._dialect.bind},
                        event_time = {self._dialect.bind},
                        updated_at = {self._dialect.bind}
                    WHERE episode_id = {self._dialect.bind}
                    """,
                    (
                        request_text,
                        clarification_summary_text,
                        turn_summary_text,
                        final_answer_text,
                        execution_history_text,
                        event_at,
                        now,
                        episode_id,
                    ),
                )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT episode_id, subject_id, conversation_id, task_id, turn_index, source_kind,
                       request_text, clarification_summary_text, turn_summary_text, final_answer_text,
                       execution_history_text, event_time, created_at, updated_at
                FROM memory_episodes
                WHERE episode_id = {self._dialect.bind}
                """,
                (episode_id,),
            ).fetchone()
        return self._row_to_episode(current)

    def get_episodes(self, episode_ids: Sequence[str]) -> List[MemoryEpisodeRow]:
        if not episode_ids:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT episode_id, subject_id, conversation_id, task_id, turn_index, source_kind,
                       request_text, clarification_summary_text, turn_summary_text, final_answer_text,
                       execution_history_text, event_time, created_at, updated_at
                FROM memory_episodes
                WHERE episode_id IN ({",".join(f"{self._dialect.bind}" for _ in episode_ids)})
                """,
                list(episode_ids),
            ).fetchall()
        return [self._row_to_episode(row) for row in rows]

    def ensure_entity(
        self,
        *,
        subject_id: str,
        entity_type: str,
        canonical_name: str,
        aliases: Optional[List[str]] = None,
        summary_text: str = "",
        embedding: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        confidence: float = 1.0,
        source_episode_id: Optional[str] = None,
        is_active: bool = True,
    ) -> MemoryEntityRow:
        now = utc_now_iso()
        normalized_name = _normalize_name(canonical_name)
        merged_aliases = sorted({_normalize_alias(v) for v in (aliases or []) if _normalize_alias(v)})
        with self._connect() as conn:
            active_profile = self._resolve_active_embedding_profile_id(conn)
            row = conn.execute(
                f"""
                SELECT entity_id, subject_id, entity_type, canonical_name, normalized_name, aliases_json,
                       summary_text, embedding, embedding_profile_id, metadata_json, confidence, source_episode_id, is_active,
                       created_at, updated_at
                FROM memory_entities
                WHERE subject_id = {self._dialect.bind} AND normalized_name = {self._dialect.bind} AND entity_type = {self._dialect.bind}
                """,
                (subject_id, normalized_name, entity_type),
            ).fetchone()
            if row is None:
                entity_id = new_id("memory_entity")
                conn.execute(
                    f"""
                    INSERT INTO memory_entities (
                        entity_id, subject_id, entity_type, canonical_name, normalized_name, aliases_json,
                        summary_text, embedding, embedding_profile_id, metadata_json, confidence, source_episode_id, is_active,
                        created_at, updated_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        entity_id,
                        subject_id,
                        entity_type,
                        canonical_name.strip(),
                        normalized_name,
                        json.dumps(merged_aliases),
                        summary_text or "",
                        embedding,
                        active_profile,
                        _json_dumps(metadata),
                        float(confidence),
                        source_episode_id,
                        1 if is_active else 0,
                        now,
                        now,
                    ),
                )
            else:
                entity_id = str(row["entity_id"])
                existing_aliases = list(json.loads(row["aliases_json"] or "[]"))
                combined_aliases = sorted({_normalize_alias(v) for v in existing_aliases + merged_aliases if _normalize_alias(v)})
                next_metadata = dict(json.loads(row["metadata_json"] or "{}"))
                next_metadata.update(metadata or {})
                conn.execute(
                    f"""
                    UPDATE memory_entities
                    SET canonical_name = {self._dialect.bind},
                        aliases_json = {self._dialect.bind},
                        summary_text = CASE WHEN {self._dialect.bind} != '' THEN {self._dialect.bind} ELSE summary_text END,
                        embedding = COALESCE({self._dialect.bind}, embedding),
                        embedding_profile_id = COALESCE({self._dialect.bind}, embedding_profile_id),
                        metadata_json = {self._dialect.bind},
                        confidence = CASE WHEN {self._dialect.bind} > confidence THEN {self._dialect.bind} ELSE confidence END,
                        source_episode_id = COALESCE({self._dialect.bind}, source_episode_id),
                        is_active = {self._dialect.bind},
                        updated_at = {self._dialect.bind}
                    WHERE entity_id = {self._dialect.bind}
                    """,
                    (
                        canonical_name.strip() or row["canonical_name"],
                        json.dumps(combined_aliases),
                        summary_text or "",
                        summary_text or "",
                        embedding,
                        active_profile,
                        _json_dumps(next_metadata),
                        float(confidence),
                        float(confidence),
                        source_episode_id,
                        1 if is_active else int(row["is_active"]),
                        now,
                        entity_id,
                    ),
                )
            self._upsert_memory_graph_vector(
                conn,
                record_kind="entity",
                record_id=entity_id,
                subject_id=subject_id,
                embedding_profile_id=active_profile,
                embedding=embedding,
                updated_at=now,
            )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT entity_id, subject_id, entity_type, canonical_name, normalized_name, aliases_json,
                       summary_text, embedding, embedding_profile_id, metadata_json, confidence, source_episode_id, is_active,
                       created_at, updated_at
                FROM memory_entities
                WHERE entity_id = {self._dialect.bind}
                """,
                (entity_id,),
            ).fetchone()
        return self._row_to_entity(current)

    def search_entities(self, *, subject_id: str, query_text: str, limit: int = 10) -> List[MemoryEntityRow]:
        terms = _search_terms(query_text)
        with self._connect() as conn:
            active_profile = self._resolve_active_embedding_profile_id(conn)
            if not terms:
                params: List[Any] = [subject_id]
                query = f"""
                    SELECT entity_id, subject_id, entity_type, canonical_name, normalized_name, aliases_json,
                           summary_text, embedding, embedding_profile_id, metadata_json, confidence, source_episode_id, is_active,
                           created_at, updated_at
                    FROM memory_entities
                    WHERE subject_id = {self._dialect.bind}
                """
                if active_profile:
                    query += f" AND COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}"
                    params.extend([active_profile, active_profile])
                query += f" ORDER BY updated_at DESC LIMIT {self._dialect.bind}"
                params.append(max(1, limit))
                rows = conn.execute(query, params).fetchall()
            else:
                where_parts = [f"subject_id = {self._dialect.bind}"]
                params: List[Any] = [subject_id]
                if active_profile:
                    where_parts.append(f"COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}")
                    params.extend([active_profile, active_profile])
                term_clauses: List[str] = []
                for term in terms:
                    term_clauses.append(
                        f"(LOWER(canonical_name) LIKE {self._dialect.bind} OR LOWER(aliases_json) LIKE {self._dialect.bind} OR LOWER(summary_text) LIKE {self._dialect.bind})"
                    )
                    params.extend([f"%{term}%", f"%{term}%", f"%{term}%"])
                where_parts.append("(" + " OR ".join(term_clauses) + ")")
                params.append(max(1, limit))
                rows = conn.execute(
                    f"""
                    SELECT entity_id, subject_id, entity_type, canonical_name, normalized_name, aliases_json,
                           summary_text, embedding, embedding_profile_id, metadata_json, confidence, source_episode_id, is_active,
                           created_at, updated_at
                    FROM memory_entities
                    WHERE {' AND '.join(where_parts)}
                    ORDER BY confidence DESC, updated_at DESC
                    LIMIT {self._dialect.bind}
                    """,
                    params,
                ).fetchall()
        return [self._row_to_entity(row) for row in rows]


    def _search_entities_by_embedding_python(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
    ) -> List[MemoryEntityRow]:
        with self._connect() as conn:
            rows = self._embedding_candidates(conn, "memory_entities", subject_id, query_embedding)
            return _rank_rows_by_embedding(
                (self._row_to_entity(row) for row in rows),
                query_embedding=query_embedding,
                embedding_getter=lambda row: row.embedding,
                limit=limit,
            )

    def _embedding_candidates(
        self, conn: Any, table: str, subject_id: str, query_embedding: Sequence[float],
        *, active_only: bool = False, active_at_utc: Optional[str] = None,
    ) -> Iterator[Any]:
        """Scope before ranking; recency must never discard an eligible match.

        The two table names are code-owned. Server/SQLite cursors and a top-k
        heap bound memory while scanning the entire eligible embedding space.
        """
        values = _coerce_embedding_values(query_embedding)
        if not values:
            return
        dimension = self._resolve_active_embedding_dimension(conn)
        if dimension is not None and dimension != len(values):
            raise RuntimeError(
                f"Memory graph query embedding dimension mismatch: active profile expects "
                f"dimension {dimension}, but query embedding has dimension {len(values)}."
            )
        where = [f"subject_id = {self._dialect.bind}", "embedding IS NOT NULL", "embedding != ''"]
        params: List[Any] = [subject_id]
        profile = self._resolve_active_embedding_profile_id(conn)
        if profile:
            where.append(f"COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}")
            params.extend([profile, profile])
        if active_only:
            where.append("status = 'active'")
            _append_claim_effective_time_filter(where, params, active_at_utc, bind=self._dialect.bind)
        yield from conn.iterate(f"SELECT * FROM {table} WHERE {' AND '.join(where)}", params)

    def _fetch_entities_by_ids(self, conn: sqlite3.Connection, entity_ids: Sequence[str]) -> List[MemoryEntityRow]:
        if not entity_ids:
            return []
        placeholders = ",".join(f"{self._dialect.bind}" for _ in entity_ids)
        rows = conn.execute(
            f"""
            SELECT entity_id, subject_id, entity_type, canonical_name, normalized_name, aliases_json,
                   summary_text, embedding, embedding_profile_id, metadata_json, confidence, source_episode_id, is_active,
                   created_at, updated_at
            FROM memory_entities
            WHERE entity_id IN ({placeholders})
            """,
            list(entity_ids),
        ).fetchall()
        rows_by_id = {str(row["entity_id"]): self._row_to_entity(row) for row in rows}
        return [rows_by_id[entity_id] for entity_id in entity_ids if entity_id in rows_by_id]

    def _search_sqlite_vector_record_ids(
        self,
        conn: sqlite3.Connection,
        *,
        record_kind: str,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int,
    ) -> List[str]:
        values = _coerce_embedding_values(query_embedding)
        if not values:
            return []
        active_dimension = self._resolve_active_embedding_dimension(conn)
        if active_dimension is not None and active_dimension != len(values):
            raise RuntimeError(
                "Memory graph query embedding dimension mismatch: "
                f"active profile expects dimension {active_dimension}, "
                f"but query embedding has dimension {len(values)}."
            )
        if not self._ensure_sqlite_vector_table(conn, len(values)):
            return []
        active_profile = self._resolve_active_embedding_profile_id(conn)
        effective_limit = max(1, limit)
        params: List[Any] = [
            serialize_float32(values),
            max(effective_limit, effective_limit * 3),
            record_kind,
            subject_id,
        ]
        profile_clause = ""
        if active_profile:
            profile_clause = f"AND embedding_profile_id = {self._dialect.bind}"
            params.append(active_profile)
        rows = conn.execute(
            f"""
            SELECT record_id
            FROM {MEMORY_GRAPH_VECTOR_TABLE}
            WHERE embedding MATCH {self._dialect.bind}
              AND k = {self._dialect.bind}
              AND record_kind = {self._dialect.bind}
              AND subject_id = {self._dialect.bind}
              {profile_clause}
            ORDER BY distance
            """,
            params,
        ).fetchall()
        return [str(row["record_id"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows[:effective_limit]]

    def create_claim(
        self,
        *,
        subject_id: str,
        subject_entity_id: Optional[str],
        predicate: str,
        object_entity_id: Optional[str],
        object_literal: Optional[str],
        fact_text: str,
        claim_type: str,
        confidence: float,
        status: str = "active",
        valid_from_utc: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        is_inferred: bool = False,
        inference_rule: Optional[str] = None,
        embedding: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryClaimRow:
        now = utc_now_iso()
        claim_id = new_id("memory_claim")
        with self._connect() as conn:
            active_profile = self._resolve_active_embedding_profile_id(conn)
            conn.execute(
                f"""
                INSERT INTO memory_claims (
                    claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                    fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc, is_inferred,
                    inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    claim_id,
                    subject_id,
                    subject_entity_id,
                    predicate,
                    object_entity_id,
                    object_literal,
                    fact_text,
                    claim_type,
                    float(confidence),
                    status,
                    valid_from_utc,
                    invalid_after_utc,
                    1 if is_inferred else 0,
                    inference_rule,
                    embedding,
                    active_profile,
                    _json_dumps(metadata),
                    now,
                    now,
                ),
            )
            self._upsert_memory_graph_vector(
                conn,
                record_kind="claim",
                record_id=claim_id,
                subject_id=subject_id,
                embedding_profile_id=active_profile,
                embedding=embedding,
                updated_at=now,
            )
            conn.commit()
            row = conn.execute(
                f"""
                SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                       fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                       is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                FROM memory_claims
                WHERE claim_id = {self._dialect.bind}
                """,
                (claim_id,),
            ).fetchone()
        return self._row_to_claim(row)

    def update_claim(
        self,
        *,
        claim_id: str,
        status: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryClaimRow]:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                       fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                       is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                FROM memory_claims
                WHERE claim_id = {self._dialect.bind}
                """,
                (claim_id,),
            ).fetchone()
            if row is None:
                return None
            next_metadata = dict(json.loads(row["metadata_json"] or "{}"))
            next_metadata.update(metadata or {})
            conn.execute(
                f"""
                UPDATE memory_claims
                SET status = COALESCE({self._dialect.bind}, status),
                    invalid_after_utc = COALESCE({self._dialect.bind}, invalid_after_utc),
                    metadata_json = {self._dialect.bind},
                    updated_at = {self._dialect.bind}
                WHERE claim_id = {self._dialect.bind}
                """,
                (
                    status,
                    invalid_after_utc,
                    _json_dumps(next_metadata),
                    now,
                    claim_id,
                ),
            )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                       fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                       is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                FROM memory_claims
                WHERE claim_id = {self._dialect.bind}
                """,
                (claim_id,),
            ).fetchone()
        return self._row_to_claim(current)

    def list_claims_for_subject(
        self,
        *,
        subject_id: str,
        limit: int = 100,
        status: Optional[str] = None,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]:
        with self._connect() as conn:
            active_profile = self._resolve_active_embedding_profile_id(conn)
            if status:
                query = f"""
                    SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                           fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                           is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                    FROM memory_claims
                    WHERE subject_id = {self._dialect.bind} AND status = {self._dialect.bind}
                """
                params: List[Any] = [subject_id, status]
                where_parts: List[str] = []
                _append_claim_effective_time_filter(where_parts, params, active_at_utc, bind=self._dialect.bind)
                if where_parts:
                    query += " AND " + " AND ".join(where_parts)
                if active_profile:
                    query += f" AND COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}"
                    params.extend([active_profile, active_profile])
                query += f" ORDER BY updated_at DESC, created_at DESC LIMIT {self._dialect.bind}"
                params.append(max(1, limit))
                rows = conn.execute(query, params).fetchall()
            else:
                query = f"""
                    SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                           fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                           is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                    FROM memory_claims
                    WHERE subject_id = {self._dialect.bind}
                """
                params = [subject_id]
                where_parts = []
                _append_claim_effective_time_filter(where_parts, params, active_at_utc, bind=self._dialect.bind)
                if where_parts:
                    query += " AND " + " AND ".join(where_parts)
                if active_profile:
                    query += f" AND COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}"
                    params.extend([active_profile, active_profile])
                query += f" ORDER BY updated_at DESC, created_at DESC LIMIT {self._dialect.bind}"
                params.append(max(1, limit))
                rows = conn.execute(query, params).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def search_claims(
        self,
        *,
        subject_id: str,
        query_text: str,
        limit: int = 10,
        active_only: bool = True,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]:
        terms = _search_terms(query_text)
        with self._connect() as conn:
            params: List[Any] = [subject_id]
            where_parts = [f"subject_id = {self._dialect.bind}"]
            active_profile = self._resolve_active_embedding_profile_id(conn)
            if active_only:
                where_parts.append("status = 'active'")
                _append_claim_effective_time_filter(where_parts, params, active_at_utc, bind=self._dialect.bind)
            if active_profile:
                where_parts.append(f"COALESCE(embedding_profile_id, {self._dialect.bind}) = {self._dialect.bind}")
                params.extend([active_profile, active_profile])
            if terms:
                term_clauses: List[str] = []
                for term in terms:
                    term_clauses.append(f"(LOWER(fact_text) LIKE {self._dialect.bind} OR LOWER(metadata_json) LIKE {self._dialect.bind})")
                    params.extend([f"%{term}%", f"%{term}%"])
                where_parts.append("(" + " OR ".join(term_clauses) + ")")
            params.append(max(1, limit))
            rows = conn.execute(
                f"""
                SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                       fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                       is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
                FROM memory_claims
                WHERE {' AND '.join(where_parts)}
                ORDER BY confidence DESC, updated_at DESC
                LIMIT {self._dialect.bind}
                """,
                params,
            ).fetchall()
        return [self._row_to_claim(row) for row in rows]


    def _search_claims_by_embedding_python(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
        active_only: bool = True,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]:
        with self._connect() as conn:
            rows = self._embedding_candidates(
                conn, "memory_claims", subject_id, query_embedding,
                active_only=active_only, active_at_utc=active_at_utc,
            )
            return _rank_rows_by_embedding(
                (self._row_to_claim(row) for row in rows),
                query_embedding=query_embedding,
                embedding_getter=lambda row: row.embedding,
                limit=limit,
            )

    def _fetch_claims_by_ids(
        self,
        conn: sqlite3.Connection,
        claim_ids: Sequence[str],
        *,
        active_only: bool,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]:
        if not claim_ids:
            return []
        placeholders = ",".join(f"{self._dialect.bind}" for _ in claim_ids)
        query = f"""
            SELECT claim_id, subject_id, subject_entity_id, predicate, object_entity_id, object_literal,
                   fact_text, claim_type, confidence, status, valid_from_utc, invalid_after_utc,
                   is_inferred, inference_rule, embedding, embedding_profile_id, metadata_json, created_at, updated_at
            FROM memory_claims
            WHERE claim_id IN ({placeholders})
        """
        params: List[Any] = list(claim_ids)
        if active_only:
            query += " AND status = 'active'"
            where_parts: List[str] = []
            _append_claim_effective_time_filter(where_parts, params, active_at_utc, bind=self._dialect.bind)
            if where_parts:
                query += " AND " + " AND ".join(where_parts)
        rows = conn.execute(query, params).fetchall()
        rows_by_id = {str(row["claim_id"]): self._row_to_claim(row) for row in rows}
        return [rows_by_id[claim_id] for claim_id in claim_ids if claim_id in rows_by_id]

    def ensure_claim_link(
        self,
        *,
        subject_id: str,
        from_claim_id: str,
        to_claim_id: str,
        link_type: str,
        confidence: float,
        rationale: Optional[Dict[str, Any]] = None,
    ) -> MemoryClaimLinkRow:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT claim_link_id, subject_id, from_claim_id, to_claim_id, link_type,
                       confidence, rationale_json, created_at
                FROM memory_claim_links
                WHERE subject_id = {self._dialect.bind} AND from_claim_id = {self._dialect.bind} AND to_claim_id = {self._dialect.bind} AND link_type = {self._dialect.bind}
                """,
                (subject_id, from_claim_id, to_claim_id, link_type),
            ).fetchone()
            if row is None:
                claim_link_id = new_id("claim_link")
                conn.execute(
                    f"""
                    INSERT INTO memory_claim_links (
                        claim_link_id, subject_id, from_claim_id, to_claim_id, link_type,
                        confidence, rationale_json, created_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        claim_link_id,
                        subject_id,
                        from_claim_id,
                        to_claim_id,
                        link_type,
                        float(confidence),
                        _json_dumps(rationale),
                        now,
                    ),
                )
            else:
                claim_link_id = str(row["claim_link_id"])
                conn.execute(
                    f"""
                    UPDATE memory_claim_links
                    SET confidence = CASE WHEN {self._dialect.bind} > confidence THEN {self._dialect.bind} ELSE confidence END,
                        rationale_json = CASE WHEN {self._dialect.bind} != '{{}}' THEN {self._dialect.bind} ELSE rationale_json END
                    WHERE claim_link_id = {self._dialect.bind}
                    """,
                    (
                        float(confidence),
                        float(confidence),
                        _json_dumps(rationale),
                        _json_dumps(rationale),
                        claim_link_id,
                    ),
                )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT claim_link_id, subject_id, from_claim_id, to_claim_id, link_type,
                       confidence, rationale_json, created_at
                FROM memory_claim_links
                WHERE claim_link_id = {self._dialect.bind}
                """,
                (claim_link_id,),
            ).fetchone()
        return self._row_to_claim_link(current)

    def list_claim_links(
        self,
        *,
        subject_id: str,
        claim_ids: Optional[Sequence[str]] = None,
        limit: int = 50,
    ) -> List[MemoryClaimLinkRow]:
        params: List[Any] = [subject_id]
        where_clause = f"subject_id = {self._dialect.bind}"
        if claim_ids:
            placeholders = ",".join(f"{self._dialect.bind}" for _ in claim_ids)
            where_clause += f" AND (from_claim_id IN ({placeholders}) OR to_claim_id IN ({placeholders}))"
            params.extend(list(claim_ids))
            params.extend(list(claim_ids))
        params.append(max(1, limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT claim_link_id, subject_id, from_claim_id, to_claim_id, link_type,
                       confidence, rationale_json, created_at
                FROM memory_claim_links
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT {self._dialect.bind}
                """,
                params,
            ).fetchall()
        return [self._row_to_claim_link(row) for row in rows]

    def add_claim_evidence(
        self,
        *,
        claim_id: str,
        episode_id: str,
        evidence_text: str,
        evidence_kind: str,
        span: Optional[Dict[str, Any]] = None,
        extraction_confidence: float = 1.0,
    ) -> MemoryClaimEvidenceRow:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT claim_evidence_id, claim_id, episode_id, evidence_text, evidence_kind,
                       span_json, extraction_confidence, created_at
                FROM memory_claim_evidence
                WHERE claim_id = {self._dialect.bind} AND episode_id = {self._dialect.bind} AND evidence_kind = {self._dialect.bind} AND evidence_text = {self._dialect.bind}
                """,
                (claim_id, episode_id, evidence_kind, evidence_text),
            ).fetchone()
            if row is None:
                claim_evidence_id = new_id("claim_evidence")
                conn.execute(
                    f"""
                    INSERT INTO memory_claim_evidence (
                        claim_evidence_id, claim_id, episode_id, evidence_text, evidence_kind,
                        span_json, extraction_confidence, created_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        claim_evidence_id,
                        claim_id,
                        episode_id,
                        evidence_text,
                        evidence_kind,
                        _json_dumps(span),
                        float(extraction_confidence),
                        now,
                    ),
                )
            else:
                claim_evidence_id = str(row["claim_evidence_id"])
                conn.execute(
                    f"""
                    UPDATE memory_claim_evidence
                    SET span_json = CASE WHEN {self._dialect.bind} != '{{}}' THEN {self._dialect.bind} ELSE span_json END,
                        extraction_confidence = CASE
                            WHEN {self._dialect.bind} > extraction_confidence THEN {self._dialect.bind}
                            ELSE extraction_confidence
                        END
                    WHERE claim_evidence_id = {self._dialect.bind}
                    """,
                    (
                        _json_dumps(span),
                        _json_dumps(span),
                        float(extraction_confidence),
                        float(extraction_confidence),
                        claim_evidence_id,
                    ),
                )
            conn.commit()
            current = conn.execute(
                f"""
                SELECT claim_evidence_id, claim_id, episode_id, evidence_text, evidence_kind,
                       span_json, extraction_confidence, created_at
                FROM memory_claim_evidence
                WHERE claim_evidence_id = {self._dialect.bind}
                """,
                (claim_evidence_id,),
            ).fetchone()
        return self._row_to_claim_evidence(current)

    def list_evidence_for_claims(self, claim_ids: Sequence[str]) -> List[MemoryClaimEvidenceRow]:
        if not claim_ids:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT claim_evidence_id, claim_id, episode_id, evidence_text, evidence_kind,
                       span_json, extraction_confidence, created_at
                FROM memory_claim_evidence
                WHERE claim_id IN ({",".join(f"{self._dialect.bind}" for _ in claim_ids)})
                ORDER BY created_at ASC
                """,
                list(claim_ids),
            ).fetchall()
        return [self._row_to_claim_evidence(row) for row in rows]

    @staticmethod
    def _row_to_episode(row: sqlite3.Row | None) -> MemoryEpisodeRow:
        if row is None:
            raise ValueError("Expected episode row, got None.")
        return MemoryEpisodeRow(
            episode_id=row["episode_id"],
            subject_id=row["subject_id"],
            conversation_id=row["conversation_id"],
            task_id=row["task_id"],
            turn_index=row["turn_index"],
            source_kind=row["source_kind"],
            request_text=row["request_text"],
            clarification_summary_text=row["clarification_summary_text"],
            turn_summary_text=row["turn_summary_text"],
            final_answer_text=row["final_answer_text"],
            execution_history_text=row["execution_history_text"],
            event_time=row["event_time"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_entity(row: sqlite3.Row | None) -> MemoryEntityRow:
        if row is None:
            raise ValueError("Expected entity row, got None.")
        return MemoryEntityRow(
            entity_id=row["entity_id"],
            subject_id=row["subject_id"],
            entity_type=row["entity_type"],
            canonical_name=row["canonical_name"],
            normalized_name=row["normalized_name"],
            aliases_json=row["aliases_json"],
            summary_text=row["summary_text"],
            embedding=row["embedding"],
            embedding_profile_id=row["embedding_profile_id"],
            metadata_json=row["metadata_json"],
            confidence=float(row["confidence"]),
            source_episode_id=row["source_episode_id"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_claim(row: sqlite3.Row | None) -> MemoryClaimRow:
        if row is None:
            raise ValueError("Expected claim row, got None.")
        return MemoryClaimRow(
            claim_id=row["claim_id"],
            subject_id=row["subject_id"],
            subject_entity_id=row["subject_entity_id"],
            predicate=row["predicate"],
            object_entity_id=row["object_entity_id"],
            object_literal=row["object_literal"],
            fact_text=row["fact_text"],
            claim_type=row["claim_type"],
            confidence=float(row["confidence"]),
            status=row["status"],
            valid_from_utc=row["valid_from_utc"],
            invalid_after_utc=row["invalid_after_utc"],
            is_inferred=bool(row["is_inferred"]),
            inference_rule=row["inference_rule"],
            embedding=row["embedding"],
            embedding_profile_id=row["embedding_profile_id"],
            metadata_json=row["metadata_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_embedding_profile(row: sqlite3.Row | None) -> MemoryEmbeddingProfileRow:
        if row is None:
            raise ValueError("Expected embedding-profile row, got None.")
        return MemoryEmbeddingProfileRow(
            embedding_profile_id=row["embedding_profile_id"],
            profile_hash=row["profile_hash"],
            provider=row["provider"],
            model_id=row["model_id"],
            api_base=row["api_base"],
            dimensions=row["dimensions"],
            status=row["status"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata_json=row["metadata_json"],
        )

    @staticmethod
    def _row_to_claim_link(row: sqlite3.Row | None) -> MemoryClaimLinkRow:
        if row is None:
            raise ValueError("Expected claim-link row, got None.")
        return MemoryClaimLinkRow(
            claim_link_id=row["claim_link_id"],
            subject_id=row["subject_id"],
            from_claim_id=row["from_claim_id"],
            to_claim_id=row["to_claim_id"],
            link_type=row["link_type"],
            confidence=float(row["confidence"]),
            rationale_json=row["rationale_json"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_claim_evidence(row: sqlite3.Row | None) -> MemoryClaimEvidenceRow:
        if row is None:
            raise ValueError("Expected claim-evidence row, got None.")
        return MemoryClaimEvidenceRow(
            claim_evidence_id=row["claim_evidence_id"],
            claim_id=row["claim_id"],
            episode_id=row["episode_id"],
            evidence_text=row["evidence_text"],
            evidence_kind=row["evidence_kind"],
            span_json=row["span_json"],
            extraction_confidence=float(row["extraction_confidence"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_task_turn_delta(row: sqlite3.Row | None) -> TaskTurnDeltaRow:
        if row is None:
            raise ValueError("Expected task-turn-delta row, got None.")
        return TaskTurnDeltaRow(
            delta_id=row["delta_id"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            turn_index=row["turn_index"],
            source_kind=row["source_kind"],
            actor_id=row["actor_id"],
            actor_name=row["actor_name"],
            actor_type=row["actor_type"],
            turn_kind=row["turn_kind"],
            delta_json=row["delta_json"],
            delta_summary=row["delta_summary"],
            extracted_at=row["extracted_at"],
            extractor_version=row["extractor_version"],
            batch_id=row["batch_id"],
        )

    @staticmethod
    def _row_to_task_state_snapshot(row: sqlite3.Row | None) -> TaskStateSnapshotRow:
        if row is None:
            raise ValueError("Expected task-state-snapshot row, got None.")
        return TaskStateSnapshotRow(
            snapshot_id=row["snapshot_id"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            through_turn_index=row["through_turn_index"],
            status=row["status"],
            assignee_id=row["assignee_id"],
            priority=row["priority"],
            due_at=row["due_at"],
            active_blockers_json=row["active_blockers_json"],
            active_dependencies_json=row["active_dependencies_json"],
            open_questions_json=row["open_questions_json"],
            next_actions_json=row["next_actions_json"],
            current_summary=row["current_summary"],
            state_json=row["state_json"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_task_handoff_event(row: sqlite3.Row | None) -> TaskHandoffEventRow:
        if row is None:
            raise ValueError("Expected task-handoff-event row, got None.")
        return TaskHandoffEventRow(
            handoff_event_id=row["handoff_event_id"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            assigner_id=row["assigner_id"],
            assigner_name=row["assigner_name"],
            assignee_id=row["assignee_id"],
            assignee_name=row["assignee_name"],
            handoff_kind=row["handoff_kind"],
            rationale=row["rationale"],
            handoff_notes=row["handoff_notes"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_case_memory(row: sqlite3.Row | None) -> CaseMemoryRow:
        if row is None:
            raise ValueError("Expected case-memory row, got None.")
        return CaseMemoryRow(
            case_memory_id=row["case_memory_id"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            project_key=row["project_key"],
            case_type=row["case_type"],
            summary=row["summary"],
            resolution=row["resolution"],
            outcome_quality=row["outcome_quality"],
            key_blockers_json=row["key_blockers_json"],
            dependencies_json=row["dependencies_json"],
            tools_used_json=row["tools_used_json"],
            lessons_learned_json=row["lessons_learned_json"],
            embedding=row["embedding"],
            embedding_profile_id=row["embedding_profile_id"],
            created_at=row["created_at"],
        )


class SqliteMemoryGraphRepository(_RelationalMemoryGraphRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(
        self,
        db_path: str,
        logger: Optional[logging.Logger] = None,
        *,
        vector_enabled: bool = True,
        vector_provider: str = SQLITE_VEC_PROVIDER,
        vector_require_extension: bool = True,
    ):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._sqlite_vector_enabled = bool(vector_enabled)
        self._sqlite_vector_provider = str(vector_provider or SQLITE_VEC_PROVIDER).strip()
        self._sqlite_vector_require_extension = bool(vector_require_extension)
        self._sqlite_vec_version: Optional[str] = None
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(
            self._db_path,
            logger=self._logger,
            row_factory=sqlite3.Row,
            sqlite_module=sqlite3,
        )


    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except Exception as exc:
            message = str(exc).lower()
            if "duplicate column" in message or "already exists" in message:
                rollback = getattr(conn, "rollback", None)
                if callable(rollback):
                    rollback()
                return
            raise


    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


    def search_entities_by_embedding(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
    ) -> List[MemoryEntityRow]:
        with self._connect() as conn:
            record_ids = self._search_sqlite_vector_record_ids(
                conn,
                record_kind="entity",
                subject_id=subject_id,
                query_embedding=query_embedding,
                limit=max(1, limit),
            )
            rows = self._fetch_entities_by_ids(conn, record_ids)
        if rows or self._sqlite_vector_enabled:
            return rows
        self._logger.warning(
            "SQLite memory-graph vector search fell back to Python entity scoring because sqlite-vec is disabled.",
            extra={"component": "sqlite_vec", "record_kind": "entity", "subject_id": subject_id},
        )
        return self._search_entities_by_embedding_python(
            subject_id=subject_id,
            query_embedding=query_embedding,
            limit=limit,
        )


    def search_claims_by_embedding(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
        active_only: bool = True,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]:
        with self._connect() as conn:
            record_ids = self._search_sqlite_vector_record_ids(
                conn,
                record_kind="claim",
                subject_id=subject_id,
                query_embedding=query_embedding,
                limit=max(1, limit),
            )
            rows = self._fetch_claims_by_ids(
                conn,
                record_ids,
                active_only=active_only,
                active_at_utc=active_at_utc,
            )
        if rows or self._sqlite_vector_enabled:
            return rows
        self._logger.warning(
            "SQLite memory-graph vector search fell back to Python claim scoring because sqlite-vec is disabled.",
            extra={"component": "sqlite_vec", "record_kind": "claim", "subject_id": subject_id},
        )
        return self._search_claims_by_embedding_python(
            subject_id=subject_id,
            query_embedding=query_embedding,
            limit=limit,
            active_only=active_only,
            active_at_utc=active_at_utc,
        )


class PostgresMemoryGraphRepository(_RelationalMemoryGraphRepository):
    """Postgres implementation for scoped memory graph persistence."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        self._sqlite_vector_enabled = False
        self._sqlite_vector_provider = SQLITE_VEC_PROVIDER
        self._sqlite_vector_require_extension = False
        self._sqlite_vec_version = None
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def _ensure_column(self, conn: PostgresConnection, table: str, column: str, definition: str) -> None:
        # PostgreSQL owns concurrent schema idempotency; an exception followed
        # by rollback would discard earlier statements in this initialization.
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")

    def _table_columns(self, conn: PostgresConnection, table: str) -> set[str]:
        rows = conn.execute(
            f"""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = {self._dialect.bind}
            """,
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}

    def search_entities_by_embedding(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
    ) -> List[MemoryEntityRow]:
        return self._search_entities_by_embedding_python(
            subject_id=subject_id,
            query_embedding=query_embedding,
            limit=limit,
        )

    def search_claims_by_embedding(
        self,
        *,
        subject_id: str,
        query_embedding: Sequence[float],
        limit: int = 10,
        active_only: bool = True,
        active_at_utc: Optional[str] = None,
    ) -> List[MemoryClaimRow]:
        return self._search_claims_by_embedding_python(
            subject_id=subject_id,
            query_embedding=query_embedding,
            limit=limit,
            active_only=active_only,
            active_at_utc=active_at_utc,
        )


def create_memory_graph_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> MemoryGraphRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="MemoryGraphRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        sqlite_vector = storage_settings.sqlite_vector
        return SqliteMemoryGraphRepository(
            storage_settings.sqlite_path,
            logger=logger,
            vector_enabled=(
                sqlite_vector.enabled
                if sqlite_vector is not None
                else True
            ),
            vector_provider=(
                sqlite_vector.provider
                if sqlite_vector is not None
                else SQLITE_VEC_PROVIDER
            ),
            vector_require_extension=(
                sqlite_vector.require_extension
                if sqlite_vector is not None
                else True
            ),
        )
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresMemoryGraphRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"MemoryGraphRepository has no implementation for backend {storage_settings.backend!r}."
    )


def _normalize_alias(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _search_terms(query_text: str) -> List[str]:
    collapsed = _normalize_name(query_text)
    if not collapsed:
        return []
    return [term for term in collapsed.split(" ") if term]


def _parse_embedding_values(raw: Optional[str]) -> List[float]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return _coerce_embedding_values(decoded)


def _coerce_embedding_values(values: Sequence[Any]) -> List[float]:
    parsed: List[float] = []
    for value in values:
        try:
            float_value = float(value)
        except (TypeError, ValueError):
            float_value = 0.0
        if not math.isfinite(float_value):
            float_value = 0.0
        parsed.append(float_value)
    return parsed


def _rank_rows_by_embedding(
    rows: Iterable[Any],
    *,
    query_embedding: Sequence[float],
    embedding_getter: Any,
    limit: int,
) -> List[Any]:
    query_values = _coerce_embedding_values(query_embedding)
    def candidates():
        for index, row in enumerate(rows):
            values = _parse_embedding_values(embedding_getter(row))
            if not values or len(values) != len(query_values):
                continue
            # sqlite-vec's default metric is L2; squaring preserves its order.
            distance = sum((a - b) ** 2 for a, b in zip(query_values, values))
            yield distance, index, row

    return [row for _, _, row in heapq.nsmallest(max(1, limit), candidates())]


def build_embedding_profile_hash(
    *,
    provider: str,
    model_id: Optional[str],
    api_base: Optional[str],
    dimensions: Optional[int],
) -> str:
    payload = {
        "provider": str(provider or "").strip().lower(),
        "model_id": str(model_id or "").strip() or None,
        "api_base": str(api_base or "").strip().rstrip("/") or None,
        "dimensions": int(dimensions) if dimensions is not None else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
