"""Find and read the pinned memories of the instance ``memory export`` copies from.

The export exists to move memories off an instance that runs an earlier
version. That instance's ``instance_config.yaml`` or ``config.yaml`` usually
holds settings this version refuses (removed authorization, tool policy,
Sealbox and location settings, older schema versions), and its files may sit in
the old single-workspace layout. So the export never uses the loaders that
refuse those files (``GatewayInstanceConfig.load``, ``config_loader.load_config``).

Design notes, so a later reader can map the structure to its reasons:

- **The configuration is optional and only locates the database.** With
  ``--sqlite`` or ``--postgres-dsn-file`` no configuration is needed. When one
  is given, only the manager's storage settings (and the instance name, a label
  kept with each fact) are read; the YAML is read as plain data and every other
  key, known or not, is ignored.
- **The team comes from the stored rows.** Team facts carry their team id, so
  the export takes the one team found there. When the database holds team
  memory for several teams it stops and asks for ``--team-id``, because the
  import puts all team facts into one team. What is unknown (no team memory, no
  instance name) is recorded as null in the file.
- **Paths are resolved the way the earlier version resolved them.** In an
  ``instance_config.yaml`` the manager database is ``state.storage.sqlite.path``;
  a relative path (default ``dbs/task_history.db``) sits under
  ``agent.workspace_dir``, which defaulted to ``<instance_root>/workspace``. The
  current layout keeps it under the instance root instead
  (``runtime_settings.instance_layout``), so both places are checked. In a
  ``config.yaml`` the database sits under ``agent.workspace_dir`` (now
  ``agent.instance_root``), and the environment overrides the earlier version
  applied (``STORAGE_BACKEND``, ``TASK_HISTORY_DB_PATH``, ``SQLITE_DB_PATH``,
  ``AGENT_WORKSPACE_DIR``, ``POSTGRES_DSN``) still apply.
  Relative folders resolve against the configuration file's folder, not the
  shell's working folder, so the command gives the same answer wherever it runs.
- **The instance may have moved.** Onboarding wrote absolute paths under
  ``instance_root`` (for Docker, ``/data``). When the old data is mounted
  somewhere else (for example read-only at ``/old`` next to the new instance's
  ``/data``), paths under the recorded root are read under the folder that
  holds the configuration file instead, and that is logged.
- **Never a guess.** When no database, or more than one, is found, the export
  stops and lists the places it checked; ``--sqlite`` or
  ``--postgres-dsn-file`` then names the database directly.
- **The export only reads.** Opening a repository runs its schema setup (create
  tables, add columns), which would change the old instance's database and
  fails on a read-only mount. This module opens its own read-only connection
  instead: SQLite with ``mode=ro`` and ``PRAGMA query_only``, PostgreSQL in a
  read-only transaction. A SQLite database in a folder nobody can write, with no
  ``-wal`` file beside it (the instance is stopped), cannot be opened with
  ``mode=ro`` alone in WAL mode, so it is opened ``immutable``; that choice is
  logged. The pinned memory table is unchanged since 0.1.0, so the same columns
  are read from every version.
- **Removed people.** The current version records each person's standing
  (``access_control.people_directory``) and the export leaves out people removed
  from the team. Earlier versions had no removed standing and no such records,
  so everyone's memory is exported from them. Without a team id, a person
  removed from any team the records list is left out.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

import yaml

from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from default_values import STORAGE_DEFAULTS
from env_secrets import get_env_with_file_fallback
from runtime_settings.instance_layout import LEGACY_WORKSPACE_DIRNAME
from runtime_settings.values import deep_merge_dicts
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_POSTGRESQL_ALIAS,
    STORAGE_BACKEND_SQLITE,
)
from storage.pinned_memory_repository import COREMEM_BANK, PinnedMemoryFactRow, utc_now_iso
from storage.postgres_connection import connect_postgres

logger = logging.getLogger(__name__)

FACTS_TABLE = "pinned_memory_facts_active"
PEOPLE_TABLE = "access_people"
PEOPLE_SYNCED_TABLE = "access_people_synced"
DEFAULT_DATABASE_PATH = STORAGE_DEFAULTS.task_history_db_path.value
# The lead agent type whose per-type overrides a config.yaml applied to the
# manager store in earlier versions.
OLD_LEAD_AGENT_TYPE = TEAM_ARCHITECT_AGENT_TYPE
_FACT_COLUMNS = tuple(item.name for item in fields(PinnedMemoryFactRow))
_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
_SQLITE_BIND = "?"
_POSTGRES_BIND = "%s"
_NAME_THE_DATABASE = "Name the old database directly with --sqlite <path> or --postgres-dsn-file <path>."


@dataclass(frozen=True)
class ExportSource:
    """The old instance's database, and its name when known, as the export reads them."""

    instance_name: Optional[str]
    backend: str
    sqlite_path: Optional[Path] = None
    # Never printed or logged: a connection string can carry a password.
    postgres_dsn: Optional[str] = field(default=None, repr=False)

    def describe(self) -> str:
        if self.backend == STORAGE_BACKEND_SQLITE:
            return f"SQLite database {self.sqlite_path}"
        return "PostgreSQL database"


@dataclass(frozen=True)
class SourceOptions:
    """What the operator gave ``memory export`` to find the old instance's database."""

    config_path: Optional[str] = None
    sqlite_path: Optional[str] = None
    postgres_dsn_file: Optional[str] = None
    instance_name: Optional[str] = None


@dataclass(frozen=True)
class SourceMemories:
    """What the export read from the old database."""

    facts: list[PinnedMemoryFactRow]
    team_id: Optional[str]
    team_note: str  # how the team id was chosen, for the operator
    removed_people: list[str]


# -- reading the old configuration ---------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def read_config_document(config_path: str) -> tuple[Path, dict[str, Any]]:
    """The configuration file as plain data; no schema check, no migration."""

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"There is no configuration file at {path}.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path} is not readable YAML ({exc}). {_NAME_THE_DATABASE}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} does not hold a configuration mapping. {_NAME_THE_DATABASE}")
    return path, data


def _is_instance_config(data: Mapping[str, Any]) -> bool:
    # The same test every version used to tell the two file kinds apart
    # (assistant_gateway.runtime_config.is_instance_config_document).
    return {"instance_name", "instance_root"}.issubset(data)


def _normalized_backend(raw: Any) -> str:
    # An unknown name is refused only when the configuration's storage is used
    # (_database_from_config), so --sqlite still works with an odd file.
    backend = _text(raw).lower() or STORAGE_DEFAULTS.backend.value
    return STORAGE_BACKEND_POSTGRES if backend == STORAGE_BACKEND_POSTGRESQL_ALIAS else backend


@dataclass(frozen=True)
class _RootMove:
    """Where the old instance's recorded root now sits (the folder holding its configuration)."""

    recorded: Optional[Path]
    actual: Path

    def place(self, path: Path) -> Path:
        """A path the old configuration names, read under the root's current place."""

        if not path.is_absolute():
            return (self.actual / path).resolve()
        if self.recorded is None or self.recorded == self.actual:
            return path.resolve()
        try:
            relative = path.relative_to(self.recorded)
        except ValueError:
            return path.resolve()
        return (self.actual / relative).resolve()


@dataclass(frozen=True)
class _StorageFound:
    """The storage settings found in the configuration, with the places to look for SQLite."""

    backend: str
    sqlite_candidates: tuple[Path, ...]
    configured_dsn: str = field(default="", repr=False)
    # config.yaml: POSTGRES_DSN overrode the file. instance_config.yaml: the
    # file's value won and POSTGRES_DSN filled in only when it was empty.
    environment_dsn_wins: bool = False

    def postgres_dsn(self, env: Mapping[str, str]) -> Optional[str]:
        """Read only for PostgreSQL, so a stray POSTGRES_DSN_FILE cannot break a SQLite export."""

        if self.configured_dsn and not self.environment_dsn_wins:
            return self.configured_dsn
        return get_env_with_file_fallback("POSTGRES_DSN", env=env) or self.configured_dsn or None


def _sqlite_candidates(configured: str, bases: tuple[Path, ...], root: _RootMove) -> tuple[Path, ...]:
    target = Path(configured or DEFAULT_DATABASE_PATH).expanduser()
    if target.is_absolute():
        return (root.place(target),)
    return tuple(dict.fromkeys((base / target).resolve() for base in bases))


def _instance_config_storage(data: Mapping[str, Any], root: _RootMove) -> _StorageFound:
    """``state.storage`` of an ``instance_config.yaml``; only ``POSTGRES_DSN`` came from the environment."""

    storage = _mapping(_mapping(data.get("state")).get("storage"))
    workspace = _text(_mapping(data.get("agent")).get("workspace_dir"))
    # Earlier versions: under agent.workspace_dir (default <root>/workspace).
    # Current layout: under the instance root.
    old_base = root.place(Path(workspace).expanduser()) if workspace else root.actual / LEGACY_WORKSPACE_DIRNAME
    return _StorageFound(
        backend=_normalized_backend(storage.get("backend")),
        sqlite_candidates=_sqlite_candidates(
            _text(_mapping(storage.get(STORAGE_BACKEND_SQLITE)).get("path")), (old_base, root.actual), root
        ),
        configured_dsn=_text(_mapping(storage.get(STORAGE_BACKEND_POSTGRES)).get("dsn")),
    )


def _config_yaml_sections(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``agent`` and ``storage`` with the old lead agent type's overrides applied."""

    overrides = _mapping(_mapping(data.get("agent_types")).get(OLD_LEAD_AGENT_TYPE))
    agent = deep_merge_dicts(_mapping(data.get("agent")), _mapping(overrides.get("agent")))
    storage = deep_merge_dicts(_mapping(data.get("storage")), _mapping(overrides.get("storage")))
    return agent, storage


def _config_yaml_storage(data: Mapping[str, Any], root: _RootMove, env: Mapping[str, str]) -> _StorageFound:
    """``storage`` of a ``config.yaml``, with the environment overrides earlier versions applied."""

    agent, storage = _config_yaml_sections(data)
    folder = _text(env.get("AGENT_WORKSPACE_DIR")) or _text(agent.get("workspace_dir")) or _text(agent.get("instance_root"))
    base = root.place(Path(folder).expanduser()) if folder else root.actual
    configured = (
        _text(env.get("SQLITE_DB_PATH"))
        or _text(env.get("TASK_HISTORY_DB_PATH"))
        or _text(_mapping(storage.get(STORAGE_BACKEND_SQLITE)).get("path"))
    )
    return _StorageFound(
        backend=_normalized_backend(_text(env.get("STORAGE_BACKEND")) or storage.get("backend")),
        sqlite_candidates=_sqlite_candidates(configured, (base,), root),
        configured_dsn=_text(_mapping(storage.get(STORAGE_BACKEND_POSTGRES)).get("dsn")),
        environment_dsn_wins=True,
    )


@dataclass(frozen=True)
class _ConfigFacts:
    instance_name: Optional[str]
    storage: _StorageFound


def _root_move(path: Path, data: Mapping[str, Any]) -> _RootMove:
    recorded_text = _text(data.get("instance_root"))
    recorded = Path(recorded_text).expanduser() if recorded_text else None
    move = _RootMove(recorded if recorded is not None and recorded.is_absolute() else None, path.parent)
    if move.recorded is not None and move.recorded != move.actual:
        logger.info(
            "The old instance's files are read where its configuration now sits. "
            "event=pinned_memory_export_root_moved recorded_root=%s actual_root=%s",
            move.recorded,
            move.actual,
        )
    return move


def read_old_config(config_path: str, env: Mapping[str, str]) -> _ConfigFacts:
    """Storage settings (and instance name) of the old instance's configuration."""

    path, data = read_config_document(config_path)
    if _is_instance_config(data):
        return _ConfigFacts(
            instance_name=_text(data.get("instance_name")) or None,
            storage=_instance_config_storage(data, _root_move(path, data)),
        )
    # A config.yaml development root has no instance name; its folder names it.
    return _ConfigFacts(
        instance_name=path.parent.name,
        storage=_config_yaml_storage(data, _RootMove(None, path.parent), env),
    )


# -- choosing the database -----------------------------------------------------------


def _existing_sqlite(candidates: tuple[Path, ...]) -> Path:
    found = [path for path in candidates if path.is_file()]
    if len(found) == 1:
        return found[0]
    looked = "\n".join(f"  {path}" for path in candidates)
    if not found:
        raise SystemExit(
            f"There is no memory database where the old configuration says. Looked at:\n{looked}\n{_NAME_THE_DATABASE}"
        )
    raise SystemExit(
        f"Found a memory database in more than one place:\n{looked}\n"
        "Name the one the old instance uses with --sqlite <path>."
    )


def _read_dsn_file(path_text: str) -> str:
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise SystemExit(f"There is no PostgreSQL connection file at {path}.")
    dsn = path.read_text(encoding="utf-8").strip()
    if not dsn:
        raise SystemExit(f"{path} is empty; it must hold the old instance's PostgreSQL connection string.")
    return dsn


def _explicit_database(options: SourceOptions) -> Optional[tuple[str, Optional[Path], Optional[str]]]:
    if options.sqlite_path:
        path = Path(options.sqlite_path).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"There is no database file at {path}.")
        return STORAGE_BACKEND_SQLITE, path, None
    if options.postgres_dsn_file:
        return STORAGE_BACKEND_POSTGRES, None, _read_dsn_file(options.postgres_dsn_file)
    return None


def _database_from_config(found: _StorageFound, env: Mapping[str, str]) -> tuple[str, Optional[Path], Optional[str]]:
    if found.backend == STORAGE_BACKEND_SQLITE:
        return STORAGE_BACKEND_SQLITE, _existing_sqlite(found.sqlite_candidates), None
    if found.backend != STORAGE_BACKEND_POSTGRES:
        raise SystemExit(f"The old configuration names storage backend {found.backend!r}. {_NAME_THE_DATABASE}")
    dsn = found.postgres_dsn(env)
    if not dsn:
        raise SystemExit(
            "The old instance uses PostgreSQL, but no connection string was found in its configuration, "
            "POSTGRES_DSN or POSTGRES_DSN_FILE. Pass --postgres-dsn-file <path>."
        )
    return STORAGE_BACKEND_POSTGRES, None, dsn


def resolve_export_source(options: SourceOptions, env: Optional[Mapping[str, str]] = None) -> ExportSource:
    """Where ``memory export`` reads from: the named database, else the one the old configuration names."""

    environment = os.environ if env is None else env
    config = read_old_config(options.config_path, environment) if options.config_path else None
    explicit = _explicit_database(options)
    database = explicit
    if database is None:
        if config is None:
            raise SystemExit("Name the old database with --sqlite or --postgres-dsn-file, or its configuration with --config.")
        database = _database_from_config(config.storage, environment)
    instance_name = _text(options.instance_name) or (config.instance_name if config else None) or None
    backend, sqlite_path, dsn = database
    logger.info(
        "Memory export source chosen. event=pinned_memory_export_source instance=%s backend=%s sqlite_path=%s "
        "from_config=%s explicit_database=%s",
        instance_name or "",
        backend,
        sqlite_path or "",
        config is not None,
        explicit is not None,
    )
    return ExportSource(instance_name, backend, sqlite_path, dsn)


# -- reading the database without changing it ---------------------------------------


def _open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    # A stopped WAL database in a folder nobody can write (a read-only mount)
    # cannot be opened with mode=ro alone: SQLite would need to create its
    # -shm file. With no -wal file there is no pending change and no writer,
    # so reading it as immutable is exact.
    immutable = not os.access(path.parent, os.W_OK) and not Path(f"{path}-wal").exists()
    uri = f"{path.as_uri()}?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
    conn.execute("PRAGMA query_only = ON")
    logger.info(
        "Opened the old memory database read-only. event=pinned_memory_export_sqlite_opened path=%s mode=%s",
        path,
        "immutable" if immutable else "read_only",
    )
    return conn


@contextmanager
def _read_only_connection(source: ExportSource) -> Iterator[Any]:
    if source.backend == STORAGE_BACKEND_SQLITE:
        conn = _open_sqlite_read_only(Path(str(source.sqlite_path)))
        try:
            yield conn
        finally:
            conn.close()
        return
    with connect_postgres(str(source.postgres_dsn)) as pg:
        pg.execute("SET TRANSACTION READ ONLY")
        yield pg


def _bind(source: ExportSource) -> str:
    return _SQLITE_BIND if source.backend == STORAGE_BACKEND_SQLITE else _POSTGRES_BIND


def _has_table(conn: Any, source: ExportSource, table: str) -> bool:
    if source.backend == STORAGE_BACKEND_SQLITE:
        query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    else:
        query = "SELECT 1 WHERE to_regclass(%s) IS NOT NULL"
    return conn.execute(query, (table,)).fetchone() is not None


def _unexpired_facts(conn: Any, source: ExportSource) -> list[PinnedMemoryFactRow]:
    """The same facts as ``PinnedMemoryRepository.list_unexpired_facts``, by explicit columns."""

    rows = conn.execute(
        f"SELECT {', '.join(_FACT_COLUMNS)} FROM {FACTS_TABLE} "
        f"WHERE invalid_after_utc IS NULL OR invalid_after_utc > {_bind(source)} "
        "ORDER BY bank ASC, team_id ASC, canonical_user_id ASC, memory_id ASC",
        (utc_now_iso(),),
    ).fetchall()
    # SQLite rows are tuples and PostgreSQL rows index by position too.
    return [PinnedMemoryFactRow(**{name: row[index] for index, name in enumerate(_FACT_COLUMNS)}) for row in rows]


@dataclass(frozen=True)
class _ConnectionPeopleStore:
    """The people records (``storage.access_repository``) read through one read-only connection."""

    conn: Any
    bind: str

    def team_person(self, team_id: str, person_id: str) -> TeamPersonV1 | None:
        row = self.conn.execute(
            f"SELECT payload_json FROM {PEOPLE_TABLE} WHERE team_id={self.bind} AND person_id={self.bind}",
            (team_id, person_id),
        ).fetchone()
        return TeamPersonV1.model_validate_json(row[0]) if row else None

    def team_people(self, team_id: str) -> list[TeamPersonV1]:
        rows = self.conn.execute(
            f"SELECT payload_json FROM {PEOPLE_TABLE} WHERE team_id={self.bind} ORDER BY person_id", (team_id,)
        ).fetchall()
        return [TeamPersonV1.model_validate_json(row[0]) for row in rows]

    def people_synced_at(self, team_id: str) -> datetime | None:
        row = self.conn.execute(
            f"SELECT synced_at FROM {PEOPLE_SYNCED_TABLE} WHERE team_id={self.bind}", (team_id,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def synced_teams(self) -> list[str]:
        rows = self.conn.execute(f"SELECT team_id FROM {PEOPLE_SYNCED_TABLE} ORDER BY team_id").fetchall()
        return [str(row[0]) for row in rows]


def _removed_people(conn: Any, source: ExportSource, team_id: Optional[str]) -> list[str]:
    """People removed from the team (from any recorded team when the team is unknown)."""

    if not (_has_table(conn, source, PEOPLE_TABLE) and _has_table(conn, source, PEOPLE_SYNCED_TABLE)):
        logger.info(
            "The old instance keeps no people records, so nobody is left out as removed. "
            "event=pinned_memory_export_no_people_records instance=%s",
            source.instance_name or "",
        )
        return []
    store = _ConnectionPeopleStore(conn, _bind(source))
    removed: set[str] = set()
    for team in [team_id] if team_id else store.synced_teams():
        directory = PeopleDirectory(store, team)
        if directory.synced():
            removed.update(pid for pid, person in directory.people().items() if person.standing is PersonStanding.REMOVED)
    return sorted(removed)


def choose_team(facts: list[PinnedMemoryFactRow], requested: Optional[str]) -> tuple[Optional[str], str]:
    """The old team id from the stored team memory, or ``--team-id``; never a guess among several."""

    stored = sorted({row.team_id for row in facts if row.bank == COREMEM_BANK and row.team_id})
    listed = ", ".join(stored)
    if requested:
        if stored and requested not in stored:
            raise SystemExit(f"The database holds no team memory for --team-id {requested}; it holds team memory for: {listed}.")
        return requested, "given with --team-id"
    if len(stored) == 1:
        return stored[0], "found in the stored team memory"
    if not stored:
        return None, "unknown: the database holds no team memory"
    raise SystemExit(
        f"The database holds team memory for several teams: {listed}. "
        "Pass --team-id to choose the one to export; the import puts all team memory into one team."
    )


def read_source_memories(source: ExportSource, *, team_id: Optional[str] = None) -> SourceMemories:
    """Every unexpired pinned fact, the team and the people removed from it; changes nothing."""

    with _read_only_connection(source) as conn:
        if not _has_table(conn, source, FACTS_TABLE):
            raise SystemExit(
                f"The {source.describe()} has no pinned memories table ({FACTS_TABLE}). "
                "Check that it is the old instance's manager database (task_history.db)."
            )
        facts = _unexpired_facts(conn, source)
        team, note = choose_team(facts, _text(team_id) or None)
        return SourceMemories(facts, team, note, _removed_people(conn, source, team))


__all__ = [
    "ExportSource",
    "SourceMemories",
    "SourceOptions",
    "choose_team",
    "read_config_document",
    "read_old_config",
    "read_source_memories",
    "resolve_export_source",
]
