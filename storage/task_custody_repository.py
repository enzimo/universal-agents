"""One holder per task across the team: ``runtime_task_custody``.

Why (2026-10-17): JetStream hands a delivered task out again when its
acknowledgement does not arrive within the wait, for example when the process
that took it stops between saving the task and acknowledging it, or NATS is
unreachable for longer than the wait. An agent checked only its own records
for duplicates, so a second worker of the type ran the copy while the first
holder, or the replacement that took over its saved work, ran the original.

The row is written in the transaction that saves the holder's runtime
snapshot (``RuntimeStateRepository.save_task_runtime``), so a row exists
exactly while some agent's saved work holds the task:

- a task entering an owner's snapshot claims its row; a row held by another
  agent refuses the save (``TaskCustodyConflict``) unless the caller names
  that holder as the one it takes over from (compare-and-set);
- a task leaving the snapshot (it ended, was handed back, or was dropped)
  releases the row, but only while this owner still holds it;
- recovery moves the rows of the adopted tasks to the replacement in the same
  transaction that moves the snapshot (``transfer_on_recovery``) and refuses a
  task another agent holds;
- clearing an owner's snapshot releases all its rows.

Only tasks an agent executes have rows. A requester's records of the tasks it
delegated (``subtask_to_parent_map``) share the worker's task id and never
claim it. Runtime-only state: the team transfer does not carry it, like the
snapshots themselves. Owners without a team id have no team-wide custody.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from storage.sql_dialect import SqlDialect

CUSTODY_TABLE = "runtime_task_custody"


class TaskCustodyConflict(RuntimeError):
    """Another agent holds this task; the save that would claim it was refused."""

    def __init__(self, task_id: str, holder_agent_id: str) -> None:
        super().__init__(f"Task {task_id} is held by agent {holder_agent_id}.")
        self.task_id = task_id
        self.holder_agent_id = holder_agent_id


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
        conn.execute(
            f"INSERT INTO {CUSTODY_TABLE} (team_id, task_id, holder_agent_id, holder_named_agent_type, claimed_at) "
            f"VALUES ({bind}, {bind}, {bind}, {bind}, {bind}) ON CONFLICT (team_id, task_id) DO NOTHING",
            (team_id, task_id, owner_agent_id, owner_named_agent_type, _now()),
        )
        row = read_rows(conn, dialect, team_id, [task_id]).get(task_id)
        if row is None or row.holder_agent_id == owner_agent_id:
            continue
        if takeover is None or takeover.task_id != task_id or takeover.from_holder_agent_id != row.holder_agent_id:
            raise TaskCustodyConflict(task_id, row.holder_agent_id)
        moved = conn.execute(
            f"UPDATE {CUSTODY_TABLE} SET holder_agent_id = {bind}, holder_named_agent_type = {bind}, claimed_at = {bind} "
            f"WHERE team_id = {bind} AND task_id = {bind} AND holder_agent_id = {bind}",
            (owner_agent_id, owner_named_agent_type, _now(), team_id, task_id, takeover.from_holder_agent_id),
        )
        if moved.rowcount != 1:
            raise TaskCustodyConflict(task_id, row.holder_agent_id)


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
    is neither a source nor the replacement: recovery must not adopt it (the
    caller filters such tasks before building the replacement snapshot, so
    this only fires when custody changed in between, for example a copy
    admitted concurrently on PostgreSQL).
    """

    bind = dialect.bind
    owners = sorted({owner for owner in source_owner_ids if owner} | {replacement_agent_id})
    owner_binds = _placeholders(dialect, len(owners))
    adopted = sorted(set(adopted_task_ids))
    now = _now()
    for task_id in adopted:
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
    "TaskCustodyRow",
    "TaskCustodyTakeover",
    "claim_entering",
    "ensure_schema",
    "executed_task_ids",
    "read_rows",
    "release_leaving",
    "release_owners",
    "transfer_on_recovery",
]
