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
It writes ``InstanceLayout.protocol1_ids_check_path`` only when every id fits and the
instance can run as it is (onboarded, in the current folder layout), so the stores it read
are the ones the instance runs with; until then it runs again at each start. It runs once
rather than at every start so that an agent type added later never stops the instance: the
agent host gives such a type no session and the Status page says why
(``agent_type_not_certifiable``).

A record file of any kind means this version has started here, so its stores may already be
changed and going back to 0.9.x is no longer the way out (``_RecordState``):

- a record for the current team id: nothing is checked again, except that a startup agent
  type that does not fit, which the configuration refuses, stops the start with a short
  message naming only those entries (``startup_types_refusal``). The store is not read;
- a record for another team id, or one that cannot be read: the whole check runs again, and
  its refusal names no 0.9.x backup;
- no record: the whole check runs, and its refusal names the 0.9.x backup to put back when
  the file was already upgraded (``upgraded_file_note``).

The check runs where the launchers start an instance. ``assistant_cli.py migrate-config``
and the command-line tool's offer to upgrade the file do not run it; when such an upgrade,
or the launcher's own on an earlier start of an instance that was not ready yet, came
first, the refusal names that backup.

What it reads, read only (``storage.read_only``: SQLite opened ``mode=ro``, PostgreSQL in
a read-only transaction), never creating a database or a table (SQLite opens its own empty
``-wal`` and ``-shm`` files beside a database in WAL mode, as for any reader):

- the team id (``gateway.team_id``);
- the agent catalog's types as the instance's processes load it (``instance_agent_catalog``),
  with the file each is defined in, and the types ``gateway.startup_agents`` names, read
  from the file itself since the configuration refuses one that does not fit
  (``load_saved_for_check``);
- agent ids over 32 characters in the team's records that phase 3 converts (13C.9 item 3),
  each with what removes them on 0.9.x:

  - ``active_task_runtime`` and ``active_task_runtime_records``: an agent's running tasks
    and their saved progress; gone when its tasks finish or are stopped, or when another
    agent takes its work over;
  - ``agent_deferred_messages``: messages waiting for an agent; gone when it takes them;
  - ``runtime_task_custody``: the tasks an agent holds; released when each ends;
  - ``worker_task_handbacks`` with status ``received``: tasks a worker handed back that the
    team lead has not dealt with yet. A settled handback stays as history, which nothing on
    0.9.x removes, so settled ones are not checked (the lead's ruling on C09); the step that
    converts handbacks to protocol 1 records handles settled rows with longer ids itself.

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
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Final, Iterable, Sequence

from custom_messages.wire.ids import AGENT_ID, AGENT_TYPE, TEAM_ID
from runtime_settings.instance_layout import CONTROL_DIRNAME, TEAM_COMPOSITION_FILENAME
from storage.config import StorageSettings
from storage.sql_dialect import POSTGRES, SQLITE, SqlDialect
from storage.worker_handback_repository import HANDBACK_STATUS_RECEIVED

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig

LOGGER = logging.getLogger(__name__)

REFUSED_EVENT: Final = "protocol1_ids_over_caps"
# A startup agent type that does not fit, once the check passed here (``startup_types_refusal``).
STARTUP_TYPES_REFUSED_EVENT: Final = "protocol1_startup_agent_types_over_cap"
# The check could not read the settings, the catalog or a store; the start stops (both launchers).
CHECK_FAILED_EVENT: Final = "protocol1_ids_check_failed"
# A team export whose ids do not fit, refused before a fresh import writes anything
# (``team_transfer.protocol1_ids``; ``import_refusal_message``).
IMPORT_REFUSED_EVENT: Final = "team_import_ids_over_caps"
# What renaming an agent type that does not fit takes, on an instance that runs 0.9.x.
_RENAME_AGENT_TYPE: Final = (
    f"rename it to at most {AGENT_TYPE.cap} letters, digits, '-' and '_' in the file that defines it and everywhere "
    "the instance names it: gateway.startup_agents in the configuration file and the team roster "
    f"({CONTROL_DIRNAME}/{TEAM_COMPOSITION_FILENAME}). For a specialist the team lead created, remove it and have it "
    "created again under the shorter name."
)
# The newest instance_config.yaml schema version 0.9.x reads (0.9.13): a newer saved file was
# upgraded for this version before the check ran, and 0.9.x refuses it.
LAST_09_CONFIG_SCHEMA_VERSION: Final = 12
# How many over-long ids each table names at most, so the message stays readable.
_IDS_PER_TABLE: Final = 20


class Protocol1IdCheckFailed(RuntimeError):
    """The check could not read the agent catalog or a store that exists; the start stops."""


# How a launcher finds the team's store, or None when it does not exist yet. Called only when
# the whole check runs, so a start the record lets through never reaches the store.
StoreResolver = Callable[[], StorageSettings | None]


class _RecordState(Enum):
    """What the check's record (``InstanceLayout.protocol1_ids_check_path``) says.

    The check writes a record only after it passed on an instance that could run, so any
    record file means this version has started here. Only ``PASSED`` lets a start skip it.
    """

    MISSING = "missing"
    PASSED = "passed"  # for the current team id
    OTHER_TEAM = "other_team"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class _Record:
    state: _RecordState
    detail: str = ""  # the recorded team id, or why the record cannot be read


@dataclass(frozen=True)
class IdOverCap:
    """One id protocol 1 cannot carry, and where it was found."""

    rule_what: str
    value: str
    problem: str
    # Agent types: where it is defined and started, as phrases; stored agent ids: the records that hold it.
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


def agent_types_over_cap(
    definitions: Iterable[tuple[str, str]], startup_types: Iterable[str] = (), config_path: Path | None = None
) -> list[IdOverCap]:
    """Agent types protocol 1 cannot carry, each once with where it is defined and started.

    ``definitions`` are the catalog's ``(type, defining file)`` pairs; ``startup_types`` the
    types ``gateway.startup_agents`` in ``config_path`` names that do not fit
    (``startup_types_over_cap``).
    """

    places: dict[str, list[str]] = {}
    for agent_type, source in sorted(definitions):
        if not AGENT_TYPE.fits(agent_type):
            places.setdefault(agent_type, []).extend([f"defined in {source}"] if source else [])
    for agent_type in sorted(set(startup_types)):
        places.setdefault(agent_type, []).append(f"started by gateway.startup_agents in {config_path}")
    return [
        IdOverCap(AGENT_TYPE.what, agent_type, AGENT_TYPE.problem(agent_type) or "", tuple(found_in))
        for agent_type, found_in in sorted(places.items())
    ]


def _startup_entries(settings: dict[str, Any]) -> list[Any]:
    gateway = settings.get("gateway")
    entries = gateway.get("startup_agents") if isinstance(gateway, dict) else None
    return entries if isinstance(entries, list) else []


def _unfit_startup_type(entry: Any) -> str | None:
    """The entry's agent type when protocol 1 cannot carry it; None otherwise.

    An empty or malformed entry is left to the configuration, which refuses it with its own reason.
    """

    agent_type = str(entry.get("named_agent_type") or "").strip() if isinstance(entry, dict) else ""
    return agent_type if agent_type and not AGENT_TYPE.fits(agent_type) else None


def _without_unfit_startup_agents(settings: dict[str, Any]) -> dict[str, Any]:
    entries = _startup_entries(settings)
    kept = [entry for entry in entries if _unfit_startup_type(entry) is None]
    if len(kept) == len(entries):
        return settings
    return {**settings, "gateway": {**settings["gateway"], "startup_agents": kept}}


def load_saved_for_check(config_path: Path) -> GatewayInstanceConfig:
    """The saved configuration as the check reads it: upgraded in memory, never saved.

    The configuration refuses a startup agent type protocol 1 cannot carry
    (``StartupAgentSettings``), so such entries are set aside before validation and the check
    can still read the team id, the catalog and the stores and name those types with every
    other id (``startup_types_over_cap`` reads them from the file). The check then refuses,
    so a configuration loaded here never starts anything without them. Raises ``ValueError``
    for any other settings the configuration refuses.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    return GatewayInstanceConfig.load_saved(config_path, prepare=_without_unfit_startup_agents)


def startup_types_over_cap(config: GatewayInstanceConfig) -> list[str]:
    """The types ``gateway.startup_agents`` names in the saved file that protocol 1 cannot carry.

    Read from the file as this version upgrades it, without validation
    (``read_upgraded_instance_config``), since the configuration refuses them. A
    configuration that was never saved has no such entries: its own were validated.
    """

    from assistant_gateway.config import read_upgraded_instance_config

    if not config.config_path.is_file():
        return []
    settings = read_upgraded_instance_config(config.config_path)
    return sorted({agent_type for entry in _startup_entries(settings) if (agent_type := _unfit_startup_type(entry))})


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
            f"The stored records could not be read ({_store_name(storage)}): {_store_error(storage, exc)}"
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


def _store_error(storage: StorageSettings | None, exc: Exception) -> str:
    """What the refusal says about a store's error: a PostgreSQL error only by its kind.

    libpq repeats the part of a connection string it cannot parse, which can be the
    password (SEC-1 of pull request 152's review), and the launchers log the refusal. A
    SQLite error names only what went wrong with the file.
    """

    if storage is not None and storage.postgres_dsn:
        return (
            f"{type(exc).__name__}. Its text is left out, since it can repeat the connection string's password: check "
            "the connection string and that PostgreSQL answers."
        )
    return str(exc) or type(exc).__name__


def _store_name(storage: StorageSettings | None) -> str:
    if storage is not None and storage.postgres_dsn:
        from storage.postgres_connection import redact_postgres_dsn

        return redact_postgres_dsn(storage.postgres_dsn)
    return str(storage.sqlite_path if storage is not None else "")


def upgraded_file_note(config_path: Path) -> str | None:
    """What to put back before starting 0.9.x when the saved file was upgraded before the check refused.

    ``assistant_cli.py migrate-config`` and the command-line tool's offer to upgrade the file
    run no id check, and the launcher upgrades it after a check that passed on an instance
    that was not ready yet (``_not_ready_to_record``); 0.9.x refuses a file newer than
    ``LAST_09_CONFIG_SCHEMA_VERSION``. Named only when a backup 0.9.x reads is beside the
    file: a file this version created has none, and never ran on 0.9.x; nor has a
    configuration that was never saved. The caller asks only when no record exists: an
    instance this version has started on goes forward, never back to 0.9.x.
    """

    from assistant_gateway.config import inspect_instance_config_version

    if not config_path.is_file():
        return None
    version = inspect_instance_config_version(config_path).detected_version
    if version <= LAST_09_CONFIG_SCHEMA_VERSION:
        return None
    backups = sorted(
        (int(number), path)
        for path in config_path.parent.glob(f"{config_path.name}.bak.v*")
        if (number := path.name.rpartition(".bak.v")[2]).isdigit() and int(number) <= LAST_09_CONFIG_SCHEMA_VERSION
    )
    if not backups:
        return None
    return (
        f"The configuration file {config_path} was already upgraded to schema_version {version} for this "
        f"version, which 0.9.x cannot read. Before starting 0.9.x again, put back {backups[-1][1]} as "
        f"{config_path.name}."
    )


def refusal_message(found: Sequence[IdOverCap], *, upgraded_note: str | None = None) -> str:
    """What stopped the start, one line per id, and what to do about each kind."""

    lines = [
        "This instance cannot start this version yet: protocol 1 carries these ids in message addresses, and "
        f"they do not fit (event={REFUSED_EVENT}). This start changed nothing: the configuration file, the "
        "stored records and the message server are as they were."
    ]
    if upgraded_note:
        lines.append(upgraded_note)
    for item in found:
        if item.rule_what == TEAM_ID.what:
            lines.append(
                f"- The team id {item.value!r} {item.problem}. This instance cannot run 0.10 with that team id: "
                "keep running 0.9.x."
            )
        elif item.rule_what == AGENT_TYPE.what:
            lines.append(
                f"- The agent type {item.value!r}{_where(item)} {item.problem}. On 0.9.x, {_RENAME_AGENT_TYPE} Then "
                "start this version again."
            )
        else:
            lines.append(
                f"- The agent id {item.value!r} {item.problem}. It is stored with {', '.join(item.found_in)}. "
                "On 0.9.x, let that agent's tasks finish or stop them, and let the team lead deal with tasks it "
                "handed back, so these records go. Then start this version again."
            )
    return "\n".join(lines)


def import_refusal_message(found: Sequence[IdOverCap]) -> str:
    """Why a team export cannot be imported into this version, one sentence per id; nothing was written.

    The import keeps the export's team id and agent types (``team_transfer.protocol1_ids``),
    so the advice is for the instance the export came from, which runs 0.9.x. Lines carry no
    list marks: onboarding's Import page shows the message as one paragraph.
    """

    lines = [
        "This export's team cannot be imported into this version: protocol 1 carries these ids in message addresses, "
        f"and they do not fit (event={IMPORT_REFUSED_EVENT}). Nothing was written."
    ]
    for item in found:
        if item.rule_what == TEAM_ID.what:
            lines.append(
                f"The team id {item.value!r} {item.problem}. A team keeps its id when it moves, and this version has "
                "no way yet to give a team a shorter one, so this export cannot be imported into it."
            )
        else:
            lines.append(
                f"The agent type {item.value!r}{_where(item)} {item.problem}. On the instance this export came from, "
                f"{_RENAME_AGENT_TYPE} Then export the team again and import the new export."
            )
    return "\n".join(lines)


def _where(item: IdOverCap) -> str:
    return f" ({'; '.join(item.found_in)})" if item.found_in else ""


def startup_types_refusal(startup_types: Sequence[str], config_path: Path) -> str:
    """The refusal for startup agent types that do not fit on an instance whose check passed.

    This version has started here, so the advice is to change the file and start again. Only
    those entries are named: nothing else the check reads stops a later start.
    """

    lines = [
        f"This instance cannot start: gateway.startup_agents in {config_path} starts agent types that protocol 1 "
        f"cannot carry in message addresses (event={STARTUP_TYPES_REFUSED_EVENT}). This start changed nothing."
    ]
    for agent_type in startup_types:
        lines.append(
            f"- The agent type {agent_type!r} {AGENT_TYPE.problem(agent_type)}. Remove its entry from "
            f"gateway.startup_agents, or change it to an agent type of at most {AGENT_TYPE.cap} letters, digits, "
            "'-' and '_' (a type you rename, rename where it is defined as well). Then start again."
        )
    return "\n".join(lines)


def check_due(config: GatewayInstanceConfig) -> bool:
    """Whether the check has anything to say: no record for the current team id, or a startup type that does not fit.

    The launchers ask this first, so a start the record lets through loads nothing more.
    ``config`` comes from ``load_saved_for_check``. Logs nothing: ``check_ids_before_upgrade``
    says why the record did not count.
    """

    return bool(startup_types_over_cap(config)) or _read_record(config).state is not _RecordState.PASSED


def check_ids_before_upgrade(config: GatewayInstanceConfig, find_store: StoreResolver) -> str | None:
    """None when the ids fit or the check already passed; else the refusal message.

    ``config`` is the saved instance configuration as this version reads it, upgraded in memory
    only (``load_saved_for_check``). ``find_store`` gives the team's store
    (``resolve_manager_storage_settings``), or None when it does not exist yet; it is called
    only when the whole check runs. When the ids fit, the record is written if the instance
    can run as it is (``_not_ready_to_record``). Raises ``Protocol1IdCheckFailed`` when the
    catalog or an existing store cannot be read, ``ValueError`` when the saved file cannot be
    read, and ``TransferError`` for a team whose import did not finish, which nothing may read
    (``team_transfer.runtime_guard``).
    """

    from team_transfer.runtime_guard import require_finished_import

    require_finished_import(config.layout)
    team_id = str(config.manager.team_id)
    startup_types = startup_types_over_cap(config)
    record = _read_record(config)
    if record.state is _RecordState.PASSED:
        if not startup_types:
            return None
        LOGGER.error(
            "Startup agent types protocol 1 cannot carry stop the start. event=%s team_id=%s agent_types=%s",
            STARTUP_TYPES_REFUSED_EVENT,
            team_id,
            ",".join(startup_types),
        )
        return startup_types_refusal(startup_types, config.config_path)
    _log_record_not_counted(record, Path(config.layout.protocol1_ids_check_path), team_id)
    catalog = catalog_definitions(config.instance_root_path, config.agent.agent_definition_paths)
    found = [
        *team_id_over_cap(team_id),
        *agent_types_over_cap(catalog, startup_types, config.config_path),
        *stored_agent_ids_over_cap(find_store(), team_id),
    ]
    if found:
        LOGGER.error(
            "Ids protocol 1 cannot carry stop the start. event=%s team_id=%s ids=%s",
            REFUSED_EVENT,
            team_id,
            ",".join(f"{item.rule_what}:{item.value}" for item in found),
        )
        # Any record means this version started here: its stores go forward, never back to 0.9.x.
        first_start = record.state is _RecordState.MISSING
        return refusal_message(found, upgraded_note=upgraded_file_note(config.config_path) if first_start else None)
    not_ready = _not_ready_to_record(config)
    if not_ready:
        LOGGER.info(
            "Every id fits protocol 1, but %s, so the check runs again at the next start. "
            "event=protocol1_ids_checked_not_recorded team_id=%s",
            not_ready,
            team_id,
        )
        return None
    _write_record(Path(config.layout.protocol1_ids_check_path), team_id)
    LOGGER.info("Every id fits protocol 1. event=protocol1_ids_checked team_id=%s", team_id)
    return None


def _read_record(config: GatewayInstanceConfig) -> _Record:
    """What the check's record says about the current team id; logs nothing."""

    path = Path(config.layout.protocol1_ids_check_path)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _Record(_RecordState.MISSING)
    except (OSError, ValueError) as exc:
        return _Record(_RecordState.UNREADABLE, str(exc))
    if not isinstance(body, dict):
        return _Record(_RecordState.UNREADABLE, "it holds no team id")
    recorded = body.get("team_id")
    if recorded != str(config.manager.team_id):
        return _Record(_RecordState.OTHER_TEAM, str(recorded))
    return _Record(_RecordState.PASSED)


def _log_record_not_counted(record: _Record, path: Path, team_id: str) -> None:
    """Why a record that exists did not let the start skip the check: once per start."""

    if record.state is _RecordState.UNREADABLE:
        LOGGER.warning(
            "The id check's record cannot be read, so the check runs again. "
            "event=protocol1_ids_record_unreadable path=%s error=%s",
            path,
            record.detail,
        )
    elif record.state is _RecordState.OTHER_TEAM:
        LOGGER.info(
            "The id check's record is for another team id, so the check runs again. "
            "event=protocol1_ids_record_other_team path=%s recorded_team_id=%s team_id=%s",
            path,
            record.detail,
            team_id,
        )


def _not_ready_to_record(config: GatewayInstanceConfig) -> str | None:
    """Why the instance cannot run as it is yet, or None.

    The launcher refuses such an instance after the check (``validate_runtime_layout``), and
    what the operator does next can change what the check reads: onboarding writes the team
    id, and the layout's moves bring the task database to where the check looks for it.
    """

    from runtime_settings.legacy_layout import legacy_layout_moves

    if not config.layout.control_dir.is_dir():
        return "the instance is not onboarded yet"
    if legacy_layout_moves(config.instance_root_path):
        return "the instance's files are still in the old layout"
    return None


def _write_record(path: Path, team_id: str) -> None:
    from custom_agents.governance.authorized_operations import atomic_write_bytes
    from release_version import release_version

    body = {"checked_at": datetime.now(timezone.utc).isoformat(), "version": release_version(), "team_id": team_id}
    atomic_write_bytes(path, (json.dumps(body, indent=2) + "\n").encode("utf-8"))


__all__ = [
    "CHECK_FAILED_EVENT",
    "IMPORT_REFUSED_EVENT",
    "IdOverCap",
    "LAST_09_CONFIG_SCHEMA_VERSION",
    "Protocol1IdCheckFailed",
    "REFUSED_EVENT",
    "STARTUP_TYPES_REFUSED_EVENT",
    "StoreResolver",
    "agent_types_over_cap",
    "catalog_definitions",
    "check_due",
    "check_ids_before_upgrade",
    "import_refusal_message",
    "load_saved_for_check",
    "refusal_message",
    "startup_types_over_cap",
    "startup_types_refusal",
    "stored_agent_ids_over_cap",
    "team_id_over_cap",
    "upgraded_file_note",
]
