from __future__ import annotations

import gc
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time

from contextlib import closing
from pathlib import Path

import pytest

from storage import sqlite_connection
from storage.read_only import read_only_repositories
from storage.sqlite_connection import (
    connect_sqlite,
    ensure_sqlite_column,
    prepare_sqlite_database,
)
from tests.sqlite_lock_support import hold_exclusive_lock, release_exclusive_lock


def test_transaction_scope_commits_or_rolls_back_and_always_closes(tmp_path):
    path = str(tmp_path / "owned.db")
    with connect_sqlite(path) as conn:
        conn.execute("CREATE TABLE work (value TEXT)")
        conn.execute("INSERT INTO work VALUES ('committed')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with pytest.raises(ValueError):
        with connect_sqlite(path) as failed:
            failed.execute("INSERT INTO work VALUES ('rolled back')")
            raise ValueError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    with connect_sqlite(path) as check:
        assert check.execute("SELECT value FROM work").fetchall() == [("committed",)]


def test_initialization_failure_closes_connection(monkeypatch):
    opened = []

    def fail(conn, **kwargs):
        opened.append(conn)
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(sqlite_connection, "configure_sqlite_connection", fail)
    with pytest.raises(RuntimeError, match="initialization failed"):
        connect_sqlite(":memory:")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux descriptor probe")
def test_repository_scopes_release_descriptors_without_collection(tmp_path):
    from storage.runtime_state_repository import SqliteRuntimeStateRepository

    repo = SqliteRuntimeStateRepository(str(tmp_path / "runtime.db"))
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(100):
            repo.load_task_runtime_by_owner("missing")
        assert len(os.listdir("/proc/self/fd")) <= before
    finally:
        if was_enabled:
            gc.enable()


def test_ensure_sqlite_column_reraises_duplicate_when_column_is_still_absent():
    class _Cursor:
        def fetchall(self):
            return []

    class _Connection:
        OperationalError = sqlite3.OperationalError

        def execute(self, statement: str):
            if statement.startswith("PRAGMA table_info"):
                return _Cursor()
            if statement.startswith("ALTER TABLE"):
                raise sqlite3.OperationalError("duplicate column name: owner_canonical_user_id")
            raise AssertionError(f"unexpected statement: {statement}")

    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        ensure_sqlite_column(
            _Connection(),
            "conversation_threads",
            "owner_canonical_user_id",
            "TEXT",
        )


class _DriverOperationalError(Exception):
    """A driver's own error class, unrelated to the standard library's, as ``pysqlite3``'s is."""


def test_a_column_another_process_added_first_is_kept_when_the_driver_has_its_own_error_class(caplog):
    """Memory graphs on macOS use ``pysqlite3``, whose ``OperationalError`` is not ``sqlite3.OperationalError``.

    Catching the standard library's class let the duplicate column error of a
    process that lost the race stop its start. CI has no ``pysqlite3``, so this
    driver is a stand-in.
    """

    class _Rows:
        def __init__(self, names: list[str]):
            self._rows = [(position, name) for position, name in enumerate(names)]

        def fetchall(self):
            return self._rows

    class _Connection:
        OperationalError = _DriverOperationalError

        def __init__(self):
            self.columns = ["conversation_id"]

        def execute(self, statement: str):
            if statement.startswith("PRAGMA table_info"):
                return _Rows(self.columns)
            if statement.startswith("ALTER TABLE"):
                self.columns.append("owner_canonical_user_id")  # Another process added it after the read.
                raise _DriverOperationalError("duplicate column name: owner_canonical_user_id")
            raise AssertionError(f"unexpected statement: {statement}")

    assert not issubclass(_DriverOperationalError, sqlite3.Error)
    logger = logging.getLogger("test.sqlite_columns")
    with caplog.at_level(logging.INFO, logger=logger.name):
        ensure_sqlite_column(_Connection(), "conversation_threads", "owner_canonical_user_id", "TEXT", logger=logger)

    assert "added concurrently" in caplog.text
    assert "event=storage_column_added" not in caplog.text


# -- lock contention and WAL handling -----------------------------------------------------
#
# Regression for a live gateway stall: every connection re-ran the WAL switch and
# retried it five times after each full busy_timeout wait (about 13 seconds at
# the 2.5 second default), and closing the last connection checkpointed the WAL
# under an exclusive lock that every new connection had to wait for.


def _wal_database(path) -> str:
    with connect_sqlite(str(path)) as conn:
        conn.execute("CREATE TABLE work (value TEXT)")
    return str(path)


def _exclusive_holder(path: str) -> sqlite3.Connection:
    """Hold the exclusive file lock, as a close-time checkpoint in another process does.

    Its release does no disk work, so a timed release frees the lock on time on
    any disk (``tests.sqlite_lock_support``).
    """

    holder = hold_exclusive_lock(path)
    holder.execute("INSERT INTO work VALUES ('held')")
    return holder


def test_the_lock_holder_releases_without_flushing_or_checkpointing(tmp_path):
    path = _wal_database(tmp_path / "holder.db")
    holder = _exclusive_holder(path)
    assert holder.execute("PRAGMA synchronous").fetchone()[0] == 0, "its commit must not flush"
    release_exclusive_lock(holder)
    # A close-time checkpoint would have flushed the WAL into the database and deleted it.
    assert os.path.getsize(f"{path}-wal") > 0
    with connect_sqlite(path) as check:
        assert check.execute("SELECT value FROM work").fetchall() == [("held",)]


def test_closing_a_connection_keeps_the_wal_instead_of_checkpointing_it(tmp_path):
    path = _wal_database(tmp_path / "kept.db")
    with connect_sqlite(path) as conn:
        assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is True
        conn.execute("INSERT INTO work VALUES ('committed')")
    # The last connection closed: without the setting SQLite would have
    # checkpointed and deleted the WAL under an exclusive lock.
    assert os.path.exists(f"{path}-wal")
    with connect_sqlite(path) as check:
        assert check.execute("SELECT value FROM work").fetchall() == [("committed",)]
        assert check.execute("PRAGMA journal_size_limit").fetchone()[0] == (
            sqlite_connection.DEFAULT_SQLITE_WAL_SIZE_LIMIT_BYTES
        )


def test_open_is_not_blocked_by_another_connections_write_transaction(tmp_path):
    path = _wal_database(tmp_path / "writer.db")
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO work VALUES ('uncommitted')")
    try:
        started = time.monotonic()
        with connect_sqlite(path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("SELECT count(*) FROM work").fetchone()[0] == 0
        assert time.monotonic() - started < 0.5
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_open_waits_for_an_exclusive_lock_released_within_the_busy_timeout(tmp_path):
    path = _wal_database(tmp_path / "released.db")
    holder = _exclusive_holder(path)
    timer = threading.Timer(0.3, release_exclusive_lock, args=(holder,))
    timer.start()
    try:
        started = time.monotonic()
        with connect_sqlite(path, busy_timeout_ms=3000) as conn:
            assert conn.execute("SELECT value FROM work").fetchall() == [("held",)]
        assert 0.2 <= time.monotonic() - started < 2.0
    finally:
        timer.join()


def test_a_lock_that_outlasts_the_busy_timeout_is_waited_for_once(tmp_path, caplog):
    path = _wal_database(tmp_path / "locked.db")
    holder = _exclusive_holder(path)
    busy_timeout_ms = 200
    try:
        started = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="sqlite-test"):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connect_sqlite(path, busy_timeout_ms=busy_timeout_ms, logger=logging.getLogger("sqlite-test"))
        elapsed = time.monotonic() - started
    finally:
        release_exclusive_lock(holder)
    # The retried WAL switch took 5 x 200 ms plus 750 ms of backoff (1.75 s).
    assert elapsed < 3 * busy_timeout_ms / 1000
    assert "event=sqlite_journal_mode_check_locked" in caplog.text
    assert "event=sqlite_wal_init_retry" not in caplog.text


def test_only_a_file_not_yet_in_wal_runs_the_switch(tmp_path, monkeypatch):
    switches = []
    original = sqlite_connection._enable_wal_with_retry

    def counted(conn, **kwargs):
        switches.append(kwargs["db_path"])
        return original(conn, **kwargs)

    monkeypatch.setattr(sqlite_connection, "_enable_wal_with_retry", counted)
    path = str(tmp_path / "fresh.db")
    for _ in range(3):
        connect_sqlite(path).close()
    assert switches == [path]


def test_a_module_without_setconfig_keeps_the_old_close_and_warns_once(tmp_path, monkeypatch, caplog):
    class _NoSetconfig:
        """A connection from a SQLite module that predates Connection.setconfig."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, statement):
            return self._conn.execute(statement)

    monkeypatch.setattr(sqlite_connection, "_CHECKPOINT_ON_CLOSE_WARNED", set())
    path = _wal_database(tmp_path / "old_module.db")
    logger = logging.getLogger("sqlite-test")
    with caplog.at_level(logging.WARNING, logger="sqlite-test"):
        for _ in range(2):
            raw = sqlite3.connect(path)
            try:
                sqlite_connection.configure_sqlite_connection(_NoSetconfig(raw), db_path=path, logger=logger)
                assert raw.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is False
            finally:
                raw.close()
    assert caplog.text.count("event=sqlite_checkpoint_on_close_kept") == 1


def test_the_first_wal_switch_retries_only_lock_errors(caplog):
    class _Switching:
        def __init__(self, errors):
            self.errors = list(errors)

        def execute(self, statement):
            assert statement == "PRAGMA journal_mode=WAL"
            if self.errors:
                raise self.errors.pop(0)
            return self

        def fetchone(self):
            return ("wal",)

    logger = logging.getLogger("sqlite-test")
    racing = _Switching([sqlite3.OperationalError("database is locked")] * 2)
    with caplog.at_level(logging.WARNING, logger="sqlite-test"):
        mode = sqlite_connection._enable_wal_with_retry(racing, db_path="x.db", logger=logger, retry_delay_seconds=0)
    assert mode == "wal"
    assert caplog.text.count("event=sqlite_wal_init_retry") == 2
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        sqlite_connection._enable_wal_with_retry(
            _Switching([sqlite3.OperationalError("disk I/O error")]), db_path="x.db", logger=logger
        )
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sqlite_connection._enable_wal_with_retry(
            _Switching([sqlite3.OperationalError("database is locked")] * 3),
            db_path="x.db",
            logger=logger,
            max_attempts=3,
            retry_delay_seconds=0,
        )
    assert "event=sqlite_wal_init_locked" in caplog.text


def test_a_slow_open_and_a_slow_statement_are_logged_without_their_values(tmp_path, monkeypatch, caplog):
    db_path = str(tmp_path / "slow.db")
    with connect_sqlite(db_path) as conn:
        conn.execute("CREATE TABLE people(name TEXT)")
        conn.commit()
    # On a slow disk the setup itself is slow and logged; only the lines below count.
    caplog.clear()

    monkeypatch.setattr(sqlite_connection, "SLOW_SQLITE_OPERATION_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="storage.sqlite_connection"):
        with connect_sqlite(db_path) as conn:
            conn.execute("INSERT INTO people(name) VALUES (?)", ("Private Person",))
            conn.commit()

    lines = [record.getMessage() for record in caplog.records]
    assert any("event=sqlite_open_slow" in line and f"database={db_path}" in line for line in lines)
    [statement] = [line for line in lines if "event=sqlite_statement_slow" in line]
    assert "statement=INSERT INTO people(name) VALUES (?)" in statement and "thread=MainThread" in statement
    assert "Private Person" not in caplog.text, "values are never logged"


def test_fast_operations_are_not_logged(tmp_path, caplog):
    path = str(tmp_path / "fast.db")
    # A new file's switch to WAL flushes to disk and is rightly logged on a slow
    # disk, so the file is created first; opening it again writes nothing.
    connect_sqlite(path).close()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="storage.sqlite_connection"):
        with connect_sqlite(path) as conn:
            conn.execute("SELECT 1").fetchone()
    assert "_slow" not in caplog.text


def test_a_process_keeps_one_idle_connection_to_each_database_it_uses(tmp_path, monkeypatch):
    sqlite_connection.close_held_open_databases()
    monkeypatch.setattr(sqlite_connection, "HELD_OPEN_DATABASES", 2)
    first, second, third = (str(tmp_path / f"{name}.db") for name in ("first", "second", "third"))

    for _ in range(3):
        with connect_sqlite(first) as conn:
            conn.execute("SELECT 1").fetchone()
    with connect_sqlite(":memory:") as conn:
        conn.execute("SELECT 1").fetchone()
    assert sqlite_connection.held_open_databases() == [os.path.realpath(first)], "one per file; none in memory"

    with connect_sqlite(second):
        pass
    with connect_sqlite(third):
        pass
    assert sqlite_connection.held_open_databases() == [os.path.realpath(second), os.path.realpath(third)]
    sqlite_connection.close_held_open_databases()
    assert sqlite_connection.held_open_databases() == []


def test_a_held_connection_does_not_block_writes_or_checkpoints(tmp_path):
    db_path = str(tmp_path / "held.db")
    with connect_sqlite(db_path) as conn:
        conn.execute("CREATE TABLE t(v TEXT)")
        conn.commit()
    assert os.path.realpath(db_path) in sqlite_connection.held_open_databases()

    with connect_sqlite(db_path) as conn:
        conn.execute("INSERT INTO t(v) VALUES ('x')")
        conn.commit()
        busy, _log_frames, _checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert busy == 0, "the idle held connection holds no lock"


def test_the_slow_log_says_whether_the_call_ran_on_an_event_loop(tmp_path, monkeypatch, caplog):
    import asyncio

    monkeypatch.setattr(sqlite_connection, "SLOW_SQLITE_OPERATION_SECONDS", 0.0)

    async def on_loop():
        with connect_sqlite(str(tmp_path / "loop.db")):
            pass

    with caplog.at_level(logging.WARNING, logger="storage.sqlite_connection"):
        asyncio.run(on_loop())
        with connect_sqlite(str(tmp_path / "thread.db")):
            pass
    opens = [record.getMessage() for record in caplog.records if "event=sqlite_open_slow" in record.getMessage()]
    assert any("loop.db" in line and "on_event_loop=true" in line for line in opens)
    assert any("thread.db" in line and "on_event_loop=false" in line for line in opens)


# --- startup preparation before several processes open a file ----------------


def test_preparing_each_start_keeps_wal_ready_without_changing_database_contents(tmp_path, caplog):
    path = str(tmp_path / "dbs" / "task_history.db")
    with caplog.at_level(logging.INFO, logger="storage.sqlite_connection"):
        assert prepare_sqlite_database(path) is True
    assert "event=sqlite_database_prepared" in caplog.text
    # The first write already created the WAL, so no service has to.
    assert os.path.getsize(path + "-wal") > 0
    raw = sqlite3.connect(path)
    raw.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)  # as the stores' connections do
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        raw.close()
    assert prepare_sqlite_database(path) is True
    assert os.path.getsize(path + "-wal") > 0


def test_preparing_switches_an_existing_file_without_changing_its_content(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE work (value TEXT)")
    raw.execute("INSERT INTO work VALUES ('kept')")
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()

    assert prepare_sqlite_database(path) is True
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 7
        assert raw.execute("SELECT value FROM work").fetchall() == [("kept",)]
    finally:
        raw.close()


@pytest.mark.parametrize("emptied_by_checkpoint", [False, True], ids=["missing-wal", "truncated-wal"])
def test_preparing_wal_mode_without_a_frame_primes_it_and_preserves_records(tmp_path, emptied_by_checkpoint):
    path = str(tmp_path / "unprimed.db")
    raw = sqlite3.connect(path)
    try:
        raw.execute("CREATE TABLE work (value TEXT)")
        raw.execute("INSERT INTO work VALUES ('kept')")
        raw.execute("PRAGMA user_version = 7")
        raw.commit()
        raw.execute("PRAGMA journal_mode=WAL")
    finally:
        raw.close()  # An external connection's normal close removes the WAL.
    if emptied_by_checkpoint:
        assert prepare_sqlite_database(path)
        with connect_sqlite(path) as conn:
            assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    assert prepare_sqlite_database(path) is True
    assert os.path.getsize(path + "-wal") > 32
    with connect_sqlite(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("SELECT value FROM work").fetchall() == [("kept",)]
    assert prepare_sqlite_database(path) is True


def test_retry_after_process_exit_between_wal_switch_and_first_write_finishes_preparing(tmp_path):
    import subprocess

    path = str(tmp_path / "interrupted.db")
    process = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from storage import sqlite_connection as store
configure = store.configure_sqlite_connection
def exit_after_switch(*args, **kwargs):
    configure(*args, **kwargs)
    os._exit(73)
store.configure_sqlite_connection = exit_after_switch
store.prepare_sqlite_database(sys.argv[1])
""", path],
        check=False, timeout=15, capture_output=True, text=True,
    )
    assert process.returncode == 73, process.stderr
    assert prepare_sqlite_database(path) is True
    assert os.path.getsize(path + "-wal") > 32
    with connect_sqlite(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert prepare_sqlite_database(path) is True


def test_preparing_recovers_spilled_uncommitted_wal_before_other_writers_start(tmp_path):
    import subprocess

    path = str(tmp_path / "spilled.db")
    process = subprocess.run([sys.executable, "-c", """
import os
import sqlite3
import sys
c = sqlite3.connect(sys.argv[1])
c.execute('CREATE TABLE work (value BLOB)')
c.commit()
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA cache_size=5')
c.execute('BEGIN')
for _ in range(50):
    c.execute('INSERT INTO work VALUES (zeroblob(10000))')
os._exit(73)
""", path], check=False, timeout=15, capture_output=True, text=True)
    assert process.returncode == 73, process.stderr
    assert os.path.getsize(path + "-wal") > 100_000, "real cache spill left full but uncommitted frames"
    assert prepare_sqlite_database(path) is True
    with connect_sqlite(path) as conn:
        assert conn.execute("SELECT count(*) FROM work").fetchone()[0] == 0
        conn.execute("INSERT INTO work VALUES ('new committed work')")
    with connect_sqlite(path) as conn:
        assert conn.execute("SELECT value FROM work").fetchall() == [("new committed work",)]


def test_preparing_creates_nothing_when_read_only_or_in_memory(tmp_path):
    path = tmp_path / "dbs" / "task_history.db"
    with read_only_repositories():
        assert prepare_sqlite_database(str(path)) is False
    assert not path.parent.exists()
    assert prepare_sqlite_database(":memory:") is False


def test_synchronous_normal_comes_before_the_wal_switch(tmp_path, monkeypatch):
    class _Recording:
        """A real connection that records each statement it runs."""

        def __init__(self, conn):
            self._conn = conn
            self.statements: list[str] = []

        def execute(self, statement):
            self.statements.append(statement)
            return self._conn.execute(statement)

        def setconfig(self, *args):
            return self._conn.setconfig(*args)

    # The product's level, not the one tests/conftest.py lowers this process to.
    monkeypatch.setattr(sqlite_connection, "SQLITE_SYNCHRONOUS", "FULL")
    path = str(tmp_path / "fresh.db")
    raw = sqlite3.connect(path)
    try:
        recording = _Recording(raw)
        sqlite_connection.configure_sqlite_connection(recording, db_path=path)
    finally:
        raw.close()
    # The new file's switch then needs one disk flush fewer while it holds the file's locks.
    switch = recording.statements.index("PRAGMA journal_mode=WAL")
    assert recording.statements.index("PRAGMA synchronous=NORMAL") < switch
    # After the switch every commit is flushed.
    assert recording.statements.index("PRAGMA synchronous=FULL") > switch


def test_every_connection_commits_to_the_disk_before_returning(tmp_path, monkeypatch):
    """A store records before publishing and admits before acknowledging, so no commit may be lost on power loss."""

    # The product's setting, read in a fresh process: tests/conftest.py lowers it for this one.
    product = subprocess.run(
        [sys.executable, "-c", "import storage.sqlite_connection as s; print(s.SQLITE_SYNCHRONOUS)"],
        check=True, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
    ).stdout.strip()
    assert product == "FULL"
    monkeypatch.setattr(sqlite_connection, "SQLITE_SYNCHRONOUS", product)
    path = str(tmp_path / "dbs" / "any_store.db")
    os.makedirs(os.path.dirname(path))
    with sqlite_connection.connect_sqlite(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_on_macos_commits_use_the_full_flush(tmp_path, monkeypatch):
    """macOS's fsync can leave a commit in the drive's cache; fullfsync flushes it."""

    monkeypatch.setattr(sqlite_connection, "_on_macos", lambda: True)
    with sqlite_connection.connect_sqlite(str(tmp_path / "mac.db")) as conn:
        assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
        assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    monkeypatch.setattr(sqlite_connection, "_on_macos", lambda: False)
    with sqlite_connection.connect_sqlite(str(tmp_path / "linux.db")) as conn:
        assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 0


@pytest.mark.parametrize(("setting", "level"), [("FULL", 2), ("NORMAL", 1)])
def test_the_brokers_credential_catalog_commits_like_every_other_store(tmp_path, monkeypatch, setting, level):
    """The broker opens its credential catalog itself and still gets the shared setup.

    SQLite's own default depends on how it was built, so NORMAL is checked too: the store
    applies the shared level (configure_sqlite_connection) rather than relying on the default.
    """

    from secure_tool_broker.credential_catalog_store import CredentialCatalogStore

    monkeypatch.setattr(sqlite_connection, "SQLITE_SYNCHRONOUS", setting)
    store = CredentialCatalogStore(tmp_path / "credential_catalog.db")
    with closing(store._connect()) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == level
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# -- Flushing before sending (module docstring) ------------------------------------------


@pytest.fixture
def deferring_process(monkeypatch):
    """This process defers opted-in stores' flushes, as an agent process does; flushes are recorded."""

    flushed: list[str] = []
    monkeypatch.setattr(sqlite_connection, "_FLUSHES_DEFERRED", True)
    monkeypatch.setattr(sqlite_connection, "_UNFLUSHED", {})
    monkeypatch.setattr(sqlite_connection, "_FOLDER_FLUSHED", set())
    real_flush = sqlite_connection._flush_descriptor

    def recording_flush(fd):
        flushed.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_flush(fd)

    monkeypatch.setattr(sqlite_connection, "_flush_descriptor", recording_flush)
    return flushed


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the flushed file's name from /proc")
def test_a_deferred_store_commits_without_a_flush_and_is_flushed_before_sending(tmp_path, deferring_process):
    path = str(tmp_path / "agent.db")
    with connect_sqlite(path, defer_flush=True) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL: no flush in the commit
        conn.execute("CREATE TABLE IF NOT EXISTS work (value TEXT)")
        conn.execute("INSERT INTO work VALUES ('saved')")
    assert sqlite_connection.has_unflushed_commits()

    sqlite_connection.flush_unflushed_commits()

    # The WAL that holds the commits, then once the folder that holds its entry.
    assert deferring_process == [f"{os.path.realpath(path)}-wal", os.path.realpath(str(tmp_path))]
    assert not sqlite_connection.has_unflushed_commits()
    with connect_sqlite(path, defer_flush=True) as conn:
        conn.execute("INSERT INTO work VALUES ('again')")
    sqlite_connection.flush_unflushed_commits()
    assert deferring_process[2:] == [f"{os.path.realpath(path)}-wal"]  # the folder only the first time


def test_reads_and_autocommitted_writes_are_noted_correctly(tmp_path, deferring_process):
    path = str(tmp_path / "agent.db")
    with connect_sqlite(path, defer_flush=True) as conn:
        conn.execute("SELECT 1").fetchone()
        conn.execute("PRAGMA table_info(sqlite_master)").fetchall()
    assert not sqlite_connection.has_unflushed_commits(), "a read leaves nothing to flush"

    conn = connect_sqlite(path, defer_flush=True)
    conn.isolation_level = None  # each statement commits by itself, without commit()
    conn.execute("CREATE TABLE autocommitted (value TEXT)")
    conn.close()
    assert sqlite_connection.has_unflushed_commits(), "closing notes autocommitted writes"


def test_only_opted_in_stores_of_a_deferring_process_skip_the_flush(tmp_path, deferring_process, monkeypatch):
    monkeypatch.setattr(sqlite_connection, "SQLITE_SYNCHRONOUS", "FULL")
    with connect_sqlite(str(tmp_path / "other.db")) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        conn.execute("CREATE TABLE work (value TEXT)")
    assert not sqlite_connection.has_unflushed_commits()

    # A process that never deferred (the gateway, the broker) ignores the request.
    monkeypatch.setattr(sqlite_connection, "_FLUSHES_DEFERRED", False)
    with connect_sqlite(str(tmp_path / "agent.db"), defer_flush=True) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        conn.execute("CREATE TABLE work (value TEXT)")
    assert not sqlite_connection.has_unflushed_commits()


def test_a_failed_flush_keeps_the_commits_noted_and_stops_the_send(tmp_path, deferring_process, monkeypatch, caplog):
    path = str(tmp_path / "agent.db")
    with connect_sqlite(path, defer_flush=True) as conn:
        conn.execute("CREATE TABLE work (value TEXT)")

    def failing_flush(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(sqlite_connection, "_flush_descriptor", failing_flush)
    with caplog.at_level(logging.ERROR, logger=sqlite_connection.__name__):
        with pytest.raises(OSError):
            sqlite_connection.flush_unflushed_commits()
    assert "event=sqlite_flush_failed" in caplog.text
    assert os.path.realpath(path) in caplog.text
    assert sqlite_connection.has_unflushed_commits(), "the next send tries again"


def test_schema_setup_commits_without_a_flush_and_data_still_with_one(tmp_path, monkeypatch):
    """Schema setup re-runs at every start, so it needs no flush; data commits keep theirs (module docstring)."""

    monkeypatch.setattr(sqlite_connection, "SQLITE_SYNCHRONOUS", "FULL")
    statements: list[str] = []
    with connect_sqlite(str(tmp_path / "store.db")) as conn:
        conn.set_trace_callback(statements.append)
        conn.execute("CREATE TABLE IF NOT EXISTS work (value TEXT)")
        conn.execute("ALTER TABLE work ADD COLUMN detail TEXT")
        conn.execute("INSERT INTO work (value) VALUES ('kept')")
        conn.commit()
        conn.execute("BEGIN")
        conn.execute("CREATE INDEX IF NOT EXISTS work_value ON work (value)")  # part of a transaction: unchanged
        conn.execute("COMMIT")
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2

    assert statements[:6] == [
        "PRAGMA synchronous=NORMAL",
        "CREATE TABLE IF NOT EXISTS work (value TEXT)",
        "PRAGMA synchronous=FULL",
        "PRAGMA synchronous=NORMAL",
        "ALTER TABLE work ADD COLUMN detail TEXT",
        "PRAGMA synchronous=FULL",
    ]
    data_and_transaction = statements[6:]
    assert not any(s.startswith("PRAGMA synchronous=") for s in data_and_transaction)


def test_a_sender_arriving_during_a_flush_waits_for_it(tmp_path, deferring_process, monkeypatch):
    """A second sender must not find the file un-noted while the first sender's flush still runs."""

    path = os.path.realpath(str(tmp_path / "agent.db"))
    Path(f"{path}-wal").write_bytes(b"frames")
    sqlite_connection._note_unflushed_commit(path)
    inside, release = threading.Event(), threading.Event()
    flushes: list[str] = []

    def slow_flush(fd):
        flushes.append("flush")
        if len(flushes) == 1:
            inside.set()
            release.wait(5)

    monkeypatch.setattr(sqlite_connection, "_flush_descriptor", slow_flush)
    monkeypatch.setattr(sqlite_connection, "_FOLDER_FLUSHED", {path})
    first = threading.Thread(target=sqlite_connection.flush_unflushed_commits)
    first.start()
    assert inside.wait(5)
    assert sqlite_connection.has_unflushed_commits(), "still noted while its flush runs"
    sqlite_connection._note_unflushed_commit(path)  # a commit lands during that flush
    second_done = threading.Event()
    second = threading.Thread(target=lambda: (sqlite_connection.flush_unflushed_commits(), second_done.set()))
    second.start()
    assert not second_done.wait(0.2), "the second sender waits for the running flush"
    release.set()
    first.join(5)
    second.join(5)
    assert second_done.is_set()
    assert flushes == ["flush", "flush"], "the commit that landed during the first flush is flushed again"
    assert not sqlite_connection.has_unflushed_commits()


def test_on_macos_a_volume_that_refuses_the_full_flush_gets_fsync(monkeypatch):
    import fcntl

    calls: list[str] = []
    monkeypatch.setattr(sqlite_connection, "_on_macos", lambda: True)
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)

    def refusing(fd, command):
        calls.append("F_FULLFSYNC")
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(fcntl, "fcntl", refusing)
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append("fsync"))
    sqlite_connection._flush_descriptor(0)
    assert calls == ["F_FULLFSYNC", "fsync"]


def test_a_folder_that_cannot_be_flushed_is_reported_not_fatal(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=sqlite_connection.__name__):
        sqlite_connection._flush_folder(str(tmp_path / "missing"))
    assert "event=sqlite_folder_flush_failed" in caplog.text


def test_a_slow_commit_is_logged(tmp_path, monkeypatch, caplog):
    """With FULL the flush happens inside the commit, while the write lock is held."""

    monkeypatch.setattr(sqlite_connection, "SLOW_SQLITE_OPERATION_SECONDS", 0.0)
    path = str(tmp_path / "slow.db")
    with caplog.at_level(logging.WARNING, logger=sqlite_connection.__name__):
        with connect_sqlite(path) as conn:
            conn.execute("CREATE TABLE work (value TEXT)")
            conn.execute("INSERT INTO work VALUES ('a')")
            conn.commit()
    commits = [record for record in caplog.records if "event=sqlite_commit_slow" in record.getMessage()]
    assert len(commits) == 2  # the explicit commit and the block's own
    assert path in commits[0].getMessage()


# -- Every database the product writes gets the shared setup -------------------------------

_SQLITE_MODULES = {"sqlite3", "pysqlite3", "pysqlite3.dbapi2"}


def _sqlite_opens(source: str) -> list[tuple[int, bool]]:
    """Each direct SQLite open in a module: (line, whether it gets the shared setup or writes nothing).

    Follows the forms a module can use: ``import sqlite3 as db``, ``from sqlite3 import connect``,
    ``from pysqlite3 import dbapi2``, and a driver chosen at run time (``importlib.import_module``
    or ``driver = sqlite3``). An open is fine when its function calls
    ``configure_sqlite_connection``, opens read-only (``mode=ro`` with ``query_only``), or opens an
    in-memory database. Comments do not count.
    """

    import ast

    tree = ast.parse(source)
    modules: set[str] = set()
    connects: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SQLITE_MODULES:
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module in _SQLITE_MODULES:
            for alias in node.names:
                if alias.name == "connect":
                    connects.add(alias.asname or alias.name)
                elif alias.name == "dbapi2":
                    modules.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            loaded = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "import_module"
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and "sqlite" in str(value.args[0].value)
            )
            if loaded or (isinstance(value, ast.Name) and value.id in modules):
                modules.add(node.targets[0].id)

    def opens(call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in connects
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            target = func.value
            if isinstance(target, ast.Attribute) and target.attr == "dbapi2":
                target = target.value
            return isinstance(target, ast.Name) and target.id in modules
        return False

    def strings(scope: ast.AST) -> list[str]:
        return [n.value for n in ast.walk(scope) if isinstance(n, ast.Constant) and isinstance(n.value, str)]

    def calls_configure(scope: ast.AST) -> bool:
        for n in ast.walk(scope):
            if isinstance(n, ast.Call):
                name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                if name == "configure_sqlite_connection":
                    return True
        return False

    scopes: list[ast.AST] = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and opens(node)):
            continue
        scope = min(
            (s for s in scopes if s.lineno <= node.lineno <= (s.end_lineno or s.lineno)),
            key=lambda s: (s.end_lineno or s.lineno) - s.lineno,
            default=tree,
        )
        texts = strings(scope)
        read_only = any("mode=ro" in t for t in texts) and any("query_only" in t for t in texts)
        in_memory = bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == ":memory:"
        found.append((node.lineno, calls_configure(scope) or read_only or in_memory))
    return found


@pytest.mark.parametrize(
    ("source", "fine"),
    [
        ("import sqlite3 as db\ndef f(p):\n    return db.connect(p)\n", False),
        ("from sqlite3 import connect\ndef f(p):\n    return connect(p)\n", False),
        ("from pysqlite3 import dbapi2 as drv\ndef f(p):\n    return drv.connect(p)\n", False),
        ("import importlib\ndef f(p):\n    d = importlib.import_module('pysqlite3')\n    return d.connect(p)\n", False),
        (  # a comment naming the shared setup is not the shared setup
            "import sqlite3\ndef f(p):\n    # configure_sqlite_connection\n    return sqlite3.connect(p)\n",
            False,
        ),
        (
            "import sqlite3\nfrom storage import sqlite_connection\ndef f(p):\n    c = sqlite3.connect(p)\n"
            "    sqlite_connection.configure_sqlite_connection(c, db_path=p)\n    return c\n",
            True,
        ),
        (
            "import sqlite3\ndef f(p):\n    uri = f'{p}?mode=ro'\n    c = sqlite3.connect(uri, uri=True)\n"
            "    c.execute('PRAGMA query_only = ON')\n    return c\n",
            True,
        ),
        ("import sqlite3\ndef f():\n    driver = sqlite3\n    return driver.connect(':memory:')\n", True),
    ],
)
def test_the_open_check_finds_every_form(source, fine):
    assert [ok for _line, ok in _sqlite_opens(source)] == [fine]


def test_product_code_opens_sqlite_only_with_the_shared_setup():
    """Every database the product writes gets this module's setup (module docstring).

    Stores open SQLite through ``connect_sqlite``; any other open must call
    ``configure_sqlite_connection``, read only, or stay in memory. The files that open SQLite
    themselves are listed, so a new one is reviewed.
    """

    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.split()
    opening: dict[str, list[tuple[int, bool]]] = {}
    for name in tracked:
        if name.startswith("tests/") or name == "storage/sqlite_connection.py":
            continue
        found = _sqlite_opens((root / name).read_text(encoding="utf-8"))
        if found:
            opening[name] = found
    assert {name: [line for line, ok in found if not ok] for name, found in opening.items()} == {
        name: [] for name in opening
    }
    assert sorted(opening) == [
        "custom_agents/pinned_memory_source.py",
        "scripts/upgrade_schema_fixture.py",
        "secure_tool_broker/credential_catalog_store.py",
    ]


def test_a_lock_failure_names_the_process_also_without_a_caller_logger(tmp_path, caplog):
    path = _wal_database(tmp_path / "locked.db")
    holder = _exclusive_holder(path)
    try:
        with caplog.at_level(logging.ERROR, logger="storage.sqlite_connection"):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connect_sqlite(path, busy_timeout_ms=200)
    finally:
        release_exclusive_lock(holder)
    [line] = [r.getMessage() for r in caplog.records if "event=sqlite_journal_mode_check_locked" in r.getMessage()]
    assert f"database={path}" in line and f"pid={os.getpid()}" in line and "program=" in line


def test_the_process_label_names_the_program_and_subcommand_but_no_values(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/app/assistant_cli.py", "run-secure-tool-broker", "--config", "/data/x.yaml"])
    assert sqlite_connection._process_label() == "assistant_cli.py:run-secure-tool-broker"
    monkeypatch.setattr(sys, "argv", ["/app/strands_main.py", "--agent-type", "TeamLead"])
    assert sqlite_connection._process_label() == "strands_main.py"


def test_a_statement_runs_again_when_the_schema_changed_while_it_was_prepared(caplog):
    calls = []

    def execute(sql, parameters):
        calls.append(sql)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database schema has changed")
        return "result"

    with caplog.at_level(logging.WARNING, logger="storage.sqlite_connection"):
        result = sqlite_connection._execute_with_schema_retry(
            execute, "SELECT count(*) FROM sqlite_master", (), db_path="task_history.db"
        )
    assert result == "result" and len(calls) == 3
    assert caplog.text.count("event=sqlite_schema_changed_retry") == 2
    assert "statement=SELECT count(*) FROM sqlite_master" in caplog.text


def test_only_a_schema_change_is_retried_and_only_a_bounded_number_of_times():
    def always(message):
        def execute(_sql, _parameters):
            calls.append(message)
            raise sqlite3.OperationalError(message)

        return execute

    calls: list[str] = []
    with pytest.raises(sqlite3.OperationalError, match="schema has changed"):
        sqlite_connection._execute_with_schema_retry(always("database schema has changed"), "x", (), db_path="x.db")
    assert len(calls) == sqlite_connection.SCHEMA_CHANGED_ATTEMPTS
    calls.clear()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sqlite_connection._execute_with_schema_retry(always("database is locked"), "x", (), db_path="x.db")
    assert len(calls) == 1, "a lock keeps its own single bounded wait"
