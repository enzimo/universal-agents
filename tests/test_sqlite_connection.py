from __future__ import annotations

import gc
import logging
import os
import sqlite3
import threading
import time

import pytest

from storage import sqlite_connection
from storage.sqlite_connection import (
    connect_sqlite,
    ensure_sqlite_column,
)


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
    """Hold the exclusive file lock, as a close-time checkpoint in another process does."""

    # Exclusive locking mode cannot start while any connection to the file is
    # open, so the idle connection this process holds (_hold_open) is closed:
    # the lock stands for another process that has none of ours.
    sqlite_connection.close_held_open_databases()
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO work VALUES ('held')")
    return holder


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
    timer = threading.Timer(0.3, lambda: (holder.execute("COMMIT"), holder.close()))
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
        holder.execute("COMMIT")
        holder.close()
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
    with caplog.at_level(logging.WARNING, logger="storage.sqlite_connection"):
        with connect_sqlite(str(tmp_path / "fast.db")) as conn:
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
