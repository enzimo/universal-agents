from __future__ import annotations

import gc
import logging
import os
import sqlite3
import sys
import threading
import time

import pytest

from storage import sqlite_connection
from storage.read_only import read_only_repositories
from storage.sqlite_connection import (
    connect_sqlite,
    ensure_sqlite_column,
    prepare_sqlite_database,
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


def test_synchronous_normal_comes_before_the_wal_switch(tmp_path):
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

    path = str(tmp_path / "fresh.db")
    raw = sqlite3.connect(path)
    try:
        recording = _Recording(raw)
        sqlite_connection.configure_sqlite_connection(recording, db_path=path)
    finally:
        raw.close()
    # The new file's switch then needs one disk flush fewer while it holds the file's locks.
    assert recording.statements.index("PRAGMA synchronous=NORMAL") < recording.statements.index(
        "PRAGMA journal_mode=WAL"
    )


def test_a_lock_failure_names_the_process_also_without_a_caller_logger(tmp_path, caplog):
    path = _wal_database(tmp_path / "locked.db")
    holder = _exclusive_holder(path)
    try:
        with caplog.at_level(logging.ERROR, logger="storage.sqlite_connection"):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connect_sqlite(path, busy_timeout_ms=200)
    finally:
        holder.execute("COMMIT")
        holder.close()
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
