"""The grant store: grants, blocks, account links, and the access event log.

Grants are immutable records; only their status, use count and revocation
fields change, and those live in their own columns so updates are atomic:

- consuming a one-time grant is a guarded update (``uses_remaining > 0``) made
  immediately before the effect, so two uses can never both succeed. A use
  taken for one tool call records the call's id (``access_grant_uses``, in
  the same transaction): Strands runs a call that paused inside its tool
  again from its start when it resumes, and the access hook decides it again,
  so that call keeps the use it took, and no other call can pass on it;
- revoking a grant marks it and every grant handed down from it as revoked in
  one transaction (validity is also checked up the parent chain at every
  decision, so a stale child can never act);
- every change bumps a per-team version, so a cache keyed by it sees a
  revocation on the next decision;
- a tool has at most one active account link per owner (the team or a
  person): creating a link reads and revokes the one it replaces in the same
  transaction, and a partial unique index refuses a second active link
  (``_one_active_link_per_owner``);
- an access grant is stored as one entry per tool (``store_planned``,
  ``access_control.grant_entries``): the caller's plan runs after the write
  lock is taken, inside the same transaction, so two grants stored at once
  each see the other;
- a person's decision on a waiting request stores its grants and its outcome
  in one transaction (``decision_transaction``), so no grant is ever in
  force, or seen by another writer, before its outcome is stored, and a
  decision that fails stores nothing. Decisions whose work also happens
  outside the database (a proposal's programs and tool files, a delivered
  file) first claim the request (``claim_decision``: a short-lived claim with
  an owner and a time limit, renewed before and after long steps by
  ``renew_decision``) and store their outcome only while the claim holds;
  any other decision is refused while such a claim is current. Claims are
  transient and never travel in a team transfer. Before writing an outcome,
  appliers persist their external effects in ``access_decision_recoveries``.
  An unavailable readback preserves those effects for Inbox reconciliation,
  including after restart; an unresolved recovery prevents another decision
  from taking over or an access transfer from exporting uncertain work;
- a grant that carries the approval id of a request that was not granted
  (only an older version could leave one) is never kept in force
  (``end_grants_without_granted_outcome``, found by the indexed
  ``approval_id``) and never exported;
- data folders (decision D16) are registered and removed here, one active
  folder per name, and bump the version too, so every process sees a new
  folder on its next use without a restart;
- each person's last known tool setup states (``ToolSetupStateV1``,
  ``access_control.tool_setup_states``) live here too, one row per person and
  tool, because the broker, the agents' runtime and the gateway all open this
  store in both authorization modes. They do not bump the version: they
  describe accounts, not access, and must not make agents reload their tools.
- so do each person's model settings (``PersonSettingsV1``,
  ``access_control.person_settings``): whether their work may use the team's
  model keys, and their own model. They do not bump the version either.
- and the team's sharing choices (``AccountSharingV1``,
  ``access_control.account_sharing``), one row per service: whether the
  team's account serves everyone's work. They pick accounts, not access, so
  they do not bump the version; each change is in the event log.

The store lives in the manager storage backend (``dbs/`` SQLite or Postgres),
which agents cannot reach; the broker opens it with the same settings.

Every write runs in a transaction that first takes the store's write lock:
``BEGIN IMMEDIATE`` in SQLite, and in PostgreSQL the store's transaction-level
advisory lock (``ACCESS_STORE_WRITE_LOCK_KEY``). Several writes read before
they write (the one-entry plan, the data folder name check, a person's
settings, the event sequence, the previous account link, a claim), and those
reads are exact only while writers take turns. PostgreSQL's default READ
COMMITTED isolation does not make them take turns, so without the lock two
processes could both pass a check. The lock key is the access store's own,
so other stores never wait for it; it covers the whole PostgreSQL database,
so instances or teams that share one database in different schemas wait for
each other's access writes. While a write transaction of this store is open
in a thread, every other read or write that thread makes on the store joins
it (``_joining``): a grant plan and a whole decision read and write through
the one connection, see their own uncommitted rows, and never open a second
connection that would wait for the lock the transaction holds. Only the
owning thread joins: work handed to another thread opens its own connection.
"""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol, TypeVar

from custom_messages.contracts.access_v2 import (
    AccessEventV1,
    AccessNoticeV1,
    AccessRequestStatus,
    AccessRequestV1,
    AccountLinkStatus,
    AccountLinkV1,
    AccountSharingV1,
    DataFolderRecordV1,
    DataFolderStatus,
    GrantKind,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TASK_BOUND_LIFETIMES,
    TeamPersonV1,
    ToolSetupStateV1,
)
from custom_messages.contracts.person_settings_v1 import PersonSettingsV1
from id_generation import new_id
from storage.config import StorageSettings
from storage.postgres_connection import ACCESS_STORE_WRITE_LOCK_KEY, connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

logger = logging.getLogger(__name__)

# The partial unique index that allows one active account link per tool and owner.
ONE_ACTIVE_LINK_INDEX = "access_account_links_one_active"
# The index that finds the grants one decision stored (``GrantV1.approval_id``).
APPROVAL_ID_INDEX = "access_grants_approval"
# The open write transaction that the owning thread's other store operations
# join (see ``_joining``). A context variable travels with copied contexts
# (``asyncio.to_thread``), so the owner's thread id is part of the record.
_OPEN_WRITE: ContextVar["_OpenWrite | None"] = ContextVar("access_store_open_write", default=None)


@dataclass
class _OpenWrite:
    """A write transaction a thread has open: whose, where, and what to do once it commits."""

    repository_id: int
    thread_id: int
    conn: Any
    after_commit: list[Callable[[], None]]

    def joins(self, repository: object) -> bool:
        return self.repository_id == id(repository) and self.thread_id == threading.get_ident()


@dataclass(frozen=True)
class DecisionClaim:
    """The claim a decision with work outside the database holds on a waiting request (``claim_decision``).

    Taken before that work starts and passed to the write that stores the
    outcome, which succeeds only while the claim still holds.
    """

    request_id: str
    claim_id: str


@dataclass(frozen=True)
class DecisionRecovery:
    """External effects saved before an outcome write, retained until its result is known.

    These are transient gateway recovery records, like decision claims, and
    never travel in a team transfer. Paths in effects are relative to the
    owning applier's instance, never agent-supplied host paths.
    """

    claim: DecisionClaim
    finished: AccessRequestV1
    effects: dict[str, Any]
    recover_after: datetime

    def matches(self, stored: AccessRequestV1 | None) -> bool:
        return (
            stored is not None
            and stored.status is self.finished.status
            and stored.decided_by == self.finished.decided_by
            and stored.decided_at_utc == self.finished.decided_at_utc
        )


class _JoinedWrite:
    """An operation made while a write transaction is open joins it (see ``AccessRepository._joining``).

    Starting and committing are the open transaction's: ``begin_write`` and
    ``commit`` do nothing here, and leaving the block neither commits nor
    closes. An error propagates and rolls the whole transaction back.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self) -> "_JoinedWrite":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._conn.execute(sql, params)

    def begin_write(self) -> None:
        return None

    def commit(self) -> None:
        return None


class GrantPlanLike(Protocol):
    """What storing new access grants does to the holder's access (``access_control.grant_entries.GrantPlan``)."""

    @property
    def new(self) -> list[GrantV1]: ...

    @property
    def replaced(self) -> dict[str, str]: ...

    @property
    def covered(self) -> dict[str, GrantV1]: ...


PlanT = TypeVar("PlanT", bound=GrantPlanLike)


@dataclass(frozen=True)
class AccessNoticePage:
    """One page of a person's notices: ``total`` counts the filtered list, ``all_total`` every notice."""

    notices: list[tuple[AccessNoticeV1, bool]]
    total: int
    unread: int
    all_total: int


# access_requests.announced: people not yet told of a waiting request; told;
# nothing left to send (the waiting agent was told of the decision, or no
# agent waits: requests decided at once and withdrawn ones).
ANNOUNCE_NEW = 0
ANNOUNCE_TOLD = 1
ANNOUNCE_DONE = 2


class AccessStoreConflict(RuntimeError):
    """A guarded update lost (for example a one-time grant already used)."""


class DecisionInProgress(AccessStoreConflict):
    """Another decision holds the waiting request's claim (``claim_decision``)."""


class DecisionClaimLost(AccessStoreConflict):
    """The decision no longer holds its claim: the request was closed, or the claim ran out and was taken."""


def store_unavailable(error: BaseException) -> bool:
    """Whether ``error`` came from the database (busy, locked, connection lost), not from an access rule."""

    if isinstance(error, sqlite3.Error):
        return True
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg ships with the runtime
        return False
    return isinstance(error, psycopg.Error)


def _utc(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _as_decided(request: AccessRequestV1) -> AccessRequestV1:
    """A decided request keeps its change's after-decision effect lines (``ChangeProposalV1.as_decided``)."""

    if request.change is None or not request.change.effect_after_decision:
        return request
    return request.model_copy(update={"change": request.change.as_decided()})


class AccessRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            # approval_id copies GrantV1.approval_id (the request whose
            # decision stored the grant) so a decision's grants are found by
            # index; stores from before 2026-10-17 get it from
            # _approval_id_column.
            conn.execute("""CREATE TABLE IF NOT EXISTS access_grants (
                grant_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
                parent_grant_id TEXT, status TEXT NOT NULL,
                uses_remaining INTEGER, revoked_at TEXT, revoked_by_json TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL, approval_id TEXT
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_grants_subject "
                "ON access_grants(team_id, subject_kind, subject_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_grants_parent ON access_grants(parent_grant_id)"
            )
            # Which call took each use of a grant with a use count (consume).
            # operation_id is the call's id from the access hook
            # (custom_agents.access_runtime.call_operation_id). Uses end with
            # their task, like one-time grants, and never travel in a team
            # transfer.
            conn.execute("""CREATE TABLE IF NOT EXISTS access_grant_uses (
                grant_id TEXT NOT NULL, operation_id TEXT NOT NULL, team_id TEXT NOT NULL,
                used_at TEXT NOT NULL,
                PRIMARY KEY (grant_id, operation_id)
            )""")
            # sequence keeps the log's order when events share a timestamp: it
            # is the team's next number, taken inside the write transaction.
            conn.execute("""CREATE TABLE IF NOT EXISTS access_events (
                event_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                kind TEXT NOT NULL, grant_id TEXT, payload_json TEXT NOT NULL, recorded_at TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_events_grant ON access_events(grant_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_events_order ON access_events(team_id, sequence)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS access_versions (
                team_id TEXT PRIMARY KEY, version INTEGER NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS access_account_links (
                link_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, tool_id TEXT NOT NULL,
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, status TEXT NOT NULL,
                revoked_at TEXT, revoked_by_json TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_account_links_owner "
                "ON access_account_links(team_id, tool_id, owner_kind, owner_id, status)"
            )
            # tool_use_key finds the request a resumed tool call made; announced
            # tracks the gateway's messages (ANNOUNCE_* below).
            conn.execute("""CREATE TABLE IF NOT EXISTS access_requests (
                request_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, status TEXT NOT NULL,
                task_id TEXT, tool_use_key TEXT UNIQUE, announced INTEGER NOT NULL DEFAULT 0,
                applied INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_requests_team ON access_requests(team_id, status, created_at)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS access_requests_task ON access_requests(team_id, task_id)")
            # One decision at a time per waiting request (claim_decision): who
            # decides it now and until when. A row lives for one decision; it
            # is transient and never travels in a team transfer.
            conn.execute("""CREATE TABLE IF NOT EXISTS access_request_claims (
                request_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL, claimed_until TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS access_decision_recoveries (
                claim_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, team_id TEXT NOT NULL,
                finished_json TEXT NOT NULL, effects_json TEXT NOT NULL, recover_after TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_decision_recoveries_request "
                "ON access_decision_recoveries(request_id)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS access_notices (
                notice_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS access_notices_team ON access_notices(team_id, created_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS access_notice_reads (
                notice_id TEXT NOT NULL, person_id TEXT NOT NULL, read_at TEXT NOT NULL,
                PRIMARY KEY (notice_id, person_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS access_data_folders (
                record_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS access_data_folders_team ON access_data_folders(team_id, status, name)"
            )
            # The exact plans behind change requests that trusted code applies at
            # the decision (access proposals, file deliveries): the card shows the
            # plan's rows and approving binds its digest.
            conn.execute("""CREATE TABLE IF NOT EXISTS access_change_plans (
                change_id TEXT PRIMARY KEY, team_id TEXT NOT NULL, kind TEXT NOT NULL,
                plan_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            # Each person's starter profile choice (access_control.starter_profiles).
            # A row is needed because "manual" leaves no grants to recognize, and
            # source tells a person's own choice from the configured default.
            conn.execute("""CREATE TABLE IF NOT EXISTS access_profile_choices (
                team_id TEXT NOT NULL, person_id TEXT NOT NULL, profile TEXT NOT NULL,
                source TEXT NOT NULL, chosen_at TEXT NOT NULL,
                PRIMARY KEY (team_id, person_id)
            )""")
            # The gateway's copy of each person's standing and roles
            # (TeamPersonV1, access_control.people_directory), for trusted code
            # that cannot read the gateway's records. access_people_synced says
            # the gateway has recorded everyone at least once, so a missing row
            # means "not known to the gateway", not "not recorded yet".
            conn.execute("""CREATE TABLE IF NOT EXISTS access_people (
                team_id TEXT NOT NULL, person_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                PRIMARY KEY (team_id, person_id)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS access_people_synced (
                team_id TEXT PRIMARY KEY, synced_at TEXT NOT NULL
            )""")
            # Last known tool setup state per person and tool (see the module docstring).
            conn.execute("""CREATE TABLE IF NOT EXISTS access_tool_setup_states (
                team_id TEXT NOT NULL, person_id TEXT NOT NULL, tool_id TEXT NOT NULL,
                state TEXT NOT NULL, payload_json TEXT NOT NULL, changed_at TEXT NOT NULL,
                PRIMARY KEY (team_id, person_id, tool_id)
            )""")
            # Each person's model settings (see the module docstring).
            conn.execute("""CREATE TABLE IF NOT EXISTS access_person_settings (
                team_id TEXT NOT NULL, person_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                PRIMARY KEY (team_id, person_id)
            )""")
            # Whether the team's account for a service serves everyone (see the module docstring).
            conn.execute("""CREATE TABLE IF NOT EXISTS access_account_sharing (
                team_id TEXT NOT NULL, subject TEXT NOT NULL, payload_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                PRIMARY KEY (team_id, subject)
            )""")
            conn.commit()
        self._approval_id_column()
        self._one_active_link_per_owner()

    def _connect(self) -> Any:
        joined = _OPEN_WRITE.get()
        if joined is not None and joined.joins(self):
            return _JoinedWrite(joined.conn)
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn, write_lock_key=ACCESS_STORE_WRITE_LOCK_KEY)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    @contextmanager
    def _joining(self, conn: Any) -> Iterator[_OpenWrite]:
        """Let this thread's other operations on this store join ``conn``'s open write transaction.

        A grant plan, or a whole decision, reads and writes through the one
        connection: it sees its own uncommitted rows, and in PostgreSQL it
        opens no second connection (one that wrote would wait for the lock
        this transaction holds). Only the thread that opened the transaction
        joins it: work handed to another thread, even with a copied context
        (``asyncio.to_thread``), opens its own connection, sees only what was
        committed, and waits for the lock to write. Other threads and
        processes see nothing until the transaction commits.
        """

        opened = _OpenWrite(id(self), threading.get_ident(), conn, [])
        token = _OPEN_WRITE.set(opened)
        try:
            yield opened
        finally:
            _OPEN_WRITE.reset(token)

    def after_commit(self, action: Callable[[], None]) -> None:
        """Run ``action`` once this thread's open decision transaction commits, or now when none is open.

        For what must only happen when the change is stored, such as a log
        line saying narrower access was replaced: a decision that rolls back
        runs none of them.
        """

        joined = _OPEN_WRITE.get()
        if joined is not None and joined.joins(self):
            joined.after_commit.append(action)
            return
        action()

    def _column_exists(self, conn: Any, table: str, column: str) -> bool:
        if self.dialect is POSTGRES:
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = %s AND column_name = %s",
                (table, column),
            ).fetchone()
            return row is not None
        return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())

    def _approval_id_column(self) -> None:
        """Give stores from before 2026-10-17 the indexed ``approval_id`` column, filled from each grant's record.

        The column and its fill are one transaction, so a store never has the
        column half filled; later opens find both with a read and take no
        write lock. The team transfer is unaffected: grants travel as their
        records (``GrantV1``, which holds the approval id) and are written by
        ``_insert_grant``, which fills the column.
        """

        with self._connect() as conn:
            if self._column_exists(conn, "access_grants", "approval_id") and self._index_exists(conn, APPROVAL_ID_INDEX):
                return
        with self._connect() as conn:
            conn.begin_write()
            if not self._column_exists(conn, "access_grants", "approval_id"):
                conn.execute("ALTER TABLE access_grants ADD COLUMN approval_id TEXT")
                approval = (
                    "payload_json::jsonb ->> 'approval_id'"
                    if self.dialect is POSTGRES
                    else "json_extract(payload_json, '$.approval_id')"
                )
                filled = conn.execute(
                    f"UPDATE access_grants SET approval_id = {approval} WHERE {approval} IS NOT NULL"
                ).rowcount
                logger.info(
                    "Added the approval id column to access grants and filled it from their records. "
                    "event=access_grants_approval_id_added grants=%s",
                    filled,
                )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {APPROVAL_ID_INDEX} ON access_grants(approval_id)")
            conn.commit()

    def _index_exists(self, conn: Any, name: str) -> bool:
        p = self.dialect.bind
        query = (
            f"SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = {p}"
            if self.dialect is POSTGRES
            else f"SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = {p}"
        )
        return conn.execute(query, (name,)).fetchone() is not None

    def _one_active_link_per_owner(self) -> None:
        """Create the index that allows one active account link per tool and owner.

        Stores written before it existed may hold two active links for one tool
        and owner (two link forms finished at once both saw no earlier link).
        Only the one ``active_link`` returned was ever used: the newest, or
        with the same time the larger link id. It stays and each other one is
        revoked as replaced by it, with an event in the log and a warning
        naming both links and the rule, before the index is created. Runs
        once per store: later opens find the index with a read and take no
        write lock.
        """

        with self._connect() as conn:
            if self._index_exists(conn, ONE_ACTIVE_LINK_INDEX):
                return
        with self._connect() as conn:
            conn.begin_write()
            if self._index_exists(conn, ONE_ACTIVE_LINK_INDEX):
                conn.commit()
                return
            self._revoke_older_duplicate_links_in(conn)
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {ONE_ACTIVE_LINK_INDEX} "
                "ON access_account_links(team_id, tool_id, owner_kind, owner_id) "
                f"WHERE status = '{AccountLinkStatus.ACTIVE.value}'"
            )
            conn.commit()

    def _revoke_older_duplicate_links_in(self, conn: Any) -> None:
        p = self.dialect.bind
        rows = conn.execute(
            f"SELECT * FROM access_account_links WHERE status={p} "
            "ORDER BY team_id, tool_id, owner_kind, owner_id, created_at DESC, link_id DESC",
            (AccountLinkStatus.ACTIVE.value,),
        ).fetchall()
        newest: dict[tuple[str, str, str, str], AccountLinkV1] = {}
        for row in rows:
            link = self._link_from_row(row)
            key = (link.team_id, link.tool_id, link.owner_kind.value, link.owner_id)
            kept = newest.setdefault(key, link)
            if kept.link_id == link.link_id:
                continue
            actor = SubjectRefV1(kind=SubjectKind.TEAM, id=link.team_id)
            at = datetime.now(timezone.utc)
            # The rule active_link used to pick the link in use.
            rule = "newer" if kept.created_at_utc > link.created_at_utc else "same_time_larger_link_id"
            self._mark_link_revoked(
                conn, link, actor=actor, at=at, replaced_by=kept.link_id, reason="duplicate_active_link", rule=rule
            )
            self._bump(conn, link.team_id)
            logger.warning(
                "Two active account links named the same tool and owner; the one in use stays and the other was "
                "revoked. The one in use is the newer one, or with the same time the one with the larger link id. "
                "event=account_link_duplicate_revoked team_id=%s tool_id=%s owner_kind=%s owner_id=%s "
                "revoked_link_id=%s kept_link_id=%s rule=%s",
                link.team_id,
                link.tool_id,
                link.owner_kind.value,
                link.owner_id,
                link.link_id,
                kept.link_id,
                rule,
            )

    # -- reading ---------------------------------------------------------------

    @staticmethod
    def _grant_from_row(row: Any) -> GrantV1:
        grant = GrantV1.model_validate_json(row["payload_json"])
        revoked_by = (
            SubjectRefV1.model_validate_json(row["revoked_by_json"]) if row["revoked_by_json"] else None
        )
        return grant.model_copy(
            update={
                "status": GrantStatus(row["status"]),
                # The column moves when the grant a hand-down came from is
                # replaced by a broader one (``_replace_in``).
                "parent_grant_id": row["parent_grant_id"],
                "uses_remaining": row["uses_remaining"],
                "revoked_at_utc": datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
                "revoked_by": revoked_by,
            }
        )

    def get(self, grant_id: str) -> GrantV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM access_grants WHERE grant_id={p}", (grant_id,)).fetchone()
        return self._grant_from_row(row) if row else None

    def for_subjects(
        self, team_id: str, subjects: Iterable[SubjectRefV1], *, statuses: Iterable[GrantStatus] = (GrantStatus.ACTIVE,)
    ) -> list[GrantV1]:
        """Grants naming any of ``subjects`` directly, in the given statuses."""

        wanted = list(subjects)
        status_values = [status.value for status in statuses]
        if not wanted or not status_values:
            return []
        p = self.dialect.bind
        subject_clause = " OR ".join(f"(subject_kind={p} AND subject_id={p})" for _ in wanted)
        status_clause = ",".join(p for _ in status_values)
        params: list[Any] = [team_id, *status_values]
        for subject in wanted:
            params += [subject.kind.value, subject.id]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM access_grants WHERE team_id={p} AND status IN ({status_clause}) "
                f"AND ({subject_clause}) ORDER BY created_at, grant_id",
                params,
            ).fetchall()
        return [self._grant_from_row(row) for row in rows]

    def task_grants(self, team_id: str) -> list[GrantV1]:
        """Active or pending grants bound to one task (lifetime task or one time).

        The task id lives only in the payload, so this reads the team's active
        grants and filters them; a team holds few active grants.
        """

        return [
            grant
            for grant in self.for_status(team_id, (GrantStatus.ACTIVE, GrantStatus.PENDING))
            if grant.task_id is not None and grant.lifetime in TASK_BOUND_LIFETIMES
        ]

    def for_status(self, team_id: str, statuses: Iterable[GrantStatus]) -> list[GrantV1]:
        status_values = [status.value for status in statuses]
        if not status_values:
            return []
        p = self.dialect.bind
        status_clause = ",".join(p for _ in status_values)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM access_grants WHERE team_id={p} AND status IN ({status_clause}) "
                "ORDER BY created_at, grant_id",
                (team_id, *status_values),
            ).fetchall()
        return [self._grant_from_row(row) for row in rows]

    def list_team(self, team_id: str) -> list[GrantV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM access_grants WHERE team_id={p} ORDER BY created_at, grant_id", (team_id,)
            ).fetchall()
        return [self._grant_from_row(row) for row in rows]

    def events(self, team_id: str, *, grant_id: str | None = None) -> list[AccessEventV1]:
        p = self.dialect.bind
        query = f"SELECT payload_json FROM access_events WHERE team_id={p}"
        params: list[Any] = [team_id]
        if grant_id is not None:
            query += f" AND grant_id={p}"
            params.append(grant_id)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY sequence, event_id", params).fetchall()
        return [AccessEventV1.model_validate_json(row[0]) for row in rows]

    def version(self, team_id: str) -> int:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(f"SELECT version FROM access_versions WHERE team_id={p}", (team_id,)).fetchone()
        return int(row[0]) if row else 0

    # -- writing ---------------------------------------------------------------

    def _bump(self, conn: Any, team_id: str) -> None:
        p = self.dialect.bind
        conn.execute(
            f"INSERT INTO access_versions(team_id, version) VALUES ({p}, 1) "
            "ON CONFLICT(team_id) DO UPDATE SET version = access_versions.version + 1",
            (team_id,),
        )

    def _event(self, conn: Any, event: AccessEventV1) -> None:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM access_events WHERE team_id={p}", (event.team_id,)
        ).fetchone()
        conn.execute(
            f"INSERT INTO access_events(event_id, team_id, sequence, kind, grant_id, payload_json, recorded_at) "
            f"VALUES ({p},{p},{p},{p},{p},{p},{p})",
            (
                event.event_id,
                event.team_id,
                int(row[0]),
                event.kind,
                event.grant_id,
                event.model_dump_json(),
                _utc(event.recorded_at_utc),
            ),
        )

    def _new_event(
        self, grant: GrantV1, kind: str, actor: SubjectRefV1 | None, at: datetime, **detail: Any
    ) -> AccessEventV1:
        return AccessEventV1(
            event_id=new_id("access_event"),
            team_id=grant.team_id,
            kind=kind,
            grant_id=grant.grant_id,
            actor=actor,
            detail=detail,
            recorded_at_utc=at,
        )

    def create(self, grant: GrantV1) -> GrantV1:
        """Store a grant as it is: blocks, hand-downs waiting for approval, and records a team transfer imports.

        Access in force goes through ``store_planned``, so it joins its entry.
        """

        with self._connect() as conn:
            conn.begin_write()
            self._insert_grant(conn, grant)
            self._bump(conn, grant.team_id)
            conn.commit()
        return grant

    def store_planned(self, grant: GrantV1, plan: Callable[[], PlanT]) -> tuple[GrantV1, PlanT]:
        """Store an access grant as one entry per tool (``access_control.grant_entries``).

        ``plan`` reads the holder's access in force and runs after the write
        lock is taken, inside the transaction (``_joining``), so of two grants
        stored at once the second sees the first: it is covered by it, or
        replaces it, and is never stored beside it (two grants in one entry,
        or a tool grant left outside its tool set's entry). Returns the stored
        grant, or the grant already covering it (nothing is then written),
        with the plan.
        """

        with self._connect() as conn:
            conn.begin_write()
            with self._joining(conn):
                planned = plan()
            cover = planned.covered.get(grant.grant_id)
            if cover is not None:
                conn.commit()
                return cover, planned
            self._insert_grant(conn, grant)
            for old_id, by in planned.replaced.items():
                if by == grant.grant_id:
                    self._replace_in(conn, old_id, grant, at=grant.created_at_utc)
            self._bump(conn, grant.team_id)
            conn.commit()
        return grant, planned

    def replace_block(self, previous: GrantV1, replacement: GrantV1) -> GrantV1:
        """Swap blocks without an unblocked interval; a competing removal must lose.

        The service checks the actor and the narrowing scope. The guarded
        update locks the old row on both storage backends before either effect
        or its audit events are written. A failed insertion rolls everything
        back, including the version used by authorization caches.
        """

        if (
            previous.kind is not GrantKind.BLOCK
            or replacement.kind is not GrantKind.BLOCK
            or previous.team_id != replacement.team_id
            or previous.subject != replacement.subject
        ):
            raise ValueError("A replacement block must keep the same team and subject.")
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            guarded = conn.execute(
                f"UPDATE access_grants SET status=status WHERE grant_id={p} AND team_id={p} AND status={p}",
                (previous.grant_id, previous.team_id, GrantStatus.ACTIVE.value),
            )
            if guarded.rowcount != 1:
                raise AccessStoreConflict("That block has already changed; review the current blocks before replacing it.")
            self._insert_grant(conn, replacement)
            self._revoke_in(
                conn,
                previous.grant_id,
                actor=replacement.granted_by,
                at=replacement.created_at_utc,
                detail={"replaced_by": replacement.grant_id},
            )
            self._bump(conn, replacement.team_id)
            conn.commit()
        return replacement

    def _replace_in(self, conn: Any, old_id: str, new: GrantV1, *, at: datetime) -> None:
        """End ``old_id`` because ``new`` covers it, inside an open write transaction.

        What was handed on from the old grant moves to the new one first, so
        ending the old grant cuts nothing off: every hand-down narrowed the old
        grant and so still narrows the broader new one. Grants are otherwise
        immutable; the move is recorded (``grant_moved``) and the end names
        its replacement (``replaced_by``).
        """

        p = self.dialect.bind
        live = (GrantStatus.ACTIVE.value, GrantStatus.PENDING.value)
        children = conn.execute(
            f"SELECT grant_id FROM access_grants WHERE parent_grant_id={p} AND status IN ({p},{p})",
            (old_id, *live),
        ).fetchall()
        conn.execute(
            f"UPDATE access_grants SET parent_grant_id={p} WHERE parent_grant_id={p} AND status IN ({p},{p})",
            (new.grant_id, old_id, *live),
        )
        for child in children:
            self._event(
                conn,
                AccessEventV1(
                    event_id=new_id("access_event"),
                    team_id=new.team_id,
                    kind="grant_moved",
                    grant_id=str(child[0]),
                    actor=new.granted_by,
                    detail={"from_parent": old_id, "to_parent": new.grant_id},
                    recorded_at_utc=at,
                ),
            )
        self._revoke_in(conn, old_id, actor=new.granted_by, at=at, detail={"replaced_by": new.grant_id})

    def _insert_grant(self, conn: Any, grant: GrantV1) -> None:
        p = self.dialect.bind
        conn.execute(
            f"INSERT INTO access_grants(grant_id, team_id, subject_kind, subject_id, parent_grant_id, "
            f"status, uses_remaining, revoked_at, revoked_by_json, payload_json, created_at, approval_id) "
            f"VALUES ({p},{p},{p},{p},{p},{p},{p},NULL,NULL,{p},{p},{p})",
            (
                grant.grant_id,
                grant.team_id,
                grant.subject.kind.value,
                grant.subject.id,
                grant.parent_grant_id,
                grant.status.value,
                grant.uses_remaining,
                grant.model_dump_json(),
                _utc(grant.created_at_utc),
                grant.approval_id,
            ),
        )
        # The kind shows blocks apart from access in the audit log.
        self._event(conn, self._new_event(grant, "grant_created", grant.granted_by, grant.created_at_utc,
                                          status=grant.status.value, grant_kind=grant.kind.value))

    # -- data folders (decision D16) ---------------------------------------------

    def data_folders(self, team_id: str) -> list[DataFolderRecordV1]:
        """The team's active data folders, by name."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_data_folders WHERE team_id={p} AND status={p} ORDER BY name",
                (team_id, DataFolderStatus.ACTIVE.value),
            ).fetchall()
        return [DataFolderRecordV1.model_validate_json(row[0]) for row in rows]

    def _folder_event(self, folder: DataFolderRecordV1, kind: str, actor: SubjectRefV1, at: datetime) -> AccessEventV1:
        return AccessEventV1(
            event_id=new_id("access_event"),
            team_id=folder.team_id,
            kind=kind,
            grant_id=folder.folder_id,
            actor=actor,
            detail={"path": folder.path, "writable": folder.writable, "reason": folder.reason},
            recorded_at_utc=at,
        )

    def _insert_data_folder(self, conn: Any, folder: DataFolderRecordV1) -> None:
        p = self.dialect.bind
        taken = conn.execute(
            f"SELECT 1 FROM access_data_folders WHERE team_id={p} AND name={p} AND status={p}",
            (folder.team_id, folder.name, DataFolderStatus.ACTIVE.value),
        ).fetchone()
        if taken:
            raise AccessStoreConflict(f"A data folder named {folder.name!r} is already registered.")
        conn.execute(
            f"INSERT INTO access_data_folders(record_id, team_id, name, status, payload_json, created_at) "
            f"VALUES ({p},{p},{p},{p},{p},{p})",
            (
                folder.record_id,
                folder.team_id,
                folder.name,
                DataFolderStatus.ACTIVE.value,
                folder.model_dump_json(),
                _utc(folder.created_at_utc),
            ),
        )
        self._event(conn, self._folder_event(folder, "data_folder_registered", folder.registered_by, folder.created_at_utc))

    def register_data_folder(self, folder: DataFolderRecordV1) -> DataFolderRecordV1:
        with self._connect() as conn:
            conn.begin_write()
            self._insert_data_folder(conn, folder)
            self._bump(conn, folder.team_id)
            conn.commit()
        return folder

    def remove_data_folder(self, team_id: str, name: str, *, actor: SubjectRefV1, at: datetime) -> DataFolderRecordV1:
        """Remove an active data folder. Grants on it stay, but name nothing until it is registered again."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT payload_json FROM access_data_folders WHERE team_id={p} AND name={p} AND status={p}",
                (team_id, name, DataFolderStatus.ACTIVE.value),
            ).fetchone()
            if row is None:
                raise KeyError(name)
            folder = DataFolderRecordV1.model_validate_json(row[0]).model_copy(
                update={"status": DataFolderStatus.REMOVED, "removed_at_utc": at, "removed_by": actor}
            )
            conn.execute(
                f"UPDATE access_data_folders SET status={p}, payload_json={p} WHERE record_id={p}",
                (DataFolderStatus.REMOVED.value, folder.model_dump_json(), folder.record_id),
            )
            self._event(conn, self._folder_event(folder, "data_folder_removed", actor, at))
            self._bump(conn, team_id)
            conn.commit()
        return folder

    # -- change plans ---------------------------------------------------------------

    def save_plan(self, team_id: str, change_id: str, kind: str, plan_digest: str, payload: dict[str, Any], *, at: datetime) -> None:
        """Keep a plan unchanged until it is decided (a change id is never reused)."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO access_change_plans(change_id, team_id, kind, plan_digest, payload_json, created_at) "
                f"VALUES ({p},{p},{p},{p},{p},{p})",
                (change_id, team_id, kind, plan_digest, json.dumps(payload, sort_keys=True), _utc(at)),
            )
            conn.commit()

    def get_plan(self, change_id: str) -> tuple[str, str, str, dict[str, Any]] | None:
        """``(team_id, kind, plan_digest, payload)`` of a stored plan, or None."""

        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT team_id, kind, plan_digest, payload_json FROM access_change_plans WHERE change_id={p}",
                (change_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), str(row[2]), json.loads(row[3])

    # -- starter profile choices ---------------------------------------------------

    def profile_choice(self, team_id: str, person_id: str) -> tuple[str, str] | None:
        """``(profile, source)`` of the person's starter profile choice, or None."""

        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT profile, source FROM access_profile_choices WHERE team_id={p} AND person_id={p}",
                (team_id, person_id),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def profile_choices(self, team_id: str) -> list[tuple[str, str, str, datetime]]:
        """Every person's starter profile choice in the team: ``(person_id, profile, source, chosen_at)``."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT person_id, profile, source, chosen_at FROM access_profile_choices WHERE team_id={p} "
                "ORDER BY person_id",
                (team_id,),
            ).fetchall()
        return [(str(row[0]), str(row[1]), str(row[2]), datetime.fromisoformat(str(row[3]))) for row in rows]

    def record_profile_choice(self, team_id: str, person_id: str, profile: str, *, source: str, at: datetime) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO access_profile_choices(team_id, person_id, profile, source, chosen_at) "
                f"VALUES ({p},{p},{p},{p},{p}) ON CONFLICT(team_id, person_id) DO UPDATE SET "
                "profile=excluded.profile, source=excluded.source, chosen_at=excluded.chosen_at",
                (team_id, person_id, profile, source, _utc(at)),
            )
            conn.commit()

    # -- people (the gateway's copy, access_control.people_directory) --------------

    def record_person(self, person: TeamPersonV1) -> None:
        """Store one person's current standing and roles (the gateway, after a change)."""

        with self._connect() as conn:
            conn.begin_write()
            self._upsert_person(conn, person)
            conn.commit()

    def replace_people(self, team_id: str, people: Iterable[TeamPersonV1], *, at: datetime) -> None:
        """Store everyone the gateway knows in the team, drop anyone it no longer knows, and mark the team synced.

        One transaction, so a reader never sees half a sync.
        """

        p = self.dialect.bind
        people = list(people)
        if any(person.team_id != team_id for person in people):
            raise ValueError("Every person must belong to the team being synced.")
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(f"DELETE FROM access_people WHERE team_id={p}", (team_id,))
            for person in people:
                self._upsert_person(conn, person)
            conn.execute(
                f"INSERT INTO access_people_synced(team_id, synced_at) VALUES ({p},{p}) "
                "ON CONFLICT(team_id) DO UPDATE SET synced_at=excluded.synced_at",
                (team_id, _utc(at)),
            )
            conn.commit()

    def _upsert_person(self, conn: Any, person: TeamPersonV1) -> None:
        p = self.dialect.bind
        conn.execute(
            f"INSERT INTO access_people(team_id, person_id, payload_json, changed_at) VALUES ({p},{p},{p},{p}) "
            "ON CONFLICT(team_id, person_id) DO UPDATE SET payload_json=excluded.payload_json, "
            "changed_at=excluded.changed_at",
            (person.team_id, person.person_id, person.model_dump_json(), _utc(person.changed_at_utc)),
        )

    def team_person(self, team_id: str, person_id: str) -> TeamPersonV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM access_people WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).fetchone()
        return TeamPersonV1.model_validate_json(row[0]) if row else None

    def team_people(self, team_id: str) -> list[TeamPersonV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_people WHERE team_id={p} ORDER BY person_id", (team_id,)
            ).fetchall()
        return [TeamPersonV1.model_validate_json(row[0]) for row in rows]

    def people_synced_at(self, team_id: str) -> datetime | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(f"SELECT synced_at FROM access_people_synced WHERE team_id={p}", (team_id,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    # -- several changes at once (access proposals, Phase 6) ----------------------

    def apply_together(
        self,
        team_id: str,
        *,
        plan: Callable[[], PlanT] | None = None,
        data_folders: Iterable[DataFolderRecordV1] = (),
        revoke_ids: Iterable[str] = (),
        finish: Callable[[PlanT | None], AccessRequestV1] | None = None,
        claim_id: str | None = None,
        actor: SubjectRefV1,
        at: datetime,
    ) -> tuple[PlanT | None, AccessRequestV1 | None]:
        """Write grants, blocks, data folders, revocations and a request's outcome in one transaction.

        Either everything is stored or nothing is: an approved proposal never
        leaves half its access in place. ``plan`` (``access_control.grant_entries``)
        runs after the write lock is taken, as in ``store_planned``: its new
        grants are stored and the grants they cover end. ``finish`` builds the
        request's outcome from the plan; it is stored under the deciding
        claim (``claim_id``, see ``finish_request``). Returns the plan and the
        stored outcome.
        """

        with self._connect() as conn:
            conn.begin_write()
            with self._joining(conn):
                planned = plan() if plan is not None else None
            for folder in data_folders:
                self._insert_data_folder(conn, folder)
            if planned is not None:
                by_id = {grant.grant_id: grant for grant in planned.new}
                for grant in planned.new:
                    self._insert_grant(conn, grant)
                for old_id, new_grant_id in planned.replaced.items():
                    self._replace_in(conn, old_id, by_id[new_grant_id], at=at)
            for grant_id in revoke_ids:
                self._revoke_in(conn, grant_id, actor=actor, at=at)
            finished = finish(planned) if finish is not None else None
            if finished is not None:
                self._finish_in(conn, finished, claim_id=claim_id)
            self._bump(conn, team_id)
            conn.commit()
        return planned, finished

    def approve_pending(
        self, grant_id: str, *, actor: SubjectRefV1, at: datetime, plan: Callable[[], PlanT]
    ) -> tuple[GrantV1, bool]:
        """Approve a hand-down waiting for approval, as one entry per tool (see ``store_planned``).

        Returns the approved grant and True, or the grant already covering it
        and False: the waiting hand-down is then not needed and is denied.
        Raises ``AccessStoreConflict`` when it no longer waits.
        """

        with self._connect() as conn:
            conn.begin_write()
            grant = self._pending_in(conn, grant_id)
            with self._joining(conn):
                planned = plan()
            cover = planned.covered.get(grant_id)
            status = GrantStatus.DENIED if cover is not None else GrantStatus.ACTIVE
            self._set_pending_status_in(conn, grant, status, actor=actor, at=at)
            if cover is None:
                for old_id, by in planned.replaced.items():
                    if by == grant_id:
                        self._replace_in(conn, old_id, grant, at=at)
            self._bump(conn, grant.team_id)
            conn.commit()
        if cover is not None:
            return cover, False
        return grant.model_copy(update={"status": GrantStatus.ACTIVE}), True

    def _pending_in(self, conn: Any, grant_id: str) -> GrantV1:
        p = self.dialect.bind
        row = conn.execute(f"SELECT * FROM access_grants WHERE grant_id={p}", (grant_id,)).fetchone()
        if row is None:
            raise KeyError(grant_id)
        grant = self._grant_from_row(row)
        if grant.status is not GrantStatus.PENDING:
            raise AccessStoreConflict(f"Grant {grant_id} is no longer pending.")
        return grant

    def _set_pending_status_in(
        self, conn: Any, grant: GrantV1, status: GrantStatus, *, actor: SubjectRefV1 | None, at: datetime
    ) -> None:
        p = self.dialect.bind
        updated = conn.execute(
            f"UPDATE access_grants SET status={p} WHERE grant_id={p} AND status={p}",
            (status.value, grant.grant_id, GrantStatus.PENDING.value),
        )
        if getattr(updated, "rowcount", 1) != 1:
            raise AccessStoreConflict(f"Grant {grant.grant_id} is no longer pending.")
        kind = "grant_approved" if status is GrantStatus.ACTIVE else "grant_denied"
        self._event(conn, self._new_event(grant, kind, actor, at))

    def consume(self, grant_id: str, *, at: datetime, operation_id: str | None = None) -> bool:
        """Use one of a grant's remaining uses for one operation, or raise if none is left.

        ``operation_id`` names the tool call the use is for (the access hook
        passes ``custom_agents.access_runtime.call_operation_id``). A call that
        already holds a use of this grant keeps it only while the grant is
        active: nothing more is used and False is returned. That happens when
        Strands runs a paused call again as it resumes and the access hook
        decides it a second time. Without an id the use belongs to no call and
        is never recognized again. True when
        a use was taken now. The check, the guarded update and the record of
        the use are one transaction, so two calls never share one use.
        """

        grant = self.get(grant_id)
        if grant is None:
            raise KeyError(grant_id)
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            if operation_id is not None and self._use_held_in(conn, grant_id, operation_id):
                # Eligibility and the pre-lock get can both predate revocation.
                # All access writers share this lock on SQLite and PostgreSQL.
                current = conn.execute(
                    f"SELECT status FROM access_grants WHERE grant_id={p}", (grant_id,)
                ).fetchone()
                if current is None or current["status"] != GrantStatus.ACTIVE.value:
                    raise AccessStoreConflict(f"Grant {grant_id} is no longer active.")
                conn.commit()
                return False
            updated = conn.execute(
                f"UPDATE access_grants SET uses_remaining = uses_remaining - 1 "
                f"WHERE grant_id={p} AND status={p} AND uses_remaining > 0",
                (grant_id, GrantStatus.ACTIVE.value),
            )
            if getattr(updated, "rowcount", 1) != 1:
                conn.commit()
                raise AccessStoreConflict(f"Grant {grant_id} has no uses left.")
            if operation_id is not None:
                conn.execute(
                    f"INSERT INTO access_grant_uses(grant_id, operation_id, team_id, used_at) VALUES ({p},{p},{p},{p})",
                    (grant_id, operation_id, grant.team_id, _utc(at)),
                )
            detail = {"operation_id": operation_id} if operation_id is not None else {}
            self._event(conn, self._new_event(grant, "grant_consumed", None, at, **detail))
            self._bump(conn, grant.team_id)
            conn.commit()
        return True

    def use_held(self, grant_id: str, operation_id: str) -> bool:
        """Whether the operation (one tool call) already took a use of the grant (``consume``)."""

        with self._connect() as conn:
            return self._use_held_in(conn, grant_id, operation_id)

    def _use_held_in(self, conn: Any, grant_id: str, operation_id: str) -> bool:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT 1 FROM access_grant_uses WHERE grant_id={p} AND operation_id={p}", (grant_id, operation_id)
        ).fetchone()
        return row is not None

    def revoke_all(self, team_id: str, grant_ids: Iterable[str], *, actor: SubjectRefV1, at: datetime) -> list[str]:
        """Revoke several grants (one entry) and what was handed down from them, in one transaction."""

        revoked: list[str] = []
        with self._connect() as conn:
            conn.begin_write()
            for grant_id in grant_ids:
                if grant_id not in revoked:
                    revoked += self._revoke_in(conn, grant_id, actor=actor, at=at)
            self._bump(conn, team_id)
            conn.commit()
        return revoked

    def revoke(self, grant_id: str, *, actor: SubjectRefV1, at: datetime) -> list[str]:
        """Revoke a grant and everything handed down from it; returns the ids."""

        grant = self.get(grant_id)
        if grant is None:
            raise KeyError(grant_id)
        with self._connect() as conn:
            conn.begin_write()
            revoked = self._revoke_in(conn, grant_id, actor=actor, at=at)
            self._bump(conn, grant.team_id)
            conn.commit()
        return revoked

    def end_task_grants(self, team_id: str, task_ids: Iterable[str], *, at: datetime) -> list[str]:
        """End the grants bound to tasks that ended, and everything handed down from them.

        Task-bound grants stop counting once no call carries their task id,
        but their records stayed active, so the Access page kept listing them
        as current access. Ending them keeps the records true. The team is
        the actor, and each event names the task (``ended_with_task``).
        """

        ended_tasks = {task_id for task_id in task_ids if task_id}
        if not ended_tasks:
            return []
        grants = [grant for grant in self.task_grants(team_id) if grant.task_id in ended_tasks]
        if not grants:
            return []
        actor = SubjectRefV1(kind=SubjectKind.TEAM, id=team_id)
        ended: list[str] = []
        with self._connect() as conn:
            conn.begin_write()
            for grant in grants:
                if grant.grant_id in ended:
                    continue  # already ended as a hand-down of an earlier one
                ended += self._revoke_in(
                    conn, grant.grant_id, actor=actor, at=at, detail={"ended_with_task": grant.task_id}
                )
            if ended:
                self._bump(conn, team_id)
            conn.commit()
        return ended

    def end_schedule_grants(self, schedule_id: str, *, at: datetime) -> list[str]:
        """End a deleted scheduled job's access (grants it holds) and what was handed down from them.

        Runs of a deleted schedule are refused anyway (``access_control.task_identity``),
        so this takes nothing away; it keeps the Access page from listing
        access no run can use. Each team is the actor for its grants, and each
        event names the schedule (``ended_with_schedule``).
        """

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM access_grants WHERE status IN ({p},{p}) ORDER BY created_at, grant_id",
                (GrantStatus.ACTIVE.value, GrantStatus.PENDING.value),
            ).fetchall()
        grants = [
            grant
            for grant in map(self._grant_from_row, rows)
            if grant.subject.kind is SubjectKind.SCHEDULE and grant.subject.id == f"{grant.team_id}/{schedule_id}"
        ]
        if not grants:
            return []
        ended: list[str] = []
        with self._connect() as conn:
            conn.begin_write()
            for grant in grants:
                if grant.grant_id in ended:
                    continue
                ended += self._revoke_in(
                    conn,
                    grant.grant_id,
                    actor=SubjectRefV1(kind=SubjectKind.TEAM, id=grant.team_id),
                    at=at,
                    detail={"ended_with_schedule": schedule_id},
                )
            for team_id in {grant.team_id for grant in grants}:
                self._bump(conn, team_id)
            conn.commit()
        return ended

    def _revoke_in(
        self, conn: Any, grant_id: str, *, actor: SubjectRefV1, at: datetime, detail: dict[str, Any] | None = None
    ) -> list[str]:
        """Mark the grant and its descendants revoked inside an open write transaction.

        ``detail`` is recorded on the grant's own event (why it ended);
        descendants record ``cascade_from``.
        """

        p = self.dialect.bind
        head = conn.execute(f"SELECT team_id FROM access_grants WHERE grant_id={p}", (grant_id,)).fetchone()
        if head is None:
            raise KeyError(grant_id)
        revoked: list[str] = []
        frontier = [grant_id]
        while frontier:
            current = frontier.pop()
            row = conn.execute(f"SELECT status FROM access_grants WHERE grant_id={p}", (current,)).fetchone()
            if row is None:
                continue
            if row[0] in {GrantStatus.ACTIVE.value, GrantStatus.PENDING.value}:
                conn.execute(
                    f"UPDATE access_grants SET status={p}, revoked_at={p}, revoked_by_json={p} WHERE grant_id={p}",
                    (GrantStatus.REVOKED.value, _utc(at), actor.model_dump_json(), current),
                )
                revoked.append(current)
            children = conn.execute(f"SELECT grant_id FROM access_grants WHERE parent_grant_id={p}", (current,)).fetchall()
            frontier += [child[0] for child in children]
        for revoked_id in revoked:
            self._event(
                conn,
                AccessEventV1(
                    event_id=new_id("access_event"),
                    team_id=str(head[0]),
                    kind="grant_revoked",
                    grant_id=revoked_id,
                    actor=actor,
                    detail={"cascade_from": grant_id} if revoked_id != grant_id else dict(detail or {}),
                    recorded_at_utc=at,
                ),
            )
        return revoked

    # -- account links -------------------------------------------------------

    @staticmethod
    def _link_from_row(row: Any) -> AccountLinkV1:
        link = AccountLinkV1.model_validate_json(row["payload_json"])
        return link.model_copy(
            update={
                "status": AccountLinkStatus(row["status"]),
                "revoked_at_utc": datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
                "revoked_by": SubjectRefV1.model_validate_json(row["revoked_by_json"])
                if row["revoked_by_json"]
                else None,
            }
        )

    def get_link(self, link_id: str) -> AccountLinkV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM access_account_links WHERE link_id={p}", (link_id,)).fetchone()
        return self._link_from_row(row) if row else None

    def active_link(
        self, team_id: str, tool_id: str, owner_kind: LinkOwnerKind, owner_id: str
    ) -> AccountLinkV1 | None:
        with self._connect() as conn:
            return self._active_link_in(conn, team_id, tool_id, owner_kind, owner_id)

    def _active_link_in(
        self, conn: Any, team_id: str, tool_id: str, owner_kind: LinkOwnerKind, owner_id: str
    ) -> AccountLinkV1 | None:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT * FROM access_account_links WHERE team_id={p} AND tool_id={p} "
            f"AND owner_kind={p} AND owner_id={p} AND status={p} ORDER BY created_at DESC, link_id DESC",
            (team_id, tool_id, owner_kind.value, owner_id, AccountLinkStatus.ACTIVE.value),
        ).fetchone()
        return self._link_from_row(row) if row else None

    def list_links(self, team_id: str, *, include_revoked: bool = False) -> list[AccountLinkV1]:
        p = self.dialect.bind
        query = f"SELECT * FROM access_account_links WHERE team_id={p}"
        params: list[Any] = [team_id]
        if not include_revoked:
            query += f" AND status={p}"
            params.append(AccountLinkStatus.ACTIVE.value)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY created_at, link_id", tuple(params)).fetchall()
        return [self._link_from_row(row) for row in rows]

    def _link_event(self, link: AccountLinkV1, kind: str, actor: SubjectRefV1, at: datetime, **detail: Any) -> AccessEventV1:
        return AccessEventV1(
            event_id=new_id("access_event"),
            team_id=link.team_id,
            kind=kind,
            grant_id=link.link_id,
            actor=actor,
            detail={
                "tool_id": link.tool_id,
                "owner_kind": link.owner_kind.value,
                "owner_id": link.owner_id,
                **detail,
            },
            recorded_at_utc=at,
        )

    def _mark_link_revoked(self, conn: Any, link: AccountLinkV1, *, actor: SubjectRefV1, at: datetime, **detail: Any) -> None:
        p = self.dialect.bind
        conn.execute(
            f"UPDATE access_account_links SET status={p}, revoked_at={p}, revoked_by_json={p} "
            f"WHERE link_id={p} AND status={p}",
            (AccountLinkStatus.REVOKED.value, _utc(at), actor.model_dump_json(), link.link_id, AccountLinkStatus.ACTIVE.value),
        )
        self._event(conn, self._link_event(link, "account_link_revoked", actor, at, **detail))

    def create_link(self, link: AccountLinkV1) -> AccountLinkV1:
        """Store a link, revoking the owner's earlier link for the same tool.

        The earlier link is read inside the write transaction, so of two links
        stored at once the second replaces the first; the unique index on
        active links (``_one_active_link_per_owner``) refuses any other path
        to a second active link.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            previous = self._active_link_in(conn, link.team_id, link.tool_id, link.owner_kind, link.owner_id)
            if previous is not None:
                self._mark_link_revoked(
                    conn, previous, actor=link.created_by, at=link.created_at_utc, replaced_by=link.link_id
                )
            conn.execute(
                f"INSERT INTO access_account_links(link_id, team_id, tool_id, owner_kind, owner_id, status, "
                f"revoked_at, revoked_by_json, payload_json, created_at) "
                f"VALUES ({p},{p},{p},{p},{p},{p},NULL,NULL,{p},{p})",
                (
                    link.link_id,
                    link.team_id,
                    link.tool_id,
                    link.owner_kind.value,
                    link.owner_id,
                    AccountLinkStatus.ACTIVE.value,
                    link.model_dump_json(),
                    _utc(link.created_at_utc),
                ),
            )
            self._event(conn, self._link_event(link, "account_link_created", link.created_by, link.created_at_utc,
                                               replaces=previous.link_id if previous else None))
            self._bump(conn, link.team_id)
            conn.commit()
        return link

    def revoke_link(self, link_id: str, *, actor: SubjectRefV1, at: datetime) -> AccountLinkV1:
        link = self.get_link(link_id)
        if link is None:
            raise KeyError(link_id)
        if link.status is not AccountLinkStatus.ACTIVE:
            return link
        with self._connect() as conn:
            conn.begin_write()
            self._mark_link_revoked(conn, link, actor=actor, at=at)
            self._bump(conn, link.team_id)
            conn.commit()
        return link.model_copy(update={"status": AccountLinkStatus.REVOKED, "revoked_at_utc": at, "revoked_by": actor})

    # -- access requests -----------------------------------------------------

    @staticmethod
    def tool_use_key(request: AccessRequestV1) -> str | None:
        if not request.tool_use_id:
            return None
        return f"{request.team_id}:{request.requester_agent_id}:{request.task_id or ''}:{request.tool_use_id}"

    def create_request(self, request: AccessRequestV1) -> AccessRequestV1:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                "INSERT INTO access_requests(request_id, team_id, status, task_id, tool_use_key, announced, "
                f"payload_json, created_at) VALUES ({p},{p},{p},{p},{p},{p},{p},{p})",
                (
                    request.request_id,
                    request.team_id,
                    request.status.value,
                    request.task_id,
                    self.tool_use_key(request),
                    ANNOUNCE_NEW if request.status is AccessRequestStatus.WAITING else ANNOUNCE_DONE,
                    request.model_dump_json(),
                    _utc(request.created_at_utc),
                ),
            )
            conn.commit()
        return request

    def request_for_tool_use(
        self, *, team_id: str, agent_id: str, task_id: str | None, tool_use_id: str
    ) -> AccessRequestV1 | None:
        p = self.dialect.bind
        key = f"{team_id}:{agent_id}:{task_id or ''}:{tool_use_id}"
        with self._connect() as conn:
            row = conn.execute(f"SELECT payload_json FROM access_requests WHERE tool_use_key={p}", (key,)).fetchone()
        return AccessRequestV1.model_validate_json(row[0]) if row else None

    def claim_change(self, request_id: str, *, at: datetime) -> AccessRequestV1:
        """Mark an approved change as being applied; each approval applies once.

        Claimed immediately before trusted code applies the change, like a
        one-time grant consumed before its effect; raises if already claimed.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT payload_json FROM access_requests WHERE request_id={p} AND status={p} AND applied=0",
                (request_id, AccessRequestStatus.GRANTED.value),
            ).fetchone()
            if row is None:
                conn.commit()
                raise AccessStoreConflict(f"Change {request_id} is not approved or was already applied.")
            claimed = AccessRequestV1.model_validate_json(row[0]).model_copy(update={"applied_at_utc": at})
            updated = conn.execute(
                f"UPDATE access_requests SET applied=1, payload_json={p} WHERE request_id={p} AND applied=0",
                (claimed.model_dump_json(), request_id),
            )
            if getattr(updated, "rowcount", 1) != 1:
                raise AccessStoreConflict(f"Change {request_id} is not approved or was already applied.")
            conn.commit()
        return claimed

    def record_change_error(self, request_id: str, error: str) -> None:
        """Keep why an approved change failed to apply (the approval stays used).

        Read and written in one transaction, so a concurrent change to the
        record (``rebind_requests``) is not overwritten.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT payload_json FROM access_requests WHERE request_id={p}", (request_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return
            updated = AccessRequestV1.model_validate_json(row[0]).model_copy(update={"apply_error": error[:2000]})
            conn.execute(
                f"UPDATE access_requests SET payload_json={p} WHERE request_id={p}",
                (updated.model_dump_json(), request_id),
            )
            conn.commit()

    def rebind_requests(
        self, team_id: str, request_ids: list[str], *, agent_type: str, to_agent_id: str
    ) -> list[str]:
        """Point requests at the agent that now holds their task; the ids changed.

        An agent's id is new after every restart, and a restarted agent of the
        same type takes over its predecessor's tasks (``runtime_recovery``).
        Decision notices are addressed to ``requester_agent_id``, so requests
        follow the task. Only requests made by the same agent type move, and
        each row is compare-and-set on its stored payload so a concurrent
        decision is not overwritten. Returns the ids that moved.
        """

        p = self.dialect.bind
        moved: list[str] = []
        with self._connect() as conn:
            conn.begin_write()
            for request_id in request_ids:
                row = conn.execute(
                    f"SELECT payload_json FROM access_requests WHERE request_id={p} AND team_id={p}",
                    (request_id, team_id),
                ).fetchone()
                if row is None:
                    continue
                request = AccessRequestV1.model_validate_json(row[0])
                if (
                    request.requester_agent_type != agent_type
                    or not request.requester_agent_id
                    or request.requester_agent_id == to_agent_id
                ):
                    continue
                updated = request.model_copy(update={"requester_agent_id": to_agent_id})
                cursor = conn.execute(
                    f"UPDATE access_requests SET payload_json={p}, tool_use_key={p} "
                    f"WHERE request_id={p} AND payload_json={p}",
                    (updated.model_dump_json(), self.tool_use_key(updated), request_id, row[0]),
                )
                if cursor.rowcount == 1:
                    moved.append(request_id)
            conn.commit()
        return moved

    def waiting_for_task(self, team_id: str, agent_id: str, task_id: str) -> list[AccessRequestV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_requests WHERE team_id={p} AND task_id={p} AND status={p}",
                (team_id, task_id, AccessRequestStatus.WAITING.value),
            ).fetchall()
        requests = [AccessRequestV1.model_validate_json(row[0]) for row in rows]
        return [request for request in requests if request.requester_agent_id == agent_id]

    def change_requests_for_task(
        self, *, team_id: str, agent_id: str, task_id: str, change_id: str
    ) -> list[AccessRequestV1]:
        """This agent's requests in one task to apply one staged change, oldest first.

        ``apply_change`` uses them when a request for the change was made by
        another call, such as a workflow's batch of approvals.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_requests WHERE team_id={p} AND task_id={p} "
                "ORDER BY created_at, request_id",
                (team_id, task_id),
            ).fetchall()
        requests = [AccessRequestV1.model_validate_json(row[0]) for row in rows]
        return [
            request
            for request in requests
            if request.requester_agent_id == agent_id
            and request.change is not None
            and request.change.change_id == change_id
        ]

    def claim_unannounced(self, team_id: str) -> list[AccessRequestV1]:
        """Waiting requests nobody was told about yet, marked as told (once each)."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            rows = conn.execute(
                f"SELECT request_id, payload_json FROM access_requests WHERE team_id={p} AND status={p} "
                f"AND announced={p} ORDER BY created_at",
                (team_id, AccessRequestStatus.WAITING.value, ANNOUNCE_NEW),
            ).fetchall()
            for row in rows:
                conn.execute(
                    f"UPDATE access_requests SET announced={p} WHERE request_id={p}", (ANNOUNCE_TOLD, row[0])
                )
            conn.commit()
        return [AccessRequestV1.model_validate_json(row[1]) for row in rows]

    def unsent_resolutions(self, team_id: str) -> list[AccessRequestV1]:
        """Decided requests whose waiting agent was not yet told (withdrawn ones need no message)."""

        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_requests WHERE team_id={p} AND status IN ({p},{p}) "
                f"AND announced<>{p} ORDER BY created_at",
                (team_id, AccessRequestStatus.GRANTED.value, AccessRequestStatus.DENIED.value, ANNOUNCE_DONE),
            ).fetchall()
        return [AccessRequestV1.model_validate_json(row[0]) for row in rows]

    def mark_resolution_sent(self, request_id: str) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(f"UPDATE access_requests SET announced={p} WHERE request_id={p}", (ANNOUNCE_DONE, request_id))
            conn.commit()

    # -- notices -----------------------------------------------------------------

    def create_notice(self, notice: AccessNoticeV1) -> AccessNoticeV1:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO access_notices(notice_id, team_id, payload_json, created_at) VALUES ({p},{p},{p},{p})",
                (notice.notice_id, notice.team_id, notice.model_dump_json(), _utc(notice.created_at_utc)),
            )
            conn.commit()
        return notice

    def create_notice_if_absent(self, notice: AccessNoticeV1) -> bool:
        """Store a notice unless one with its id exists; True when stored.

        Trusted runtime code gives a notice an id derived from what it reports,
        so the same report from several processes, or from every maintenance
        run, reaches people once.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            cursor = conn.execute(
                f"INSERT INTO access_notices(notice_id, team_id, payload_json, created_at) VALUES ({p},{p},{p},{p}) "
                "ON CONFLICT(notice_id) DO NOTHING",
                (notice.notice_id, notice.team_id, notice.model_dump_json(), _utc(notice.created_at_utc)),
            )
            conn.commit()
        return cursor.rowcount > 0

    def _visible_notices(
        self, conn: Any, team_id: str, *, person_id: str, roles: tuple[str, ...]
    ) -> list[tuple[AccessNoticeV1, bool]]:
        """Every notice for the person (theirs or one of their roles'), newest first, with whether they read it.

        Recipients live in the payload, so they are matched here rather than
        in SQL; teams keep at most a few thousand notices, read once per
        Inbox view.
        """

        p = self.dialect.bind
        rows = conn.execute(
            f"SELECT n.payload_json, r.read_at FROM access_notices n LEFT JOIN access_notice_reads r "
            f"ON r.notice_id = n.notice_id AND r.person_id = {p} WHERE n.team_id = {p} "
            "ORDER BY n.created_at DESC, n.notice_id",
            (person_id, team_id),
        ).fetchall()
        wanted = set(roles)
        result: list[tuple[AccessNoticeV1, bool]] = []
        for row in rows:
            notice = AccessNoticeV1.model_validate_json(row[0])
            if notice.recipient_person_id == person_id or wanted.intersection(notice.recipient_roles):
                result.append((notice, row[1] is not None))
        return result

    def notices_for(
        self, team_id: str, *, person_id: str, roles: tuple[str, ...]
    ) -> list[tuple[AccessNoticeV1, bool]]:
        """The person's notices, newest first, each with whether they read it."""

        with self._connect() as conn:
            return self._visible_notices(conn, team_id, person_id=person_id, roles=roles)

    def notice_page(
        self,
        team_id: str,
        *,
        person_id: str,
        roles: tuple[str, ...],
        unread_only: bool,
        offset: int,
        limit: int,
    ) -> AccessNoticePage:
        """One page of the person's notices (all, or unread only), with the totals the Inbox shows."""

        notices = self.notices_for(team_id, person_id=person_id, roles=roles)
        unread = sum(1 for _notice, read in notices if not read)
        shown = [entry for entry in notices if not entry[1]] if unread_only else notices
        return AccessNoticePage(
            notices=shown[offset : offset + limit], total=len(shown), unread=unread, all_total=len(notices)
        )

    def mark_notices_read(
        self,
        team_id: str,
        *,
        person_id: str,
        roles: tuple[str, ...],
        notice_ids: Iterable[str] | None,
        at: datetime,
    ) -> int:
        """Mark the person's unread notices read: those in ``notice_ids``, or all when it is None.

        Only notices the person can see are marked; other ids are ignored.
        Returns how many notices changed from unread to read.
        """

        p = self.dialect.bind
        wanted = None if notice_ids is None else set(notice_ids)
        with self._connect() as conn:
            conn.begin_write()
            unread = [
                notice.notice_id
                for notice, read in self._visible_notices(conn, team_id, person_id=person_id, roles=roles)
                if not read and (wanted is None or notice.notice_id in wanted)
            ]
            for notice_id in unread:
                conn.execute(
                    f"INSERT INTO access_notice_reads(notice_id, person_id, read_at) VALUES ({p},{p},{p}) "
                    "ON CONFLICT(notice_id, person_id) DO NOTHING",
                    (notice_id, person_id, _utc(at)),
                )
            conn.commit()
        return len(unread)

    # -- tool setup states (access_control.tool_setup_states) -----------------------

    def tool_setup_state(self, team_id: str, person_id: str, tool_id: str) -> ToolSetupStateV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM access_tool_setup_states WHERE team_id={p} AND person_id={p} AND tool_id={p}",
                (team_id, person_id, tool_id),
            ).fetchone()
        return ToolSetupStateV1.model_validate_json(row[0]) if row else None

    def tool_setup_states(self, team_id: str, *, person_id: str | None = None) -> list[ToolSetupStateV1]:
        """The team's states, or one person's, ordered by person and tool."""

        p = self.dialect.bind
        query = f"SELECT payload_json FROM access_tool_setup_states WHERE team_id={p}"
        params: tuple[str, ...] = (team_id,)
        if person_id is not None:
            query += f" AND person_id={p}"
            params = (team_id, person_id)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY person_id, tool_id", params).fetchall()
        return [ToolSetupStateV1.model_validate_json(row[0]) for row in rows]

    def put_tool_setup_state(self, state: ToolSetupStateV1) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO access_tool_setup_states(team_id, person_id, tool_id, state, payload_json, changed_at) "
                f"VALUES ({p},{p},{p},{p},{p},{p}) ON CONFLICT(team_id, person_id, tool_id) DO UPDATE SET "
                "state=excluded.state, payload_json=excluded.payload_json, changed_at=excluded.changed_at",
                (
                    state.team_id,
                    state.person_id,
                    state.tool_id,
                    state.state.value,
                    state.model_dump_json(),
                    _utc(state.changed_at_utc),
                ),
            )
            conn.commit()

    # -- people's model settings (access_control.person_settings) --------------------

    def person_settings(self, team_id: str, person_id: str) -> PersonSettingsV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM access_person_settings WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).fetchone()
        return PersonSettingsV1.model_validate_json(row[0]) if row else None

    def team_person_settings(self, team_id: str) -> list[PersonSettingsV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_person_settings WHERE team_id={p} ORDER BY person_id", (team_id,)
            ).fetchall()
        return [PersonSettingsV1.model_validate_json(row[0]) for row in rows]

    def change_person_settings(
        self,
        team_id: str,
        person_id: str,
        change: Callable[[PersonSettingsV1 | None], PersonSettingsV1],
    ) -> PersonSettingsV1:
        """Read, change and store one person's record in one write transaction.

        The gateway changes the team-key switch and the broker the own model;
        reading inside the write keeps one writer from undoing the other.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT payload_json FROM access_person_settings WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).fetchone()
            current = PersonSettingsV1.model_validate_json(row[0]) if row else None
            changed = change(current)
            if (changed.team_id, changed.person_id) != (team_id, person_id):
                raise ValueError("A person's model settings must stay with the same team and person.")
            conn.execute(
                f"INSERT INTO access_person_settings(team_id, person_id, payload_json, changed_at) VALUES ({p},{p},{p},{p}) "
                "ON CONFLICT(team_id, person_id) DO UPDATE SET payload_json=excluded.payload_json, "
                "changed_at=excluded.changed_at",
                (team_id, person_id, changed.model_dump_json(), _utc(changed.changed_at_utc)),
            )
            conn.commit()
        return changed

    # -- the team's sharing choices (access_control.account_sharing) ------------

    def account_sharing(self, team_id: str, subject: str) -> AccountSharingV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM access_account_sharing WHERE team_id={p} AND subject={p}", (team_id, subject)
            ).fetchone()
        return AccountSharingV1.model_validate_json(row[0]) if row else None

    def team_account_sharing(self, team_id: str) -> list[AccountSharingV1]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM access_account_sharing WHERE team_id={p} ORDER BY subject", (team_id,)
            ).fetchall()
        return [AccountSharingV1.model_validate_json(row[0]) for row in rows]

    def set_account_sharing(self, record: AccountSharingV1) -> AccountSharingV1:
        """Store the team's choice for one service, replacing the earlier one, and log the change."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT payload_json FROM access_account_sharing WHERE team_id={p} AND subject={p}",
                (record.team_id, record.subject),
            ).fetchone()
            previous = AccountSharingV1.model_validate_json(row[0]) if row else None
            conn.execute(
                f"INSERT INTO access_account_sharing(team_id, subject, payload_json, changed_at) VALUES ({p},{p},{p},{p}) "
                "ON CONFLICT(team_id, subject) DO UPDATE SET payload_json=excluded.payload_json, "
                "changed_at=excluded.changed_at",
                (record.team_id, record.subject, record.model_dump_json(), _utc(record.changed_at_utc)),
            )
            self._event(
                conn,
                AccessEventV1(
                    event_id=new_id("access_event"),
                    team_id=record.team_id,
                    kind="account_sharing_changed",
                    actor=(
                        SubjectRefV1(kind=SubjectKind.USER, id=record.changed_by_person_id)
                        if record.changed_by_person_id
                        else None
                    ),
                    detail={
                        "subject": record.subject,
                        "sharing": record.sharing.value,
                        "previous": previous.sharing.value if previous is not None else None,
                    },
                    recorded_at_utc=record.changed_at_utc,
                ),
            )
            conn.commit()
        return record

    # -- a person removed from the team (assistant_gateway.person_departure) -----

    def forget_person_records(self, team_id: str, person_id: str, *, request_ids: Iterable[str]) -> tuple[int, int, bool]:
        """Delete a removed person's requests, notices, starter-profile choice, tool setup states and own model in one transaction.

        ``request_ids`` are the requests the caller found for the person. The
        access event log is kept. Returns (requests, notices, profile choice deleted).
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            requests = 0
            for request_id in request_ids:
                conn.execute(f"DELETE FROM access_decision_recoveries WHERE request_id={p}", (request_id,))
                conn.execute(f"DELETE FROM access_request_claims WHERE request_id={p}", (request_id,))
                requests += int(
                    conn.execute(f"DELETE FROM access_requests WHERE request_id={p}", (request_id,)).rowcount or 0
                )
            rows = conn.execute(f"SELECT notice_id, payload_json FROM access_notices WHERE team_id={p}", (team_id,)).fetchall()
            notice_ids = [
                row[0] for row in rows if AccessNoticeV1.model_validate_json(row[1]).recipient_person_id == person_id
            ]
            for notice_id in notice_ids:
                conn.execute(f"DELETE FROM access_notice_reads WHERE notice_id={p}", (notice_id,))
                conn.execute(f"DELETE FROM access_notices WHERE notice_id={p}", (notice_id,))
            conn.execute(f"DELETE FROM access_notice_reads WHERE person_id={p}", (person_id,))
            choice = conn.execute(
                f"DELETE FROM access_profile_choices WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            ).rowcount
            conn.execute(
                f"DELETE FROM access_tool_setup_states WHERE team_id={p} AND person_id={p}", (team_id, person_id)
            )
            self._forget_own_model(conn, team_id, person_id)
            conn.commit()
        return requests, len(notice_ids), bool(choice)

    def _forget_own_model(self, conn: Any, team_id: str, person_id: str) -> None:
        """A removed person's own model goes with their keys; whether they may use the team's keys stays."""

        p = self.dialect.bind
        row = conn.execute(
            f"SELECT payload_json FROM access_person_settings WHERE team_id={p} AND person_id={p}", (team_id, person_id)
        ).fetchone()
        if row is None:
            return
        settings = PersonSettingsV1.model_validate_json(row[0])
        if settings.own_model is None:
            return
        conn.execute(
            f"UPDATE access_person_settings SET payload_json={p} WHERE team_id={p} AND person_id={p}",
            (settings.model_copy(update={"own_model": None}).model_dump_json(), team_id, person_id),
        )

    def get_request(self, request_id: str) -> AccessRequestV1 | None:
        p = self.dialect.bind
        with self._connect() as conn:
            row = conn.execute(f"SELECT payload_json FROM access_requests WHERE request_id={p}", (request_id,)).fetchone()
        return AccessRequestV1.model_validate_json(row[0]) if row else None

    def list_requests(self, team_id: str, *, status: AccessRequestStatus | None = None) -> list[AccessRequestV1]:
        p = self.dialect.bind
        query = f"SELECT payload_json FROM access_requests WHERE team_id={p}"
        params: list[Any] = [team_id]
        if status is not None:
            query += f" AND status={p}"
            params.append(status.value)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY created_at, request_id", tuple(params)).fetchall()
        return [AccessRequestV1.model_validate_json(row[0]) for row in rows]

    def finish_request(self, updated: AccessRequestV1, *, claim_id: str | None = None) -> AccessRequestV1:
        """Record a waiting request's outcome once (raises if it was already decided).

        Inside ``decision_transaction`` this joins the decision's transaction,
        so its grants and its outcome are stored together. A decision with
        work outside the database passes its claim (``claim_decision``); a
        withdrawal passes none (see ``_finish_in``).
        """

        with self._connect() as conn:
            conn.begin_write()
            self._finish_in(conn, updated, claim_id=claim_id)
            conn.commit()
        return _as_decided(updated)

    def _finish_in(self, conn: Any, updated: AccessRequestV1, *, claim_id: str | None) -> None:
        """Record the outcome inside an open write transaction; a lost race raises and rolls it back.

        - ``claim_id`` is the deciding claim of a decision with work outside
          the database: the outcome is stored only while that claim still
          holds the request.
        - A withdrawal (the task ended, or its person left) passes no claim
          and closes the request even while such a decision runs; that
          decision then fails to store its outcome and takes back its work.
        - When the request stops waiting, its hand-downs still waiting for
          approval are denied in this transaction, except those a granted
          outcome names (approved in place). The outcome of a Deny or a
          withdrawal is a copy of the waiting request, so its ``grant_ids``
          still name the hand-downs to deny.
        - A staged change keeps only its ``effect_after_decision`` lines from
          now on, when it has them (``ChangeProposalV1.as_decided``).
        """

        p = self.dialect.bind
        updated = _as_decided(updated)
        if claim_id is not None:
            waiting = self._require_claim_in(conn, DecisionClaim(updated.request_id, claim_id))
        else:
            waiting = self._waiting_in(conn, updated.request_id, message="Access request {request_id} was already decided.")
        # A withdrawn request's task ended; no agent waits for a message.
        done = updated.status is AccessRequestStatus.WITHDRAWN
        result = conn.execute(
            f"UPDATE access_requests SET status={p}, payload_json={p}"
            + (f", announced={p}" if done else "")
            + f" WHERE request_id={p} AND status={p}",
            (
                updated.status.value,
                updated.model_dump_json(),
                *((ANNOUNCE_DONE,) if done else ()),
                updated.request_id,
                AccessRequestStatus.WAITING.value,
            ),
        )
        if getattr(result, "rowcount", 1) != 1:
            raise AccessStoreConflict(f"Access request {updated.request_id} was already decided.")
        conn.execute(f"DELETE FROM access_request_claims WHERE request_id={p}", (updated.request_id,))
        actor = updated.decided_by or SubjectRefV1(kind=SubjectKind.TEAM, id=updated.team_id)
        at = updated.decided_at_utc or datetime.now(timezone.utc)
        keep = set(updated.grant_ids) if updated.status is AccessRequestStatus.GRANTED else set()
        if self._deny_waiting_hand_downs_in(conn, waiting, keep=keep, actor=actor, at=at):
            self._bump(conn, updated.team_id)

    def _waiting_in(self, conn: Any, request_id: str, *, message: str) -> AccessRequestV1:
        """The stored request, which must still wait; ``message`` (with ``{request_id}``) explains a refusal."""

        p = self.dialect.bind
        row = conn.execute(
            f"SELECT status, payload_json FROM access_requests WHERE request_id={p}", (request_id,)
        ).fetchone()
        if row is None:
            raise AccessStoreConflict(f"There is no access request {request_id}.")
        if row[0] != AccessRequestStatus.WAITING.value:
            raise AccessStoreConflict(message.format(request_id=request_id, status=row[0]))
        return AccessRequestV1.model_validate_json(row[1])

    def _require_claim_in(self, conn: Any, claim: DecisionClaim) -> AccessRequestV1:
        """The waiting request, while ``claim`` still holds it; raises ``DecisionClaimLost`` otherwise."""

        p = self.dialect.bind
        try:
            waiting = self._waiting_in(
                conn,
                claim.request_id,
                message=(
                    "Access request {request_id} was already {status} by another decision or a withdrawal while "
                    "this decision ran, so this decision's outcome was not stored."
                ),
            )
        except AccessStoreConflict as exc:
            raise DecisionClaimLost(str(exc)) from exc
        held = conn.execute(
            f"SELECT claim_id FROM access_request_claims WHERE request_id={p}", (claim.request_id,)
        ).fetchone()
        if held is None or held[0] != claim.claim_id:
            raise DecisionClaimLost(
                f"This decision's claim on access request {claim.request_id} ran out and another decision took "
                "it, so this decision's outcome was not stored. Look at the request again."
            )
        return waiting

    def _deny_waiting_hand_downs_in(
        self, conn: Any, waiting: AccessRequestV1, *, keep: set[str], actor: SubjectRefV1, at: datetime
    ) -> int:
        """Deny the request's hand-downs still waiting for approval, except those in ``keep``."""

        p = self.dialect.bind
        denied = 0
        for grant_id in waiting.grant_ids:
            if grant_id in keep:
                continue
            row = conn.execute(f"SELECT * FROM access_grants WHERE grant_id={p}", (grant_id,)).fetchone()
            if row is None or row["status"] != GrantStatus.PENDING.value:
                continue
            self._set_pending_status_in(conn, self._grant_from_row(row), GrantStatus.DENIED, actor=actor, at=at)
            denied += 1
        return denied

    # -- one decision at a time ------------------------------------------------------

    @contextmanager
    def decision_transaction(self, request_id: str, *, at: datetime) -> Iterator[AccessRequestV1]:
        """One transaction for a person's decision: its grants and its outcome are stored together.

        Takes the store's write lock, checks that the request still waits and
        that no decision with work outside the database holds a current claim
        on it (``DecisionInProgress``), and yields the request. Everything the
        decision does on this store in this thread joins the transaction
        (``_joining``): its authority checks and plans see the grants it
        stored a moment ago, other writers see none of them, and if anything
        fails nothing was stored. A second decision waits for the lock and
        then finds the request decided.
        """

        with self._connect() as conn:
            conn.begin_write()
            request = self._waiting_in(conn, request_id, message="That access request was already {status}.")
            self._refuse_current_claim_in(conn, request_id, at=at)
            self._end_older_leftovers_in(conn, request, at=at)
            with self._joining(conn) as opened:
                yield request
            conn.commit()
        for action in opened.after_commit:
            action()

    def _end_older_leftovers_in(self, conn: Any, request: AccessRequestV1, *, at: datetime) -> None:
        """End grants carrying this waiting request's approval id before the decision writes any.

        This version stores a decision's grants with its outcome, so a grant
        carrying the id of a request that still waits was left by an older
        version that stopped between them. A decision made before the
        gateway's Inbox pass ended it (``end_grants_without_granted_outcome``)
        would otherwise make it permanent; ending it here, by the indexed
        approval id, keeps only what this decision gives.
        """

        p = self.dialect.bind
        rows = conn.execute(
            f"SELECT grant_id FROM access_grants WHERE approval_id={p} AND status IN ({p},{p})",
            (request.request_id, GrantStatus.ACTIVE.value, GrantStatus.PENDING.value),
        ).fetchall()
        if not rows:
            return
        team = SubjectRefV1(kind=SubjectKind.TEAM, id=request.team_id)
        ended: list[str] = []
        for row in rows:
            if str(row[0]) not in ended:
                ended += self._revoke_in(
                    conn, str(row[0]), actor=team, at=at, detail={"approval_not_granted": request.request_id}
                )
        self._bump(conn, request.team_id)
        logger.warning(
            "A waiting access request had grants an older version left; they were ended before the decision. "
            "event=grant_without_granted_outcome_ended request_id=%s request_status=%s grant_ids=%s",
            request.request_id,
            request.status.value,
            ",".join(ended),
        )

    def _refuse_current_claim_in(self, conn: Any, request_id: str, *, at: datetime) -> None:
        self._refuse_recovery_in(conn, request_id)
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT claimed_until FROM access_request_claims WHERE request_id={p}", (request_id,)
        ).fetchone()
        if row is not None and datetime.fromisoformat(row[0]) > at:
            raise DecisionInProgress(f"Access request {request_id} is being decided right now.")

    def claim_decision(self, request_id: str, *, claim_id: str, at: datetime, until: datetime) -> AccessRequestV1:
        """Claim a waiting request for a decision with work outside the database; returns the request.

        Raises ``AccessStoreConflict`` when the request is missing or no
        longer waits, and ``DecisionInProgress`` while another decision's
        claim is current. A claim past ``claimed_until`` belonged to a
        decision that stopped without finishing (a process that ended) and is
        taken over. A live decision renews its claim before and after each
        long step (``renew_decision``).
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            request = self._waiting_in(conn, request_id, message="That access request was already {status}.")
            self._refuse_recovery_in(conn, request_id)
            row = conn.execute(
                f"SELECT claim_id, claimed_until FROM access_request_claims WHERE request_id={p}", (request_id,)
            ).fetchone()
            if row is not None and datetime.fromisoformat(row[1]) > at:
                raise DecisionInProgress(f"Access request {request_id} is being decided right now.")
            if row is not None:
                logger.warning(
                    "A decision's claim on an access request ran out before it finished; a new decision takes "
                    "over. event=access_decision_claim_taken_over request_id=%s expired_claim_id=%s claim_id=%s",
                    request_id,
                    row[0],
                    claim_id,
                )
            conn.execute(
                f"INSERT INTO access_request_claims(request_id, team_id, claim_id, claimed_at, claimed_until) "
                f"VALUES ({p},{p},{p},{p},{p}) ON CONFLICT(request_id) DO UPDATE SET claim_id=excluded.claim_id, "
                "claimed_at=excluded.claimed_at, claimed_until=excluded.claimed_until",
                (request_id, request.team_id, claim_id, _utc(at), _utc(until)),
            )
            conn.commit()
        return request

    def renew_decision(self, claim: DecisionClaim, *, until: datetime) -> None:
        """Extend a claim this decision still holds to ``until``; raises ``DecisionClaimLost`` when it does not.

        Called before and after each long step (a program download): the
        step gets the claim's whole time limit, and a decision that lost its
        claim meanwhile stops before it changes anything else.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            self._require_claim_in(conn, claim)
            conn.execute(
                f"UPDATE access_request_claims SET claimed_until={p} WHERE request_id={p} AND claim_id={p}",
                (_utc(until), claim.request_id, claim.claim_id),
            )
            conn.execute(
                f"UPDATE access_decision_recoveries SET recover_after={p} WHERE request_id={p} AND claim_id={p}",
                (_utc(until), claim.request_id, claim.claim_id),
            )
            conn.commit()

    def release_decision(self, request_id: str, claim_id: str) -> bool:
        """Give up a claim this decision still holds (it failed and took back its work)."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            released = conn.execute(
                f"DELETE FROM access_request_claims WHERE request_id={p} AND claim_id={p}", (request_id, claim_id)
            ).rowcount
            conn.commit()
        return bool(released)

    # -- external effects whose outcome needs reconciliation -------------------

    def _refuse_recovery_in(self, conn: Any, request_id: str) -> None:
        p = self.dialect.bind
        row = conn.execute(
            f"SELECT 1 FROM access_decision_recoveries WHERE request_id={p}", (request_id,)
        ).fetchone()
        if row is not None:
            raise DecisionInProgress(
                f"Access request {request_id} is checking the result of an earlier decision before it can be decided again."
            )

    def save_decision_recovery(self, recovery: DecisionRecovery) -> None:
        """Persist effects before attempting the outcome, independently of its commit or rollback."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            self._require_claim_in(conn, recovery.claim)
            held_until = conn.execute(
                f"SELECT claimed_until FROM access_request_claims WHERE request_id={p}", (recovery.claim.request_id,)
            ).fetchone()
            recover_after = max(recovery.recover_after, datetime.fromisoformat(held_until[0]))
            conn.execute(
                "INSERT INTO access_decision_recoveries "
                "(claim_id, request_id, team_id, finished_json, effects_json, recover_after) "
                f"VALUES ({p},{p},{p},{p},{p},{p}) ON CONFLICT(claim_id) DO UPDATE SET "
                "effects_json=excluded.effects_json, finished_json=excluded.finished_json, "
                "recover_after=excluded.recover_after",
                (
                    recovery.claim.claim_id, recovery.claim.request_id, recovery.finished.team_id,
                    recovery.finished.model_dump_json(), json.dumps(recovery.effects), _utc(recover_after),
                ),
            )
            conn.commit()

    def clear_decision_recovery(self, claim: DecisionClaim) -> None:
        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"DELETE FROM access_decision_recoveries WHERE request_id={p} AND claim_id={p}",
                (claim.request_id, claim.claim_id),
            )
            conn.commit()

    @contextmanager
    def decision_effect_transaction(self, claim: DecisionClaim) -> Iterator[None]:
        """Publish a journaled external effect while excluding recovery cleanup.

        The journal is already committed. If cleanup won first, its released
        claim prevents publication; otherwise cleanup waits and sees the effect.
        Only bounded local publication belongs here, never a download.
        """

        with self._connect() as conn:
            conn.begin_write()
            self._require_claim_in(conn, claim)
            yield
            conn.commit()

    def decision_recoveries(self, team_id: str, *, now: datetime | None = None) -> list[DecisionRecovery]:
        p = self.dialect.bind
        query = f"SELECT * FROM access_decision_recoveries WHERE team_id={p}"
        params = (team_id,) if now is None else (team_id, _utc(now))
        if now is not None:
            query += f" AND recover_after<={p}"
        rows = self._read_rows(
            query, params,
        )
        return [self._decision_recovery_from_row(row) for row in rows]

    @staticmethod
    def _decision_recovery_from_row(row: Any) -> DecisionRecovery:
        return DecisionRecovery(
            DecisionClaim(str(row["request_id"]), str(row["claim_id"])),
            AccessRequestV1.model_validate_json(row["finished_json"]),
            json.loads(row["effects_json"]), datetime.fromisoformat(row["recover_after"]),
        )

    @contextmanager
    def decision_cleanup_transaction(
        self, claim: DecisionClaim, *, require_record: bool = False, now: datetime | None = None
    ) -> Iterator[tuple[AccessRequestV1 | None, bool, DecisionRecovery | None] | None]:
        """Recheck the outcome and claim while excluding a takeover during local cleanup.

        A failed read is never evidence that a commit failed. The caller must
        preserve its effects when entering this transaction fails. A successful
        cleanup removes its recovery record and releases only its own claim.
        """

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            row = conn.execute(
                f"SELECT * FROM access_decision_recoveries WHERE claim_id={p}", (claim.claim_id,)
            ).fetchone()
            if require_record and row is None:
                # Another maintenance pass already reconciled this record.
                yield None
                return
            with self._joining(conn):
                current = self.get_request(claim.request_id)
                held = conn.execute(
                    f"SELECT claim_id, claimed_until FROM access_request_claims WHERE request_id={p}", (claim.request_id,)
                ).fetchone()
                if (
                    require_record and held is not None and held[0] == claim.claim_id
                    and datetime.fromisoformat(held[1]) > (now or datetime.now(timezone.utc))
                ):
                    # A download renewed after the maintenance pass read its
                    # list. Its current claim takes precedence over that list.
                    yield None
                    return
                # The claimant may have journaled another file after the
                # maintenance scan. Only this locked read can supply the
                # complete effects list that cleanup is about to clear.
                latest = self._decision_recovery_from_row(row) if row is not None else None
                yield current, held is not None and held[0] != claim.claim_id, latest
                self.clear_decision_recovery(claim)
                self.release_decision(claim.request_id, claim.claim_id)
            conn.commit()

    # -- grants without a granted outcome --------------------------------------------

    def grants_without_granted_outcome(self, team_id: str) -> list[tuple[GrantV1, AccessRequestV1]]:
        """Grants in force or waiting that carry the approval id of a request that was not granted.

        A decision stores its grants and its outcome together, so this finds
        none that the current version wrote. An older version stored grants
        before their outcome and could leave them behind when it stopped in
        between (a crash, a failure it could not undo). Such grants must not
        stay in force (``end_grants_without_granted_outcome``) or travel in a
        team transfer (``team_transfer.access``).
        """

        p = self.dialect.bind
        rows = self._read_rows(
            "SELECT g.*, r.payload_json AS request_json FROM access_grants g "
            "JOIN access_requests r ON r.request_id = g.approval_id "
            f"WHERE g.team_id={p} AND g.status IN ({p},{p}) AND r.status <> {p}",
            (team_id, GrantStatus.ACTIVE.value, GrantStatus.PENDING.value, AccessRequestStatus.GRANTED.value),
        )
        return [(self._grant_from_row(row), AccessRequestV1.model_validate_json(row["request_json"])) for row in rows]

    def end_grants_without_granted_outcome(self, team_id: str, now: datetime) -> list[str]:
        """End the grants ``grants_without_granted_outcome`` finds (and what was handed down from them).

        The gateway's Inbox pass runs this; the usual pass only reads and
        takes no write lock. Each ended grant is logged with its request.
        """

        if not self.grants_without_granted_outcome(team_id):
            return []
        team = SubjectRefV1(kind=SubjectKind.TEAM, id=team_id)
        ended: list[str] = []
        with self._connect() as conn:
            conn.begin_write()
            with self._joining(conn):
                found = self.grants_without_granted_outcome(team_id)
            for grant, request in found:
                if grant.grant_id in ended:
                    continue
                ended += self._revoke_in(
                    conn, grant.grant_id, actor=team, at=now, detail={"approval_not_granted": request.request_id}
                )
                logger.warning(
                    "A grant carried the approval id of an access request that was not granted; it was ended. "
                    "event=grant_without_granted_outcome_ended grant_id=%s request_id=%s request_status=%s",
                    grant.grant_id,
                    request.request_id,
                    request.status.value,
                )
            if ended:
                self._bump(conn, team_id)
            conn.commit()
        return ended

    def _read_rows(self, query: str, params: tuple[Any, ...]) -> list[Any]:
        with self._connect() as reader:
            return list(reader.execute(query, params).fetchall())

    def record_history_cleanup(
        self, team_id: str, detail: dict[str, Any], *, actor: SubjectRefV1 | None, at: datetime
    ) -> None:
        """Log one side of a person's history clean-up (kind ``history_cleanup``): ids, approval and counts."""

        with self._connect() as conn:
            conn.begin_write()
            self._event(
                conn,
                AccessEventV1(
                    event_id=new_id("access_event"),
                    team_id=team_id,
                    kind="history_cleanup",
                    actor=actor,
                    detail=detail,
                    recorded_at_utc=at,
                ),
            )
            conn.commit()

    def record_decision(self, team_id: str, detail: dict[str, Any], *, at: datetime) -> None:
        with self._connect() as conn:
            conn.begin_write()
            self._event(
                conn,
                AccessEventV1(
                    event_id=new_id("access_event"),
                    team_id=team_id,
                    kind="decision",
                    detail=detail,
                    recorded_at_utc=at,
                ),
            )
            conn.commit()


__all__ = [
    "ANNOUNCE_DONE",
    "ANNOUNCE_NEW",
    "ANNOUNCE_TOLD",
    "AccessNoticePage",
    "AccessRepository",
    "AccessStoreConflict",
]
