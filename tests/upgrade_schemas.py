"""Run the test suite on databases an earlier release created (``--upgrade-from``).

A release upgrades an instance in place from every earlier release of its own
minor version and of the previous one (AGENTS.md, "Change Process"), so every
store has to open the tables those releases created and add what they lack.
With ``--upgrade-from tests/fixtures/upgrade/0.9-sqlite.sql`` each new SQLite
database a store creates starts with the oldest form of every table a 0.9
release created, so a store that misses an upgrade step fails the tests that
use it. Dimension-specific vec0 tables are checked separately with a stored
embedding and the complete historical fixture (tests/test_upgrade_schemas.py).
Both stdlib sqlite3 and the memory store's macOS pysqlite3 driver are hooked.
Tests that spawn workers prepare their shared store in the parent first:
spawned interpreters do not inherit pytest's hooks. Databases a test creates itself keep the tables the test builds, and
so do all databases of a test marked ``own_database``. Release candidates run the suite this way (``scripts/run_tests.sh
upgrade``).

``--capture-schema PATH`` records the tables, indexes and triggers product
code creates while the suite runs, one statement per line; tests' own tables
are left out. ``scripts/upgrade_schema_fixture.py`` merges the captures of a
minor version's releases into the fixture and lists what a newer release
changes in them.
"""

from __future__ import annotations

import os
import importlib
import sqlite3
import sys
import threading
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from scripts.upgrade_schema_fixture import is_vector_table

_REPO = Path(__file__).resolve().parents[1]
_TESTS = str(_REPO / "tests") + os.sep
_SCHEMA_PREFIXES = (
    "CREATE TABLE",
    "CREATE UNIQUE INDEX",
    "CREATE INDEX",
    "CREATE VIRTUAL TABLE",
    "CREATE TRIGGER",
    "CREATE VIEW",
)
_original_connect = sqlite3.connect

try:
    pysqlite3 = importlib.import_module("pysqlite3")
except ImportError:
    _DRIVERS: tuple[Any, ...]
    _DRIVERS = (sqlite3,)
else:
    _DRIVERS = (sqlite3, pysqlite3)
_ORIGINAL_CONNECTS = {driver: driver.connect for driver in _DRIVERS}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("upgrade", "in-place upgrade checks")
    group.addoption(
        "--upgrade-from",
        metavar="PATH",
        help="Start every new SQLite database with an earlier release's tables (tests/fixtures/upgrade).",
    )
    group.addoption(
        "--capture-schema",
        metavar="PATH",
        help="Write the SQLite schema statements product code runs to PATH.",
    )


def pytest_configure(config: pytest.Config) -> None:
    seed_path = config.getoption("--upgrade-from")
    capture_path = config.getoption("--capture-schema")
    if not seed_path and not capture_path:
        return
    schemas = _Schemas(
        seed=read_schema_statements(Path(seed_path)) if seed_path else (),
        capture=Path(capture_path) if capture_path else None,
    )
    config.stash[_SCHEMAS] = schemas
    for driver in _DRIVERS:
        driver.connect = partial(schemas.connect, _driver=driver)


def pytest_runtest_setup(item: pytest.Item) -> None:
    schemas = item.config.stash.get(_SCHEMAS, None)
    if schemas is not None:
        schemas.paused = item.get_closest_marker("own_database") is not None


def pytest_unconfigure(config: pytest.Config) -> None:
    schemas = config.stash.get(_SCHEMAS, None)
    if schemas is None:
        return
    for driver in _DRIVERS:
        driver.connect = _ORIGINAL_CONNECTS[driver]
    schemas.finish()


def read_schema_statements(path: Path) -> tuple[str, ...]:
    """The statements of a schema file: one per line, ``--`` lines are comments."""

    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return tuple(line.removesuffix(";") for line in lines if line and not line.startswith("--"))


class _Schemas:
    def __init__(self, *, seed: tuple[str, ...], capture: Path | None):
        # A vec0 table's dimension varies across tests. The full historical
        # fixture, including stored vectors, is exercised by its dedicated test
        # rather than forcing one embedding dimension on every test database.
        self._seed = tuple(statement for statement in seed if not is_vector_table(statement))
        self._capture = capture
        self._captured: dict[str, None] = {}
        self._kinds: dict[str, str] = {}
        self._lock = threading.Lock()
        # Set for a test marked own_database, which builds an older instance's
        # database through a store and must keep exactly what it builds.
        self.paused = False

    def connect(self, *args: Any, _driver: Any = sqlite3, **kwargs: Any) -> sqlite3.Connection:
        target = args[0] if args else kwargs.get("database")
        # Only a database a store creates: a test that builds its own tables
        # (an older layout, or none) keeps them.
        new = bool(self._seed) and not self.paused and _is_new_database(target) and self._product_caller()
        conn: sqlite3.Connection = _ORIGINAL_CONNECTS[_driver](*args, **kwargs)
        if new:
            try:
                self._start_with_older_tables(conn, target, _driver)
            except BaseException:
                conn.close()
                raise
        if self._capture is not None:
            conn.set_trace_callback(self._record)
        return conn

    def _start_with_older_tables(self, conn: sqlite3.Connection, target: object, driver: Any) -> None:
        """Create the earlier release's tables in one transaction; a statement that fails stops the run."""

        isolation = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Another thread may have initialized this file since the earlier
            # size check. Recheck while holding the write lock before seeding.
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone():
                for statement in self._seed:
                    conn.execute(statement)
            conn.execute("COMMIT")
        except driver.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise pytest.UsageError(f"--upgrade-from could not create its tables in {target!r}: {exc}") from exc
        finally:
            conn.isolation_level = isolation

    def _record(self, statement: str) -> None:
        text = " ".join(statement.split())
        if not text.upper().startswith(_SCHEMA_PREFIXES) or not self._product_caller():
            return
        with self._lock:
            self._captured.setdefault(text, None)

    def _product_caller(self) -> bool:
        """Whether product code made the call (a connection or a statement), rather than a test.

        The first repository frame outside the shared connection modules
        decides: under ``tests/`` it is a test's own table.
        """

        frame: FrameType | None = sys._getframe(2)
        while frame is not None:
            kind = self._kinds.get(frame.f_code.co_filename)
            if kind is None:
                kind = self._kinds[frame.f_code.co_filename] = _frame_kind(frame.f_code.co_filename)
            if kind != _OTHER:
                return kind == _PRODUCT
            frame = frame.f_back
        return False

    def finish(self) -> None:
        if self._capture is None:
            return
        with self._lock:
            text = "".join(f"{statement};\n" for statement in self._captured)
        self._capture.write_text(text, encoding="utf-8")


_PRODUCT, _TEST, _OTHER = "product", "test", "other"
# Connection wrappers and fixture tooling do not own runtime schemas. Their
# caller decides ownership, so tests of the capture utility cannot pollute a
# later release's capture with the utility tests' invented tables.
_CONNECTION_MODULES = {
    str(_REPO / "storage" / "sqlite_connection.py"),
    str(_REPO / "storage" / "postgres_connection.py"),
    str(_REPO / "scripts" / "upgrade_schema_fixture.py"),
}


def _frame_kind(filename: str) -> str:
    path = os.path.realpath(filename)
    if path in _CONNECTION_MODULES or not path.startswith(str(_REPO) + os.sep) or f"{os.sep}.venv{os.sep}" in path:
        return _OTHER
    return _TEST if path.startswith(_TESTS) else _PRODUCT


def _is_new_database(target: object) -> bool:
    if isinstance(target, (bytes, os.PathLike)):
        target = os.fsdecode(target)
    if not isinstance(target, str):
        return False
    if target in ("", ":memory:"):
        return True
    if target.startswith("file:"):
        return False
    return not os.path.exists(target) or os.path.getsize(target) == 0


_SCHEMAS = pytest.StashKey[_Schemas]()
