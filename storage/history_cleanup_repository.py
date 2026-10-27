"""Orders to clean up one person's chat history, and the manager's part of carrying one out.

A person can let TeamLead delete their own old chats and finished tasks
(``custom_agents.history_cleanup``). The records live in two databases:

- the manager's (``task_history.db`` or its Postgres database): conversation
  turns (``conversation_task_links``), their summaries and tags, conversation
  artifacts and the files they name, memory-review jobs made from those turns,
  memory-graph rows, and finished tasks (``completed_tasks``);
- the gateway's: the chat transcripts and their copies (requests, deliveries,
  questions, attachments), which only the gateway reads and writes.

The manager never opens the gateway's database, so a clean-up is an order
record in this table, which both processes already open (the gateway keeps
departure holds and the access store here too). TeamLead's process stages the
order as an exact plan, deletes the manager's records once the plan is
approved, and leaves the order for the gateway, which deletes its own
records, sends the person a notice and closes the order
(``assistant_gateway.history_cleanup``). No message on the wire is needed.

Every deletion is bounded by the plan: by turn index for conversation turns
and by the time it was staged (``selected_at``) for everything else, so it can
run again after a failure and never touches what was written afterwards.

This module writes other repositories' tables directly, on purpose, as
``storage.person_departure_repository`` does: the manager's part is one
transaction over several tables of this database. Identifiers come only from
the constants below.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict

from access_control import safe_open
from access_control.safe_open import UnsafePath

from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

logger = logging.getLogger(__name__)

ORDERS_TABLE = "history_cleanup_orders"
# Finished and expired orders are kept this long, then removed: an order
# names conversation ids and counts only, and the access event log keeps the
# lasting record (bounded growth).
ORDER_RETENTION = timedelta(days=30)
# A turn still running (any status but a finished one) that changed within
# this time keeps its whole conversation out of a clean-up.
ACTIVE_TURN_WINDOW = timedelta(hours=24)
FINISHED_TURN_STATUSES = frozenset({"completed", "failed", "cancelled", "abandoned", "partial"})
# Conversation turn text counted for sizes (characters, which is close to bytes for most text).
_TURN_TEXT_COLUMNS = ("problem_text", "final_answer_text", "execution_history_text", "turn_summary_text")
_MEMORY_GRAPH_TASK_TABLES = (
    "task_turn_deltas",
    "task_state_snapshots",
    "task_handoff_events",
    "case_memories",
    "memory_episodes",
)
_BATCH = 400

# Said wherever a clean-up is described: memory is not part of it (owner's
# decision 2026-10-27); the person manages remembered facts under Memories.
MEMORIES_KEPT = "Remembered facts are kept; you manage them under Memories."


def size_words(chars: int) -> str:
    """A rough size for people: characters stored, in kB or MB."""

    if chars >= 1_000_000:
        return f"about {chars / 1_000_000:.1f} MB"
    if chars >= 1_000:
        return f"about {max(chars // 1_000, 1)} kB"
    return "under 1 kB"


What = Literal["conversations", "tasks", "both"]


class OrderStatus(StrEnum):
    PLANNED = "planned"  # staged; nothing deleted yet
    DELETING = "deleting"  # approved; the manager deletes its records (retried until done)
    WAITING_FOR_GATEWAY = "waiting_for_gateway"  # the manager's part is done; the gateway applies its own
    DONE = "done"  # both parts done and the person was told


class ConversationSelectionV1(BaseModel):
    """What a clean-up deletes of one conversation: its turns up to ``through_turn_index``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str
    through_turn_index: int
    # Every turn the conversation had when the plan was staged: the thread,
    # its tags and the gateway's pointers to it go too.
    whole: bool
    # When the first kept turn started (None when whole): the gateway keeps
    # this conversation's messages from then on.
    keep_from: str | None = None
    turns: int
    first_at: str
    last_at: str
    text_chars: int = 0
    # The task of the last deleted turn (the gateway's session pointer).
    latest_task_id: str | None = None
    # The gateway request of the first kept turn: its question was written
    # in the gateway just before the turn started here, so the gateway keeps
    # everything from that request's first record on.
    keep_correlation_id: str | None = None


class HistoryCleanupPlanV1(BaseModel):
    """One staged clean-up of one person's history (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    order_id: str
    plan_digest: str = ""
    team_id: str
    person_id: str
    actor_agent_id: str
    actor_agent_type: str
    source_task_id: str
    # person_approval (authorization on) or agreement (authorization off).
    authority_mode: Literal["person_approval", "agreement"]
    what: What
    older_than: date | None = None
    named_conversation_ids: tuple[str, ...] = ()
    created_at: str
    expires_at: str
    # The time the selection was made: nothing written later is deleted.
    selected_at: str
    # Conversations left out: the one the clean-up was asked in, and those
    # with work still running.
    excluded_conversation_ids: tuple[str, ...] = ()
    conversations: tuple[ConversationSelectionV1, ...] = ()
    task_count: int = 0
    task_chars: int = 0
    artifact_count: int = 0

    @property
    def deletes_conversations(self) -> bool:
        return self.what in ("conversations", "both")

    @property
    def deletes_tasks(self) -> bool:
        return self.what in ("tasks", "both")

    @property
    def cutoff_at(self) -> str | None:
        """The start of ``older_than`` (UTC), as stored timestamps are written."""

        if self.older_than is None:
            return None
        return datetime(self.older_than.year, self.older_than.month, self.older_than.day, tzinfo=timezone.utc).isoformat()

    @property
    def limit_at(self) -> str:
        """Rows from before this time are deleted: the cutoff, or the time the plan was staged."""

        cutoff = self.cutoff_at
        return min(cutoff, self.selected_at) if cutoff else self.selected_at


@dataclass
class HistoryCleanupOrder:
    """A stored order: the plan, its status and what each side deleted."""

    plan: HistoryCleanupPlanV1
    status: OrderStatus
    approval: dict[str, Any] = field(default_factory=dict)
    manager_result: dict[str, Any] = field(default_factory=dict)
    gateway_result: dict[str, Any] = field(default_factory=dict)
    artifact_paths: tuple[str, ...] = ()
    updated_at: str = ""


@dataclass
class TaskRow:
    task_id: str
    parent_task_id: str | None
    timestamp: str
    chars: int


@dataclass
class TurnRow:
    conversation_id: str
    subject_preview: str
    task_id: str
    turn_index: int
    task_status: str
    created_at: str
    updated_at: str
    chars: int
    # The gateway request the turn answers (the same id there).
    correlation_id: str | None = None


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _batches(values: Iterable[str]) -> Iterable[list[str]]:
    items = list(values)
    for start in range(0, len(items), _BATCH):
        yield items[start : start + _BATCH]


def select_conversation_turns(
    turns: Iterable[TurnRow],
    *,
    limit_at: str,
    selected_at: str,
    excluded: Iterable[str] = (),
    active_since: str | None = None,
) -> tuple[list[ConversationSelectionV1], list[str]]:
    """Which turns of each conversation a clean-up deletes, and which conversations it leaves out.

    ``turns`` are the person's turns in turn order per conversation. A
    conversation in ``excluded`` is left out; with ``active_since``, so is
    one with a turn still running that changed after it. Otherwise the turns
    started before ``limit_at`` are deleted, as a run from the first turn on:
    the turns kept are always the newest. ``selected_at`` is when the plan
    was staged; turns started later make the conversation partly kept.
    """

    excluded_ids = set(excluded)
    grouped: dict[str, list[TurnRow]] = {}
    for turn in turns:
        grouped.setdefault(turn.conversation_id, []).append(turn)
    selections: list[ConversationSelectionV1] = []
    left_out: list[str] = []
    for conversation_id, rows in grouped.items():
        rows.sort(key=lambda row: row.turn_index)
        if conversation_id in excluded_ids:
            left_out.append(conversation_id)
            continue
        if active_since is not None and any(
            row.task_status not in FINISHED_TURN_STATUSES and row.updated_at > active_since for row in rows
        ):
            left_out.append(conversation_id)
            continue
        chosen: list[TurnRow] = []
        for row in rows:
            if row.created_at >= min(limit_at, selected_at):
                break
            chosen.append(row)
        if not chosen:
            continue
        kept = rows[len(chosen) :]
        selections.append(
            ConversationSelectionV1(
                conversation_id=conversation_id,
                through_turn_index=chosen[-1].turn_index,
                whole=not kept,
                keep_from=kept[0].created_at if kept else None,
                keep_correlation_id=kept[0].correlation_id if kept else None,
                turns=len(chosen),
                first_at=chosen[0].created_at,
                last_at=chosen[-1].created_at,
                text_chars=sum(row.chars for row in chosen),
                latest_task_id=chosen[-1].task_id,
            )
        )
    selections.sort(key=lambda item: (item.last_at, item.conversation_id), reverse=True)
    return selections, sorted(left_out)


def task_family(tasks: Iterable[TaskRow], roots: Iterable[str]) -> set[str]:
    """``roots`` and every task delegated from them, however deep (by ``parent_task_id``)."""

    children: dict[str, list[str]] = {}
    for row in tasks:
        if row.parent_task_id:
            children.setdefault(row.parent_task_id, []).append(row.task_id)
    found: set[str] = set()
    pending = list(roots)
    while pending:
        task_id = pending.pop()
        if task_id in found:
            continue
        found.add(task_id)
        pending.extend(children.get(task_id, ()))
    return found


def task_ids_to_delete(
    tasks: Iterable[TaskRow],
    *,
    limit_at: str,
    roots: Iterable[str] | None,
    excluded_roots: Iterable[str] = (),
) -> list[TaskRow]:
    """The finished tasks a clean-up deletes.

    With ``roots`` (the turns of named conversations), those tasks and the
    tasks delegated from them; otherwise every finished task. Either way only
    tasks finished before ``limit_at``, and never the tasks of a left-out
    conversation (``excluded_roots``) or the tasks delegated from them.
    """

    rows = list(tasks)
    excluded = task_family(rows, excluded_roots)
    wanted = task_family(rows, roots) if roots is not None else None
    return [
        row
        for row in rows
        if row.timestamp < limit_at
        and row.task_id not in excluded
        and (wanted is None or row.task_id in wanted)
    ]


class HistoryCleanupRepository:
    """Orders (module docstring) and the manager's records of one person's history."""

    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {ORDERS_TABLE} (
                order_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, person_id TEXT NOT NULL,
                status TEXT NOT NULL, plan_digest TEXT NOT NULL, plan_json TEXT NOT NULL,
                approval_json TEXT NOT NULL DEFAULT '{{}}', manager_result_json TEXT NOT NULL DEFAULT '{{}}',
                gateway_result_json TEXT NOT NULL DEFAULT '{{}}', artifact_paths_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{ORDERS_TABLE}_status ON {ORDERS_TABLE}(team_id, status, updated_at)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{ORDERS_TABLE}_person ON {ORDERS_TABLE}(team_id, person_id, updated_at)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def _table_exists(self, conn: Any, table: str) -> bool:
        p = self.dialect.bind
        if self.dialect is POSTGRES:
            sql = f"SELECT 1 FROM pg_catalog.pg_class WHERE oid=to_regclass({p}) AND relkind IN ('r', 'p')"
        else:
            sql = f"SELECT 1 FROM sqlite_master WHERE type='table' AND name={p}"
        return conn.execute(sql, (table,)).fetchone() is not None

    # -- orders ------------------------------------------------------------------------

    def save_plan(self, plan: HistoryCleanupPlanV1, *, at: datetime) -> None:
        p = self.dialect.bind
        now = _utc_iso(at)
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO {ORDERS_TABLE} (order_id, team_id, person_id, status, plan_digest, plan_json, "
                f"created_at, updated_at) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})",
                (
                    plan.order_id,
                    plan.team_id,
                    plan.person_id,
                    OrderStatus.PLANNED.value,
                    plan.plan_digest,
                    plan.model_dump_json(),
                    now,
                    now,
                ),
            )
            conn.commit()

    @staticmethod
    def _order(row: Any) -> HistoryCleanupOrder:
        return HistoryCleanupOrder(
            plan=HistoryCleanupPlanV1.model_validate_json(row["plan_json"]),
            status=OrderStatus(row["status"]),
            approval=json.loads(row["approval_json"] or "{}"),
            manager_result=json.loads(row["manager_result_json"] or "{}"),
            gateway_result=json.loads(row["gateway_result_json"] or "{}"),
            artifact_paths=tuple(json.loads(row["artifact_paths_json"] or "[]")),
            updated_at=str(row["updated_at"]),
        )

    def get(self, order_id: str) -> HistoryCleanupOrder | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {ORDERS_TABLE} WHERE order_id={p}", (order_id,)).fetchone()
        return self._order(row) if row else None

    def _with_status(self, team_id: str, status: OrderStatus) -> list[HistoryCleanupOrder]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {ORDERS_TABLE} WHERE team_id={p} AND status={p} ORDER BY updated_at, order_id",
                (team_id, status.value),
            ).fetchall()
        return [self._order(row) for row in rows]

    def deleting(self, team_id: str) -> list[HistoryCleanupOrder]:
        """Approved orders whose manager part has not finished (a run stopped part way)."""

        return self._with_status(team_id, OrderStatus.DELETING)

    def waiting_for_gateway(self, team_id: str) -> list[HistoryCleanupOrder]:
        return self._with_status(team_id, OrderStatus.WAITING_FOR_GATEWAY)

    def _move(
        self, order_id: str, *, from_status: OrderStatus, to_status: OrderStatus, at: datetime, **columns: str
    ) -> bool:
        """Change an order's status if it still has ``from_status``; True when this call changed it."""

        p = self.dialect.bind
        assignments = "".join(f", {name}={p}" for name in columns)
        with self._connect() as conn:
            conn.begin_write()
            result = conn.execute(
                f"UPDATE {ORDERS_TABLE} SET status={p}, updated_at={p}{assignments} WHERE order_id={p} AND status={p}",
                (to_status.value, _utc_iso(at), *columns.values(), order_id, from_status.value),
            )
            changed = int(getattr(result, "rowcount", 0) or 0) == 1
            conn.commit()
        return changed

    def start_deleting(
        self, order_id: str, *, approval: dict[str, Any], artifact_paths: Iterable[str], at: datetime
    ) -> bool:
        """Mark a staged order approved; False when it was already applied (each plan applies once).

        The files its conversation artifacts name are recorded first, so a run
        that stops part way deletes them on the next try even after their
        records are gone.
        """

        return self._move(
            order_id,
            from_status=OrderStatus.PLANNED,
            to_status=OrderStatus.DELETING,
            at=at,
            approval_json=json.dumps(approval, sort_keys=True),
            artifact_paths_json=json.dumps(sorted(set(artifact_paths))),
        )

    def finish_manager_part(self, order_id: str, *, result: dict[str, Any], at: datetime) -> bool:
        return self._move(
            order_id,
            from_status=OrderStatus.DELETING,
            to_status=OrderStatus.WAITING_FOR_GATEWAY,
            at=at,
            manager_result_json=json.dumps(result, sort_keys=True),
        )

    def finish_gateway_part(self, order_id: str, *, result: dict[str, Any], at: datetime) -> bool:
        return self._move(
            order_id,
            from_status=OrderStatus.WAITING_FOR_GATEWAY,
            to_status=OrderStatus.DONE,
            at=at,
            gateway_result_json=json.dumps(result, sort_keys=True),
        )

    def prune(self, team_id: str, *, now: datetime) -> int:
        """Remove staged orders past their expiry and orders finished longer than ``ORDER_RETENTION`` ago."""

        p = self.dialect.bind
        removed = 0
        with self._connect() as conn:
            conn.begin_write()
            rows = conn.execute(
                f"SELECT order_id, plan_json FROM {ORDERS_TABLE} WHERE team_id={p} AND status={p}",
                (team_id, OrderStatus.PLANNED.value),
            ).fetchall()
            expired = [
                str(row["order_id"])
                for row in rows
                if HistoryCleanupPlanV1.model_validate_json(row["plan_json"]).expires_at <= _utc_iso(now)
            ]
            for order_id in expired:
                conn.execute(f"DELETE FROM {ORDERS_TABLE} WHERE order_id={p}", (order_id,))
            removed += len(expired)
            result = conn.execute(
                f"DELETE FROM {ORDERS_TABLE} WHERE team_id={p} AND status={p} AND updated_at < {p}",
                (team_id, OrderStatus.DONE.value, _utc_iso(now - ORDER_RETENTION)),
            )
            removed += int(getattr(result, "rowcount", 0) or 0)
            conn.commit()
        return removed

    def conversation_deleted(self, team_id: str, person_id: str, conversation_id: str) -> bool:
        """Whether an approved clean-up deleted this whole conversation of this person."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT plan_json FROM {ORDERS_TABLE} WHERE team_id={p} AND person_id={p} AND status<>{p}",
                (team_id, person_id, OrderStatus.PLANNED.value),
            ).fetchall()
        for row in rows:
            plan = HistoryCleanupPlanV1.model_validate_json(row["plan_json"])
            if plan.deletes_conversations and any(
                item.whole and item.conversation_id == conversation_id for item in plan.conversations
            ):
                return True
        return False

    # -- what the person has ---------------------------------------------------------

    def person_turns(self, person_id: str, conversation_ids: Iterable[str] | None = None) -> list[TurnRow]:
        """The person's conversation turns: all of them (``None``), or those of these conversations."""

        p = self.dialect.bind
        chars = " + ".join(f"LENGTH(COALESCE(l.{column}, ''))" for column in _TURN_TEXT_COLUMNS)
        query = (
            f"SELECT t.conversation_id, COALESCE(t.subject_preview, '') AS subject_preview, l.task_id, l.turn_index, "
            f"COALESCE(l.task_status, '') AS task_status, COALESCE(l.created_at, '') AS created_at, "
            f"COALESCE(l.updated_at, '') AS updated_at, l.correlation_id AS correlation_id, {chars} AS chars "
            "FROM conversation_threads t JOIN conversation_task_links l ON l.conversation_id = t.conversation_id "
            f"WHERE t.owner_canonical_user_id = {p}"
        )
        wanted = None if conversation_ids is None else sorted(set(conversation_ids))
        if wanted == []:
            return []
        rows: list[Any] = []
        with self._connect() as conn:
            if not self._table_exists(conn, "conversation_threads") or not self._table_exists(
                conn, "conversation_task_links"
            ):
                return []
            if wanted is not None:
                for batch in _batches(wanted):
                    marks = ", ".join(p for _ in batch)
                    rows += conn.execute(
                        query + f" AND t.conversation_id IN ({marks})", (person_id, *batch)
                    ).fetchall()
            else:
                rows = conn.execute(query, (person_id,)).fetchall()
        return [
            TurnRow(
                conversation_id=str(row["conversation_id"]),
                subject_preview=str(row["subject_preview"] or ""),
                task_id=str(row["task_id"]),
                turn_index=int(row["turn_index"]),
                task_status=str(row["task_status"] or ""),
                created_at=str(row["created_at"] or ""),
                updated_at=str(row["updated_at"] or ""),
                chars=int(row["chars"] or 0),
                correlation_id=str(row["correlation_id"]) if row["correlation_id"] else None,
            )
            for row in rows
        ]

    def conversation_titles(self, person_id: str, conversation_ids: Iterable[str]) -> dict[str, str]:
        """Each of the person's conversations' first request, as its title (``subject_preview``)."""

        return {
            turn.conversation_id: turn.subject_preview
            for turn in self.person_turns(person_id, list(conversation_ids))
        }

    def person_conversation_ids(self, person_id: str, conversation_ids: Iterable[str]) -> set[str]:
        """Which of these conversations belong to the person."""

        p = self.dialect.bind
        found: set[str] = set()
        with self._connect() as conn:
            if not self._table_exists(conn, "conversation_threads"):
                return found
            for batch in _batches(sorted(set(conversation_ids))):
                marks = ", ".join(p for _ in batch)
                rows = conn.execute(
                    f"SELECT conversation_id FROM conversation_threads WHERE owner_canonical_user_id = {p} "
                    f"AND conversation_id IN ({marks})",
                    (person_id, *batch),
                ).fetchall()
                found.update(str(row["conversation_id"]) for row in rows)
        return found

    def person_tasks(self, person_id: str) -> list[TaskRow]:
        """The person's finished tasks: id, the task it was delegated from, when it finished, and its size."""

        p = self.dialect.bind
        parent = (
            "CAST(task_data AS JSONB) ->> 'parent_task_id'"
            if self.dialect is POSTGRES
            else "json_extract(task_data, '$.parent_task_id')"
        )
        with self._connect() as conn:
            if not self._table_exists(conn, "completed_tasks"):
                return []
            rows = conn.execute(
                f"SELECT task_id, {parent} AS parent_task_id, COALESCE(completed_tasks.timestamp, '') AS finished_at, "
                f"LENGTH(COALESCE(task_data, '')) AS chars FROM completed_tasks WHERE owner_canonical_user_id = {p}",
                (person_id,),
            ).fetchall()
        return [
            TaskRow(
                task_id=str(row["task_id"]),
                parent_task_id=str(row["parent_task_id"]) if row["parent_task_id"] else None,
                timestamp=str(row["finished_at"] or ""),
                chars=int(row["chars"] or 0),
            )
            for row in rows
        ]

    def artifact_rows(self, selections: Iterable[ConversationSelectionV1], *, selected_at: str) -> list[tuple[str, str]]:
        """``(artifact_id, storage_path)`` of the conversation artifacts the selections delete."""

        p = self.dialect.bind
        found: list[tuple[str, str]] = []
        with self._connect() as conn:
            if not self._table_exists(conn, "conversation_artifacts"):
                return found
            for item in selections:
                until = item.keep_from or selected_at
                rows = conn.execute(
                    "SELECT artifact_id, COALESCE(storage_path, '') AS storage_path FROM conversation_artifacts "
                    f"WHERE conversation_id = {p} AND COALESCE(created_at, '') < {p}",
                    (item.conversation_id, until),
                ).fetchall()
                found += [(str(row["artifact_id"]), str(row["storage_path"])) for row in rows]
        return found

    # -- the manager's part of a clean-up ---------------------------------------------------

    def delete_manager_records(
        self,
        plan: HistoryCleanupPlanV1,
        *,
        task_ids: Iterable[str],
        turn_task_ids: Iterable[str],
    ) -> dict[str, int]:
        """Delete the manager's records a plan covers, in one transaction; rows deleted by table.

        ``task_ids`` are the finished tasks to delete (``task_ids_to_delete``),
        and ``turn_task_ids`` the tasks of the deleted turns and of the tasks
        delegated from them (memory-graph rows are kept by task). Running it
        again deletes nothing more.
        """

        counts: dict[str, int] = {}

        def add(table: str, result: Any) -> None:
            counts[table] = counts.get(table, 0) + max(int(getattr(result, "rowcount", 0) or 0), 0)

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            tables = {
                table
                for table in (
                    "conversation_task_links",
                    "conversation_turn_tags",
                    "conversation_context_epochs",
                    "conversation_artifacts",
                    "conversation_thread_tags",
                    "conversation_threads",
                    "memory_review_jobs",
                    "completed_tasks",
                    *_MEMORY_GRAPH_TASK_TABLES,
                )
                if self._table_exists(conn, table)
            }
            if plan.deletes_conversations:
                for item in plan.conversations:
                    self._delete_conversation_turns(conn, item, plan.selected_at, tables, add)
                self._delete_memory_graph_rows(conn, plan, list(turn_task_ids), tables, add)
            if plan.deletes_tasks and "completed_tasks" in tables:
                for batch in _batches(sorted(set(task_ids))):
                    marks = ", ".join(p for _ in batch)
                    add(
                        "completed_tasks",
                        conn.execute(
                            f"DELETE FROM completed_tasks WHERE owner_canonical_user_id = {p} AND task_id IN ({marks})",
                            (plan.person_id, *batch),
                        ),
                    )
            conn.commit()
        return counts

    def _delete_conversation_turns(
        self, conn: Any, item: ConversationSelectionV1, selected_at: str, tables: set[str], add: Any
    ) -> None:
        p = self.dialect.bind
        conversation = (item.conversation_id,)
        through = (item.conversation_id, item.through_turn_index)
        if "conversation_task_links" in tables:
            add(
                "conversation_task_links",
                conn.execute(
                    f"DELETE FROM conversation_task_links WHERE conversation_id = {p} AND turn_index <= {p}", through
                ),
            )
        if "conversation_turn_tags" in tables:
            add(
                "conversation_turn_tags",
                conn.execute(
                    f"DELETE FROM conversation_turn_tags WHERE conversation_id = {p} AND turn_index <= {p}", through
                ),
            )
        if "memory_review_jobs" in tables:
            add(
                "memory_review_jobs",
                conn.execute(f"DELETE FROM memory_review_jobs WHERE conversation_id = {p} AND turn_index <= {p}", through),
            )
        # Every summary of the conversation covers its earliest turns, which
        # are the ones deleted, so none may stay; the kept turns are shown in
        # full until the conversation is compacted again.
        if "conversation_context_epochs" in tables:
            add(
                "conversation_context_epochs",
                conn.execute(f"DELETE FROM conversation_context_epochs WHERE conversation_id = {p}", conversation),
            )
        if "conversation_artifacts" in tables:
            add(
                "conversation_artifacts",
                conn.execute(
                    f"DELETE FROM conversation_artifacts WHERE conversation_id = {p} AND COALESCE(created_at, '') < {p}",
                    (item.conversation_id, item.keep_from or selected_at),
                ),
            )
        if "conversation_threads" not in tables:
            return
        remaining = conn.execute(
            f"SELECT task_id, COALESCE(problem_text, '') AS problem_text FROM conversation_task_links "
            f"WHERE conversation_id = {p} ORDER BY turn_index LIMIT 1",
            conversation,
        ).fetchone() if "conversation_task_links" in tables else None
        if remaining is None:
            if "conversation_thread_tags" in tables:
                add(
                    "conversation_thread_tags",
                    conn.execute(f"DELETE FROM conversation_thread_tags WHERE conversation_id = {p}", conversation),
                )
            add(
                "conversation_threads",
                conn.execute(f"DELETE FROM conversation_threads WHERE conversation_id = {p}", conversation),
            )
            return
        # Partly deleted: the thread's summary and compaction state came from
        # the deleted turns too, and its title from the first of them.
        conn.execute(
            "UPDATE conversation_threads SET synopsis_json = NULL, synopsis_markdown = NULL, "
            "summarized_through_turn_index = 0, compaction_state_json = NULL, last_compacted_at = NULL, "
            f"subject_preview = {p}, root_task_id = {p} WHERE conversation_id = {p}",
            (
                str(remaining["problem_text"]).strip()[:160] or None,
                str(remaining["task_id"]),
                item.conversation_id,
            ),
        )

    def _delete_memory_graph_rows(
        self, conn: Any, plan: HistoryCleanupPlanV1, turn_task_ids: list[str], tables: set[str], add: Any
    ) -> None:
        """Memory-graph rows made from the deleted turns (vectors: ``MemoryGraphRepository.forget_case_memories``)."""

        p = self.dialect.bind
        whole = [item.conversation_id for item in plan.conversations if item.whole]
        for table in _MEMORY_GRAPH_TASK_TABLES:
            if table not in tables:
                continue
            for batch in _batches(sorted(set(turn_task_ids))):
                marks = ", ".join(p for _ in batch)
                add(table, conn.execute(f"DELETE FROM {table} WHERE task_id IN ({marks})", tuple(batch)))
            for batch in _batches(whole):
                marks = ", ".join(p for _ in batch)
                add(table, conn.execute(f"DELETE FROM {table} WHERE conversation_id IN ({marks})", tuple(batch)))

    def case_memory_ids(self, plan: HistoryCleanupPlanV1, turn_task_ids: Iterable[str]) -> list[str]:
        """The memory graph's case memories a plan deletes, so their vectors go too."""

        p = self.dialect.bind
        found: set[str] = set()
        whole = [item.conversation_id for item in plan.conversations if item.whole]
        with self._connect() as conn:
            if not self._table_exists(conn, "case_memories"):
                return []
            for batch in _batches(sorted(set(turn_task_ids))):
                marks = ", ".join(p for _ in batch)
                rows = conn.execute(f"SELECT case_memory_id FROM case_memories WHERE task_id IN ({marks})", tuple(batch))
                found.update(str(row["case_memory_id"]) for row in rows.fetchall())
            for batch in _batches(whole):
                marks = ", ".join(p for _ in batch)
                rows = conn.execute(
                    f"SELECT case_memory_id FROM case_memories WHERE conversation_id IN ({marks})", tuple(batch)
                )
                found.update(str(row["case_memory_id"]) for row in rows.fetchall())
        return sorted(found)


def remove_recorded_files(root: Path | None, paths: Iterable[str]) -> tuple[int, int, int]:
    """Delete files that deleted records named, only inside ``root`` and never through a link.

    Returns files removed, bytes removed and files that could not be removed
    (logged). A file already gone counts as removed by an earlier run. An
    emptied conversation folder is removed too.
    """

    removed = size = failed = 0
    if root is None:
        return removed, size, failed
    base = Path(os.path.abspath(root))
    for raw in paths:
        try:
            parts = Path(os.path.abspath(raw)).relative_to(base).parts
        except ValueError:
            logger.warning(
                "A deleted record named a file outside its folder; it was left alone. "
                "event=history_cleanup_file_outside path=%s",
                raw,
            )
            failed += 1
            continue
        try:
            info = safe_open.entry_status(base, parts)
            if info is None or not stat.S_ISREG(info.st_mode):
                continue
            safe_open.remove_file(base, parts)
        except UnsafePath as exc:
            if exc.reason != safe_open.REASON_NOT_FOUND:
                logger.warning(
                    "A file a deleted record named could not be removed safely. event=history_cleanup_file_skipped "
                    "path=%s reason=%s",
                    raw,
                    exc.reason,
                )
                failed += 1
            continue
        except OSError:
            logger.exception("Could not remove a file a deleted record named. event=history_cleanup_file_failed path=%s", raw)
            failed += 1
            continue
        removed += 1
        size += info.st_size
        _remove_emptied_folder(base, parts[:-1])
    return removed, size, failed


def _remove_emptied_folder(root: Path, parts: tuple[str, ...]) -> None:
    if not parts:
        return
    try:
        parent = safe_open.open_directory(root, parts[:-1])
    except UnsafePath:
        return
    try:
        os.rmdir(parts[-1], dir_fd=parent)  # only an empty folder, never through a link
    except OSError:
        return
    finally:
        os.close(parent)


__all__ = [
    "ACTIVE_TURN_WINDOW",
    "ConversationSelectionV1",
    "HistoryCleanupOrder",
    "HistoryCleanupPlanV1",
    "HistoryCleanupRepository",
    "ORDERS_TABLE",
    "MEMORIES_KEPT",
    "ORDER_RETENTION",
    "remove_recorded_files",
    "OrderStatus",
    "TaskRow",
    "TurnRow",
    "select_conversation_turns",
    "size_words",
    "task_family",
    "task_ids_to_delete",
]
