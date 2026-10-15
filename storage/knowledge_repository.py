"""Backend-neutral store for the shared knowledge base.

Tables (all in the manager database, ``state.storage``):

- ``knowledge_pages``: the current text of each page, keyed by team, space,
  owner (empty for the team space) and slug.
- ``knowledge_revisions``: a full copy of every revision with who made it,
  for whom, in which task, and from which contribution.
- ``knowledge_notes``: observations attached to a page without rewriting it.
- ``knowledge_contributions``: team-space changes proposed from one person's
  work, private to that person until published.
- ``knowledge_links``: ``[[links]]`` between pages, rebuilt on each revision.
- ``knowledge_redirects``: the names of merged pages, each leading to the page
  it was merged into, so old ``[[links]]`` and references keep working.
- ``knowledge_write_events``: every write and refusal, for the write limits
  and the curator's activity report.
- ``knowledge_write_receipts``: retry receipts for review lesson writes,
  committed with the page or contribution, without its text. Reviews and
  their receipts stay behind in a team transfer.

Search uses each database's own full-text index, kept current by database
triggers: an FTS5 table over ``knowledge_pages`` in SQLite, a ``tsvector``
side table in PostgreSQL. The index is derived: it is never copied by person
departure or team transfer, and is rebuilt from the page rows the triggers see.
(A PostgreSQL generated column was rejected because departure restores rows
with ``INSERT`` of every stored column.)
PostgreSQL 14 or later is needed (``CREATE OR REPLACE TRIGGER``); CI and the
bundled image use 16 or later.

Every write runs in one transaction that first takes the store's write lock
(``BEGIN IMMEDIATE`` in SQLite, a transaction advisory lock in PostgreSQL), so
the revision check and the write limits are exact across processes. The 2026-10
benchmark (``ua-architecture`` research note) showed writes stay in single
milliseconds under this serialization with 200 agents.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional, Protocol

from id_generation import new_id
from knowledge_base.records import (
    ActivityReport,
    AgentActivity,
    PageProblems,
    CODE_CONFLICT,
    CODE_EXISTS,
    CODE_FROZEN,
    CODE_LIMIT,
    CODE_NOT_FOUND,
    CODE_NOT_PENDING,
    CODE_REPEAT,
    KnowledgeActor,
    KnowledgeContribution,
    KnowledgeError,
    KnowledgeNote,
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeWriteKey,
    KnowledgeWriteReceipt,
    PageChange,
    PageKey,
    PageMerge,
    RecentFreeze,
    SearchHit,
    CODE_MERGED,
    CODE_WRITE_MISMATCH,
)
from knowledge_base.vocabulary import (
    PAGE_STATE_ACTIONS,
    FREEZE_REASON_EDIT_WAR,
    ActorKind,
    EVENT_ID_PREFIX,
    NOTE_ID_PREFIX,
    PAGE_ID_PREFIX,
    REVISION_ID_PREFIX,
    ChangeKind,
    ContributionKind,
    ContributionStatus,
    KnowledgeSpace,
    NoteStatus,
    PageStatus,
    WriteAction,
    WriteOutcome,
)
from runtime_settings.knowledge import KnowledgeWriteLimits
from storage.read_only import repositories_are_read_only
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect

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
from .sqlite_connection import connect_sqlite

KnowledgeDatabaseRow = sqlite3.Row | DatabaseRow

# Tables a person's departure holds (``storage.person_departure_repository``):
# the owner column names the person for personal rows and pending contributions.
KNOWLEDGE_PERSONAL_TABLES = (
    "knowledge_links",
    "knowledge_redirects",
    "knowledge_notes",
    "knowledge_revisions",
    "knowledge_contributions",
    "knowledge_pages",
)
KNOWLEDGE_OWNER_COLUMN = "owner_person_id"
KNOWLEDGE_EVENTS_TABLE = "knowledge_write_events"
KNOWLEDGE_EVENTS_PERSON_COLUMN = "work_person_id"
KNOWLEDGE_RECEIPTS_TABLE = "knowledge_write_receipts"
KNOWLEDGE_RECEIPTS_PERSON_COLUMN = "work_person_id"

# Search widens from all words to any word only below this many results
# (2026-10 benchmark: any-word searches over common words were 20-40x slower).
_ANY_WORD_FALLBACK_BELOW = 3
_SNIPPET_WORDS = 16
# Fixed advisory lock key for knowledge writes in PostgreSQL.
_PG_WRITE_LOCK_KEY = 7_302_211_551
_COUNTED_OUTCOMES = (WriteOutcome.APPLIED.value, WriteOutcome.PENDING.value)
_PAGE_STATE_VALUES = tuple(action.value for action in PAGE_STATE_ACTIONS)


def utc_text(moment: datetime) -> str:
    """Fixed-width UTC timestamps, so text comparison orders them correctly."""

    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _tags(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value or "[]"))


def _tags_json(tags: tuple[str, ...]) -> str:
    return json.dumps(list(tags))


class KnowledgeRepository(Protocol):
    def get_write_receipt(self, actor: KnowledgeActor, write_key: KnowledgeWriteKey) -> Optional[KnowledgeWriteReceipt]: ...

    def get_page(self, key: PageKey) -> Optional[KnowledgePage]: ...

    def get_page_by_id(self, page_id: str) -> Optional[KnowledgePage]: ...

    def get_revision(self, page_id: str, revision: int) -> Optional[KnowledgeRevision]: ...

    def list_revisions(self, page_id: str, *, limit: int) -> list[KnowledgeRevision]: ...

    def search(
        self,
        *,
        team_id: str,
        reader_person_id: str,
        spaces: tuple[KnowledgeSpace, ...],
        terms: list[str],
        page_type: str = "",
        tag: str = "",
        limit: int,
        offset: int = 0,
        status: PageStatus = PageStatus.ACTIVE,
    ) -> list[SearchHit]: ...

    def count_pages(
        self, *, team_id: str, reader_person_id: str, spaces: tuple[KnowledgeSpace, ...], terms: list[str],
        status: PageStatus = PageStatus.ACTIVE,
    ) -> int: ...

    def backlinks(self, page: KnowledgePage, *, reader_person_id: str, limit: int) -> list[KnowledgePage]: ...

    def get_redirect_target(self, key: PageKey) -> Optional[KnowledgePage]: ...

    def list_notes(self, page_id: str, *, status: Optional[NoteStatus], limit: int) -> list[KnowledgeNote]: ...

    def get_contribution(self, contribution_id: str) -> Optional[KnowledgeContribution]: ...

    def list_contributions(
        self,
        *,
        team_id: str,
        owner_person_id: str,
        status: Optional[ContributionStatus],
        page_id: str = "",
        limit: int,
        offset: int = 0,
    ) -> list[KnowledgeContribution]: ...

    def count_contributions(
        self,
        *,
        team_id: str,
        owner_person_id: str,
        status: Optional[ContributionStatus],
        page_id: str = "",
    ) -> int: ...

    def apply_page_change(
        self, change: PageChange, *, limits: Optional[KnowledgeWriteLimits], now: datetime,
        write_key: Optional[KnowledgeWriteKey] = None,
    ) -> KnowledgePage: ...

    def merge_pages(self, merge: PageMerge, *, now: datetime) -> tuple[KnowledgePage, KnowledgePage]: ...

    def add_note(
        self,
        page: KnowledgePage,
        *,
        body: str,
        actor: KnowledgeActor,
        limits: Optional[KnowledgeWriteLimits],
        now: datetime,
        contribution_id: str = "",
    ) -> KnowledgeNote: ...

    def add_contribution(
        self, contribution: KnowledgeContribution, *, limits: Optional[KnowledgeWriteLimits], now: datetime,
        write_key: Optional[KnowledgeWriteKey] = None,
    ) -> KnowledgeContribution: ...

    def decide_contribution(
        self,
        contribution_id: str,
        *,
        status: ContributionStatus,
        actor: KnowledgeActor,
        reason: str,
        now: datetime,
    ) -> KnowledgeContribution: ...

    def record_refusal(self, actor: KnowledgeActor, *, action: WriteAction, page_id: str, reason: str, now: datetime) -> None: ...

    def activity(self, *, team_id: str, since: datetime, now: datetime, limit: int) -> ActivityReport: ...

    def list_open_notes(self, *, team_id: str, space: KnowledgeSpace, owner_person_id: str, limit: int) -> list[KnowledgeNote]: ...

    def resolve_note(self, note_id: str, *, team_id: str, status: NoteStatus, actor: KnowledgeActor, now: datetime) -> bool: ...

    def set_frozen_until(
        self, page_id: str, frozen_until_utc: Optional[str], *, actor: KnowledgeActor, now: datetime
    ) -> None: ...

    def page_problems(self, *, team_id: str, stale_before: datetime, limit: int) -> PageProblems: ...

    def export_rows(self, table: str) -> list[dict[str, Any]]: ...

    def import_rows(self, table: str, rows: list[dict[str, Any]]) -> int: ...


class _RelationalKnowledgeRepository:
    """SQL shared by both databases; subclasses supply connections and search."""

    _dialect: SqlDialect
    _db_path: str
    _logger: logging.Logger

    # -- schema --------------------------------------------------------------------------

    def _init_schema(self) -> None:
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(self._db_path)
        with self._connect() as conn:
            # Every agent process opens the store at start; the lock keeps
            # their CREATE ... and CREATE OR REPLACE statements from racing.
            self._take_write_lock(conn)
            for statement in self._table_statements():
                conn.execute(statement)
            for statement in self._search_index_statements():
                conn.execute(statement)
            conn.commit()

    def _table_statements(self) -> list[str]:
        actor_columns = """
                    actor_kind TEXT NOT NULL,
                    actor_id TEXT NOT NULL DEFAULT '',
                    actor_agent_type TEXT NOT NULL DEFAULT '',
                    actor_display_name TEXT NOT NULL DEFAULT '',
        """
        return [
            """
            CREATE TABLE IF NOT EXISTS knowledge_pages (
                page_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                space TEXT NOT NULL,
                owner_person_id TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                page_type TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                frozen_until_utc TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                updated_by_kind TEXT NOT NULL,
                updated_by_id TEXT NOT NULL DEFAULT '',
                updated_by_agent_type TEXT NOT NULL DEFAULT '',
                UNIQUE (team_id, space, owner_person_id, slug)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_pages_listing
            ON knowledge_pages(team_id, space, owner_person_id, status, updated_at_utc)
            """,
            f"""
            CREATE TABLE IF NOT EXISTS knowledge_revisions (
                revision_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                team_id TEXT NOT NULL,
                space TEXT NOT NULL,
                owner_person_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                page_type TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                change_kind TEXT NOT NULL,
                change_summary TEXT NOT NULL DEFAULT '',
                {actor_columns}
                work_person_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                work_id TEXT NOT NULL DEFAULT '',
                contribution_id TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                UNIQUE (page_id, revision)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS knowledge_notes (
                note_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                space TEXT NOT NULL,
                owner_person_id TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                {actor_columns}
                work_person_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                work_id TEXT NOT NULL DEFAULT '',
                contribution_id TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                resolved_at_utc TEXT,
                resolved_by_kind TEXT NOT NULL DEFAULT '',
                resolved_by_id TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_knowledge_notes_page ON knowledge_notes(page_id, status, created_at_utc)",
            f"""
            CREATE TABLE IF NOT EXISTS knowledge_contributions (
                contribution_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                owner_person_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                page_id TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL,
                base_revision INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                page_type TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                body TEXT NOT NULL DEFAULT '',
                change_summary TEXT NOT NULL DEFAULT '',
                review_kinds_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                {actor_columns}
                task_id TEXT NOT NULL DEFAULT '',
                work_id TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                decided_at_utc TEXT,
                decided_by_kind TEXT NOT NULL DEFAULT '',
                decided_by_id TEXT NOT NULL DEFAULT '',
                decision_reason TEXT NOT NULL DEFAULT '',
                published_revision INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_contributions_owner
            ON knowledge_contributions(team_id, owner_person_id, status, created_at_utc)
            """,
            """
            CREATE TABLE IF NOT EXISTS knowledge_links (
                page_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                owner_person_id TEXT NOT NULL DEFAULT '',
                target_space TEXT NOT NULL,
                target_slug TEXT NOT NULL,
                PRIMARY KEY (page_id, target_space, target_slug)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_knowledge_links_target ON knowledge_links(team_id, target_space, target_slug)",
            # A merged page's name leads to the page it was merged into. One
            # level only: a later merge of the target moves its redirects on.
            """
            CREATE TABLE IF NOT EXISTS knowledge_redirects (
                team_id TEXT NOT NULL,
                space TEXT NOT NULL,
                owner_person_id TEXT NOT NULL DEFAULT '',
                from_slug TEXT NOT NULL,
                from_page_id TEXT NOT NULL,
                to_page_id TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY (team_id, space, owner_person_id, from_slug)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_knowledge_redirects_target ON knowledge_redirects(to_page_id)",
            """
            CREATE TABLE IF NOT EXISTS knowledge_write_events (
                event_id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                page_id TEXT NOT NULL DEFAULT '',
                contribution_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                actor_kind TEXT NOT NULL,
                actor_id TEXT NOT NULL DEFAULT '',
                actor_agent_type TEXT NOT NULL DEFAULT '',
                work_person_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                work_id TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_knowledge_events_actor ON knowledge_write_events(actor_id, created_at_utc)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_events_page ON knowledge_write_events(page_id, created_at_utc)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_events_work ON knowledge_write_events(work_id, action)",
            f"""
            CREATE TABLE IF NOT EXISTS {KNOWLEDGE_RECEIPTS_TABLE} (
                team_id TEXT NOT NULL,
                work_person_id TEXT NOT NULL DEFAULT '',
                operation_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                page_reference TEXT NOT NULL,
                page_id TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                contribution_id TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY (team_id, work_person_id, operation_id)
            )
            """,
        ]

    def _search_index_statements(self) -> list[str]:
        raise NotImplementedError

    # -- connections and the write lock --------------------------------------------------

    def _connect(self) -> Any:
        raise NotImplementedError

    def _take_write_lock(self, conn: Any) -> None:
        """SQLite's ``BEGIN IMMEDIATE`` already serializes writers."""

    @contextmanager
    def _write(self) -> Iterator[Any]:
        with self._connect() as conn:
            conn.begin_write()
            self._take_write_lock(conn)
            yield conn
            conn.commit()

    # -- reads -----------------------------------------------------------------------------

    def get_write_receipt(self, actor: KnowledgeActor, write_key: KnowledgeWriteKey) -> Optional[KnowledgeWriteReceipt]:
        with self._connect() as conn:
            return self._write_receipt(conn, actor, write_key)

    def _write_receipt(
        self, conn: Any, actor: KnowledgeActor, write_key: KnowledgeWriteKey
    ) -> Optional[KnowledgeWriteReceipt]:
        """Find an outcome for this person's exact operation, including under the write lock."""

        p = self._dialect.bind
        row = conn.execute(
            f"SELECT * FROM {KNOWLEDGE_RECEIPTS_TABLE} "
            f"WHERE team_id = {p} AND work_person_id = {p} AND operation_id = {p}",
            (actor.team_id, actor.work_person_id, write_key.operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["payload_digest"] != write_key.payload_digest:
            raise KnowledgeError(
                CODE_WRITE_MISMATCH,
                "This review already saved a different lesson. Retry with its original lesson fields to finish the review.",
            )
        return KnowledgeWriteReceipt(
            reference=str(row["page_reference"]), page_id=str(row["page_id"]), revision=int(row["revision"]),
            contribution_id=str(row["contribution_id"]),
        )

    def _store_write_receipt(
        self, conn: Any, actor: KnowledgeActor, write_key: KnowledgeWriteKey,
        receipt: KnowledgeWriteReceipt, *, now: datetime,
    ) -> None:
        p = self._dialect.bind
        conn.execute(
            f"INSERT INTO {KNOWLEDGE_RECEIPTS_TABLE} "
            "(team_id, work_person_id, operation_id, payload_digest, page_reference, page_id, revision, contribution_id, created_at_utc) "
            f"VALUES ({', '.join([p] * 9)})",
            (actor.team_id, actor.work_person_id, write_key.operation_id, write_key.payload_digest,
             receipt.reference, receipt.page_id, receipt.revision, receipt.contribution_id, utc_text(now)),
        )

    def get_page(self, key: PageKey) -> Optional[KnowledgePage]:
        p = self._dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM knowledge_pages
                WHERE team_id = {p} AND space = {p} AND owner_person_id = {p} AND slug = {p}
                """,
                (key.team_id, key.space.value, key.owner_person_id, key.slug),
            ).fetchone()
        return self._page(row) if row is not None else None

    def get_page_by_id(self, page_id: str) -> Optional[KnowledgePage]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM knowledge_pages WHERE page_id = {self._dialect.bind}", (page_id,)
            ).fetchone()
        return self._page(row) if row is not None else None

    def get_revision(self, page_id: str, revision: int) -> Optional[KnowledgeRevision]:
        p = self._dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM knowledge_revisions WHERE page_id = {p} AND revision = {p}",
                (page_id, int(revision)),
            ).fetchone()
        return self._revision(row) if row is not None else None

    def list_revisions(self, page_id: str, *, limit: int) -> list[KnowledgeRevision]:
        p = self._dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM knowledge_revisions WHERE page_id = {p}
                ORDER BY revision DESC LIMIT {p}
                """,
                (page_id, int(limit)),
            ).fetchall()
        return [self._revision(row) for row in rows]

    def search(
        self,
        *,
        team_id: str,
        reader_person_id: str,
        spaces: tuple[KnowledgeSpace, ...],
        terms: list[str],
        page_type: str = "",
        tag: str = "",
        limit: int,
        offset: int = 0,
        status: PageStatus = PageStatus.ACTIVE,
    ) -> list[SearchHit]:
        """Pages matching all ``terms`` (any term when that finds too few); recent pages without terms."""

        where, params = self._visible_pages_filter(team_id, reader_person_id, spaces, page_type, tag, status)
        with self._connect() as conn:
            if not terms:
                p = self._dialect.bind
                rows = conn.execute(
                    f"""
                    SELECT p.*, '' AS snippet FROM knowledge_pages p WHERE {where}
                    ORDER BY p.updated_at_utc DESC, p.page_id LIMIT {p} OFFSET {p}
                    """,
                    (*params, int(limit), int(offset)),
                ).fetchall()
                return [SearchHit(self._page(row), "") for row in rows]
            rows = self._search_rows(conn, terms, any_word=False, where=where, params=params, limit=limit + offset)
            if len(rows) < _ANY_WORD_FALLBACK_BELOW and len(terms) > 1:
                seen = {row["page_id"] for row in rows}
                widened = self._search_rows(conn, terms, any_word=True, where=where, params=params, limit=limit + offset)
                rows = list(rows) + [row for row in widened if row["page_id"] not in seen]
        return [SearchHit(self._page(row), str(row["snippet"] or "")) for row in rows[offset : offset + limit]]

    def _search_rows(
        self, conn: Any, terms: list[str], *, any_word: bool, where: str, params: tuple[Any, ...], limit: int
    ) -> list[Any]:
        raise NotImplementedError

    def count_pages(
        self, *, team_id: str, reader_person_id: str, spaces: tuple[KnowledgeSpace, ...], terms: list[str],
        status: PageStatus = PageStatus.ACTIVE,
    ) -> int:
        """How many pages ``search`` finds for these terms, for paged lists (the Web Chat Knowledge page).

        The same widening as ``search``: all words, or any word when all words
        find fewer than ``_ANY_WORD_FALLBACK_BELOW`` pages (any-word matches
        include every all-word match, so their count is the widened total).
        """

        where, params = self._visible_pages_filter(team_id, reader_person_id, spaces, "", "", status)
        with self._connect() as conn:
            if not terms:
                row = conn.execute(f"SELECT COUNT(*) AS total FROM knowledge_pages p WHERE {where}", params).fetchone()
                return int(row["total"])
            total = self._count_search_rows(conn, terms, any_word=False, where=where, params=params)
            if total < _ANY_WORD_FALLBACK_BELOW and len(terms) > 1:
                total = self._count_search_rows(conn, terms, any_word=True, where=where, params=params)
        return total

    def _count_search_rows(
        self, conn: Any, terms: list[str], *, any_word: bool, where: str, params: tuple[Any, ...]
    ) -> int:
        raise NotImplementedError

    def _visible_pages_filter(
        self,
        team_id: str,
        reader_person_id: str,
        spaces: tuple[KnowledgeSpace, ...],
        page_type: str,
        tag: str,
        status: PageStatus = PageStatus.ACTIVE,
    ) -> tuple[str, tuple[Any, ...]]:
        """Pages of the requested status and spaces; personal pages only for their own person."""

        p = self._dialect.bind
        clauses = [f"p.team_id = {p}", f"p.status = {p}"]
        params: list[Any] = [team_id, status.value]
        if status is PageStatus.ARCHIVED:
            # Merged sources are archived records, but their names now lead
            # to another page. They cannot be restored as independent pages.
            clauses.append("""NOT EXISTS (
                SELECT 1 FROM knowledge_redirects r
                WHERE r.team_id = p.team_id AND r.space = p.space
                AND r.owner_person_id = p.owner_person_id AND r.from_slug = p.slug
            )""")
        space_clauses = []
        if KnowledgeSpace.TEAM in spaces:
            space_clauses.append(f"p.space = {p}")
            params.append(KnowledgeSpace.TEAM.value)
        if KnowledgeSpace.PERSONAL in spaces and reader_person_id:
            space_clauses.append(f"(p.space = {p} AND p.owner_person_id = {p})")
            params.extend([KnowledgeSpace.PERSONAL.value, reader_person_id])
        if not space_clauses:
            space_clauses.append("1 = 0")
        clauses.append("(" + " OR ".join(space_clauses) + ")")
        if page_type:
            clauses.append(f"p.page_type = {p}")
            params.append(page_type)
        if tag:
            # Tags are slugs (letters, digits, hyphens), so no LIKE wildcard can appear.
            clauses.append(f"p.tags_json LIKE {p}")
            params.append(f'%"{tag}"%')
        return " AND ".join(clauses), tuple(params)

    def backlinks(self, page: KnowledgePage, *, reader_person_id: str, limit: int) -> list[KnowledgePage]:
        """Active pages the reader may see that link to ``page``."""

        p = self._dialect.bind
        # Links to the names of pages merged into this one count too.
        clauses = [
            f"l.team_id = {p}",
            f"l.target_space = {p}",
            f"(l.target_slug = {p} OR l.target_slug IN (SELECT r.from_slug FROM knowledge_redirects r WHERE r.to_page_id = {p}))",
            f"p.status = {p}",
        ]
        params: list[Any] = [page.team_id, page.space.value, page.slug, page.page_id, PageStatus.ACTIVE.value]
        if reader_person_id:
            clauses.append(f"(p.space = {p} OR (p.space = {p} AND p.owner_person_id = {p}))")
            params.extend([KnowledgeSpace.TEAM.value, KnowledgeSpace.PERSONAL.value, reader_person_id])
        else:
            # Work for nobody sees only team pages.
            clauses.append(f"p.space = {p}")
            params.append(KnowledgeSpace.TEAM.value)
        if page.space is KnowledgeSpace.PERSONAL:
            clauses.append(f"l.owner_person_id = {p}")
            params.append(page.owner_person_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT p.* FROM knowledge_links l JOIN knowledge_pages p ON p.page_id = l.page_id
                WHERE {' AND '.join(clauses)}
                ORDER BY p.title, p.page_id LIMIT {p}
                """,
                (*params, int(limit)),
            ).fetchall()
        return [self._page(row) for row in rows]

    def get_redirect_target(self, key: PageKey) -> Optional[KnowledgePage]:
        """The page a merged page's name leads to, or None when the name is not a redirect."""

        with self._connect() as conn:
            return self._redirect_target(conn, key)

    def _redirect_target(self, conn: Any, key: PageKey) -> Optional[KnowledgePage]:
        p = self._dialect.bind
        row = conn.execute(
            f"""
            SELECT p.* FROM knowledge_redirects r JOIN knowledge_pages p ON p.page_id = r.to_page_id
            WHERE r.team_id = {p} AND r.space = {p} AND r.owner_person_id = {p} AND r.from_slug = {p}
            """,
            (key.team_id, key.space.value, key.owner_person_id, key.slug),
        ).fetchone()
        return self._page(row) if row is not None else None

    def list_notes(self, page_id: str, *, status: Optional[NoteStatus], limit: int) -> list[KnowledgeNote]:
        p = self._dialect.bind
        status_clause = f"AND status = {p}" if status is not None else ""
        params: tuple[Any, ...] = (page_id, status.value, int(limit)) if status is not None else (page_id, int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM knowledge_notes WHERE page_id = {p} {status_clause}
                ORDER BY created_at_utc DESC, note_id LIMIT {p}
                """,
                params,
            ).fetchall()
        return [self._note(row) for row in rows]

    def get_contribution(self, contribution_id: str) -> Optional[KnowledgeContribution]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM knowledge_contributions WHERE contribution_id = {self._dialect.bind}",
                (contribution_id,),
            ).fetchone()
        return self._contribution(row) if row is not None else None

    def list_contributions(
        self,
        *,
        team_id: str,
        owner_person_id: str,
        status: Optional[ContributionStatus],
        page_id: str = "",
        limit: int,
        offset: int = 0,
    ) -> list[KnowledgeContribution]:
        p = self._dialect.bind
        where, params = self._contributions_filter(team_id, owner_person_id, status, page_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM knowledge_contributions WHERE {where}
                ORDER BY created_at_utc DESC, contribution_id LIMIT {p} OFFSET {p}
                """,
                (*params, int(limit), int(offset)),
            ).fetchall()
        return [self._contribution(row) for row in rows]

    def count_contributions(
        self,
        *,
        team_id: str,
        owner_person_id: str,
        status: Optional[ContributionStatus],
        page_id: str = "",
    ) -> int:
        """Exact total for the same owner, team, status and page as ``list_contributions``."""

        where, params = self._contributions_filter(team_id, owner_person_id, status, page_id)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS total FROM knowledge_contributions WHERE {where}", params).fetchone()
        return int(row["total"])

    def _contributions_filter(
        self, team_id: str, owner_person_id: str, status: Optional[ContributionStatus], page_id: str,
    ) -> tuple[str, tuple[Any, ...]]:
        """Keep paged lists and their totals scoped to identical contribution rows."""

        p = self._dialect.bind
        clauses = [f"team_id = {p}", f"owner_person_id = {p}"]
        params: list[Any] = [team_id, owner_person_id]
        if status is not None:
            clauses.append(f"status = {p}")
            params.append(status.value)
        if page_id:
            clauses.append(f"page_id = {p}")
            params.append(page_id)
        return " AND ".join(clauses), tuple(params)

    # -- writes ----------------------------------------------------------------------------

    def apply_page_change(
        self, change: PageChange, *, limits: Optional[KnowledgeWriteLimits], now: datetime,
        write_key: Optional[KnowledgeWriteKey] = None,
    ) -> KnowledgePage:
        """Create, edit, archive or restore one page in one transaction.

        ``limits`` is given for agent writes and ``None`` for people's edits
        and the curator's publishing. A refusal raises ``KnowledgeError`` and
        is recorded in the edit log.
        """

        action = _action_for(change.change_kind)
        try:
            with self._write() as conn:
                if write_key is not None:
                    receipt = self._write_receipt(conn, change.actor, write_key)
                    if receipt is not None:
                        return self._page_after_write(conn, receipt.page_id)
                page = self._apply_page_change(conn, change, action=action, limits=limits, now=now)
                if write_key is not None:
                    self._store_write_receipt(
                        conn, change.actor, write_key,
                        KnowledgeWriteReceipt(reference=page.reference(), page_id=page.page_id, revision=page.revision),
                        now=now,
                    )
                return page
        except KnowledgeError as error:
            self.record_refusal(
                change.actor,
                action=action,
                page_id=str(error.details.get("page_id") or ""),
                reason=error.code,
                now=now,
            )
            raise

    def _apply_page_change(
        self,
        conn: Any,
        change: PageChange,
        *,
        action: WriteAction,
        limits: Optional[KnowledgeWriteLimits],
        now: datetime,
    ) -> KnowledgePage:
        # A merged page's name leads to its target; it is never created or
        # changed again by name (the service follows redirects first, so this
        # catches races and callers that skip it).
        redirect = self._redirect_target(conn, change.key)
        if redirect is not None:
            raise KnowledgeError(
                CODE_MERGED,
                f"{change.key.reference()} was merged into {redirect.reference()}, and its name now leads there. "
                f"Change {redirect.reference()} instead, or choose another name for a new page.",
                {"page_id": redirect.page_id, "redirects_to": redirect.reference()},
            )
        current = self._page_for_update(conn, change.key)
        if change.base_revision == 0:
            if current is not None:
                raise KnowledgeError(
                    CODE_EXISTS,
                    f"A page named {change.key.reference()} already exists (revision {current.revision}). "
                    "Read it and pass its revision to edit it.",
                    {"page_id": current.page_id, "current_revision": current.revision},
                )
        elif current is None:
            raise KnowledgeError(CODE_NOT_FOUND, f"There is no page named {change.key.reference()}.")
        elif current.revision != change.base_revision:
            raise KnowledgeError(
                CODE_CONFLICT,
                f"{change.key.reference()} changed since you read it: it is at revision {current.revision}, "
                f"not {change.base_revision}. Read it again, merge your change, and save with the new revision.",
                {"page_id": current.page_id, "current_revision": current.revision},
            )
        if limits is not None:
            self._check_agent_limits(conn, change, current=current, action=action, limits=limits, now=now)
        if current is None:
            page = self._insert_page(conn, change, now=now)
        else:
            page = self._update_page(conn, change, current, now=now)
        self._insert_revision(conn, page, change, now=now)
        self._replace_links(conn, page, change.link_targets)
        if change.contribution_id:
            self._mark_published(conn, change.contribution_id, actor=change.actor, revision=page.revision, now=now)
        self._insert_event(
            conn,
            change.actor,
            action=action,
            outcome=WriteOutcome.APPLIED,
            page_id=page.page_id,
            contribution_id=change.contribution_id,
            now=now,
        )
        return page

    def _check_agent_limits(
        self,
        conn: Any,
        change: PageChange,
        *,
        current: Optional[KnowledgePage],
        action: WriteAction,
        limits: KnowledgeWriteLimits,
        now: datetime,
    ) -> None:
        actor = change.actor
        page_id = current.page_id if current is not None else ""
        if current is not None and current.frozen_until_utc and current.frozen_until_utc > utc_text(now):
            raise KnowledgeError(
                CODE_FROZEN,
                f"{change.key.reference()} is frozen until {current.frozen_until_utc} because several agents "
                "edited it many times in a short time. Add a note with your change instead; the curator will merge it.",
                {"page_id": page_id, "frozen_until_utc": current.frozen_until_utc},
            )
        self._check_hourly_writes(conn, actor, limits=limits, now=now, page_id=page_id)
        if action is WriteAction.CREATE:
            self._check_new_pages(conn, actor, limits=limits, now=now)
            return
        assert current is not None
        if action is WriteAction.ARCHIVE:
            self._check_archives(conn, actor, limits=limits, now=now, page_id=page_id)
        if action is WriteAction.EDIT:
            self._check_repeat(conn, change, current, limits=limits)
        self._check_page_edits(conn, actor, page_id=page_id, limits=limits, now=now, label=change.key.reference())
        self._check_edit_war(conn, change, current, limits=limits, now=now)

    def _count_events(self, conn: Any, where: str, params: tuple[Any, ...]) -> int:
        row = conn.execute(f"SELECT COUNT(*) FROM knowledge_write_events WHERE {where}", params).fetchone()
        return int(row[0] or 0)

    def _counted(self) -> str:
        p = self._dialect.bind
        return f"outcome IN ({p}, {p})"

    def _check_hourly_writes(
        self, conn: Any, actor: KnowledgeActor, *, limits: KnowledgeWriteLimits, now: datetime, page_id: str
    ) -> None:
        p = self._dialect.bind
        since = utc_text(now - timedelta(hours=1))
        used = self._count_events(
            conn,
            f"actor_id = {p} AND {self._counted()} AND created_at_utc >= {p} AND action NOT IN ({p}, {p})",
            (actor.actor_id, *_COUNTED_OUTCOMES, since, *_PAGE_STATE_VALUES),
        )
        if used >= limits.writes_per_agent_per_hour:
            raise KnowledgeError(
                CODE_LIMIT,
                f"You reached the limit of {limits.writes_per_agent_per_hour} knowledge writes per hour. "
                "Finish your task without further knowledge writes, or mention what you learned in your answer.",
                {"limit": "writes_per_agent_per_hour", "page_id": page_id},
            )

    def _check_new_pages(self, conn: Any, actor: KnowledgeActor, *, limits: KnowledgeWriteLimits, now: datetime) -> None:
        p = self._dialect.bind
        used = self._count_events(
            conn,
            f"work_id = {p} AND action = {p} AND {self._counted()}",
            (_work_key(actor), WriteAction.CREATE.value, *_COUNTED_OUTCOMES),
        )
        if used >= limits.new_pages_per_work:
            raise KnowledgeError(
                CODE_LIMIT,
                f"This request already created {used} knowledge pages, the limit. "
                "Add what you learned to an existing page or as a note instead.",
                {"limit": "new_pages_per_work"},
            )

    def _check_archives(
        self, conn: Any, actor: KnowledgeActor, *, limits: KnowledgeWriteLimits, now: datetime, page_id: str
    ) -> None:
        p = self._dialect.bind
        used = self._count_events(
            conn,
            f"actor_id = {p} AND action = {p} AND {self._counted()} AND created_at_utc >= {p}",
            (actor.actor_id, WriteAction.ARCHIVE.value, *_COUNTED_OUTCOMES, utc_text(now - timedelta(hours=1))),
        )
        if used >= limits.archives_per_agent_per_hour:
            raise KnowledgeError(
                CODE_LIMIT,
                f"You reached the limit of {limits.archives_per_agent_per_hour} archived pages per hour.",
                {"limit": "archives_per_agent_per_hour", "page_id": page_id},
            )

    def _check_page_edits(
        self,
        conn: Any,
        actor: KnowledgeActor,
        *,
        page_id: str,
        limits: KnowledgeWriteLimits,
        now: datetime,
        label: str,
    ) -> None:
        p = self._dialect.bind
        used = self._count_events(
            conn,
            f"actor_id = {p} AND page_id = {p} AND action IN ({p}, {p}) AND {self._counted()} AND created_at_utc >= {p}",
            (
                actor.actor_id,
                page_id,
                WriteAction.EDIT.value,
                WriteAction.ARCHIVE.value,
                *_COUNTED_OUTCOMES,
                utc_text(now - timedelta(minutes=limits.window_minutes)),
            ),
        )
        if used >= limits.page_edits_per_agent:
            raise KnowledgeError(
                CODE_LIMIT,
                f"You already changed {label} {used} times in the last {limits.window_minutes} minutes, the limit. "
                "Add a note to the page instead of another edit.",
                {"limit": "page_edits_per_agent", "page_id": page_id},
            )

    def _check_repeat(
        self, conn: Any, change: PageChange, current: KnowledgePage, *, limits: KnowledgeWriteLimits
    ) -> None:
        p = self._dialect.bind
        rows = conn.execute(
            f"""
            SELECT revision, fingerprint FROM knowledge_revisions WHERE page_id = {p}
            ORDER BY revision DESC LIMIT {p}
            """,
            (current.page_id, int(limits.recent_revisions_compared) + 1),
        ).fetchall()
        for row in rows:
            if str(row["fingerprint"]) == change.fingerprint:
                revision = int(row["revision"])
                what = "the current text" if revision == current.revision else f"revision {revision}"
                raise KnowledgeError(
                    CODE_REPEAT,
                    f"Your edit to {change.key.reference()} repeats {what}, so it was not saved. "
                    "If you disagree with a recent change, add a note explaining why instead of undoing it.",
                    {"page_id": current.page_id, "repeated_revision": revision},
                )

    def _check_edit_war(
        self,
        conn: Any,
        change: PageChange,
        current: KnowledgePage,
        *,
        limits: KnowledgeWriteLimits,
        now: datetime,
    ) -> None:
        """Freeze a page that several agents keep changing; the freeze is kept, the edit refused."""

        p = self._dialect.bind
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS edits, COUNT(DISTINCT actor_id) AS agents FROM knowledge_write_events
            WHERE page_id = {p} AND action IN ({p}, {p}) AND outcome = {p} AND actor_kind = {p}
              AND created_at_utc >= {p}
            """,
            (
                current.page_id,
                WriteAction.EDIT.value,
                WriteAction.ARCHIVE.value,
                WriteOutcome.APPLIED.value,
                change.actor.kind.value,
                utc_text(now - timedelta(minutes=limits.window_minutes)),
            ),
        ).fetchone()
        edits, agents = int(row["edits"] or 0), int(row["agents"] or 0)
        if edits < limits.edit_war_edits or agents < 2:
            return
        frozen_until = utc_text(now + timedelta(minutes=limits.edit_war_freeze_minutes))
        conn.execute(
            f"UPDATE knowledge_pages SET frozen_until_utc = {p} WHERE page_id = {p}",
            (frozen_until, current.page_id),
        )
        # The store froze it, not the agent whose edit was refused.
        self._insert_event(
            conn,
            KnowledgeActor(kind=ActorKind.SYSTEM, actor_id="", team_id=current.team_id),
            action=WriteAction.FREEZE,
            outcome=WriteOutcome.APPLIED,
            page_id=current.page_id,
            contribution_id="",
            now=now,
            reason=FREEZE_REASON_EDIT_WAR,
        )
        conn.commit()
        self._logger.warning(
            "Knowledge page frozen after repeated edits by several agents. event=knowledge_page_frozen "
            "page_id=%s team_id=%s edits=%s agents=%s frozen_until=%s",
            current.page_id,
            current.team_id,
            edits,
            agents,
            frozen_until,
        )
        raise KnowledgeError(
            CODE_FROZEN,
            f"{change.key.reference()} was changed {edits} times by {agents} agents in the last "
            f"{limits.window_minutes} minutes, so it is now frozen until {frozen_until}. "
            "Add a note with your change instead; the curator will merge it.",
            {"page_id": current.page_id, "frozen_until_utc": frozen_until},
        )

    def merge_pages(self, merge: PageMerge, *, now: datetime) -> tuple[KnowledgePage, KnowledgePage]:
        """Merge one page into another in one transaction; returns (source, target) as stored.

        Writes the target's new revision and archives the source (both
        ``ChangeKind.MERGE``, base revisions checked), keeps the source's
        name as a redirect to the target (moving on any redirects that led to
        the source), moves the source's open notes to the target, and points
        pending contributions for the source at the target. A refusal changes
        nothing and is recorded in the edit log.
        """

        try:
            with self._write() as conn:
                return self._merge_pages(conn, merge, now=now)
        except KnowledgeError as error:
            self.record_refusal(
                merge.target.actor,
                action=WriteAction.MERGE,
                page_id=str(error.details.get("page_id") or ""),
                reason=error.code,
                now=now,
            )
            raise

    def _merge_pages(self, conn: Any, merge: PageMerge, *, now: datetime) -> tuple[KnowledgePage, KnowledgePage]:
        if merge.refuse_frozen:
            for change in (merge.source, merge.target):
                self._refuse_frozen(conn, change.key, now=now)
        target = self._apply_page_change(conn, merge.target, action=WriteAction.MERGE, limits=None, now=now)
        source = self._apply_page_change(conn, merge.source, action=WriteAction.MERGE, limits=None, now=now)
        p = self._dialect.bind
        conn.execute(
            f"UPDATE knowledge_redirects SET to_page_id = {p} WHERE to_page_id = {p}", (target.page_id, source.page_id)
        )
        conn.execute(
            f"""
            INSERT INTO knowledge_redirects (
                team_id, space, owner_person_id, from_slug, from_page_id, to_page_id, created_at_utc
            ) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})
            """,
            (
                source.team_id,
                source.space.value,
                source.owner_person_id,
                source.slug,
                source.page_id,
                target.page_id,
                utc_text(now),
            ),
        )
        notes = conn.execute(
            f"UPDATE knowledge_notes SET page_id = {p} WHERE page_id = {p} AND status = {p}",
            (target.page_id, source.page_id, NoteStatus.OPEN.value),
        ).rowcount
        # Proposed against the target as it was before the merge, so publishing
        # one unchanged meets a revision conflict and the reviewer merges it.
        contributions = conn.execute(
            f"""
            UPDATE knowledge_contributions SET page_id = {p}, slug = {p}, base_revision = {p}
            WHERE page_id = {p} AND status = {p}
            """,
            (
                target.page_id,
                target.slug,
                merge.target.base_revision,
                source.page_id,
                ContributionStatus.PENDING.value,
            ),
        ).rowcount
        self._logger.info(
            "Knowledge pages merged. event=knowledge_pages_merged team_id=%s source_page_id=%s target_page_id=%s "
            "target_revision=%s notes_moved=%s contributions_moved=%s actor_kind=%s actor_id=%s",
            target.team_id,
            source.page_id,
            target.page_id,
            target.revision,
            notes,
            contributions,
            merge.target.actor.kind.value,
            merge.target.actor.actor_id,
        )
        return source, target

    def _refuse_frozen(self, conn: Any, key: PageKey, *, now: datetime) -> None:
        page = self._page_for_update(conn, key)
        if page is not None and page.frozen_until_utc and page.frozen_until_utc > utc_text(now):
            raise KnowledgeError(
                CODE_FROZEN,
                f"{key.reference()} is frozen until {page.frozen_until_utc}. Merge it after the freeze ends, "
                "or lift the freeze first if the pages are settled.",
                {"page_id": page.page_id, "frozen_until_utc": page.frozen_until_utc},
            )

    def add_note(
        self,
        page: KnowledgePage,
        *,
        body: str,
        actor: KnowledgeActor,
        limits: Optional[KnowledgeWriteLimits],
        now: datetime,
        contribution_id: str = "",
    ) -> KnowledgeNote:
        note = KnowledgeNote(
            note_id=new_id(NOTE_ID_PREFIX),
            page_id=page.page_id,
            team_id=page.team_id,
            space=page.space,
            owner_person_id=page.owner_person_id,
            body=body,
            status=NoteStatus.OPEN,
            actor_kind=actor.kind.value,
            actor_id=actor.actor_id,
            actor_agent_type=actor.agent_type,
            work_person_id=actor.work_person_id,
            created_at_utc=utc_text(now),
        )
        p = self._dialect.bind
        try:
            with self._write() as conn:
                # The service read the page before this transaction. A merge
                # may have committed meanwhile, so follow its name again under
                # the write lock, including when publishing a proposed note.
                page = self._redirect_target(conn, page.key) or page
                note = replace(
                    note, page_id=page.page_id, team_id=page.team_id,
                    space=page.space, owner_person_id=page.owner_person_id,
                )
                if limits is not None:
                    self._check_hourly_writes(conn, actor, limits=limits, now=now, page_id=page.page_id)
                conn.execute(
                    f"""
                    INSERT INTO knowledge_notes (
                        note_id, page_id, team_id, space, owner_person_id, body, status,
                        actor_kind, actor_id, actor_agent_type, actor_display_name,
                        work_person_id, task_id, work_id, contribution_id, created_at_utc
                    ) VALUES ({', '.join([p] * 16)})
                    """,
                    (
                        note.note_id,
                        note.page_id,
                        note.team_id,
                        note.space.value,
                        note.owner_person_id,
                        note.body,
                        note.status.value,
                        actor.kind.value,
                        actor.actor_id,
                        actor.agent_type,
                        actor.display_name,
                        actor.work_person_id,
                        actor.task_id,
                        _work_key(actor),
                        contribution_id,
                        note.created_at_utc,
                    ),
                )
                if contribution_id:
                    self._mark_published(conn, contribution_id, actor=actor, revision=page.revision, now=now)
                self._insert_event(
                    conn,
                    actor,
                    action=WriteAction.NOTE,
                    outcome=WriteOutcome.APPLIED,
                    page_id=page.page_id,
                    contribution_id=contribution_id,
                    now=now,
                )
        except KnowledgeError as error:
            self.record_refusal(actor, action=WriteAction.NOTE, page_id=page.page_id, reason=error.code, now=now)
            raise
        return note

    def add_contribution(
        self, contribution: KnowledgeContribution, *, limits: Optional[KnowledgeWriteLimits], now: datetime,
        write_key: Optional[KnowledgeWriteKey] = None,
    ) -> KnowledgeContribution:
        """Store a team change proposed from a person's work; it stays private to that person until published."""

        actor = _contribution_actor(contribution)
        action = _action_for_contribution(contribution.kind)
        stored = replace(contribution, status=ContributionStatus.PENDING, created_at_utc=utc_text(now))
        p = self._dialect.bind
        try:
            with self._write() as conn:
                if write_key is not None:
                    receipt = self._write_receipt(conn, actor, write_key)
                    if receipt is not None:
                        row = conn.execute(
                            f"SELECT * FROM knowledge_contributions WHERE contribution_id = {p}",
                            (receipt.contribution_id,),
                        ).fetchone()
                        if row is None:
                            raise KnowledgeError(CODE_NOT_FOUND, "The saved lesson's proposed change is no longer available.")
                        return self._contribution(row)
                stored = self._contribution_after_merge(conn, stored)
                if limits is not None:
                    self._check_hourly_writes(conn, actor, limits=limits, now=now, page_id=stored.page_id)
                    # Pending archives spend the same budget as applied archives.
                    # Check before inserting their event while the write lock is held.
                    if action is WriteAction.ARCHIVE:
                        self._check_archives(conn, actor, limits=limits, now=now, page_id=stored.page_id)
                    if action is WriteAction.CREATE:
                        self._check_new_pages(conn, actor, limits=limits, now=now)
                    elif action in (WriteAction.EDIT, WriteAction.ARCHIVE):
                        self._check_page_edits(
                            conn,
                            actor,
                            page_id=stored.page_id,
                            limits=limits,
                            now=now,
                            label=f"{KnowledgeSpace.TEAM.value}/{stored.slug}",
                        )
                conn.execute(
                    f"""
                    INSERT INTO knowledge_contributions (
                        contribution_id, team_id, owner_person_id, kind, page_id, slug, base_revision,
                        title, page_type, summary, tags_json, body, change_summary, review_kinds_json, status,
                        actor_kind, actor_id, actor_agent_type, actor_display_name, task_id, work_id, created_at_utc
                    ) VALUES ({', '.join([p] * 22)})
                    """,
                    (
                        stored.contribution_id,
                        stored.team_id,
                        stored.owner_person_id,
                        stored.kind.value,
                        stored.page_id,
                        stored.slug,
                        stored.base_revision,
                        stored.title,
                        stored.page_type,
                        stored.summary,
                        _tags_json(stored.tags),
                        stored.body,
                        stored.change_summary,
                        json.dumps(list(stored.review_kinds)),
                        stored.status.value,
                        stored.actor_kind,
                        stored.actor_id,
                        stored.actor_agent_type,
                        actor.display_name,
                        stored.task_id,
                        _work_key(actor),
                        stored.created_at_utc,
                    ),
                )
                self._insert_event(
                    conn,
                    actor,
                    action=action,
                    outcome=WriteOutcome.PENDING,
                    page_id=stored.page_id,
                    contribution_id=stored.contribution_id,
                    now=now,
                )
                if write_key is not None:
                    self._store_write_receipt(
                        conn, actor, write_key,
                        KnowledgeWriteReceipt(
                            reference=f"{KnowledgeSpace.TEAM.value}/{stored.slug}", page_id=stored.page_id,
                            contribution_id=stored.contribution_id,
                        ),
                        now=now,
                    )
        except KnowledgeError as error:
            self.record_refusal(
                actor, action=action, page_id=str(error.details.get("page_id") or stored.page_id), reason=error.code, now=now
            )
            raise
        return stored

    def _contribution_after_merge(
        self, conn: Any, contribution: KnowledgeContribution
    ) -> KnowledgeContribution:
        """Follow a merge under the write lock and require another edit/archive review."""

        key = PageKey(contribution.team_id, KnowledgeSpace.TEAM, "", contribution.slug)
        target = self._redirect_target(conn, key)
        if target is None:
            return contribution
        if contribution.kind is ContributionKind.CREATE:
            raise KnowledgeError(
                CODE_MERGED,
                f"{key.reference()} was merged into {target.reference()}, and its name now leads there. "
                "Choose another name for a new page.",
                {"page_id": target.page_id, "redirects_to": target.reference()},
            )
        # A proposal prepared before the merge must be reviewed against the
        # target's current text rather than overwriting it unchanged.
        return replace(
            contribution, page_id=target.page_id, slug=target.slug,
            base_revision=target.revision - 1,
        )

    def decide_contribution(
        self,
        contribution_id: str,
        *,
        status: ContributionStatus,
        actor: KnowledgeActor,
        reason: str,
        now: datetime,
    ) -> KnowledgeContribution:
        """Reject or withdraw a pending contribution (publishing goes through the page change)."""

        p = self._dialect.bind
        with self._write() as conn:
            updated = conn.execute(
                f"""
                UPDATE knowledge_contributions
                SET status = {p}, decided_at_utc = {p}, decided_by_kind = {p}, decided_by_id = {p}, decision_reason = {p}
                WHERE contribution_id = {p} AND status = {p}
                """,
                (
                    status.value,
                    utc_text(now),
                    actor.kind.value,
                    actor.actor_id,
                    reason,
                    contribution_id,
                    ContributionStatus.PENDING.value,
                ),
            ).rowcount
            if not updated:
                raise KnowledgeError(CODE_NOT_PENDING, "That contribution is no longer waiting for a decision.")
            self._insert_event(
                conn,
                actor,
                action=WriteAction.DECIDE,
                outcome=WriteOutcome.APPLIED,
                page_id="",
                contribution_id=contribution_id,
                now=now,
                reason=status.value,
            )
            row = conn.execute(
                f"SELECT * FROM knowledge_contributions WHERE contribution_id = {p}", (contribution_id,)
            ).fetchone()
        return self._contribution(row)

    def record_refusal(self, actor: KnowledgeActor, *, action: WriteAction, page_id: str, reason: str, now: datetime) -> None:
        """Log a refused write (outside the refused transaction, which rolled back)."""

        if repositories_are_read_only():
            return
        with self._connect() as conn:
            self._insert_event(
                conn, actor, action=action, outcome=WriteOutcome.REFUSED, page_id=page_id, contribution_id="", now=now, reason=reason
            )
            conn.commit()
        self._logger.info(
            "Knowledge write refused. event=knowledge_write_refused reason=%s action=%s actor_kind=%s actor_id=%s "
            "agent_type=%s page_id=%s team_id=%s task_id=%s",
            reason,
            action.value,
            actor.kind.value,
            actor.actor_id,
            actor.agent_type,
            page_id,
            actor.team_id,
            actor.task_id,
        )

    # -- curation ----------------------------------------------------------------------------

    def activity(self, *, team_id: str, since: datetime, now: datetime, limit: int) -> ActivityReport:
        """Writes per agent type since ``since``, the busiest team pages, and frozen pages."""

        p = self._dialect.bind
        stamp = utc_text(since)
        # Freezes change no text, so they are not writes here (PAGE_STATE_ACTIONS).
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT actor_agent_type, outcome, reason, COUNT(*) AS writes FROM knowledge_write_events
                WHERE team_id = {p} AND created_at_utc >= {p} AND actor_kind = {p} AND action NOT IN ({p}, {p})
                GROUP BY actor_agent_type, outcome, reason
                """,
                (team_id, stamp, ActorKind.AGENT.value, *_PAGE_STATE_VALUES),
            ).fetchall()
            busiest = conn.execute(
                f"""
                SELECT p.slug, COUNT(*) AS edits FROM knowledge_write_events e
                JOIN knowledge_pages p ON p.page_id = e.page_id
                WHERE e.team_id = {p} AND e.created_at_utc >= {p} AND e.outcome = {p} AND p.space = {p}
                  AND e.action NOT IN ({p}, {p})
                GROUP BY p.slug ORDER BY edits DESC, p.slug LIMIT {p}
                """,
                (team_id, stamp, WriteOutcome.APPLIED.value, KnowledgeSpace.TEAM.value, *_PAGE_STATE_VALUES, int(limit)),
            ).fetchall()
            frozen = conn.execute(
                f"""
                SELECT slug, frozen_until_utc FROM knowledge_pages
                WHERE team_id = {p} AND space = {p} AND frozen_until_utc > {p} ORDER BY slug LIMIT {p}
                """,
                (team_id, KnowledgeSpace.TEAM.value, utc_text(now), int(limit)),
            ).fetchall()
            recently_frozen = self._recently_frozen(conn, team_id, stamp=stamp, now=now, limit=limit)
        by_type: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = by_type.setdefault(str(row["actor_agent_type"] or ""), {"applied": 0, "pending": 0, "refused": {}})
            outcome, count = str(row["outcome"]), int(row["writes"])
            if outcome == WriteOutcome.REFUSED.value:
                entry["refused"][str(row["reason"] or "")] = entry["refused"].get(str(row["reason"] or ""), 0) + count
            else:
                entry[outcome] = entry.get(outcome, 0) + count
        return ActivityReport(
            since_utc=stamp,
            by_agent_type=[
                AgentActivity(agent_type, entry["applied"], entry["pending"], dict(entry["refused"]))
                for agent_type, entry in sorted(by_type.items())
            ],
            busiest_pages=[(f"{KnowledgeSpace.TEAM.value}/{row['slug']}", int(row["edits"])) for row in busiest],
            frozen_pages=[(f"{KnowledgeSpace.TEAM.value}/{row['slug']}", str(row["frozen_until_utc"])) for row in frozen],
            recently_frozen=recently_frozen,
        )

    def _recently_frozen(self, conn: Any, team_id: str, *, stamp: str, now: datetime, limit: int) -> list[RecentFreeze]:
        """Team freeze events in the report's period, even after expiry or an early lift.

        The curator runs daily while automatic freezes usually last an hour.
        Selecting only pages still frozen would lose most events before an
        administrator could hear about them. Current state is carried along
        for accurate wording; personal page names stay private.
        """

        p = self._dialect.bind
        rows = conn.execute(
            f"""
            SELECT p.slug, p.title, p.frozen_until_utc,
                   MAX(CASE WHEN e.reason = {p} THEN 1 ELSE 0 END) AS edit_war
            FROM knowledge_write_events e JOIN knowledge_pages p ON p.page_id = e.page_id
            WHERE e.team_id = {p} AND e.action = {p} AND e.outcome = {p}
              AND e.created_at_utc >= {p} AND e.created_at_utc <= {p} AND p.space = {p}
            GROUP BY p.slug, p.title, p.frozen_until_utc ORDER BY p.slug LIMIT {p}
            """,
            (
                FREEZE_REASON_EDIT_WAR,
                team_id,
                WriteAction.FREEZE.value,
                WriteOutcome.APPLIED.value,
                stamp,
                utc_text(now),
                KnowledgeSpace.TEAM.value,
                int(limit),
            ),
        ).fetchall()
        return [
            RecentFreeze(
                reference=f"{KnowledgeSpace.TEAM.value}/{row['slug']}",
                title=str(row["title"]),
                frozen_until_utc=str(row["frozen_until_utc"]) if row["frozen_until_utc"] is not None else None,
                after_edit_war=bool(row["edit_war"]),
            )
            for row in rows
        ]

    def list_open_notes(self, *, team_id: str, space: KnowledgeSpace, owner_person_id: str, limit: int) -> list[KnowledgeNote]:
        p = self._dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM knowledge_notes
                WHERE team_id = {p} AND space = {p} AND owner_person_id = {p} AND status = {p}
                ORDER BY created_at_utc, note_id LIMIT {p}
                """,
                (team_id, space.value, owner_person_id, NoteStatus.OPEN.value, int(limit)),
            ).fetchall()
        return [self._note(row) for row in rows]

    def resolve_note(self, note_id: str, *, team_id: str, status: NoteStatus, actor: KnowledgeActor, now: datetime) -> bool:
        p = self._dialect.bind
        with self._write() as conn:
            updated = conn.execute(
                f"""
                UPDATE knowledge_notes SET status = {p}, resolved_at_utc = {p}, resolved_by_kind = {p}, resolved_by_id = {p}
                WHERE note_id = {p} AND team_id = {p} AND status = {p}
                """,
                (status.value, utc_text(now), actor.kind.value, actor.actor_id, note_id, team_id, NoteStatus.OPEN.value),
            ).rowcount
        return bool(updated)

    def set_frozen_until(
        self, page_id: str, frozen_until_utc: Optional[str], *, actor: KnowledgeActor, now: datetime
    ) -> None:
        """Freeze a page until a time, or lift its freeze (``None``), and record which in the edit log."""

        with self._write() as conn:
            conn.execute(
                f"UPDATE knowledge_pages SET frozen_until_utc = {self._dialect.bind} WHERE page_id = {self._dialect.bind}",
                (frozen_until_utc, page_id),
            )
            self._insert_event(
                conn,
                actor,
                action=WriteAction.FREEZE if frozen_until_utc else WriteAction.UNFREEZE,
                outcome=WriteOutcome.APPLIED,
                page_id=page_id,
                contribution_id="",
                now=now,
            )

    def page_problems(self, *, team_id: str, stale_before: datetime, limit: int) -> PageProblems:
        """Team-space checks: broken links, stale pages, open notes and exact duplicates."""

        p = self._dialect.bind
        team = KnowledgeSpace.TEAM.value
        with self._connect() as conn:
            broken = conn.execute(
                f"""
                SELECT p.slug, l.target_slug FROM knowledge_links l
                JOIN knowledge_pages p ON p.page_id = l.page_id
                WHERE l.team_id = {p} AND p.space = {p} AND l.target_space = {p} AND p.status = {p}
                  AND NOT EXISTS (
                    SELECT 1 FROM knowledge_pages t
                    WHERE t.team_id = l.team_id AND t.space = {p} AND t.owner_person_id = '' AND t.slug = l.target_slug
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM knowledge_redirects r
                    WHERE r.team_id = l.team_id AND r.space = {p} AND r.owner_person_id = '' AND r.from_slug = l.target_slug
                  )
                ORDER BY p.slug, l.target_slug LIMIT {p}
                """,
                (team_id, team, team, PageStatus.ACTIVE.value, team, team, int(limit)),
            ).fetchall()
            stale = conn.execute(
                f"""
                SELECT slug, updated_at_utc FROM knowledge_pages
                WHERE team_id = {p} AND space = {p} AND status = {p} AND updated_at_utc < {p}
                ORDER BY updated_at_utc, slug LIMIT {p}
                """,
                (team_id, team, PageStatus.ACTIVE.value, utc_text(stale_before), int(limit)),
            ).fetchall()
            noted = conn.execute(
                f"""
                SELECT p.slug, COUNT(*) AS notes FROM knowledge_notes n JOIN knowledge_pages p ON p.page_id = n.page_id
                WHERE n.team_id = {p} AND n.space = {p} AND n.status = {p}
                GROUP BY p.slug ORDER BY notes DESC, p.slug LIMIT {p}
                """,
                (team_id, team, NoteStatus.OPEN.value, int(limit)),
            ).fetchall()
            # Fingerprints already normalize case and whitespace across all
            # page content. Choose one stable name per group, then report each
            # extra active team page once. Personal and other teams' pages
            # must not create candidates or reach the maintenance report.
            duplicates = conn.execute(
                f"""
                SELECT d.kept_slug, p.slug FROM knowledge_pages p
                JOIN (
                    SELECT fingerprint, MIN(slug) AS kept_slug FROM knowledge_pages
                    WHERE team_id = {p} AND space = {p} AND status = {p}
                    GROUP BY fingerprint HAVING COUNT(*) > 1
                ) d ON p.fingerprint = d.fingerprint AND p.slug <> d.kept_slug
                WHERE p.team_id = {p} AND p.space = {p} AND p.status = {p}
                ORDER BY d.kept_slug, p.slug LIMIT {p}
                """,
                (team_id, team, PageStatus.ACTIVE.value, team_id, team, PageStatus.ACTIVE.value, int(limit)),
            ).fetchall()
        return PageProblems(
            broken_links=[(f"{team}/{row[0]}", f"{team}/{row[1]}") for row in broken],
            stale_pages=[(f"{team}/{row[0]}", str(row[1])) for row in stale],
            pages_with_open_notes=[(f"{team}/{row[0]}", int(row[1])) for row in noted],
            duplicate_pages=[(f"{team}/{row[0]}", f"{team}/{row[1]}") for row in duplicates],
        )

    # -- write helpers ---------------------------------------------------------------------

    def _page_for_update(self, conn: Any, key: PageKey) -> Optional[KnowledgePage]:
        p = self._dialect.bind
        row = conn.execute(
            f"""
            SELECT * FROM knowledge_pages
            WHERE team_id = {p} AND space = {p} AND owner_person_id = {p} AND slug = {p}
            """,
            (key.team_id, key.space.value, key.owner_person_id, key.slug),
        ).fetchone()
        return self._page(row) if row is not None else None

    def _insert_page(self, conn: Any, change: PageChange, *, now: datetime) -> KnowledgePage:
        p = self._dialect.bind
        stamp = utc_text(now)
        page_id = new_id(PAGE_ID_PREFIX)
        conn.execute(
            f"""
            INSERT INTO knowledge_pages (
                page_id, team_id, space, owner_person_id, slug, title, page_type, summary, tags_json, body,
                status, revision, fingerprint, frozen_until_utc, created_at_utc, updated_at_utc,
                updated_by_kind, updated_by_id, updated_by_agent_type
            ) VALUES ({', '.join([p] * 19)})
            """,
            (
                page_id,
                change.key.team_id,
                change.key.space.value,
                change.key.owner_person_id,
                change.key.slug,
                change.draft.title,
                change.draft.page_type,
                change.draft.summary,
                _tags_json(change.draft.tags),
                change.draft.body,
                change.status.value,
                1,
                change.fingerprint,
                None,
                stamp,
                stamp,
                change.actor.kind.value,
                change.actor.actor_id,
                change.actor.agent_type,
            ),
        )
        return self._page_after_write(conn, page_id)

    def _update_page(self, conn: Any, change: PageChange, current: KnowledgePage, *, now: datetime) -> KnowledgePage:
        p = self._dialect.bind
        updated = conn.execute(
            f"""
            UPDATE knowledge_pages
            SET title = {p}, page_type = {p}, summary = {p}, tags_json = {p}, body = {p}, status = {p},
                revision = {p}, fingerprint = {p}, updated_at_utc = {p}, updated_by_kind = {p},
                updated_by_id = {p}, updated_by_agent_type = {p}
            WHERE page_id = {p} AND revision = {p}
            """,
            (
                change.draft.title,
                change.draft.page_type,
                change.draft.summary,
                _tags_json(change.draft.tags),
                change.draft.body,
                change.status.value,
                current.revision + 1,
                change.fingerprint,
                utc_text(now),
                change.actor.kind.value,
                change.actor.actor_id,
                change.actor.agent_type,
                current.page_id,
                current.revision,
            ),
        ).rowcount
        if not updated:
            # The write lock makes this unreachable; keep the check so a lost update can never pass silently.
            raise KnowledgeError(CODE_CONFLICT, f"{change.key.reference()} changed during the save. Read it again.")
        return self._page_after_write(conn, current.page_id)

    def _page_after_write(self, conn: Any, page_id: str) -> KnowledgePage:
        row = conn.execute(f"SELECT * FROM knowledge_pages WHERE page_id = {self._dialect.bind}", (page_id,)).fetchone()
        return self._page(row)

    def _insert_revision(self, conn: Any, page: KnowledgePage, change: PageChange, *, now: datetime) -> None:
        p = self._dialect.bind
        actor = change.actor
        conn.execute(
            f"""
            INSERT INTO knowledge_revisions (
                revision_id, page_id, revision, team_id, space, owner_person_id, title, page_type, summary,
                tags_json, body, status, fingerprint, change_kind, change_summary, actor_kind, actor_id,
                actor_agent_type, actor_display_name, work_person_id, task_id, work_id, contribution_id, created_at_utc
            ) VALUES ({', '.join([p] * 24)})
            """,
            (
                new_id(REVISION_ID_PREFIX),
                page.page_id,
                page.revision,
                page.team_id,
                page.space.value,
                page.owner_person_id,
                page.title,
                page.page_type,
                page.summary,
                _tags_json(page.tags),
                page.body,
                page.status.value,
                change.fingerprint,
                change.change_kind.value,
                change.change_summary,
                actor.kind.value,
                actor.actor_id,
                actor.agent_type,
                actor.display_name,
                actor.work_person_id,
                actor.task_id,
                _work_key(actor),
                change.contribution_id,
                utc_text(now),
            ),
        )

    def _replace_links(
        self, conn: Any, page: KnowledgePage, targets: tuple[tuple[KnowledgeSpace, str], ...]
    ) -> None:
        p = self._dialect.bind
        conn.execute(f"DELETE FROM knowledge_links WHERE page_id = {p}", (page.page_id,))
        for space, slug in dict.fromkeys(targets):
            conn.execute(
                f"""
                INSERT INTO knowledge_links (page_id, team_id, owner_person_id, target_space, target_slug)
                VALUES ({p}, {p}, {p}, {p}, {p})
                """,
                (page.page_id, page.team_id, page.owner_person_id, space.value, slug),
            )

    def _mark_published(
        self, conn: Any, contribution_id: str, *, actor: KnowledgeActor, revision: int, now: datetime
    ) -> None:
        p = self._dialect.bind
        updated = conn.execute(
            f"""
            UPDATE knowledge_contributions
            SET status = {p}, decided_at_utc = {p}, decided_by_kind = {p}, decided_by_id = {p}, published_revision = {p}
            WHERE contribution_id = {p} AND status = {p}
            """,
            (
                ContributionStatus.PUBLISHED.value,
                utc_text(now),
                actor.kind.value,
                actor.actor_id,
                int(revision),
                contribution_id,
                ContributionStatus.PENDING.value,
            ),
        ).rowcount
        if not updated:
            raise KnowledgeError(CODE_NOT_PENDING, "That contribution is no longer waiting for a decision.")

    def _insert_event(
        self,
        conn: Any,
        actor: KnowledgeActor,
        *,
        action: WriteAction,
        outcome: WriteOutcome,
        page_id: str,
        contribution_id: str,
        now: datetime,
        reason: str = "",
    ) -> None:
        p = self._dialect.bind
        conn.execute(
            f"""
            INSERT INTO knowledge_write_events (
                event_id, team_id, page_id, contribution_id, action, outcome, reason, actor_kind, actor_id,
                actor_agent_type, work_person_id, task_id, work_id, created_at_utc
            ) VALUES ({', '.join([p] * 14)})
            """,
            (
                new_id(EVENT_ID_PREFIX),
                actor.team_id,
                page_id,
                contribution_id,
                action.value,
                outcome.value,
                reason,
                actor.kind.value,
                actor.actor_id,
                actor.agent_type,
                actor.work_person_id,
                actor.task_id,
                _work_key(actor),
                utc_text(now),
            ),
        )

    # -- transfer --------------------------------------------------------------------------

    def export_rows(self, table: str) -> list[dict[str, Any]]:
        """Every stored row of one knowledge table, for the team transfer."""

        _require_transfer_table(table)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        return [dict(row) for row in rows]

    def import_rows(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Insert transferred rows, keeping ids; rows already present are left as they are."""

        _require_transfer_table(table)
        if not rows:
            return 0
        p = self._dialect.bind
        inserted = 0
        with self._write() as conn:
            for row in rows:
                columns = list(row)
                inserted += int(
                    conn.execute(
                        f"""
                        INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join([p] * len(columns))})
                        ON CONFLICT DO NOTHING
                        """,
                        tuple(row[column] for column in columns),
                    ).rowcount
                    or 0
                )
        return inserted

    # -- row conversion --------------------------------------------------------------------

    @staticmethod
    def _page(row: KnowledgeDatabaseRow) -> KnowledgePage:
        return KnowledgePage(
            page_id=str(row["page_id"]),
            team_id=str(row["team_id"]),
            space=KnowledgeSpace(str(row["space"])),
            owner_person_id=str(row["owner_person_id"] or ""),
            slug=str(row["slug"]),
            title=str(row["title"]),
            page_type=str(row["page_type"]),
            summary=str(row["summary"] or ""),
            tags=_tags(str(row["tags_json"] or "[]")),
            body=str(row["body"]),
            status=PageStatus(str(row["status"])),
            revision=int(row["revision"]),
            frozen_until_utc=row["frozen_until_utc"],
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
            updated_by_kind=str(row["updated_by_kind"]),
            updated_by_agent_type=str(row["updated_by_agent_type"] or ""),
        )

    @staticmethod
    def _revision(row: KnowledgeDatabaseRow) -> KnowledgeRevision:
        return KnowledgeRevision(
            revision_id=str(row["revision_id"]),
            page_id=str(row["page_id"]),
            revision=int(row["revision"]),
            title=str(row["title"]),
            page_type=str(row["page_type"]),
            summary=str(row["summary"] or ""),
            tags=_tags(str(row["tags_json"] or "[]")),
            body=str(row["body"]),
            status=PageStatus(str(row["status"])),
            change_kind=ChangeKind(str(row["change_kind"])),
            change_summary=str(row["change_summary"] or ""),
            actor_kind=str(row["actor_kind"]),
            actor_id=str(row["actor_id"] or ""),
            actor_agent_type=str(row["actor_agent_type"] or ""),
            work_person_id=str(row["work_person_id"] or ""),
            contribution_id=str(row["contribution_id"] or ""),
            created_at_utc=str(row["created_at_utc"]),
        )

    @staticmethod
    def _note(row: KnowledgeDatabaseRow) -> KnowledgeNote:
        return KnowledgeNote(
            note_id=str(row["note_id"]),
            page_id=str(row["page_id"]),
            team_id=str(row["team_id"]),
            space=KnowledgeSpace(str(row["space"])),
            owner_person_id=str(row["owner_person_id"] or ""),
            body=str(row["body"]),
            status=NoteStatus(str(row["status"])),
            actor_kind=str(row["actor_kind"]),
            actor_id=str(row["actor_id"] or ""),
            actor_agent_type=str(row["actor_agent_type"] or ""),
            work_person_id=str(row["work_person_id"] or ""),
            created_at_utc=str(row["created_at_utc"]),
        )

    @staticmethod
    def _contribution(row: KnowledgeDatabaseRow) -> KnowledgeContribution:
        return KnowledgeContribution(
            contribution_id=str(row["contribution_id"]),
            team_id=str(row["team_id"]),
            owner_person_id=str(row["owner_person_id"]),
            kind=ContributionKind(str(row["kind"])),
            page_id=str(row["page_id"] or ""),
            slug=str(row["slug"]),
            base_revision=int(row["base_revision"] or 0),
            title=str(row["title"] or ""),
            page_type=str(row["page_type"] or ""),
            summary=str(row["summary"] or ""),
            tags=_tags(str(row["tags_json"] or "[]")),
            body=str(row["body"] or ""),
            change_summary=str(row["change_summary"] or ""),
            review_kinds=tuple(json.loads(str(row["review_kinds_json"] or "[]"))),
            status=ContributionStatus(str(row["status"])),
            actor_kind=str(row["actor_kind"]),
            actor_id=str(row["actor_id"] or ""),
            actor_agent_type=str(row["actor_agent_type"] or ""),
            task_id=str(row["task_id"] or ""),
            work_id=str(row["work_id"] or ""),
            created_at_utc=str(row["created_at_utc"]),
            decided_at_utc=row["decided_at_utc"],
            decided_by_kind=str(row["decided_by_kind"] or ""),
            decided_by_id=str(row["decided_by_id"] or ""),
            decision_reason=str(row["decision_reason"] or ""),
            published_revision=int(row["published_revision"] or 0),
        )


# Tables the team transfer carries, in import order (pages before what refers
# to them), with their stored columns. The edit log is not carried: limits
# start fresh in the new instance. ``tests/test_knowledge_repository.py``
# checks these lists against the created tables.
KNOWLEDGE_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "knowledge_pages": (
        "page_id", "team_id", "space", "owner_person_id", "slug", "title", "page_type", "summary", "tags_json",
        "body", "status", "revision", "fingerprint", "frozen_until_utc", "created_at_utc", "updated_at_utc",
        "updated_by_kind", "updated_by_id", "updated_by_agent_type",
    ),
    "knowledge_revisions": (
        "revision_id", "page_id", "revision", "team_id", "space", "owner_person_id", "title", "page_type",
        "summary", "tags_json", "body", "status", "fingerprint", "change_kind", "change_summary", "actor_kind",
        "actor_id", "actor_agent_type", "actor_display_name", "work_person_id", "task_id", "work_id",
        "contribution_id", "created_at_utc",
    ),
    "knowledge_notes": (
        "note_id", "page_id", "team_id", "space", "owner_person_id", "body", "status", "actor_kind", "actor_id",
        "actor_agent_type", "actor_display_name", "work_person_id", "task_id", "work_id", "contribution_id",
        "created_at_utc", "resolved_at_utc", "resolved_by_kind", "resolved_by_id",
    ),
    "knowledge_links": ("page_id", "team_id", "owner_person_id", "target_space", "target_slug"),
    "knowledge_redirects": (
        "team_id", "space", "owner_person_id", "from_slug", "from_page_id", "to_page_id", "created_at_utc",
    ),
    "knowledge_contributions": (
        "contribution_id", "team_id", "owner_person_id", "kind", "page_id", "slug", "base_revision", "title",
        "page_type", "summary", "tags_json", "body", "change_summary", "review_kinds_json", "status", "actor_kind",
        "actor_id", "actor_agent_type", "actor_display_name", "task_id", "work_id", "created_at_utc",
        "decided_at_utc", "decided_by_kind", "decided_by_id", "decision_reason", "published_revision",
    ),
}
KNOWLEDGE_TRANSFER_TABLES = tuple(KNOWLEDGE_TABLE_COLUMNS)


def knowledge_row_converters() -> dict[str, Any]:
    """Stored row to record, per carried table except links and redirects; the transfer checks rows with these."""

    return {
        "knowledge_pages": _RelationalKnowledgeRepository._page,
        "knowledge_revisions": _RelationalKnowledgeRepository._revision,
        "knowledge_notes": _RelationalKnowledgeRepository._note,
        "knowledge_contributions": _RelationalKnowledgeRepository._contribution,
    }


def _require_transfer_table(table: str) -> None:
    if table not in KNOWLEDGE_TRANSFER_TABLES:
        raise ValueError(f"{table!r} is not a knowledge table the transfer carries.")


def _work_key(actor: KnowledgeActor) -> str:
    """Limits per request count all agents working on it; without one, the task or the agent."""

    return actor.work_id or actor.task_id or actor.actor_id


def _action_for(change_kind: ChangeKind) -> WriteAction:
    return {
        ChangeKind.CREATE: WriteAction.CREATE,
        ChangeKind.EDIT: WriteAction.EDIT,
        ChangeKind.REVERT: WriteAction.EDIT,
        ChangeKind.ARCHIVE: WriteAction.ARCHIVE,
        ChangeKind.RESTORE: WriteAction.RESTORE,
        ChangeKind.MERGE: WriteAction.MERGE,
    }[change_kind]


def _action_for_contribution(kind: ContributionKind) -> WriteAction:
    return {
        ContributionKind.CREATE: WriteAction.CREATE,
        ContributionKind.EDIT: WriteAction.EDIT,
        ContributionKind.NOTE: WriteAction.NOTE,
        ContributionKind.ARCHIVE: WriteAction.ARCHIVE,
    }[kind]


def _contribution_actor(contribution: KnowledgeContribution) -> KnowledgeActor:
    return KnowledgeActor(
        kind=ActorKind(contribution.actor_kind),
        actor_id=contribution.actor_id,
        team_id=contribution.team_id,
        agent_type=contribution.actor_agent_type,
        work_person_id=contribution.owner_person_id,
        task_id=contribution.task_id,
        work_id=contribution.work_id,
    )


class SqliteKnowledgeRepository(_RelationalKnowledgeRepository):
    """SQLite: FTS5 over the page rows (external content), kept in sync by triggers."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger, row_factory=sqlite3.Row)

    def _search_index_statements(self) -> list[str]:
        columns = "title, summary, tags_json, body"
        new_values = "new.title, new.summary, new.tags_json, new.body"
        old_values = "old.title, old.summary, old.tags_json, old.body"
        return [
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_pages_fts USING fts5(
                {columns}, content='knowledge_pages', content_rowid='rowid',
                tokenize='porter unicode61 remove_diacritics 2'
            )
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS knowledge_pages_fts_insert AFTER INSERT ON knowledge_pages BEGIN
                INSERT INTO knowledge_pages_fts(rowid, {columns}) VALUES (new.rowid, {new_values});
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS knowledge_pages_fts_delete AFTER DELETE ON knowledge_pages BEGIN
                INSERT INTO knowledge_pages_fts(knowledge_pages_fts, rowid, {columns})
                VALUES ('delete', old.rowid, {old_values});
            END
            """,
            f"""
            CREATE TRIGGER IF NOT EXISTS knowledge_pages_fts_update AFTER UPDATE ON knowledge_pages BEGIN
                INSERT INTO knowledge_pages_fts(knowledge_pages_fts, rowid, {columns})
                VALUES ('delete', old.rowid, {old_values});
                INSERT INTO knowledge_pages_fts(rowid, {columns}) VALUES (new.rowid, {new_values});
            END
            """,
        ]

    def _search_rows(
        self, conn: Any, terms: list[str], *, any_word: bool, where: str, params: tuple[Any, ...], limit: int
    ) -> list[Any]:
        # Each term is letters and digits only (knowledge_base.text.query_terms), quoted as an FTS5 string.
        match = (" OR " if any_word else " ").join(f'"{term}"' for term in terms)
        rows: list[Any] = conn.execute(
            f"""
            SELECT p.*, snippet(knowledge_pages_fts, 3, '[', ']', '…', {_SNIPPET_WORDS}) AS snippet
            FROM knowledge_pages_fts JOIN knowledge_pages p ON p.rowid = knowledge_pages_fts.rowid
            WHERE knowledge_pages_fts MATCH ? AND {where}
            ORDER BY bm25(knowledge_pages_fts, 10.0, 5.0, 4.0, 1.0), p.page_id
            LIMIT ?
            """,
            (match, *params, int(limit)),
        ).fetchall()
        return rows

    def _count_search_rows(
        self, conn: Any, terms: list[str], *, any_word: bool, where: str, params: tuple[Any, ...]
    ) -> int:
        match = (" OR " if any_word else " ").join(f'"{term}"' for term in terms)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM knowledge_pages_fts JOIN knowledge_pages p ON p.rowid = knowledge_pages_fts.rowid
            WHERE knowledge_pages_fts MATCH ? AND {where}
            """,
            (match, *params),
        ).fetchone()
        return int(row["total"])


class PostgresKnowledgeRepository(_RelationalKnowledgeRepository):
    """PostgreSQL: a ``tsvector`` side table kept in sync by a trigger, with a GIN index."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    def _take_write_lock(self, conn: Any) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_WRITE_LOCK_KEY,))

    def _search_index_statements(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS knowledge_page_search (
                page_id TEXT PRIMARY KEY,
                document tsvector NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_knowledge_page_search ON knowledge_page_search USING GIN (document)",
            """
            CREATE OR REPLACE FUNCTION knowledge_page_search_sync() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    DELETE FROM knowledge_page_search WHERE page_id = OLD.page_id;
                    RETURN OLD;
                END IF;
                INSERT INTO knowledge_page_search (page_id, document) VALUES (
                    NEW.page_id,
                    setweight(to_tsvector('english', NEW.title), 'A')
                    || setweight(to_tsvector('english', NEW.summary), 'B')
                    || setweight(to_tsvector('english', NEW.tags_json), 'B')
                    || setweight(to_tsvector('english', NEW.body), 'D')
                )
                ON CONFLICT (page_id) DO UPDATE SET document = EXCLUDED.document;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """,
            """
            CREATE OR REPLACE TRIGGER knowledge_page_search_sync
            AFTER INSERT OR UPDATE OR DELETE ON knowledge_pages
            FOR EACH ROW EXECUTE FUNCTION knowledge_page_search_sync()
            """,
        ]

    def _search_rows(
        self, conn: Any, terms: list[str], *, any_word: bool, where: str, params: tuple[Any, ...], limit: int
    ) -> list[Any]:
        # Terms are letters and digits only (knowledge_base.text.query_terms), so the joined text is a valid tsquery.
        query = (" | " if any_word else " & ").join(terms)
        rows: list[Any] = conn.execute(
            f"""
            SELECT p.*, ts_headline(
                'english', p.body, q.query,
                'MaxWords={_SNIPPET_WORDS}, MinWords=6, StartSel=[, StopSel=], ShortWord=2'
            ) AS snippet
            FROM (
                SELECT p.page_id, ts_rank(s.document, q.query) AS rank
                FROM knowledge_page_search s
                JOIN knowledge_pages p ON p.page_id = s.page_id
                CROSS JOIN to_tsquery('english', %s) AS q(query)
                WHERE s.document @@ q.query AND {where}
                ORDER BY rank DESC, p.page_id
                LIMIT %s
            ) ranked
            JOIN knowledge_pages p ON p.page_id = ranked.page_id
            CROSS JOIN to_tsquery('english', %s) AS q(query)
            ORDER BY ranked.rank DESC, p.page_id
            """,
            (query, *params, int(limit), query),
        ).fetchall()
        return rows

    def _count_search_rows(
        self, conn: Any, terms: list[str], *, any_word: bool, where: str, params: tuple[Any, ...]
    ) -> int:
        query = (" | " if any_word else " & ").join(terms)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM knowledge_page_search s JOIN knowledge_pages p ON p.page_id = s.page_id
            WHERE s.document @@ to_tsquery('english', %s) AND {where}
            """,
            (query, *params),
        ).fetchone()
        return int(row["total"])


def create_knowledge_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> KnowledgeRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="KnowledgeRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if storage_settings.backend == STORAGE_BACKEND_SQLITE and storage_settings.sqlite_path:
        return SqliteKnowledgeRepository(storage_settings.sqlite_path, logger=logger)
    if storage_settings.backend == STORAGE_BACKEND_POSTGRES and storage_settings.postgres_dsn:
        return PostgresKnowledgeRepository(storage_settings.postgres_dsn, logger=logger)
    raise NotImplementedError(f"KnowledgeRepository has no implementation for backend {storage_settings.backend!r}.")
