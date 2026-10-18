"""One holder per task across the team: ``runtime_task_custody``.

Why (2026-10-17): JetStream hands a delivered task out again when its
acknowledgement does not arrive within the wait, for example when the process
that took it stops between saving the task and acknowledging it, or NATS is
unreachable for longer than the wait. An agent checked only its own records
for duplicates, so a second worker of the type ran the copy while the first
holder, or the replacement that took over its saved work, ran the original.

Rows change only inside the transactions that save, recover or clear an
agent's runtime snapshot (``RuntimeStateRepository.save_task_runtime``), so a
row names the agent whose saved work last claimed the task:

- a task entering an owner's snapshot claims its row; a row held by another
  agent refuses the save (``TaskCustodyConflict``) unless the caller names
  that holder as the one it takes over from (compare-and-set on that holder;
  a row the holder released meanwhile is never claimed afresh,
  ``TaskCustodyReleased``);
- every acquisition (a fresh claim, a takeover, a recovery) reads the
  finished-task archive in its own transaction and refuses a finished task
  (``TaskCustodyFinished``), since a holder archives before it releases;
- a task that stays in the snapshot but whose row another agent took over, or
  whose row is gone although this owner never released it (the taker gave it
  up again, finished or not), refuses the save (``TaskCustodyTakenOver``)
  before anything is written; the snapshot coordinator drops it and saves
  again, so a holder wrongly judged gone stops running it;
- a task leaving the snapshot (it ended, was handed back, or was dropped)
  releases the row, but only while this owner still holds it;
- recovery moves the rows of the adopted tasks to the replacement in the same
  transaction that moves the snapshot (``transfer_on_recovery``) and refuses a
  task another agent holds or the team finished;
- clearing an owner's snapshot releases all its rows.

Two gaps are expected. After a takeover, the gone holder's snapshot still
lists the task until a replacement recovers it and leaves the task out, or,
if that holder is still running, until its next save drops it. A
task in a snapshot saved before this table existed has no row until that
snapshot is restored (every restore commits through recovery, which creates
the missing rows).

Only tasks an agent executes have rows. A requester's records of the tasks it
delegated (``subtask_to_parent_map``) share the worker's task id and never
claim it. Runtime-only state: the team transfer does not carry it, like the
snapshots themselves. Owners without a team id have no team-wide custody.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Iterable, Optional

from storage.sql_dialect import POSTGRES, SqlDialect

CUSTODY_TABLE = "runtime_task_custody"

_LOGGER = logging.getLogger(__name__)


class TaskCustodyConflict(RuntimeError):
    """Another agent holds this task; the save that would claim it was refused."""

    def __init__(self, task_id: str, holder_agent_id: str, message: Optional[str] = None) -> None:
        super().__init__(message or f"Task {task_id} is held by agent {holder_agent_id}.")
        self.task_id = task_id
        self.holder_agent_id = holder_agent_id


class TaskCustodyReleased(TaskCustodyConflict):
    """The holder a takeover named released or finished the task before the takeover's save.

    It finished the task, handed it back or dropped it, so it was alive after
    all. The takeover never claims a fresh row for such a task: the admitting
    caller re-checks the team's finished tasks (``task_message_runtime``).
    """

    def __init__(self, task_id: str, holder_agent_id: str) -> None:
        super().__init__(
            task_id, holder_agent_id, f"Task {task_id} was released by agent {holder_agent_id} before the takeover."
        )


class TaskCustodyFinished(TaskCustodyConflict):
    """The task is in the team's finished-task archive, so no agent may hold it again.

    Read inside the transaction that would claim it (a fresh claim, a takeover
    or a recovery): an agent that finished the task archived it before its
    save released custody, so a claim after that release finds the archive
    row (2026-10-18). ``holder_agent_id`` is the agent that finished it.
    """

    def __init__(self, task_id: str, finished_by: str) -> None:
        super().__init__(task_id, finished_by, f"Task {task_id} was already finished by agent {finished_by}.")
        self.finished_by = finished_by


class TaskCustodyTakenOver(RuntimeError):
    """Tasks of this owner's saved work that it no longer holds.

    Raised by a save before it writes anything. Another agent took each task
    over after judging this owner gone (a redelivered copy,
    ``custom_agents.task_custody``); only the custody row moved, so this
    owner's snapshot still lists the task. ``holders`` maps tasks another
    agent holds now to that agent. ``released`` maps tasks whose row is gone
    although this owner never released it: the agent that took it over gave it
    up again, either finished (the agent that archived it) or not (None; for
    example handed back after a lost lease). The snapshot coordinator drops
    all of them and saves again (``task_manager_runtime_snapshots``). Not a
    ``TaskCustodyConflict``: an admission in progress must not read it as a
    refusal of the task it admits.
    """

    def __init__(
        self, owner_agent_id: str, holders: dict[str, str], released: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        released = dict(released or {})
        super().__init__(
            f"Tasks {sorted(holders) + sorted(released)} of {owner_agent_id} are no longer held by it."
        )
        self.owner_agent_id = owner_agent_id
        self.holders = dict(holders)
        self.released = released


@dataclass(frozen=True)
class TaskCustodyRow:
    team_id: str
    task_id: str
    holder_agent_id: str
    holder_named_agent_type: Optional[str]
    claimed_at: str


@dataclass(frozen=True)
class TaskCustodyTakeover:
    """Admission may replace this holder's row for this task (see task_custody)."""

    task_id: str
    from_holder_agent_id: str


def ensure_schema(conn: Any) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CUSTODY_TABLE} (
            team_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            holder_agent_id TEXT NOT NULL,
            holder_named_agent_type TEXT,
            claimed_at TEXT NOT NULL,
            PRIMARY KEY (team_id, task_id)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{CUSTODY_TABLE}_holder ON {CUSTODY_TABLE}(holder_agent_id)"
    )


def executed_task_ids(records: Iterable[tuple[str, str]]) -> set[str]:
    """Tasks a snapshot executes: its tasks minus the ones it delegated out."""

    keys = list(records)
    tasks = {key for section, key in keys if section == "tasks"}
    delegated = {key for section, key in keys if section == "subtask_to_parent_map"}
    return tasks - delegated


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _placeholders(dialect: SqlDialect, count: int) -> str:
    return ",".join(dialect.bind for _ in range(count))


def read_rows(conn: Any, dialect: SqlDialect, team_id: str, task_ids: Iterable[str]) -> dict[str, TaskCustodyRow]:
    ids = sorted(set(task_ids))
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT team_id, task_id, holder_agent_id, holder_named_agent_type, claimed_at FROM {CUSTODY_TABLE} "
        f"WHERE team_id = {dialect.bind} AND task_id IN ({_placeholders(dialect, len(ids))})",
        (team_id, *ids),
    ).fetchall()
    return {row[1]: TaskCustodyRow(*(row[index] for index in range(5))) for row in rows}


def claim_entering(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str,
    owner_agent_id: str,
    owner_named_agent_type: Optional[str],
    task_ids: Iterable[str],
    takeover: Optional[TaskCustodyTakeover],
) -> None:
    """Claim rows for tasks newly entering this owner's snapshot (inside its save)."""

    bind = dialect.bind
    for task_id in sorted(set(task_ids)):
        if takeover is not None and takeover.task_id == task_id:
            _take_over(conn, dialect, team_id=team_id, owner_agent_id=owner_agent_id,
                       owner_named_agent_type=owner_named_agent_type, takeover=takeover)
            continue
        conn.execute(
            f"INSERT INTO {CUSTODY_TABLE} (team_id, task_id, holder_agent_id, holder_named_agent_type, claimed_at) "
            f"VALUES ({bind}, {bind}, {bind}, {bind}, {bind}) ON CONFLICT (team_id, task_id) DO NOTHING",
            (team_id, task_id, owner_agent_id, owner_named_agent_type, _now()),
        )
        row = read_rows(conn, dialect, team_id, [task_id]).get(task_id)
        if row is not None and row.holder_agent_id != owner_agent_id:
            raise TaskCustodyConflict(task_id, row.holder_agent_id)
        _refuse_finished(conn, dialect, team_id=team_id, task_id=task_id)


def _refuse_finished(conn: Any, dialect: SqlDialect, *, team_id: str, task_id: str) -> None:
    """Refuse a claim of a task the team already finished (read in the claiming transaction).

    Every custody acquisition calls this: a fresh claim, a takeover and a
    recovery. The admission's own archive read comes earlier; an agent that
    finishes and releases the task between that read and this transaction
    must not see it claimed and run again (2026-10-18).
    """
    finished_by = finished_by_team(conn, dialect, team_id=team_id, task_id=task_id)
    if finished_by is not None:
        raise TaskCustodyFinished(task_id, finished_by)


def _take_over(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str,
    owner_agent_id: str,
    owner_named_agent_type: Optional[str],
    takeover: TaskCustodyTakeover,
) -> None:
    """Move the named holder's row to this owner: a compare-and-set on that holder.

    The update applies only while the judged-gone holder still holds the row.
    A row that is gone means the holder released the task meanwhile (it was
    alive after all): nothing is inserted, ``TaskCustodyReleased``. Before
    2026-10-17 the claim inserted a new row first, so a copy was admitted
    after its holder had finished the task. A task in the finished-task
    archive is refused the same way, read after the update in this
    transaction: the holder archives before its save releases the row, so a
    holder that finished between the admission's archive read and this save
    is caught too.
    """

    bind = dialect.bind
    task_id = takeover.task_id
    moved = conn.execute(
        f"UPDATE {CUSTODY_TABLE} SET holder_agent_id = {bind}, holder_named_agent_type = {bind}, claimed_at = {bind} "
        f"WHERE team_id = {bind} AND task_id = {bind} AND holder_agent_id = {bind}",
        (owner_agent_id, owner_named_agent_type, _now(), team_id, task_id, takeover.from_holder_agent_id),
    )
    if moved.rowcount == 1:
        # The holder may have archived the task after the admission read the
        # archive, and not saved its release yet: it was alive and finished it.
        _refuse_finished(conn, dialect, team_id=team_id, task_id=task_id)
        return
    row = read_rows(conn, dialect, team_id, [task_id]).get(task_id)
    if row is None:
        raise TaskCustodyReleased(task_id, takeover.from_holder_agent_id)
    if row.holder_agent_id != owner_agent_id:
        raise TaskCustodyConflict(task_id, row.holder_agent_id)


# The finished-task archive (``storage.task_archive_repository``) lives in the
# same database as the snapshots: both repositories are built from the same
# storage settings. A takeover reads it inside its own transaction; this is
# the one place custody reads another repository's table (intentional coupling).
ARCHIVE_TABLE = "completed_tasks"
# Who finished a task whose archive row names no agent.
UNKNOWN_FINISHER = "unknown"
_MISSING_ARCHIVE_LOGGED = False


def finished_by_team(conn: Any, dialect: SqlDialect, *, team_id: str, task_id: str) -> Optional[str]:
    """The agent of the team that archived the task, or None; read in the caller's transaction.

    A holder archives a task before its save releases custody, so a read in
    the claiming transaction closes the gap between the admission's own
    archive read and its save (2026-10-17, every acquisition since
    2026-10-18). An archive row without an owner reads as ``UNKNOWN_FINISHER``.
    A database without the archive table (a store used on its own, as in
    isolated tests) has no finished tasks; a running team always has the
    table, so its absence is logged once.
    """

    bind = dialect.bind
    if dialect == POSTGRES:
        exists = conn.execute("SELECT to_regclass(%s)", (ARCHIVE_TABLE,)).fetchone()[0] is not None
    else:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (ARCHIVE_TABLE,)
        ).fetchone() is not None
    if not exists:
        global _MISSING_ARCHIVE_LOGGED
        if not _MISSING_ARCHIVE_LOGGED:
            _MISSING_ARCHIVE_LOGGED = True
            _LOGGER.warning(
                "The finished-task archive table is missing from the runtime database, so a takeover "
                "cannot see tasks finished after the admission's own archive read. "
                "event=task_custody_archive_missing table=%s team_id=%s task_id=%s",
                ARCHIVE_TABLE,
                team_id,
                task_id,
            )
        return None
    row = conn.execute(
        f"SELECT owner_agent_id FROM {ARCHIVE_TABLE} WHERE task_id = {bind} AND team_id = {bind} LIMIT 1",
        (task_id, team_id),
    ).fetchone()
    if row is None:
        return None
    return row[0] or UNKNOWN_FINISHER


def taken_over(
    conn: Any, dialect: SqlDialect, *, team_id: str, owner_agent_id: str, task_ids: Iterable[str]
) -> tuple[dict[str, str], dict[str, Optional[str]]]:
    """Of tasks this owner held in its previous save, those it no longer holds.

    Returns (held, released): tasks another agent holds now (task id ->
    holder), and tasks whose row is gone (task id -> the agent that finished
    it, or None when it is not in the archive). Only a holder deletes its own
    row and this owner did not release these, so a missing row means another
    agent took the task over and gave it up again (2026-10-18). Rows for
    snapshots saved before custody existed are created when the snapshot is
    first restored, so every task of a saved snapshot has a row.
    """

    ids = sorted(set(task_ids))
    rows = read_rows(conn, dialect, team_id, ids)
    held = {task_id: row.holder_agent_id for task_id, row in rows.items() if row.holder_agent_id != owner_agent_id}
    released = {
        task_id: finished_by_team(conn, dialect, team_id=team_id, task_id=task_id)
        for task_id in ids
        if task_id not in rows
    }
    return held, released


def release_leaving(
    conn: Any, dialect: SqlDialect, *, team_id: str, owner_agent_id: str, task_ids: Iterable[str]
) -> None:
    """Release rows this owner still holds for tasks that left its snapshot."""

    ids = sorted(set(task_ids))
    if not ids:
        return
    conn.execute(
        f"DELETE FROM {CUSTODY_TABLE} WHERE team_id = {dialect.bind} AND holder_agent_id = {dialect.bind} "
        f"AND task_id IN ({_placeholders(dialect, len(ids))})",
        (team_id, owner_agent_id, *ids),
    )


def release_owners(conn: Any, dialect: SqlDialect, owner_agent_ids: Iterable[str]) -> None:
    """Release every row these owners hold (their snapshots were cleared)."""

    owners = sorted({owner for owner in owner_agent_ids if owner})
    if not owners:
        return
    conn.execute(
        f"DELETE FROM {CUSTODY_TABLE} WHERE holder_agent_id IN ({_placeholders(dialect, len(owners))})",
        owners,
    )


def transfer_on_recovery(
    conn: Any,
    dialect: SqlDialect,
    *,
    team_id: str,
    source_owner_ids: Iterable[str],
    replacement_agent_id: str,
    replacement_named_agent_type: Optional[str],
    adopted_task_ids: Iterable[str],
) -> None:
    """Move custody of adopted tasks to the replacement; release the sources' others.

    Raises ``TaskCustodyConflict`` when an adopted task is held by an agent that
    is neither a source nor the replacement, and ``TaskCustodyFinished`` when
    any agent of the team already finished it: recovery must not adopt it (the
    caller filters such tasks before building the replacement snapshot, so
    this only fires when custody or the archive changed in between, for
    example a copy admitted, or a takeover finished, concurrently).
    """

    bind = dialect.bind
    owners = sorted({owner for owner in source_owner_ids if owner} | {replacement_agent_id})
    owner_binds = _placeholders(dialect, len(owners))
    adopted = sorted(set(adopted_task_ids))
    now = _now()
    for task_id in adopted:
        _refuse_finished(conn, dialect, team_id=team_id, task_id=task_id)
        # Rows from before custody existed are created here, so a recovered
        # task is protected from its first save on. The update applies only to
        # a row the sources or the replacement hold: an agent that claimed the
        # task meanwhile (a redelivered copy, concurrently on PostgreSQL) keeps it.
        written = conn.execute(
            f"INSERT INTO {CUSTODY_TABLE} (team_id, task_id, holder_agent_id, holder_named_agent_type, claimed_at) "
            f"VALUES ({bind}, {bind}, {bind}, {bind}, {bind}) ON CONFLICT (team_id, task_id) DO UPDATE SET "
            "holder_agent_id = excluded.holder_agent_id, holder_named_agent_type = excluded.holder_named_agent_type, "
            f"claimed_at = excluded.claimed_at WHERE {CUSTODY_TABLE}.holder_agent_id IN ({owner_binds})",
            (team_id, task_id, replacement_agent_id, replacement_named_agent_type, now, *owners),
        )
        if written.rowcount != 1:
            row = read_rows(conn, dialect, team_id, [task_id]).get(task_id)
            raise TaskCustodyConflict(task_id, row.holder_agent_id if row else "unknown")
    held = conn.execute(
        f"SELECT task_id FROM {CUSTODY_TABLE} WHERE team_id = {bind} AND holder_agent_id IN ({owner_binds})",
        (team_id, *owners),
    ).fetchall()
    for task_id in sorted({row[0] for row in held} - set(adopted)):
        conn.execute(
            f"DELETE FROM {CUSTODY_TABLE} WHERE team_id = {bind} AND task_id = {bind} "
            f"AND holder_agent_id IN ({owner_binds})",
            (team_id, task_id, *owners),
        )


__all__ = [
    "CUSTODY_TABLE",
    "TaskCustodyConflict",
    "TaskCustodyFinished",
    "TaskCustodyReleased",
    "TaskCustodyRow",
    "TaskCustodyTakenOver",
    "TaskCustodyTakeover",
    "claim_entering",
    "finished_by_team",
    "ensure_schema",
    "executed_task_ids",
    "read_rows",
    "release_leaving",
    "release_owners",
    "taken_over",
    "transfer_on_recovery",
]
