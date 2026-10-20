"""Shared SQLite connection configuration for repository-owned stores.

Repositories open one short-lived connection per call, from several processes
(gateway, manager, workers, broker) against the same database file. Two SQLite
behaviors made that pattern block callers for seconds:

- When the last open connection to a file closes, SQLite by default
  checkpoints the whole write-ahead log (WAL) into the database and deletes it
  while holding an exclusive lock. With per-call connections almost every
  close can be "the last", so any process opening a connection at that moment
  waits for the whole checkpoint. File connections therefore turn that off
  (``SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE``): the WAL stays on disk, commits keep
  checkpointing it as usual (automatic checkpoints, which never stop readers),
  and ``journal_size_limit`` shrinks the file after a checkpoint resets it.
- WAL mode is stored in the database file header, so it only has to be switched
  on once per file. Every connection only asks for the current mode (a read
  that waits for locks at most ``busy_timeout``); only a file that is not in WAL
  yet runs the switch and its bounded retry for the first-open race.

- Each process keeps one idle connection open to every database file it uses
  (``_hold_open``, at most ``HELD_OPEN_DATABASES`` files). When a process opens
  a file in WAL mode while it has no other connection to it, SQLite first
  rebuilds its index of the WAL by reading the whole WAL. With per-call
  connections that happened on nearly every open. On an NFS-mounted instance
  (owner's measurement, 2026-10-02) the client also drops its cached copy of
  a file another write changed, so each of those opens read the WAL over the
  network again: with a writer active, opens took a median 6.5 ms, and one
  waited 30 seconds; with one connection held open the slowest of 30 took
  2.3 ms. A held connection runs no transaction, so it holds no lock and does
  not stop checkpoints.

- A new file's one-time setup (``prepare_sqlite_database``) runs once, alone,
  before several processes open the file. The first connection to a new file
  switches it to WAL, and the first write then creates the WAL. Both hold
  SQLite's locks across several disk flushes, and the other processes' first
  statement waits only one ``busy_timeout``. On a slow disk (one flush taking
  about 0.6 s or more) several services opening the new manager database at
  the same moment failed with "database is locked", and the launcher stopped
  every service. The headless launcher therefore prepares the shared manager
  database before it starts any service (``docker.launch_headless``). Each
  connection also sets ``synchronous=NORMAL`` before the switch, which saves
  one of its flushes.
- While several processes create their tables at once, a statement can fail
  with "database schema has changed": SQLite retries only once when the schema
  changes while a statement is prepared. Such a statement has not run, so it
  runs again, a bounded number of times (``_execute_with_schema_retry``).

An open or statement that takes longer than ``SLOW_SQLITE_OPERATION_SECONDS``
is logged as a warning (``sqlite_open_slow``, ``sqlite_statement_slow``) with
the database, the thread, the process and the statement's first words, never
its values. A gateway froze for over 20 seconds inside a connection open with
no lock timeout and no NFS timeout to explain it; these lines name the slow
database and caller the next time instead of leaving only a stack sample. Lock
failures while opening are logged the same way even when the caller passes no
logger, so a failed start names the database and the process that gave up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any
from pathlib import Path

from .sql_dialect import SQLITE
from .read_only import repositories_are_read_only


DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 2500
DEFAULT_SQLITE_WAL_INIT_MAX_ATTEMPTS = 5
DEFAULT_SQLITE_WAL_INIT_RETRY_DELAY_SECONDS = 0.05
# Size the WAL file is cut back to after a checkpoint lets SQLite reuse it from
# the start. Without it a WAL that once grew (a large transaction, or a reader
# that held back checkpoints) would keep its largest size for good now that
# closing connections no longer delete it.
DEFAULT_SQLITE_WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
# How many database files a process keeps one idle connection open to (module
# docstring); the least recently used one is closed beyond that. Each held
# connection keeps three files open (the database, its WAL and its index).
HELD_OPEN_DATABASES = 32
_HELD_OPEN: "OrderedDict[str, sqlite3.Connection]" = OrderedDict()
_HELD_OPEN_LOCK = threading.Lock()
# An open or statement slower than this is logged (module docstring).
SLOW_SQLITE_OPERATION_SECONDS = 1.0
# How often a statement runs at most when SQLite reports that the schema
# changed under it, and the pause before the first rerun, doubled for each
# further one up to the cap (``_execute_with_schema_retry``). All pauses
# together stay under a quarter of a second.
SCHEMA_CHANGED_ATTEMPTS = 10
SCHEMA_CHANGED_FIRST_PAUSE_SECONDS = 0.001
SCHEMA_CHANGED_MAX_PAUSE_SECONDS = 0.05
# How much of a slow statement is logged: its first words, never its values.
_SLOW_STATEMENT_PREVIEW_CHARS = 80
_LOGGER = logging.getLogger(__name__)
# File paths already warned about a SQLite module that cannot turn off the
# checkpoint on close (one warning per file and process, not per connection).
_CHECKPOINT_ON_CLOSE_WARNED: set[str] = set()


@lru_cache(maxsize=2)
def _owned_connection_type(connection_type: type) -> type:
    # Support both stdlib sqlite3 and pysqlite3 without wrapping their extension API.
    class OwnedConnection(connection_type):
        """A repository transaction scope also owns the connection lifetime."""

        dialect = SQLITE

        def begin_write(self):
            self.execute("BEGIN IMMEDIATE")

        def execute(self, sql: str, parameters: Any = (), /) -> Any:
            started = time.perf_counter()
            try:
                return _execute_with_schema_retry(
                    super().execute, sql, parameters, db_path=getattr(self, "slow_log_path", "")
                )
            finally:
                _log_if_slow(
                    getattr(self, "slow_log_path", ""),
                    "statement",
                    time.perf_counter() - started,
                    statement=str(sql),
                )

        def __exit__(self, exc_type, exc, traceback):
            try:
                return super().__exit__(exc_type, exc, traceback)
            finally:
                self.close()

        def iterate(self, query, params=(), *, batch_size=256):
            """Read a query without materializing all candidate rows in Python."""
            cursor = self.execute(query, params)
            try:
                while rows := cursor.fetchmany(batch_size):
                    yield from rows
            finally:
                cursor.close()

    return OwnedConnection


def connect_sqlite(
    db_path: str,
    *,
    logger: logging.Logger | None = None,
    row_factory: Any | None = None,
    sqlite_module: Any = sqlite3,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a connection closed by its transaction context or explicit owner.

    Reusing a connection after leaving ``with`` is intentionally unsupported.
    Borrowed connections inside a larger unit of work must not enter this scope.
    """

    normalized_timeout_ms = _normalize_busy_timeout_ms(busy_timeout_ms)
    started = time.perf_counter()
    if repositories_are_read_only():
        # Do not create a file, switch journal mode, or open a held writer.
        conn = sqlite_module.connect(
            Path(db_path).resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=normalized_timeout_ms / 1000.0,
            factory=_owned_connection_type(sqlite_module.Connection),
        )
        conn.row_factory = row_factory
        conn.execute("PRAGMA query_only=ON")
        return conn
    conn = sqlite_module.connect(
        db_path,
        timeout=normalized_timeout_ms / 1000.0,
        factory=_owned_connection_type(sqlite_module.Connection),
    )
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        configure_sqlite_connection(
            conn,
            db_path=db_path,
            logger=logger,
            busy_timeout_ms=normalized_timeout_ms,
            operational_error_type=getattr(sqlite_module, "OperationalError", sqlite3.OperationalError),
        )
    except BaseException:
        conn.close()
        raise
    finally:
        _log_if_slow(db_path, "open", time.perf_counter() - started, logger=logger)
    # Set after configuring, so the open's own statements count once, as the open.
    conn.slow_log_path = db_path
    if _should_enable_wal(db_path):
        _hold_open(db_path, sqlite_module=sqlite_module, logger=logger, busy_timeout_ms=normalized_timeout_ms)
    return conn


def _hold_open(db_path: str, *, sqlite_module: Any, logger: logging.Logger | None, busy_timeout_ms: int) -> None:
    """Keep one idle connection to this file open for the process's life (module docstring)."""

    key = os.path.realpath(db_path)
    with _HELD_OPEN_LOCK:
        if key in _HELD_OPEN:
            _HELD_OPEN.move_to_end(key)
            return
    # Opened outside the lock: on a stalled file system the open can wait a
    # long time, and every other open in the process takes this lock.
    # Never used for statements; any thread may close it when it is evicted.
    held = sqlite_module.connect(db_path, timeout=busy_timeout_ms / 1000.0, check_same_thread=False)
    try:
        held.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        _keep_wal_on_close(held, db_path=db_path, logger=logger)
        # Reading the schema makes this connection map the WAL index, which
        # is what keeps later opens from rebuilding it.
        _execute_with_schema_retry(
            held.execute, "SELECT count(*) FROM sqlite_master", (), db_path=db_path, logger=logger
        ).fetchone()
    except BaseException:
        held.close()
        raise
    with _HELD_OPEN_LOCK:
        if key in _HELD_OPEN:
            # Another thread held this file meanwhile; keep theirs.
            extra, evicted = held, None
        else:
            _HELD_OPEN[key] = held
            extra = None
            evicted = _HELD_OPEN.popitem(last=False)[1] if len(_HELD_OPEN) > HELD_OPEN_DATABASES else None
    for connection in (extra, evicted):
        if connection is not None:
            connection.close()


def _execute_with_schema_retry(
    execute: Any,
    sql: str,
    parameters: Any,
    *,
    db_path: str,
    logger: logging.Logger | None = None,
) -> Any:
    """Run a statement again when SQLite reports that the schema changed under it.

    SQLite prepares a statement again when the schema changed after it was
    prepared, but it retries only once when the schema changes while the
    statement is being prepared. On a first start several processes create
    their tables at the same moment (one commit about every 2 ms was
    measured), and a process preparing its first statement, which reads the
    whole schema, failed with "database schema has changed" and stopped. A
    statement that fails this way has not run, so running it again is safe; it
    is tried at most ``SCHEMA_CHANGED_ATTEMPTS`` times with a short growing
    pause, so a burst of table creation can pass, and each retry is logged.
    """

    for attempt in range(1, SCHEMA_CHANGED_ATTEMPTS + 1):
        try:
            return execute(sql, parameters)
        except Exception as exc:  # the stdlib's or pysqlite3's OperationalError
            if attempt >= SCHEMA_CHANGED_ATTEMPTS or "database schema has changed" not in str(exc).lower():
                raise
            (logger or _LOGGER).warning(
                "The SQLite schema changed while a statement was prepared; running it again. "
                "event=sqlite_schema_changed_retry database=%s attempt=%s/%s pid=%s program=%s statement=%s",
                db_path or "-",
                attempt,
                SCHEMA_CHANGED_ATTEMPTS,
                os.getpid(),
                _process_label(),
                " ".join(str(sql).split())[:_SLOW_STATEMENT_PREVIEW_CHARS],
                extra={"component": "sqlite_connection", "storage_target": db_path, "operation": "statement"},
            )
            time.sleep(min(SCHEMA_CHANGED_FIRST_PAUSE_SECONDS * 2 ** (attempt - 1), SCHEMA_CHANGED_MAX_PAUSE_SECONDS))
    raise AssertionError("The schema-change retry loop exited unexpectedly.")


def held_open_databases() -> list[str]:
    """The database files this process keeps one idle connection open to, least recently used first."""

    with _HELD_OPEN_LOCK:
        return list(_HELD_OPEN)


def close_held_open_databases() -> None:
    """Close every held connection (tests, and a caller about to replace database files)."""

    with _HELD_OPEN_LOCK:
        held = list(_HELD_OPEN.values())
        _HELD_OPEN.clear()
    for connection in held:
        connection.close()


def _on_event_loop() -> bool:
    """Whether this thread is running an asyncio event loop (a slow call here stalls everything on it)."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _log_if_slow(
    db_path: str,
    operation: str,
    seconds: float,
    *,
    statement: str = "",
    logger: logging.Logger | None = None,
) -> None:
    """Log an open or statement that took longer than ``SLOW_SQLITE_OPERATION_SECONDS``."""

    if seconds < SLOW_SQLITE_OPERATION_SECONDS or not db_path:
        return
    preview = " ".join(statement.split())[:_SLOW_STATEMENT_PREVIEW_CHARS]
    (logger or _LOGGER).warning(
        "A SQLite %s was slow. event=sqlite_%s_slow database=%s seconds=%.2f thread=%s on_event_loop=%s "
        "pid=%s program=%s statement=%s",
        operation,
        operation,
        db_path,
        seconds,
        threading.current_thread().name,
        str(_on_event_loop()).lower(),
        os.getpid(),
        _process_label(),
        preview or "-",
        extra={"component": "sqlite_connection", "storage_target": db_path, "operation": operation},
    )


# A subcommand such as ``run-secure-tool-broker``. Other arguments are left
# out of log lines, because they can carry values.
_SUBCOMMAND_PATTERN = re.compile(r"[a-z][a-z0-9-]*")


def _process_label() -> str:
    """This process for log lines: its program and subcommand, e.g. ``assistant_cli.py:run-gateway``.

    Every service runs in its own process from the same few programs, so the
    subcommand is what tells the gateway from the broker in a shared log.
    """

    argv = sys.argv or [""]
    parts = [Path(argv[0]).name or "python"]
    if len(argv) > 1 and _SUBCOMMAND_PATTERN.fullmatch(argv[1]):
        parts.append(argv[1])
    return ":".join(parts)


def sqlite_column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Return SQLite column names for a repository-owned table."""

    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def ensure_sqlite_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_type: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Add a SQLite column, tolerating a concurrent process that added it first."""

    if column_name in sqlite_column_names(conn, table_name):
        return
    try:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if ("duplicate column" in message or "already exists" in message) and column_name in sqlite_column_names(
            conn,
            table_name,
        ):
            if logger is not None:
                logger.info(
                    "SQLite column was added concurrently during schema initialization: table=%s column=%s.",
                    table_name,
                    column_name,
                )
            return
        raise


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    db_path: str = "",
    logger: logging.Logger | None = None,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    operational_error_type: Any = sqlite3.OperationalError,
) -> None:
    """Apply deterministic SQLite connection PRAGMAs."""

    normalized_timeout_ms = _normalize_busy_timeout_ms(busy_timeout_ms)
    conn.execute(f"PRAGMA busy_timeout = {normalized_timeout_ms}")
    if _should_enable_wal(db_path):
        _keep_wal_on_close(conn, db_path=db_path, logger=logger)
        conn.execute(f"PRAGMA journal_size_limit = {DEFAULT_SQLITE_WAL_SIZE_LIMIT_BYTES}")
        journal_mode = _ensure_wal_mode(
            conn,
            db_path=db_path,
            logger=logger,
            busy_timeout_ms=normalized_timeout_ms,
            operational_error_type=operational_error_type,
        )
        if journal_mode.lower() != "wal":
            message = (
                f"SQLite database at {db_path!r} did not enter WAL journal mode; "
                f"SQLite reported {journal_mode!r}."
            )
            (logger or _LOGGER).error(
                message,
                extra={
                    "component": "sqlite_connection",
                    "storage_target": db_path,
                    "reported_journal_mode": journal_mode,
                },
            )
            raise RuntimeError(message)
    conn.execute("PRAGMA synchronous=NORMAL")


def prepare_sqlite_database(db_path: str, *, logger: logging.Logger | None = None) -> bool:
    """Do a database file's one-time setup before several processes open it.

    Switches a file that is not in WAL mode yet (including a new one) to WAL
    and makes its first write, so the WAL exists before any service opens the
    file (module docstring). The write sets ``user_version`` to the value it
    already has: it writes one WAL frame and changes nothing the stores read.
    A file already in WAL mode is left as it is. Returns whether the file was
    set up now. Nothing is created in read-only mode or for an in-memory path.

    Call it only while no other process uses the file, as the headless
    launcher does before it starts the services.
    """

    if repositories_are_read_only() or not _should_enable_wal(db_path):
        return False
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=DEFAULT_SQLITE_BUSY_TIMEOUT_MS / 1000.0)
    try:
        # First, also for a file that is already prepared: closing the only
        # connection would otherwise checkpoint and delete the WAL, and the
        # services' first write would have to create it again.
        _keep_wal_on_close(conn, db_path=db_path, logger=logger)
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_SQLITE_BUSY_TIMEOUT_MS}")
        if _first_cell(conn.execute("PRAGMA journal_mode").fetchone()).lower() == "wal":
            return False
        configure_sqlite_connection(conn, db_path=db_path, logger=logger)
        version = int(_first_cell(conn.execute("PRAGMA user_version").fetchone()) or 0)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()
    (logger or _LOGGER).info(
        "Prepared a SQLite database before its processes start. event=sqlite_database_prepared database=%s",
        db_path,
        extra={"component": "sqlite_connection", "storage_target": db_path, "operation": "prepare"},
    )
    return True


def _keep_wal_on_close(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    logger: logging.Logger | None,
) -> None:
    """Stop this connection's close from checkpointing and deleting the WAL.

    Closing the last connection to a file otherwise takes an exclusive lock for
    a full checkpoint, and every other process opening the file waits for it.
    """

    setconfig = getattr(conn, "setconfig", None)
    if setconfig is not None:
        setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        return
    # Only a SQLite module without Connection.setconfig (Python 3.12+ has it)
    # lands here; the connection still works, it just keeps the old close.
    if logger is None or db_path in _CHECKPOINT_ON_CLOSE_WARNED:
        return
    _CHECKPOINT_ON_CLOSE_WARNED.add(db_path)
    logger.warning(
        "This SQLite module cannot keep the write-ahead log on close, so closing the last "
        "connection may briefly block other connections. event=sqlite_checkpoint_on_close_kept "
        "database=%s connection_type=%s",
        db_path,
        type(conn).__name__,
        extra={"component": "sqlite_connection", "storage_target": db_path},
    )


def _ensure_wal_mode(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    logger: logging.Logger | None,
    busy_timeout_ms: int,
    operational_error_type: Any = sqlite3.OperationalError,
) -> str:
    """Return the journal mode, switching the file to WAL only when it is not yet.

    Asking for the mode needs only a read lock and waits at most one
    ``busy_timeout``; it is not retried, because a lock held that long is not
    the first-open race the switch retry exists for, and repeating the wait
    only multiplies how long the caller is blocked.
    """

    try:
        journal_mode = _first_cell(conn.execute("PRAGMA journal_mode").fetchone())
    except operational_error_type as exc:
        if _is_sqlite_lock_error(exc):
            (logger or _LOGGER).error(
                "SQLite database stayed locked while opening a connection. "
                "event=sqlite_journal_mode_check_locked database=%s busy_timeout_ms=%s pid=%s program=%s",
                db_path,
                busy_timeout_ms,
                os.getpid(),
                _process_label(),
                extra={"component": "sqlite_connection", "storage_target": db_path, "operation": "check_wal"},
            )
        raise
    if journal_mode.lower() == "wal":
        return journal_mode
    # Before the switch, so a new file's switch needs one disk flush fewer
    # while it holds the file's locks (module docstring). Not earlier: the
    # journal-mode read above is the open's first statement that waits for a
    # lock, and the one that logs it.
    conn.execute("PRAGMA synchronous=NORMAL")
    return _enable_wal_with_retry(
        conn,
        db_path=db_path,
        logger=logger,
        operational_error_type=operational_error_type,
    )


def _enable_wal_with_retry(
    conn: sqlite3.Connection,
    *,
    db_path: str,
    logger: logging.Logger | None,
    operational_error_type: Any = sqlite3.OperationalError,
    max_attempts: int = DEFAULT_SQLITE_WAL_INIT_MAX_ATTEMPTS,
    retry_delay_seconds: float = DEFAULT_SQLITE_WAL_INIT_RETRY_DELAY_SECONDS,
) -> str:
    """Enable WAL while tolerating the bounded first-open initialization race."""

    attempts = max(1, int(max_attempts))
    base_delay = max(0.0, float(retry_delay_seconds))
    for attempt in range(1, attempts + 1):
        try:
            return _first_cell(conn.execute("PRAGMA journal_mode=WAL").fetchone())
        except operational_error_type as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            if attempt >= attempts:
                _log_wal_init_locked(logger, db_path=db_path, attempt=attempt, attempts=attempts)
                raise
            delay = base_delay * (2 ** (attempt - 1))
            _log_wal_init_retry(logger, db_path=db_path, attempt=attempt, attempts=attempts, delay=delay)
            if delay:
                time.sleep(delay)
    raise AssertionError("SQLite WAL initialization retry loop exited unexpectedly.")


def _log_wal_init_locked(logger: logging.Logger | None, *, db_path: str, attempt: int, attempts: int) -> None:
    (logger or _LOGGER).error(
        "SQLite WAL initialization remained locked after bounded retries. "
        "event=sqlite_wal_init_locked database=%s attempts=%s pid=%s program=%s",
        db_path,
        attempt,
        os.getpid(),
        _process_label(),
        extra={
            "component": "sqlite_connection",
            "storage_target": db_path,
            "operation": "enable_wal",
            "attempt": attempt,
            "max_attempts": attempts,
        },
    )


def _log_wal_init_retry(
    logger: logging.Logger | None,
    *,
    db_path: str,
    attempt: int,
    attempts: int,
    delay: float,
) -> None:
    (logger or _LOGGER).warning(
        "SQLite WAL initialization raced another connection; retrying. "
        "event=sqlite_wal_init_retry database=%s attempt=%s/%s delay_seconds=%.3f pid=%s program=%s",
        db_path,
        attempt,
        attempts,
        delay,
        os.getpid(),
        _process_label(),
        extra={
            "component": "sqlite_connection",
            "storage_target": db_path,
            "operation": "enable_wal",
            "attempt": attempt,
            "max_attempts": attempts,
            "retry_delay_seconds": delay,
        },
    )


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    message = str(exc).strip().lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
        )
    )


def _normalize_busy_timeout_ms(value: int) -> int:
    try:
        timeout_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SQLite busy_timeout must be an integer millisecond value, got {value!r}.") from exc
    if timeout_ms <= 0:
        raise ValueError(f"SQLite busy_timeout must be positive, got {timeout_ms}.")
    return timeout_ms


def _should_enable_wal(db_path: str) -> bool:
    raw_path = str(db_path or "").strip()
    lowered = raw_path.lower()
    if not raw_path:
        return False
    if lowered == ":memory:":
        return False
    if lowered.startswith("file::memory:") or "mode=memory" in lowered:
        return False
    return True


def _first_cell(row: Any) -> str:
    if row is None:
        return ""
    return str(row[0])
