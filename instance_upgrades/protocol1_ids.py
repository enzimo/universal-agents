"""The id check at the first start of 0.10: every id protocol 1 carries must fit its cap.

Why: protocol 1 carries the team id, each agent's type and each agent's id in NATS
subjects, consumer names, record keys and host credentials, with caps of 24, 24 and 32
characters (plan decisions 56 and 98; ``custom_messages.wire.ids``). Phase 3 converts the
stored records that hold agent ids into protocol 1's forms and moves queued work into the
partition streams, so an id over its cap would fail part-way through an upgrade. This
check runs before any of that (plan 13C.9, step 1) and stops the start with what to do,
while nothing has changed yet.

When: once, at the first start of a release with protocol 1 streams, before the
configuration file is upgraded, before any first-start step, the stream set-up or any
process start. The headless launcher (``docker.launch_headless.main``) runs it first; the
Docker bundle (``docker.run_bundle``) runs it as soon as its PostgreSQL answers, since
stored ids may be there, and before it upgrades the file or starts NATS or the launcher.
It writes ``InstanceLayout.protocol1_ids_check_path`` only when every id fits and does
nothing while that record exists. It runs once rather than at every start so that an
agent type added later never stops the instance: the agent host gives such a type no
session and the Status page says why (``agent_type_not_certifiable``).

What it reads, read only (``storage.read_only``: SQLite opened ``mode=ro``, PostgreSQL in
a read-only transaction), never creating a database or a table (SQLite opens its own empty
``-wal`` and ``-shm`` files beside a database in WAL mode, as for any reader):

- the team id (``gateway.team_id``);
- the agent catalog's types as the instance's processes load it (``instance_agent_catalog``),
  with the file each is defined in;
- agent ids over 32 characters in the team's records that phase 3 converts (13C.9 item 3),
  each with what removes them on 0.9.x:

  - ``active_task_runtime`` and ``active_task_runtime_records``: an agent's running tasks
    and their saved progress; gone when its tasks finish or are stopped, or when another
    agent takes its work over;
  - ``agent_deferred_messages``: messages waiting for an agent; gone when it takes them;
  - ``runtime_task_custody``: the tasks an agent holds; released when each ends;
  - ``worker_task_handbacks`` with status ``received``: tasks a worker handed back that the
    team lead has not dealt with yet. A settled handback stays as history, which nothing on
    0.9.x removes and the 0.9 code reads only by task, so settled ones are not checked.

  Rows of another team in a shared database are left out; rows without a team id are
  checked. A store, file or table that does not exist yet holds none. Only ids over the
  cap are selected, at most ``_IDS_PER_TABLE`` per table. Stored agent ids are checked for
  length only, as the plan asks; their characters come from the runtime's own id
  generator (``id_generation.new_id``).

A store or catalog that exists but cannot be read stops the start (fail closed): the later
upgrade steps read the same records.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Sequence

from custom_messages.wire.ids import AGENT_ID, AGENT_TYPE, TEAM_ID
from storage.config import StorageSettings
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.worker_handback_repository import HANDBACK_STATUS_RECEIVED

LOGGER = logging.getLogger(__name__)

REFUSED_EVENT: Final = "protocol1_ids_over_caps"
# How many over-long ids each table names at most, so the message stays readable.
_IDS_PER_TABLE: Final = 20


class Protocol1IdCheckFailed(RuntimeError):
    """The check could not read the agent catalog or a store that exists; the start stops."""


@dataclass(frozen=True)
class IdOverCap:
    """One id protocol 1 cannot carry, and where it was found."""

    rule_what: str
    value: str
    problem: str
    # Agent types: the file that defines it; stored agent ids: the records that hold it.
    found_in: tuple[str, ...] = ()


@dataclass(frozen=True)
class _StoredIds:
    """A column of agent ids phase 3 converts, and what its rows are in words."""

    table: str
    column: str
    holds: str
    condition: str  # SQL with ``{p}`` binds; its parameters are the team id


_OF_THIS_TEAM: Final = "(team_id = {p} OR team_id IS NULL)"
_STORED: Final[tuple[_StoredIds, ...]] = (
    _StoredIds("active_task_runtime", "owner_agent_id", "its running tasks", _OF_THIS_TEAM),
    _StoredIds(
        "active_task_runtime_records",
        "owner_agent_id",
        "its running tasks' saved progress",
        # No team column: rows not owned by another team's snapshot.
        "NOT EXISTS (SELECT 1 FROM active_task_runtime s WHERE s.owner_agent_id = "
        "active_task_runtime_records.owner_agent_id AND s.team_id IS NOT NULL AND s.team_id <> {p})",
    ),
    _StoredIds("agent_deferred_messages", "owner_agent_id", "messages waiting for it", _OF_THIS_TEAM),
    _StoredIds("runtime_task_custody", "holder_agent_id", "the tasks it holds", _OF_THIS_TEAM),
    _StoredIds(
        "worker_task_handbacks",
        "worker_agent_id",
        "tasks it handed back that the team lead has not dealt with yet",
        _OF_THIS_TEAM + f" AND status = '{HANDBACK_STATUS_RECEIVED}'",
    ),
)


def team_id_over_cap(team_id: str) -> list[IdOverCap]:
    problem = TEAM_ID.problem(team_id)
    return [] if problem is None else [IdOverCap(TEAM_ID.what, team_id, problem)]


def agent_types_over_cap(definitions: Iterable[tuple[str, str]]) -> list[IdOverCap]:
    """The catalog's types protocol 1 cannot carry, from ``(type, defining file)`` pairs."""

    found: list[IdOverCap] = []
    for agent_type, source in sorted(definitions):
        problem = AGENT_TYPE.problem(agent_type)
        if problem is not None:
            found.append(IdOverCap(AGENT_TYPE.what, agent_type, problem, (source,) if source else ()))
    return found


def catalog_definitions(instance_root: Path, agent_definition_paths: Sequence[str]) -> list[tuple[str, str]]:
    """Each agent type of the instance's catalog and the file that defines it."""

    from custom_agents.agent_catalog import instance_agent_catalog

    try:
        catalog = instance_agent_catalog(str(instance_root), agent_definition_paths)
    except Exception as exc:  # noqa: BLE001 - fail closed with the catalog's own message
        raise Protocol1IdCheckFailed(f"The agent catalog could not be read: {exc}") from exc
    return [(agent_type, definition.source_path) for agent_type, definition in catalog.agent_types.items()]


def stored_agent_ids_over_cap(storage: StorageSettings | None, team_id: str) -> list[IdOverCap]:
    """Agent ids over the cap in the team's records that phase 3 converts; reads only, creates nothing."""

    connect = _read_only_connection(storage)
    if connect is None:
        return []
    dialect = POSTGRES if storage is not None and storage.postgres_dsn else SQLITE
    holders: dict[str, list[str]] = {}
    from storage.read_only import read_only_repositories

    try:
        with read_only_repositories(), connect() as conn:
            for stored in _STORED:
                if not _table_exists(conn, dialect, stored.table):
                    continue
                for value in _over_cap(conn, dialect, stored, team_id):
                    holders.setdefault(value, []).append(stored.holds)
    except Exception as exc:  # noqa: BLE001 - fail closed: the later upgrade steps read the same records
        raise Protocol1IdCheckFailed(
            f"The stored records could not be read ({_store_name(storage)}): {str(exc) or type(exc).__name__}"
        ) from exc
    return [
        IdOverCap(AGENT_ID.what, value, AGENT_ID.problem(value) or "", tuple(kinds))
        for value, kinds in sorted(holders.items())
    ]


def _read_only_connection(storage: StorageSettings | None) -> Callable[[], Any] | None:
    """How to open the store, or None when it does not exist yet (nothing stored)."""

    if storage is None:
        return None
    if storage.postgres_dsn:
        from storage.postgres_connection import connect_postgres

        dsn = storage.postgres_dsn
        return lambda: connect_postgres(dsn)
    path = storage.sqlite_path
    if not path or not Path(path).is_file():
        return None
    from storage.sqlite_connection import connect_sqlite

    return lambda: connect_sqlite(str(path))


def _table_exists(conn: Any, dialect: SqlDialect, table: str) -> bool:
    if dialect is POSTGRES:
        row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
        return bool(row and row[0])
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _over_cap(conn: Any, dialect: SqlDialect, stored: _StoredIds, team_id: str) -> list[str]:
    condition = stored.condition.format(p=dialect.bind)
    rows = conn.execute(
        f"SELECT DISTINCT {stored.column} FROM {stored.table} "
        f"WHERE length({stored.column}) > {AGENT_ID.cap} AND {condition} "
        f"ORDER BY {stored.column} LIMIT {_IDS_PER_TABLE}",
        (team_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _store_name(storage: StorageSettings | None) -> str:
    if storage is not None and storage.postgres_dsn:
        from storage.postgres_connection import redact_postgres_dsn

        return redact_postgres_dsn(storage.postgres_dsn)
    return str(storage.sqlite_path if storage is not None else "")


def refusal_message(found: Sequence[IdOverCap]) -> str:
    """What stopped the start, one line per id, and what to do about each kind."""

    lines = [
        "This instance cannot start this version yet: protocol 1 carries these ids in message addresses, and "
        f"they do not fit (event={REFUSED_EVENT}). Nothing was changed: the configuration file, the stored "
        "records and the message server are as they were."
    ]
    for item in found:
        if item.rule_what == TEAM_ID.what:
            lines.append(
                f"- The team id {item.value!r} {item.problem}. This instance cannot run 0.10 with that team id: "
                "keep running 0.9.x."
            )
        elif item.rule_what == AGENT_TYPE.what:
            where = f" (defined in {item.found_in[0]})" if item.found_in else ""
            lines.append(
                f"- The agent type {item.value!r}{where} {item.problem}. On 0.9.x, rename it to at most "
                f"{AGENT_TYPE.cap} letters, digits, '-' and '_' in that file; for a specialist the team lead "
                "created, remove it and have it created again under the shorter name. Then start this version "
                "again."
            )
        else:
            lines.append(
                f"- The agent id {item.value!r} {item.problem}. It is stored with {', '.join(item.found_in)}. "
                "On 0.9.x, let that agent's tasks finish or stop them, and let the team lead deal with tasks it "
                "handed back, so these records go. Then start this version again."
            )
    return "\n".join(lines)


def check_ids_before_upgrade(config: Any, storage: StorageSettings | None) -> str | None:
    """None when the ids fit (the record is written) or the check already passed; else the refusal message.

    ``config`` is the saved instance configuration as this version reads it, upgraded in memory
    only (``GatewayInstanceConfig.load_saved``); ``storage`` is the team's store
    (``resolve_manager_storage_settings``), or None when it does not exist yet. Raises
    ``Protocol1IdCheckFailed`` when the catalog or an existing store cannot be read, and
    ``TransferError`` for a team whose import did not finish, which nothing may read
    (``team_transfer.runtime_guard``).
    """

    from team_transfer.runtime_guard import require_finished_import

    require_finished_import(config.layout)
    record = Path(config.layout.protocol1_ids_check_path)
    if record.exists():
        return None
    team_id = str(config.manager.team_id)
    found = [
        *team_id_over_cap(team_id),
        *agent_types_over_cap(catalog_definitions(config.instance_root_path, config.agent.agent_definition_paths)),
        *stored_agent_ids_over_cap(storage, team_id),
    ]
    if found:
        LOGGER.error(
            "Ids protocol 1 cannot carry stop the start. event=%s team_id=%s ids=%s",
            REFUSED_EVENT,
            team_id,
            ",".join(f"{item.rule_what}:{item.value}" for item in found),
        )
        return refusal_message(found)
    _write_record(record, team_id)
    LOGGER.info("Every id fits protocol 1. event=protocol1_ids_checked team_id=%s", team_id)
    return None


def _write_record(path: Path, team_id: str) -> None:
    from custom_agents.governance.authorized_operations import atomic_write_bytes
    from release_version import release_version

    body = {"checked_at": datetime.now(timezone.utc).isoformat(), "version": release_version(), "team_id": team_id}
    atomic_write_bytes(path, (json.dumps(body, indent=2) + "\n").encode("utf-8"))


__all__ = [
    "IdOverCap",
    "Protocol1IdCheckFailed",
    "REFUSED_EVENT",
    "agent_types_over_cap",
    "catalog_definitions",
    "check_ids_before_upgrade",
    "refusal_message",
    "stored_agent_ids_over_cap",
    "team_id_over_cap",
]
