"""Backend-neutral repository boundary for durable conversation-thread persistence."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from custom_agents.conversation_delivery_states import (
    MODEL_VISIBLE_TIMER_DELIVERY_STATES,
)
from id_generation import new_id

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite, ensure_sqlite_column


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_created_at_filter(
    value: Optional[str],
    *,
    boundary: str,
) -> tuple[Optional[str], Optional[datetime], Optional[str]]:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None, None, None

    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid {boundary} filter {raw_value!r}. Use an ISO-8601 timestamp or YYYY-MM-DD date."
        ) from exc

    is_date_only = "T" not in raw_value and " " not in raw_value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    if boundary == "created_before":
        if is_date_only:
            parsed = parsed + timedelta(days=1)
        return parsed.isoformat(), parsed, "<"
    return parsed.isoformat(), parsed, ">="


def enum_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return getattr(value, "value", value)


def _memory_review_initial_status(
    *,
    memory_review_status: Optional[str],
    requester_type: Optional[str],
    owner_named_agent_type: Optional[str],
    problem_text: str,
) -> str:
    explicit_status = enum_value(memory_review_status)
    if explicit_status:
        if explicit_status not in VALID_TURN_MEMORY_REVIEW_STATUSES:
            raise ValueError(f"Invalid memory_review_status: {explicit_status}")
        return explicit_status

    requester_value = enum_value(requester_type)
    if requester_value is not None and requester_value != "user_req":
        return TURN_MEMORY_REVIEW_IGNORED
    if owner_named_agent_type == MEMORY_REVIEW_AGENT_TYPE:
        return TURN_MEMORY_REVIEW_IGNORED
    if str(problem_text or "").startswith(MEMORY_REVIEW_PROMPT_PREFIX):
        return TURN_MEMORY_REVIEW_IGNORED
    return TURN_MEMORY_REVIEW_UNREVIEWED


def _is_memory_review_source_turn(turn: dict[str, Any]) -> bool:
    if turn.get("task_status") != "completed":
        return False
    requester_type = enum_value(turn.get("requester_type"))
    if requester_type is not None and requester_type != "user_req":
        return False
    if turn.get("owner_named_agent_type") == MEMORY_REVIEW_AGENT_TYPE:
        return False
    if str(turn.get("problem_text") or "").startswith(MEMORY_REVIEW_PROMPT_PREFIX):
        return False
    return True


def default_synopsis() -> dict[str, Any]:
    return {
        "goals": [],
        "decisions": [],
        "rationales": [],
        "constraints": [],
        "active_assumptions": [],
        "important_facts": [],
        "tool_results": [],
        "artifacts": [],
        "rejected_approaches": [],
        "open_risks": [],
        "open_questions": [],
        "current_state": "",
        "next_step_intent": "",
        "summary_markdown": "",
    }


def _json_loads(raw_value: Any, default: Any) -> Any:
    if raw_value in (None, ""):
        return default
    try:
        return json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return default


def normalize_topic_tag(raw_value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(raw_value or "").strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not normalized:
        raise ValueError("Topic tag must contain at least one letter or number.")
    return normalized


TURN_MEMORY_REVIEW_UNREVIEWED = "unreviewed"
TURN_MEMORY_REVIEW_QUEUED = "review_queued"
TURN_MEMORY_REVIEW_REVIEWED = "reviewed"
TURN_MEMORY_REVIEW_FAILED = "review_failed"
TURN_MEMORY_REVIEW_IGNORED = "ignored"

MEMORY_REVIEW_AGENT_TYPE = "MemReviewAgent"
MEMORY_REVIEW_PROMPT_PREFIX = "Review the queued memory job below."
MEMORY_REVIEW_PROMPT_LIKE_PATTERN = f"{MEMORY_REVIEW_PROMPT_PREFIX}%"
def _memory_review_eligible_turn_sql(bind: str) -> str:
    return f"""
                  AND (requester_type IS NULL OR requester_type = 'user_req')
                  AND COALESCE(owner_named_agent_type, '') <> 'MemReviewAgent'
                  AND (problem_text IS NULL OR problem_text NOT LIKE {bind})
"""
VALID_TURN_MEMORY_REVIEW_STATUSES = {
    TURN_MEMORY_REVIEW_UNREVIEWED,
    TURN_MEMORY_REVIEW_QUEUED,
    TURN_MEMORY_REVIEW_REVIEWED,
    TURN_MEMORY_REVIEW_FAILED,
    TURN_MEMORY_REVIEW_IGNORED,
}


TURN_SELECT_COLUMNS = """
    conversation_id, task_id, followup_to_task_id, turn_index,
    owner_canonical_user_id,
    owner_agent_id, owner_named_agent_type, requester_name, requester_id, requester_type,
    correlation_id, delivery_mode, delivery_state, delivery_via, delivered_at,
    task_status, problem_text, final_answer_text, clarification_summary_text,
    execution_history_text, turn_summary_text, user_request_summary,
    assistant_response_summary, context_summary_text, artifact_summary_json,
    summary_generator, summary_generated_at, tool_artifact_manifest_json,
    gateway_context_json,
    memory_review_status, memory_review_job_id, memory_review_queued_at_utc,
    memory_reviewed_at_utc, memory_reviewed_by_agent_id,
    created_at, updated_at
"""


class ConversationRepository(Protocol):
    def ensure_thread(
        self,
        *,
        conversation_id: Optional[str],
        team_id: Optional[str],
        owner_agent_id: Optional[str],
        owner_agent_name: Optional[str],
        requester_name: Optional[str],
        requester_id: Optional[str],
        root_task_id: str,
        subject_preview: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def get_thread(self, conversation_id: str) -> Optional[dict[str, Any]]: ...

    def update_thread_compaction(
        self,
        conversation_id: str,
        *,
        synopsis_json: Optional[dict[str, Any]] = None,
        synopsis_markdown: Optional[str] = None,
        summarized_through_turn_index: Optional[int] = None,
        last_token_estimate: Optional[int] = None,
        last_compacted_at: Optional[str] = None,
        compaction_version: Optional[int] = None,
        compactor_type: Optional[str] = None,
        compaction_state: Optional[dict[str, Any]] = None,
        status: Optional[str] = None,
        latest_task_id: Optional[str] = None,
    ) -> None: ...

    def get_turn_by_task_id(self, task_id: str) -> Optional[dict[str, Any]]: ...

    def get_latest_task_link(self, conversation_id: str) -> Optional[dict[str, Any]]: ...

    def append_turn(
        self,
        *,
        conversation_id: str,
        task_id: str,
        followup_to_task_id: Optional[str],
        owner_agent_id: Optional[str],
        requester_name: Optional[str],
        requester_id: Optional[str],
        correlation_id: Optional[str],
        problem_text: str,
        owner_named_agent_type: Optional[str] = None,
        requester_type: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        delivery_state: Optional[str] = None,
        gateway_context: Optional[dict[str, Any]] = None,
        task_status: str = "accepted",
        memory_review_status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def append_clarification(
        self,
        *,
        task_id: str,
        message: str,
    ) -> None: ...

    def update_turn_completion(
        self,
        *,
        task_id: str,
        task_status: str,
        final_answer_text: str,
        turn_summary_text: str,
        clarification_summary_text: Optional[str] = None,
        execution_history_text: Optional[str] = None,
        tool_artifact_manifest: Optional[list[dict[str, Any]]] = None,
        user_request_summary: Optional[str] = None,
        assistant_response_summary: Optional[str] = None,
        context_summary_text: Optional[str] = None,
        artifact_summary: Optional[list[dict[str, Any]]] = None,
        summary_generator: Optional[str] = None,
    ) -> Optional[dict[str, Any]]: ...

    def update_turn_delivery(
        self,
        *,
        task_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        delivery_state: str,
        delivery_via: Optional[str] = None,
        delivered_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]: ...

    def list_conversations(
        self,
        *,
        limit: int = 10,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    def get_conversation_turns(
        self,
        *,
        conversation_id: str,
        start_turn: int = 1,
        limit: int = 10,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...

    def get_recent_turns(
        self,
        conversation_id: str,
        limit: Optional[int] = None,
        *,
        include_pending_delivery: bool = True,
    ) -> list[dict[str, Any]]: ...

    def offload_large_text(
        self,
        *,
        conversation_id: str,
        task_id: str,
        artifact_type: str,
        raw_text: str,
        summary_text: str,
        extension: str = "txt",
    ) -> dict[str, Any]: ...

    def delete_artifacts_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]: ...

    def count_artifacts_created_before(self, cutoff: str) -> int: ...

    def create_context_epoch(
        self,
        *,
        conversation_id: str,
        trigger: str,
        through_turn_index: int,
        preserved_suffix_start_turn: int,
        latest_task_id: Optional[str] = None,
        summary_json: Optional[dict[str, Any]] = None,
        summary_markdown: Optional[str] = None,
        rehydration_manifest: Optional[dict[str, Any]] = None,
        source_lengths_before: Optional[list[dict[str, Any]]] = None,
        source_lengths_after: Optional[list[dict[str, Any]]] = None,
        pre_compact_estimated_tokens: Optional[int] = None,
        post_compact_estimated_tokens: Optional[int] = None,
        compactor_type: Optional[str] = None,
        created_by_agent_id: Optional[str] = None,
        status: str = "active",
        error_text: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def get_latest_context_epoch(self, conversation_id: str) -> Optional[dict[str, Any]]: ...

    def list_context_epochs(self, conversation_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...

    def get_context_epoch(self, context_epoch_id: str) -> Optional[dict[str, Any]]: ...

    def mark_context_epoch_failed(self, context_epoch_id: str, *, error_text: str) -> Optional[dict[str, Any]]: ...

    def tag_thread(
        self,
        *,
        conversation_id: str,
        tag: str,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        source: str = "manual",
        created_by_agent_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def untag_thread(self, *, conversation_id: str, tag: str) -> bool: ...

    def list_thread_tags(self, conversation_id: str) -> list[dict[str, Any]]: ...

    def list_known_thread_tags(
        self,
        *,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    def find_threads_by_tags(
        self,
        *,
        tags: list[str],
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...

    def tag_turn(
        self,
        *,
        conversation_id: str,
        turn_index: int,
        tag: str,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        source: str = "manual",
        created_by_agent_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def list_turn_tags(self, *, conversation_id: str, turn_index: int) -> list[dict[str, Any]]: ...

    def get_memory_review_window(
        self,
        *,
        conversation_id: str,
        max_unreviewed_turns: int,
        overlap_turns: int = 0,
        allow_partial: bool = False,
    ) -> Optional[dict[str, Any]]: ...

    def list_conversation_memory_review_candidates(
        self,
        *,
        team_id: Optional[str] = None,
        stale_before_utc: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    def mark_turns_memory_review_queued(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
    ) -> int: ...

    def mark_turns_memory_reviewed(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
        reviewed_by_agent_id: Optional[str] = None,
    ) -> int: ...

    def mark_turns_memory_review_failed(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
    ) -> int: ...


class _RelationalConversationRepository:
    """Shared record operations for durable conversation-thread persistence."""

    _dialect: SqlDialect


    def _init_schema(self) -> None:
        ensure_storage_parent_dir(self._db_path)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_threads (
                    conversation_id TEXT PRIMARY KEY,
                    team_id TEXT,
                    owner_canonical_user_id TEXT,
                    owner_agent_id TEXT,
                    owner_agent_name TEXT,
                    requester_name TEXT,
                    requester_id TEXT,
                    root_task_id TEXT,
                    latest_task_id TEXT,
                    status TEXT,
                    subject_preview TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    synopsis_json TEXT,
                    synopsis_markdown TEXT,
                    summarized_through_turn_index INTEGER DEFAULT 0,
                    last_token_estimate INTEGER DEFAULT 0,
                    last_compacted_at TEXT,
                    compaction_version INTEGER DEFAULT 1,
                    compactor_type TEXT,
                    compaction_state_json TEXT
                )
                """
            )
            self._ensure_column(conn, "conversation_threads", "team_id", "TEXT")
            self._ensure_column(conn, "conversation_threads", "owner_canonical_user_id", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_task_links (
                    conversation_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    followup_to_task_id TEXT,
                    turn_index INTEGER NOT NULL,
                    owner_canonical_user_id TEXT,
                    owner_agent_id TEXT,
                    owner_named_agent_type TEXT,
                    requester_name TEXT,
                    requester_id TEXT,
                    requester_type TEXT,
                    correlation_id TEXT,
                    delivery_mode TEXT,
                    delivery_state TEXT,
                    delivery_via TEXT,
                    delivered_at TEXT,
                    task_status TEXT,
                    problem_text TEXT,
                    final_answer_text TEXT,
                    clarification_summary_text TEXT,
                    execution_history_text TEXT,
                    turn_summary_text TEXT,
                    user_request_summary TEXT,
                    assistant_response_summary TEXT,
                    context_summary_text TEXT,
                    artifact_summary_json TEXT,
                    summary_generator TEXT,
                    summary_generated_at TEXT,
                    tool_artifact_manifest_json TEXT,
                    gateway_context_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (conversation_id, task_id)
                )
                """
            )
            self._ensure_column(conn, "conversation_task_links", "owner_canonical_user_id", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "owner_named_agent_type", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "user_request_summary", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "assistant_response_summary", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "context_summary_text", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "artifact_summary_json", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "summary_generator", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "summary_generated_at", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "gateway_context_json", "TEXT")
            self._ensure_column(
                conn,
                "conversation_task_links",
                "memory_review_status",
                f"TEXT NOT NULL DEFAULT '{TURN_MEMORY_REVIEW_UNREVIEWED}'",
            )
            self._ensure_column(conn, "conversation_task_links", "memory_review_job_id", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "memory_review_queued_at_utc", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "memory_reviewed_at_utc", "TEXT")
            self._ensure_column(conn, "conversation_task_links", "memory_reviewed_by_agent_id", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    task_id TEXT,
                    artifact_type TEXT,
                    storage_path TEXT,
                    summary_text TEXT,
                    created_at TEXT
                )
                """
            )
            # The saved-output cleanup deletes the oldest records first
            # (delete_artifacts_created_before).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_artifacts_created_at "
                "ON conversation_artifacts(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_context_epochs (
                    context_epoch_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    epoch_index INTEGER NOT NULL,
                    owner_canonical_user_id TEXT,
                    trigger TEXT,
                    status TEXT,
                    through_turn_index INTEGER NOT NULL,
                    preserved_suffix_start_turn INTEGER NOT NULL,
                    latest_task_id TEXT,
                    summary_json TEXT,
                    summary_markdown TEXT,
                    rehydration_manifest_json TEXT,
                    source_lengths_before_json TEXT,
                    source_lengths_after_json TEXT,
                    pre_compact_estimated_tokens INTEGER,
                    post_compact_estimated_tokens INTEGER,
                    compactor_type TEXT,
                    created_by_agent_id TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    error_text TEXT
                )
                """
            )
            self._ensure_column(conn, "conversation_context_epochs", "owner_canonical_user_id", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_thread_tags (
                    conversation_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    team_id TEXT,
                    canonical_user_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by_agent_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, tag)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turn_tags (
                    conversation_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    team_id TEXT,
                    canonical_user_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by_agent_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, turn_index, tag)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_task_links_conversation_turn
                ON conversation_task_links(conversation_id, turn_index)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_task_links_updated
                ON conversation_task_links(conversation_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_task_links_created
                ON conversation_task_links(conversation_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_task_links_task_id
                ON conversation_task_links(task_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_task_links_owner
                ON conversation_task_links(owner_agent_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_task_links_memory_review
                ON conversation_task_links(conversation_id, memory_review_status, turn_index)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_threads_owner
                ON conversation_threads(owner_agent_id, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_context_epochs_conversation
                ON conversation_context_epochs(conversation_id, epoch_index DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_context_epochs_status
                ON conversation_context_epochs(conversation_id, status, epoch_index DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_thread_tags_lookup
                ON conversation_thread_tags(tag, team_id, canonical_user_id, status, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_turn_tags_lookup
                ON conversation_turn_tags(tag, team_id, canonical_user_id, status, updated_at DESC)
                """
            )
            conn.commit()


    def ensure_thread(
        self,
        *,
        conversation_id: Optional[str],
        team_id: Optional[str],
        owner_agent_id: Optional[str],
        owner_agent_name: Optional[str],
        requester_name: Optional[str],
        requester_id: Optional[str],
        root_task_id: str,
        subject_preview: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_conversation_id = conversation_id or new_id("conversation_id")
        existing = self.get_thread(resolved_conversation_id)
        if existing:
            return existing

        now_ts = utc_now_iso()
        synopsis = default_synopsis()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO conversation_threads (
                    conversation_id, team_id, owner_canonical_user_id, owner_agent_id, owner_agent_name,
                    requester_name, requester_id, root_task_id, latest_task_id,
                    status, subject_preview, created_at, updated_at,
                    synopsis_json, synopsis_markdown, summarized_through_turn_index,
                    last_token_estimate, last_compacted_at, compaction_version,
                    compactor_type, compaction_state_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    resolved_conversation_id,
                    team_id,
                    owner_canonical_user_id,
                    owner_agent_id,
                    owner_agent_name,
                    requester_name,
                    requester_id,
                    root_task_id,
                    root_task_id,
                    "active",
                    subject_preview,
                    now_ts,
                    now_ts,
                    json.dumps(synopsis),
                    "",
                    0,
                    0,
                    None,
                    1,
                    None,
                    json.dumps({}),
                ),
            )
            conn.commit()
        return self.get_thread(resolved_conversation_id) or {}

    def get_thread(self, conversation_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT conversation_id, team_id, owner_agent_id, owner_agent_name,
                       requester_name, requester_id, root_task_id, latest_task_id,
                       status, subject_preview, created_at, updated_at,
                       synopsis_json, synopsis_markdown, summarized_through_turn_index,
                       last_token_estimate, last_compacted_at, compaction_version,
                       compactor_type, compaction_state_json, owner_canonical_user_id
                FROM conversation_threads
                WHERE conversation_id = {self._dialect.bind}
                """,
                (conversation_id,),
            ).fetchone()
        return self._row_to_thread(row) if row else None

    def _row_to_thread(self, row: sqlite3.Row) -> dict[str, Any]:
        synopsis_raw = row["synopsis_json"] or json.dumps(default_synopsis())
        compaction_state_raw = row["compaction_state_json"] or "{}"
        try:
            synopsis = json.loads(synopsis_raw)
        except json.JSONDecodeError:
            synopsis = default_synopsis()
        try:
            compaction_state = json.loads(compaction_state_raw)
        except json.JSONDecodeError:
            compaction_state = {}
        return {
            "conversation_id": row["conversation_id"],
            "team_id": row["team_id"],
            "owner_canonical_user_id": row["owner_canonical_user_id"],
            "owner_agent_id": row["owner_agent_id"],
            "owner_agent_name": row["owner_agent_name"],
            "requester_name": row["requester_name"],
            "requester_id": row["requester_id"],
            "root_task_id": row["root_task_id"],
            "latest_task_id": row["latest_task_id"],
            "status": row["status"],
            "subject_preview": row["subject_preview"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "synopsis_json": synopsis,
            "synopsis_markdown": row["synopsis_markdown"] or "",
            "summarized_through_turn_index": row["summarized_through_turn_index"] or 0,
            "last_token_estimate": row["last_token_estimate"] or 0,
            "last_compacted_at": row["last_compacted_at"],
            "compaction_version": row["compaction_version"] or 1,
            "compactor_type": row["compactor_type"],
            "compaction_state": compaction_state,
        }

    def update_thread_compaction(
        self,
        conversation_id: str,
        *,
        synopsis_json: Optional[dict[str, Any]] = None,
        synopsis_markdown: Optional[str] = None,
        summarized_through_turn_index: Optional[int] = None,
        last_token_estimate: Optional[int] = None,
        last_compacted_at: Optional[str] = None,
        compaction_version: Optional[int] = None,
        compactor_type: Optional[str] = None,
        compaction_state: Optional[dict[str, Any]] = None,
        status: Optional[str] = None,
        latest_task_id: Optional[str] = None,
    ) -> None:
        assignments: list[str] = [f"updated_at = {self._dialect.bind}"]
        params: list[Any] = [utc_now_iso()]
        if synopsis_json is not None:
            assignments.append(f"synopsis_json = {self._dialect.bind}")
            params.append(json.dumps(synopsis_json))
        if synopsis_markdown is not None:
            assignments.append(f"synopsis_markdown = {self._dialect.bind}")
            params.append(synopsis_markdown)
        if summarized_through_turn_index is not None:
            assignments.append(f"summarized_through_turn_index = {self._dialect.bind}")
            params.append(int(summarized_through_turn_index))
        if last_token_estimate is not None:
            assignments.append(f"last_token_estimate = {self._dialect.bind}")
            params.append(int(last_token_estimate))
        if last_compacted_at is not None:
            assignments.append(f"last_compacted_at = {self._dialect.bind}")
            params.append(last_compacted_at)
        if compaction_version is not None:
            assignments.append(f"compaction_version = {self._dialect.bind}")
            params.append(int(compaction_version))
        if compactor_type is not None:
            assignments.append(f"compactor_type = {self._dialect.bind}")
            params.append(compactor_type)
        if compaction_state is not None:
            assignments.append(f"compaction_state_json = {self._dialect.bind}")
            params.append(json.dumps(compaction_state))
        if status is not None:
            assignments.append(f"status = {self._dialect.bind}")
            params.append(status)
        if latest_task_id is not None:
            assignments.append(f"latest_task_id = {self._dialect.bind}")
            params.append(latest_task_id)
        params.append(conversation_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE conversation_threads SET {', '.join(assignments)} WHERE conversation_id = {self._dialect.bind}",
                params,
            )
            conn.commit()

    def get_turn_by_task_id(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {TURN_SELECT_COLUMNS}
                FROM conversation_task_links
                WHERE task_id = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_turn(row) if row else None

    def get_latest_task_link(self, conversation_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT {TURN_SELECT_COLUMNS}
                FROM conversation_task_links
                WHERE conversation_id = {self._dialect.bind}
                ORDER BY turn_index DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._row_to_turn(row) if row else None

    def _row_to_turn(self, row: sqlite3.Row) -> dict[str, Any]:
        manifest_raw = row["tool_artifact_manifest_json"] or "[]"
        try:
            manifest = json.loads(manifest_raw)
        except json.JSONDecodeError:
            manifest = []
        artifact_summary_raw = row["artifact_summary_json"] or "[]"
        try:
            artifact_summary = json.loads(artifact_summary_raw)
        except json.JSONDecodeError:
            artifact_summary = []
        gateway_context_raw = row["gateway_context_json"] or "{}"
        try:
            gateway_context = json.loads(gateway_context_raw)
        except json.JSONDecodeError:
            gateway_context = {}
        return {
            "conversation_id": row["conversation_id"],
            "task_id": row["task_id"],
            "followup_to_task_id": row["followup_to_task_id"],
            "turn_index": row["turn_index"],
            "owner_canonical_user_id": row["owner_canonical_user_id"],
            "owner_agent_id": row["owner_agent_id"],
            "owner_named_agent_type": row["owner_named_agent_type"],
            "requester_name": row["requester_name"],
            "requester_id": row["requester_id"],
            "requester_type": row["requester_type"],
            "correlation_id": row["correlation_id"],
            "delivery_mode": row["delivery_mode"],
            "delivery_state": row["delivery_state"] or "not_applicable",
            "delivery_via": row["delivery_via"],
            "delivered_at": row["delivered_at"],
            "task_status": row["task_status"],
            "problem_text": row["problem_text"] or "",
            "final_answer_text": row["final_answer_text"] or "",
            "clarification_summary_text": row["clarification_summary_text"] or "",
            "execution_history_text": row["execution_history_text"] or "",
            "turn_summary_text": row["turn_summary_text"] or "",
            "user_request_summary": row["user_request_summary"] or "",
            "assistant_response_summary": row["assistant_response_summary"] or "",
            "context_summary_text": row["context_summary_text"] or "",
            "artifact_summary": artifact_summary,
            "summary_generator": row["summary_generator"] or "",
            "summary_generated_at": row["summary_generated_at"],
            "tool_artifact_manifest": manifest,
            "gateway_context": gateway_context if isinstance(gateway_context, dict) else {},
            "memory_review_status": row["memory_review_status"] or TURN_MEMORY_REVIEW_UNREVIEWED,
            "memory_review_job_id": row["memory_review_job_id"],
            "memory_review_queued_at_utc": row["memory_review_queued_at_utc"],
            "memory_reviewed_at_utc": row["memory_reviewed_at_utc"],
            "memory_reviewed_by_agent_id": row["memory_reviewed_by_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def append_turn(
        self,
        *,
        conversation_id: str,
        task_id: str,
        followup_to_task_id: Optional[str],
        owner_agent_id: Optional[str],
        requester_name: Optional[str],
        requester_id: Optional[str],
        correlation_id: Optional[str],
        problem_text: str,
        owner_named_agent_type: Optional[str] = None,
        requester_type: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        delivery_state: Optional[str] = None,
        gateway_context: Optional[dict[str, Any]] = None,
        task_status: str = "accepted",
        memory_review_status: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = self.get_turn_by_task_id(task_id)
        if existing:
            return existing

        initial_memory_review_status = _memory_review_initial_status(
            memory_review_status=memory_review_status,
            requester_type=requester_type,
            owner_named_agent_type=owner_named_agent_type,
            problem_text=problem_text,
        )
        # Only the caller's verified owner counts, never the gateway context's
        # claim (custom_agents.conversation_runtime).
        resolved_owner = str(owner_canonical_user_id or "").strip() or None

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(turn_index), 0) FROM conversation_task_links WHERE conversation_id = {self._dialect.bind}",
                (conversation_id,),
            ).fetchone()
            next_turn_index = int((row[0] or 0) + 1)
            now_ts = utc_now_iso()
            conn.execute(
                f"""
                INSERT INTO conversation_task_links (
                    conversation_id, task_id, followup_to_task_id, turn_index,
                    owner_canonical_user_id,
                    owner_agent_id, owner_named_agent_type, requester_name, requester_id, requester_type,
                    correlation_id, delivery_mode, delivery_state, delivery_via, delivered_at,
                    task_status, problem_text, final_answer_text, clarification_summary_text,
                    execution_history_text, turn_summary_text, user_request_summary,
                    assistant_response_summary, context_summary_text, artifact_summary_json,
                    summary_generator, summary_generated_at, tool_artifact_manifest_json,
                    gateway_context_json,
                    memory_review_status,
                    created_at, updated_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    conversation_id,
                    task_id,
                    followup_to_task_id,
                    next_turn_index,
                    resolved_owner,
                    owner_agent_id,
                    owner_named_agent_type,
                    requester_name,
                    requester_id,
                    enum_value(requester_type),
                    correlation_id,
                    delivery_mode,
                    delivery_state or "not_applicable",
                    None,
                    None,
                    task_status,
                    problem_text,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "[]",
                    "",
                    None,
                    "[]",
                    json.dumps(gateway_context or {}),
                    initial_memory_review_status,
                    now_ts,
                    now_ts,
                ),
            )
            conn.execute(
                f"""
                UPDATE conversation_threads
                SET latest_task_id = {self._dialect.bind},
                    updated_at = {self._dialect.bind},
                    status = {self._dialect.bind},
                    root_task_id = COALESCE(root_task_id, {self._dialect.bind}),
                    owner_canonical_user_id = COALESCE(owner_canonical_user_id, {self._dialect.bind})
                WHERE conversation_id = {self._dialect.bind}
                """,
                (task_id, now_ts, task_status, task_id, resolved_owner, conversation_id),
            )
            conn.commit()
        return self.get_turn_by_task_id(task_id) or {}

    def append_clarification(
        self,
        *,
        task_id: str,
        message: str,
    ) -> None:
        turn = self.get_turn_by_task_id(task_id)
        if not turn:
            return
        existing = turn.get("clarification_summary_text") or ""
        merged = existing.strip()
        entry = message.strip()
        if entry:
            merged = f"{merged}\n{entry}".strip() if merged else entry
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE conversation_task_links
                SET clarification_summary_text = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE task_id = {self._dialect.bind}
                """,
                (merged, utc_now_iso(), task_id),
            )
            conn.commit()

    def update_turn_completion(
        self,
        *,
        task_id: str,
        task_status: str,
        final_answer_text: str,
        turn_summary_text: str,
        clarification_summary_text: Optional[str] = None,
        execution_history_text: Optional[str] = None,
        tool_artifact_manifest: Optional[list[dict[str, Any]]] = None,
        user_request_summary: Optional[str] = None,
        assistant_response_summary: Optional[str] = None,
        context_summary_text: Optional[str] = None,
        artifact_summary: Optional[list[dict[str, Any]]] = None,
        summary_generator: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        turn = self.get_turn_by_task_id(task_id)
        if not turn:
            return None
        manifest_json = json.dumps(tool_artifact_manifest or turn.get("tool_artifact_manifest") or [])
        clarification_text = (
            clarification_summary_text
            if clarification_summary_text is not None
            else turn.get("clarification_summary_text", "")
        )
        history_text = (
            execution_history_text
            if execution_history_text is not None
            else turn.get("execution_history_text", "")
        )
        artifact_summary_json = json.dumps(artifact_summary if artifact_summary is not None else turn.get("artifact_summary") or [])
        generated_at = utc_now_iso() if summary_generator or user_request_summary or assistant_response_summary or context_summary_text or artifact_summary else turn.get("summary_generated_at")
        now_ts = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE conversation_task_links
                SET task_status = {self._dialect.bind}, final_answer_text = {self._dialect.bind}, clarification_summary_text = {self._dialect.bind},
                    execution_history_text = {self._dialect.bind}, turn_summary_text = {self._dialect.bind}, user_request_summary = {self._dialect.bind},
                    assistant_response_summary = {self._dialect.bind}, context_summary_text = {self._dialect.bind}, artifact_summary_json = {self._dialect.bind},
                    summary_generator = {self._dialect.bind}, summary_generated_at = {self._dialect.bind}, tool_artifact_manifest_json = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE task_id = {self._dialect.bind}
                """,
                (
                    task_status,
                    final_answer_text or "",
                    clarification_text or "",
                    history_text or "",
                    turn_summary_text or "",
                    user_request_summary if user_request_summary is not None else turn.get("user_request_summary", ""),
                    assistant_response_summary if assistant_response_summary is not None else turn.get("assistant_response_summary", ""),
                    context_summary_text if context_summary_text is not None else turn.get("context_summary_text", ""),
                    artifact_summary_json,
                    summary_generator if summary_generator is not None else turn.get("summary_generator", ""),
                    generated_at,
                    manifest_json,
                    now_ts,
                    task_id,
                ),
            )
            conn.execute(
                f"""
                UPDATE conversation_threads
                SET latest_task_id = {self._dialect.bind}, updated_at = {self._dialect.bind}, status = {self._dialect.bind}
                WHERE conversation_id = {self._dialect.bind}
                """,
                (task_id, now_ts, task_status, turn["conversation_id"]),
            )
            conn.commit()
        return self.get_turn_by_task_id(task_id)

    def update_turn_delivery(
        self,
        *,
        task_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        delivery_state: str,
        delivery_via: Optional[str] = None,
        delivered_at: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not task_id and not (conversation_id and correlation_id):
            raise ValueError("update_turn_delivery requires task_id or conversation_id + correlation_id")

        with self._connect() as conn:
            row = None
            if task_id:
                row = conn.execute(
                    f"""
                    SELECT conversation_id, task_id
                    FROM conversation_task_links
                    WHERE task_id = {self._dialect.bind}
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
            if row is None and conversation_id and correlation_id:
                row = conn.execute(
                    f"""
                    SELECT conversation_id, task_id
                    FROM conversation_task_links
                    WHERE conversation_id = {self._dialect.bind} AND correlation_id = {self._dialect.bind}
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (conversation_id, correlation_id),
                ).fetchone()
            if row is None:
                return None

            conn.execute(
                f"""
                UPDATE conversation_task_links
                SET delivery_state = {self._dialect.bind}, delivery_via = {self._dialect.bind}, delivered_at = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE conversation_id = {self._dialect.bind} AND task_id = {self._dialect.bind}
                """,
                (
                    delivery_state,
                    delivery_via,
                    delivered_at,
                    utc_now_iso(),
                    row["conversation_id"],
                    row["task_id"],
                ),
            )
            conn.commit()
        return self.get_turn_by_task_id(row["task_id"])

    def list_conversations(
        self,
        *,
        limit: int = 10,
        owner_canonical_user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        query = """
            SELECT conversation_id, team_id, owner_canonical_user_id, owner_agent_id, owner_agent_name,
                   requester_name, requester_id, root_task_id, latest_task_id,
                   status, subject_preview, created_at, updated_at,
                   synopsis_markdown, summarized_through_turn_index, last_token_estimate,
                   compactor_type
            FROM conversation_threads
        """
        params: list[Any] = []
        if owner_canonical_user_id is not None:
            query += f" WHERE owner_canonical_user_id = {self._dialect.bind}"
            params.append(owner_canonical_user_id)
        query += f" ORDER BY updated_at DESC LIMIT {self._dialect.bind}"
        params.append(safe_limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        conversations: list[dict[str, Any]] = []
        for row in rows:
            conversations.append(
                {
                    "conversation_id": row["conversation_id"],
                    "team_id": row["team_id"],
                    "owner_canonical_user_id": row["owner_canonical_user_id"],
                    "owner_agent_id": row["owner_agent_id"],
                    "owner_agent_name": row["owner_agent_name"],
                    "requester_name": row["requester_name"],
                    "requester_id": row["requester_id"],
                    "root_task_id": row["root_task_id"],
                    "latest_task_id": row["latest_task_id"],
                    "status": row["status"],
                    "subject_preview": row["subject_preview"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "has_synopsis": bool(row["synopsis_markdown"]),
                    "summarized_through_turn_index": row["summarized_through_turn_index"] or 0,
                    "last_token_estimate": row["last_token_estimate"] or 0,
                    "compactor_type": row["compactor_type"],
                }
            )
        return conversations

    def get_conversation_turns(
        self,
        *,
        conversation_id: str,
        start_turn: int = 1,
        limit: int = 10,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        safe_start = max(1, start_turn)
        safe_limit = max(1, min(limit, 200))
        normalized_after, after_dt, after_op = _normalize_created_at_filter(
            created_after,
            boundary="created_after",
        )
        normalized_before, before_dt, before_op = _normalize_created_at_filter(
            created_before,
            boundary="created_before",
        )
        if after_dt is not None and before_dt is not None and after_dt >= before_dt:
            raise ValueError("created_after must be earlier than created_before.")

        query = f"""
            SELECT {{columns}}
            FROM conversation_task_links
            WHERE conversation_id = {self._dialect.bind} AND turn_index >= {self._dialect.bind}
        """.format(columns=TURN_SELECT_COLUMNS)
        params: list[Any] = [conversation_id, safe_start]
        if normalized_after:
            query += f" AND created_at {after_op} {self._dialect.bind}"
            params.append(normalized_after)
        if normalized_before:
            query += f" AND created_at {before_op} {self._dialect.bind}"
            params.append(normalized_before)
        query += f" ORDER BY turn_index ASC LIMIT {self._dialect.bind}"
        params.append(safe_limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def get_recent_turns(
        self,
        conversation_id: str,
        limit: Optional[int] = None,
        *,
        include_pending_delivery: bool = True,
    ) -> list[dict[str, Any]]:
        query = f"""
            SELECT {{columns}}
            FROM conversation_task_links
            WHERE conversation_id = {self._dialect.bind}
        """.format(columns=TURN_SELECT_COLUMNS)
        params: list[Any] = [conversation_id]
        if not include_pending_delivery:
            visible_states = sorted(MODEL_VISIBLE_TIMER_DELIVERY_STATES)
            placeholders = ", ".join([f"{self._dialect.bind}"] * len(visible_states))
            query += """
            AND (
                requester_type IS NULL
                OR requester_type != 'timer_req'
                OR delivery_state IN ({placeholders})
            )
            """.format(placeholders=placeholders)
            params.extend(visible_states)
        if limit is not None:
            query = (
                "SELECT * FROM ("
                + query
                + f" ORDER BY turn_index DESC, created_at DESC, task_id DESC LIMIT {self._dialect.bind}"
                + ") AS recent_turns ORDER BY turn_index ASC, created_at ASC, task_id ASC"
            )
            params.append(max(1, limit))
        else:
            query += " ORDER BY turn_index ASC, created_at ASC, task_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def get_memory_review_window(
        self,
        *,
        conversation_id: str,
        max_unreviewed_turns: int,
        overlap_turns: int = 0,
        allow_partial: bool = False,
    ) -> Optional[dict[str, Any]]:
        safe_max = max(1, min(int(max_unreviewed_turns or 1), 200))
        safe_overlap = max(0, min(int(overlap_turns or 0), 200))
        with self._connect() as conn:
            unreviewed_rows = conn.execute(
                f"""
                SELECT turn_index
                FROM conversation_task_links
                WHERE conversation_id = {self._dialect.bind}
                  AND task_status = 'completed'
                  AND memory_review_status = {self._dialect.bind}
{_memory_review_eligible_turn_sql(self._dialect.bind)}
                ORDER BY turn_index ASC
                LIMIT {self._dialect.bind}
                """,
                (
                    conversation_id,
                    TURN_MEMORY_REVIEW_UNREVIEWED,
                    MEMORY_REVIEW_PROMPT_LIKE_PATTERN,
                    safe_max,
                ),
            ).fetchall()
        if not unreviewed_rows:
            return None
        if not allow_partial and len(unreviewed_rows) < safe_max:
            return None

        unreviewed_indexes = [int(row["turn_index"]) for row in unreviewed_rows]
        first_unreviewed = unreviewed_indexes[0]
        last_unreviewed = unreviewed_indexes[-1]
        window_start = max(1, first_unreviewed - safe_overlap)
        window_end = last_unreviewed
        turns = self.get_conversation_turns(
            conversation_id=conversation_id,
            start_turn=window_start,
            limit=(window_end - window_start) + 1,
        )
        turns = [turn for turn in turns if _is_memory_review_source_turn(turn)]
        return {
            "conversation_id": conversation_id,
            "window_start_turn": window_start,
            "window_end_turn": window_end,
            "first_unreviewed_turn": first_unreviewed,
            "last_unreviewed_turn": last_unreviewed,
            "unreviewed_turn_indexes": unreviewed_indexes,
            "unreviewed_count": len(unreviewed_indexes),
            "overlap_turns": max(0, first_unreviewed - window_start),
            "turns": turns,
        }

    def list_conversation_memory_review_candidates(
        self,
        *,
        team_id: Optional[str] = None,
        stale_before_utc: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 500))
        query = f"""
            SELECT
                ct.conversation_id,
                ct.team_id,
                ct.latest_task_id,
                ct.status,
                ct.subject_preview,
                ct.updated_at,
                COUNT(tl.turn_index) AS unreviewed_count,
                MIN(tl.turn_index) AS first_unreviewed_turn,
                MAX(tl.turn_index) AS last_unreviewed_turn
            FROM conversation_threads ct
            JOIN conversation_task_links tl ON tl.conversation_id = ct.conversation_id
            WHERE tl.task_status = 'completed'
              AND tl.memory_review_status = {self._dialect.bind}
{_memory_review_eligible_turn_sql(self._dialect.bind)}
        """
        params: list[Any] = [
            TURN_MEMORY_REVIEW_UNREVIEWED,
            MEMORY_REVIEW_PROMPT_LIKE_PATTERN,
        ]
        if team_id:
            query += f" AND ct.team_id = {self._dialect.bind}"
            params.append(team_id)
        if canonical_user_id:
            query += f" AND ct.owner_canonical_user_id = {self._dialect.bind}"
            params.append(canonical_user_id)
        if stale_before_utc:
            query += f" AND ct.updated_at <= {self._dialect.bind}"
            params.append(stale_before_utc)
        query += f"""
            GROUP BY ct.conversation_id, ct.team_id, ct.latest_task_id, ct.status, ct.subject_preview, ct.updated_at
            ORDER BY ct.updated_at ASC, ct.conversation_id ASC
            LIMIT {self._dialect.bind}
        """
        params.append(safe_limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            {
                "conversation_id": row["conversation_id"],
                "team_id": row["team_id"],
                "latest_task_id": row["latest_task_id"],
                "status": row["status"],
                "subject_preview": row["subject_preview"],
                "updated_at": row["updated_at"],
                "unreviewed_count": int(row["unreviewed_count"] or 0),
                "first_unreviewed_turn": int(row["first_unreviewed_turn"] or 0),
                "last_unreviewed_turn": int(row["last_unreviewed_turn"] or 0),
            }
            for row in rows
        ]

    def mark_turns_memory_review_queued(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
    ) -> int:
        now_ts = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_task_links
                SET memory_review_status = {self._dialect.bind},
                    memory_review_job_id = {self._dialect.bind},
                    memory_review_queued_at_utc = {self._dialect.bind},
                    updated_at = {self._dialect.bind}
                WHERE conversation_id = {self._dialect.bind}
                  AND turn_index BETWEEN {self._dialect.bind} AND {self._dialect.bind}
                  AND task_status = 'completed'
                  AND memory_review_status IN ({self._dialect.bind}, {self._dialect.bind})
{_memory_review_eligible_turn_sql(self._dialect.bind)}
                """,
                (
                    TURN_MEMORY_REVIEW_QUEUED,
                    review_job_id,
                    now_ts,
                    now_ts,
                    conversation_id,
                    int(start_turn),
                    int(end_turn),
                    TURN_MEMORY_REVIEW_UNREVIEWED,
                    TURN_MEMORY_REVIEW_FAILED,
                    MEMORY_REVIEW_PROMPT_LIKE_PATTERN,
                ),
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def mark_turns_memory_reviewed(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
        reviewed_by_agent_id: Optional[str] = None,
    ) -> int:
        now_ts = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_task_links
                SET memory_review_status = {self._dialect.bind},
                    memory_review_job_id = {self._dialect.bind},
                    memory_reviewed_at_utc = {self._dialect.bind},
                    memory_reviewed_by_agent_id = {self._dialect.bind},
                    updated_at = {self._dialect.bind}
                WHERE conversation_id = {self._dialect.bind}
                  AND turn_index BETWEEN {self._dialect.bind} AND {self._dialect.bind}
                  AND task_status = 'completed'
                  AND (
                        memory_review_job_id = {self._dialect.bind}
                        OR memory_review_status IN ({self._dialect.bind}, {self._dialect.bind})
                  )
{_memory_review_eligible_turn_sql(self._dialect.bind)}
                """,
                (
                    TURN_MEMORY_REVIEW_REVIEWED,
                    review_job_id,
                    now_ts,
                    reviewed_by_agent_id,
                    now_ts,
                    conversation_id,
                    int(start_turn),
                    int(end_turn),
                    review_job_id,
                    TURN_MEMORY_REVIEW_UNREVIEWED,
                    TURN_MEMORY_REVIEW_FAILED,
                    MEMORY_REVIEW_PROMPT_LIKE_PATTERN,
                ),
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def mark_turns_memory_review_failed(
        self,
        *,
        conversation_id: str,
        start_turn: int,
        end_turn: int,
        review_job_id: str,
    ) -> int:
        now_ts = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_task_links
                SET memory_review_status = {self._dialect.bind},
                    updated_at = {self._dialect.bind}
                WHERE conversation_id = {self._dialect.bind}
                  AND turn_index BETWEEN {self._dialect.bind} AND {self._dialect.bind}
                  AND task_status = 'completed'
                  AND memory_review_job_id = {self._dialect.bind}
                """,
                (
                    TURN_MEMORY_REVIEW_FAILED,
                    now_ts,
                    conversation_id,
                    int(start_turn),
                    int(end_turn),
                    review_job_id,
                ),
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def tag_thread(
        self,
        *,
        conversation_id: str,
        tag: str,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        source: str = "manual",
        created_by_agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_tag = normalize_topic_tag(tag)
        thread = self.get_thread(conversation_id)
        if thread is None:
            raise ValueError(f"Conversation {conversation_id!r} was not found.")
        resolved_team_id = team_id if team_id is not None else thread.get("team_id")
        resolved_user_id = str(canonical_user_id or "").strip()
        now_ts = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                f"""
                SELECT conversation_id, tag
                FROM conversation_thread_tags
                WHERE conversation_id = {self._dialect.bind} AND tag = {self._dialect.bind}
                """,
                (conversation_id, normalized_tag),
            ).fetchone()
            if existing is None:
                conn.execute(
                    f"""
                    INSERT INTO conversation_thread_tags (
                        conversation_id, tag, team_id, canonical_user_id, source, status,
                        created_by_agent_id, created_at, updated_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        conversation_id,
                        normalized_tag,
                        resolved_team_id,
                        resolved_user_id,
                        source or "manual",
                        "active",
                        created_by_agent_id,
                        now_ts,
                        now_ts,
                    ),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE conversation_thread_tags
                    SET team_id = {self._dialect.bind}, canonical_user_id = {self._dialect.bind}, source = {self._dialect.bind}, status = {self._dialect.bind},
                        created_by_agent_id = {self._dialect.bind}, updated_at = {self._dialect.bind}
                    WHERE conversation_id = {self._dialect.bind} AND tag = {self._dialect.bind}
                    """,
                    (
                        resolved_team_id,
                        resolved_user_id,
                        source or "manual",
                        "active",
                        created_by_agent_id,
                        now_ts,
                        conversation_id,
                        normalized_tag,
                    ),
                )
            conn.commit()
        tags = self.list_thread_tags(conversation_id)
        return next(item for item in tags if item["tag"] == normalized_tag)

    def untag_thread(self, *, conversation_id: str, tag: str) -> bool:
        normalized_tag = normalize_topic_tag(tag)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE conversation_thread_tags
                SET status = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE conversation_id = {self._dialect.bind} AND tag = {self._dialect.bind} AND status = {self._dialect.bind}
                """,
                ("inactive", utc_now_iso(), conversation_id, normalized_tag, "active"),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def list_thread_tags(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT conversation_id, tag, team_id, canonical_user_id, source, status,
                       created_by_agent_id, created_at, updated_at
                FROM conversation_thread_tags
                WHERE conversation_id = {self._dialect.bind} AND status = {self._dialect.bind}
                ORDER BY tag ASC
                """,
                (conversation_id, "active"),
            ).fetchall()
        return [self._row_to_topic_tag(row) for row in rows]

    def list_known_thread_tags(
        self,
        *,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 200), 1000))
        query = f"""
            SELECT tt.tag, COUNT(DISTINCT tt.conversation_id) AS thread_count, MAX(tt.updated_at) AS updated_at
            FROM conversation_thread_tags tt
            LEFT JOIN conversation_threads ct ON ct.conversation_id = tt.conversation_id
            WHERE tt.status = {self._dialect.bind}
        """
        params: list[Any] = ["active"]
        if team_id:
            query += f" AND (tt.team_id = {self._dialect.bind} OR ct.team_id = {self._dialect.bind})"
            params.extend([team_id, team_id])
        if canonical_user_id:
            query += f" AND ct.owner_canonical_user_id = {self._dialect.bind}"
            params.append(canonical_user_id)
            query += f" AND (tt.canonical_user_id = '' OR tt.canonical_user_id = {self._dialect.bind})"
            params.append(canonical_user_id)
        else:
            query += " AND tt.canonical_user_id = ''"
        query += f" GROUP BY tt.tag ORDER BY updated_at DESC, tt.tag ASC LIMIT {self._dialect.bind}"
        params.append(safe_limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "tag": row["tag"],
                "thread_count": int(row["thread_count"] or 0),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def find_threads_by_tags(
        self,
        *,
        tags: list[str],
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized_tags = []
        seen: set[str] = set()
        for tag in tags:
            normalized = normalize_topic_tag(tag)
            if normalized not in seen:
                normalized_tags.append(normalized)
                seen.add(normalized)
        if not normalized_tags:
            return []
        placeholders = ", ".join(f"{self._dialect.bind}" for _ in normalized_tags)
        safe_limit = max(1, min(int(limit or 10), 100))
        query = f"""
            SELECT tt.tag, ct.conversation_id, ct.team_id, ct.owner_agent_name,
                   ct.requester_name, ct.requester_id, ct.latest_task_id, ct.status,
                   ct.subject_preview, ct.updated_at, ct.synopsis_markdown
            FROM conversation_thread_tags tt
            JOIN conversation_threads ct ON ct.conversation_id = tt.conversation_id
            WHERE tt.status = {self._dialect.bind}
              AND tt.tag IN ({placeholders})
        """
        params: list[Any] = ["active", *normalized_tags]
        if team_id:
            query += f" AND (tt.team_id = {self._dialect.bind} OR ct.team_id = {self._dialect.bind})"
            params.extend([team_id, team_id])
        if canonical_user_id:
            query += f" AND ct.owner_canonical_user_id = {self._dialect.bind}"
            params.append(canonical_user_id)
            query += f" AND (tt.canonical_user_id = '' OR tt.canonical_user_id = {self._dialect.bind})"
            params.append(canonical_user_id)
        else:
            query += " AND tt.canonical_user_id = ''"
        query += " ORDER BY ct.updated_at DESC, ct.conversation_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            conversation_id = row["conversation_id"]
            item = grouped.setdefault(
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "team_id": row["team_id"],
                    "owner_agent_name": row["owner_agent_name"],
                    "requester_name": row["requester_name"],
                    "requester_id": row["requester_id"],
                    "latest_task_id": row["latest_task_id"],
                    "status": row["status"],
                    "subject_preview": row["subject_preview"],
                    "updated_at": row["updated_at"],
                    "synopsis_markdown": row["synopsis_markdown"] or "",
                    "matched_tags": [],
                },
            )
            item["matched_tags"].append(row["tag"])
        return list(grouped.values())[:safe_limit]

    def tag_turn(
        self,
        *,
        conversation_id: str,
        turn_index: int,
        tag: str,
        team_id: Optional[str] = None,
        canonical_user_id: Optional[str] = None,
        source: str = "manual",
        created_by_agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_tag = normalize_topic_tag(tag)
        now_ts = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                f"""
                SELECT conversation_id, turn_index, tag
                FROM conversation_turn_tags
                WHERE conversation_id = {self._dialect.bind} AND turn_index = {self._dialect.bind} AND tag = {self._dialect.bind}
                """,
                (conversation_id, int(turn_index), normalized_tag),
            ).fetchone()
            if existing is None:
                conn.execute(
                    f"""
                    INSERT INTO conversation_turn_tags (
                        conversation_id, turn_index, tag, team_id, canonical_user_id, source,
                        status, created_by_agent_id, created_at, updated_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        conversation_id,
                        int(turn_index),
                        normalized_tag,
                        team_id,
                        str(canonical_user_id or "").strip(),
                        source or "manual",
                        "active",
                        created_by_agent_id,
                        now_ts,
                        now_ts,
                    ),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE conversation_turn_tags
                    SET team_id = {self._dialect.bind}, canonical_user_id = {self._dialect.bind}, source = {self._dialect.bind}, status = {self._dialect.bind},
                        created_by_agent_id = {self._dialect.bind}, updated_at = {self._dialect.bind}
                    WHERE conversation_id = {self._dialect.bind} AND turn_index = {self._dialect.bind} AND tag = {self._dialect.bind}
                    """,
                    (
                        team_id,
                        str(canonical_user_id or "").strip(),
                        source or "manual",
                        "active",
                        created_by_agent_id,
                        now_ts,
                        conversation_id,
                        int(turn_index),
                        normalized_tag,
                    ),
                )
            conn.commit()
        tags = self.list_turn_tags(conversation_id=conversation_id, turn_index=int(turn_index))
        return next(item for item in tags if item["tag"] == normalized_tag)

    def list_turn_tags(self, *, conversation_id: str, turn_index: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT conversation_id, turn_index, tag, team_id, canonical_user_id, source, status,
                       created_by_agent_id, created_at, updated_at
                FROM conversation_turn_tags
                WHERE conversation_id = {self._dialect.bind} AND turn_index = {self._dialect.bind} AND status = {self._dialect.bind}
                ORDER BY tag ASC
                """,
                (conversation_id, int(turn_index), "active"),
            ).fetchall()
        return [self._row_to_topic_tag(row) for row in rows]

    @staticmethod
    def _row_to_topic_tag(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        if "turn_index" in payload and payload["turn_index"] is not None:
            payload["turn_index"] = int(payload["turn_index"])
        return payload

    def offload_large_text(
        self,
        *,
        conversation_id: str,
        task_id: str,
        artifact_type: str,
        raw_text: str,
        summary_text: str,
        extension: str = "txt",
    ) -> dict[str, Any]:
        artifact_id = new_id("artifact")
        conv_dir = self._artifacts_dir / conversation_id
        conv_dir.mkdir(parents=True, exist_ok=True)
        file_path = conv_dir / f"{task_id}_{artifact_id}.{extension}"
        file_path.write_text(raw_text, encoding="utf-8")
        now_ts = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO conversation_artifacts (
                    artifact_id, conversation_id, task_id, artifact_type,
                    storage_path, summary_text, created_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (artifact_id, conversation_id, task_id, artifact_type, str(file_path), summary_text, now_ts),
            )
            conn.commit()
        return {
            "artifact_id": artifact_id,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "artifact_type": artifact_type,
            "storage_path": str(file_path),
            "summary_text": summary_text,
            "created_at": now_ts,
        }

    def delete_artifacts_created_before(self, cutoff: str, *, limit: int) -> tuple[str, ...]:
        """Delete up to ``limit`` artifact records created before ``cutoff``, oldest first.

        ``cutoff`` is an ISO 8601 UTC time in the same form as ``created_at``
        (``utc_now_iso``), so the text comparison orders them by time.
        Returns the files the records named, for the caller to delete
        (``custom_agents.output_retention``): the record goes first, so a file
        that cannot be removed is left without a record, which the cleanup
        removes by its age later, never a record without its file. The turns
        that mention an artifact keep its summary.
        """

        bind = self._dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                DELETE FROM conversation_artifacts
                WHERE artifact_id IN (
                    SELECT artifact_id FROM conversation_artifacts
                    WHERE created_at < {bind}
                    ORDER BY created_at
                    LIMIT {bind}
                )
                RETURNING storage_path
                """,
                (cutoff, int(limit)),
            ).fetchall()
            conn.commit()
        return tuple(str(row[0] or "") for row in rows)

    def count_artifacts_created_before(self, cutoff: str) -> int:
        """How many artifact records ``delete_artifacts_created_before`` would delete (a dry run)."""

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM conversation_artifacts WHERE created_at < {self._dialect.bind}",
                (cutoff,),
            ).fetchone()
        return int(row[0]) if row else 0

    def create_context_epoch(
        self,
        *,
        conversation_id: str,
        trigger: str,
        through_turn_index: int,
        preserved_suffix_start_turn: int,
        latest_task_id: Optional[str] = None,
        summary_json: Optional[dict[str, Any]] = None,
        summary_markdown: Optional[str] = None,
        rehydration_manifest: Optional[dict[str, Any]] = None,
        source_lengths_before: Optional[list[dict[str, Any]]] = None,
        source_lengths_after: Optional[list[dict[str, Any]]] = None,
        pre_compact_estimated_tokens: Optional[int] = None,
        post_compact_estimated_tokens: Optional[int] = None,
        compactor_type: Optional[str] = None,
        created_by_agent_id: Optional[str] = None,
        status: str = "active",
        error_text: Optional[str] = None,
        owner_canonical_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now_ts = utc_now_iso()
        context_epoch_id = new_id("context_epoch")
        resolved_owner = str(owner_canonical_user_id or "").strip() or None
        if resolved_owner is None:
            thread = self.get_thread(conversation_id) or {}
            resolved_owner = str(thread.get("owner_canonical_user_id") or "").strip() or None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(epoch_index), 0) FROM conversation_context_epochs WHERE conversation_id = {self._dialect.bind}",
                (conversation_id,),
            ).fetchone()
            epoch_index = int((row[0] or 0) + 1)
            conn.execute(
                f"""
                INSERT INTO conversation_context_epochs (
                    context_epoch_id, conversation_id, epoch_index, owner_canonical_user_id, trigger, status,
                    through_turn_index, preserved_suffix_start_turn, latest_task_id,
                    summary_json, summary_markdown, rehydration_manifest_json,
                    source_lengths_before_json, source_lengths_after_json,
                    pre_compact_estimated_tokens, post_compact_estimated_tokens,
                    compactor_type, created_by_agent_id, created_at, updated_at, error_text
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                """,
                (
                    context_epoch_id,
                    conversation_id,
                    epoch_index,
                    resolved_owner,
                    trigger,
                    status or "active",
                    int(through_turn_index),
                    int(preserved_suffix_start_turn),
                    latest_task_id,
                    json.dumps(summary_json or {}),
                    summary_markdown or "",
                    json.dumps(rehydration_manifest or {}),
                    json.dumps(source_lengths_before or []),
                    json.dumps(source_lengths_after or []),
                    int(pre_compact_estimated_tokens) if pre_compact_estimated_tokens is not None else None,
                    int(post_compact_estimated_tokens) if post_compact_estimated_tokens is not None else None,
                    compactor_type,
                    created_by_agent_id,
                    now_ts,
                    now_ts,
                    error_text,
                ),
            )
            conn.commit()
        return self.get_context_epoch(context_epoch_id) or {}

    def get_latest_context_epoch(self, conversation_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT context_epoch_id, conversation_id, epoch_index, trigger, status,
                       owner_canonical_user_id,
                       through_turn_index, preserved_suffix_start_turn, latest_task_id,
                       summary_json, summary_markdown, rehydration_manifest_json,
                       source_lengths_before_json, source_lengths_after_json,
                       pre_compact_estimated_tokens, post_compact_estimated_tokens,
                       compactor_type, created_by_agent_id, created_at, updated_at, error_text
                FROM conversation_context_epochs
                WHERE conversation_id = {self._dialect.bind} AND status = 'active'
                ORDER BY epoch_index DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._row_to_context_epoch(row) if row else None

    def list_context_epochs(self, conversation_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 20), 100))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT context_epoch_id, conversation_id, epoch_index, trigger, status,
                       owner_canonical_user_id,
                       through_turn_index, preserved_suffix_start_turn, latest_task_id,
                       summary_json, summary_markdown, rehydration_manifest_json,
                       source_lengths_before_json, source_lengths_after_json,
                       pre_compact_estimated_tokens, post_compact_estimated_tokens,
                       compactor_type, created_by_agent_id, created_at, updated_at, error_text
                FROM conversation_context_epochs
                WHERE conversation_id = {self._dialect.bind}
                ORDER BY epoch_index DESC
                LIMIT {self._dialect.bind}
                """,
                (conversation_id, safe_limit),
            ).fetchall()
        return [self._row_to_context_epoch(row) for row in rows]

    def get_context_epoch(self, context_epoch_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT context_epoch_id, conversation_id, epoch_index, trigger, status,
                       owner_canonical_user_id,
                       through_turn_index, preserved_suffix_start_turn, latest_task_id,
                       summary_json, summary_markdown, rehydration_manifest_json,
                       source_lengths_before_json, source_lengths_after_json,
                       pre_compact_estimated_tokens, post_compact_estimated_tokens,
                       compactor_type, created_by_agent_id, created_at, updated_at, error_text
                FROM conversation_context_epochs
                WHERE context_epoch_id = {self._dialect.bind}
                """,
                (context_epoch_id,),
            ).fetchone()
        return self._row_to_context_epoch(row) if row else None

    def mark_context_epoch_failed(self, context_epoch_id: str, *, error_text: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE conversation_context_epochs
                SET status = {self._dialect.bind}, error_text = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE context_epoch_id = {self._dialect.bind}
                """,
                ("failed", error_text, utc_now_iso(), context_epoch_id),
            )
            conn.commit()
        return self.get_context_epoch(context_epoch_id)

    def _row_to_context_epoch(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "context_epoch_id": row["context_epoch_id"],
            "conversation_id": row["conversation_id"],
            "epoch_index": row["epoch_index"],
            "owner_canonical_user_id": row["owner_canonical_user_id"],
            "trigger": row["trigger"],
            "status": row["status"] or "active",
            "through_turn_index": row["through_turn_index"] or 0,
            "preserved_suffix_start_turn": row["preserved_suffix_start_turn"] or 1,
            "latest_task_id": row["latest_task_id"],
            "summary_json": _json_loads(row["summary_json"], {}),
            "summary_markdown": row["summary_markdown"] or "",
            "rehydration_manifest": _json_loads(row["rehydration_manifest_json"], {}),
            "source_lengths_before": _json_loads(row["source_lengths_before_json"], []),
            "source_lengths_after": _json_loads(row["source_lengths_after_json"], []),
            "pre_compact_estimated_tokens": row["pre_compact_estimated_tokens"],
            "post_compact_estimated_tokens": row["post_compact_estimated_tokens"],
            "compactor_type": row["compactor_type"],
            "created_by_agent_id": row["created_by_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error_text": row["error_text"],
        }


class SqliteConversationRepository(_RelationalConversationRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(
        self,
        db_path: str,
        *,
        artifacts_dir: str | Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._artifacts_dir = Path(artifacts_dir)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger, row_factory=sqlite3.Row)


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        ensure_sqlite_column(conn, table_name, column_name, column_type, logger=self._logger)


class PostgresConversationRepository(_RelationalConversationRepository):
    """Postgres implementation for durable conversation-thread persistence."""

    _dialect = POSTGRES

    def __init__(
        self,
        dsn: str,
        *,
        artifacts_dir: str | Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        self._artifacts_dir = Path(artifacts_dir)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)


    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
        conn.execute(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_type}')
        return


def create_conversation_repository(
    storage_settings: StorageSettings,
    *,
    artifacts_dir: str | Path,
    logger: Optional[logging.Logger] = None,
) -> ConversationRepository:
    """The conversation repository for this backend.

    ``artifacts_dir`` is the instance's ``InstanceLayout.conversation_artifacts_dir``
    (``dbs/conversation_artifacts``) for every backend; it is never guessed
    from the database path or the working directory.
    """

    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="ConversationRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteConversationRepository(
            storage_settings.sqlite_path,
            logger=logger,
            artifacts_dir=artifacts_dir,
        )
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresConversationRepository(
            storage_settings.postgres_dsn,
            logger=logger,
            artifacts_dir=artifacts_dir,
        )
    raise NotImplementedError(
        f"ConversationRepository has no implementation for backend {storage_settings.backend!r}."
    )
