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
Tests that spawn workers prepare their shared store in the parent first when
these hooks are on (``connection_hooks_active``): spawned interpreters do not
inherit pytest's hooks. Databases a test creates itself keep the tables the test builds, and
so do all databases of a test marked ``own_database``. Release candidates run the suite this way (``scripts/run_tests.sh
upgrade``).

``--capture-schema PATH`` records the tables, indexes and triggers product
code creates while the suite runs, and the columns it adds to existing tables,
one statement per line; tests' own tables are left out. A capture is saved
only from a run that passed, because tables only failed or unrun tests create
would be missing from it. It starts with the release it records (``VERSION``)
and the tests the run skipped: their tables are missing too (the access tests
skip until the Cedar authorizer is built). ``scripts/upgrade_schema_fixture.py``
merges the captures of a minor version's releases into the fixture and lists
what a newer release changes in them.

Product code that catches every error could hide a failure to create the older
tables, so such a failure also fails the run, after the test that met it.
"""

from __future__ import annotations

import os
import importlib
import re
import sqlite3
import sys
import threading
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from scripts.upgrade_schema_fixture import RELEASE, SKIPPED, is_added_column, is_vector_table

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
    # Its hooks follow the run's outcome, which decides whether the capture is saved.
    config.pluginmanager.register(schemas, "upgrade_schemas")
    for driver in _DRIVERS:
        driver.connect = partial(schemas.connect, _driver=driver)


def pytest_runtest_setup(item: pytest.Item) -> None:
    schemas = item.config.stash.get(_SCHEMAS, None)
    if schemas is not None:
        schemas.paused = item.get_closest_marker("own_database") is not None
        schemas.test = item.nodeid


def connection_hooks_active(config: pytest.Config) -> bool:
    """Whether new SQLite connections are seeded or recorded (--upgrade-from, --capture-schema).

    Spawned interpreters do not inherit these hooks, so a store that only
    worker processes would create is prepared in the parent while they are on.
    """

    return config.stash.get(_SCHEMAS, None) is not None


def previous_minor_fixture() -> Path:
    """The fixture the upgrade run starts from: ``UPGRADE_FROM`` in scripts/run_tests.sh, the one place it is named."""

    script = (_REPO / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
    match = re.search(r'^UPGRADE_FROM="([^"]+)"$', script, re.MULTILINE)
    if match is None:
        raise RuntimeError("scripts/run_tests.sh no longer sets UPGRADE_FROM")
    return _REPO / match.group(1)


def pytest_unconfigure(config: pytest.Config) -> None:
    schemas = config.stash.get(_SCHEMAS, None)
    if schemas is None:
        return
    for driver in _DRIVERS:
        driver.connect = _ORIGINAL_CONNECTS[driver]


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
        self._failed: dict[str, None] = {}
        self._skipped: dict[str, None] = {}
        self._seed_errors: list[str] = []
        self._outcome: list[tuple[str, str]] = []  # Lines for the terminal summary.
        self.test = ""  # The running test, named in a seeding failure.
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
            message = f"--upgrade-from could not create its tables in {target!r}: {exc}"
            with self._lock:
                self._seed_errors.append(f"{self.test or 'outside a test'}: {message}")
            raise pytest.UsageError(message) from exc
        finally:
            conn.isolation_level = isolation

    def _record(self, statement: str) -> None:
        text = " ".join(statement.split())
        schema = text.upper().startswith(_SCHEMA_PREFIXES) or is_added_column(text)
        if not schema or not self._product_caller():
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

    # pytest hooks, registered in pytest_configure. A test that fails, or never
    # runs, may be the only one that creates a table.
    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self._note(report)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._note(report)

    def _note(self, report: pytest.CollectReport | pytest.TestReport) -> None:
        if report.failed:
            self._failed.setdefault(report.nodeid, None)
        elif report.skipped and not hasattr(report, "wasxfail"):
            self._skipped.setdefault(report.nodeid, None)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self._seed_errors:
            # The test may still have passed, if product code caught the error.
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            self._outcome.append(
                ("red", f"--upgrade-from failed {len(self._seed_errors)} time(s); the first: {self._seed_errors[0]}")
            )
        self.finish(passed=exitstatus == 0 and not self._failed and not self._seed_errors)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        for color, message in self._outcome:
            terminalreporter.write_line(message, **{color: True, "bold": True})

    def finish(self, *, passed: bool) -> None:
        """Save the capture from a run that passed, listing the tests it skipped first."""

        if self._capture is None:
            return
        if not passed:
            failed = list(self._failed)
            reason = f"{len(failed)} failed, the first {failed[0]}" if failed else "it stopped early"
            self._outcome.append((
                "red",
                f"--capture-schema: not saved, because the run did not pass ({reason}). "
                "Tables only the failed or unrun tests create would be missing from it.",
            ))
            return
        with self._lock:
            statements = "".join(f"{statement};\n" for statement in self._captured)
        release = (_REPO / "VERSION").read_text(encoding="utf-8").strip()
        skipped = "".join(f"{SKIPPED}{nodeid}\n" for nodeid in self._skipped)
        self._capture.write_text(f"{RELEASE}{release}\n" + skipped + statements, encoding="utf-8")
        if self._skipped:
            self._outcome.append((
                "yellow",
                f"--capture-schema: {len(self._skipped)} skipped test(s) are listed at the top of {self._capture}. "
                "Tables only they create are missing from it.",
            ))


_PRODUCT, _TEST, _OTHER = "product", "test", "other"
# Connection wrappers and fixture tooling do not own runtime schemas. Their
# caller decides ownership, so tests of the capture utility cannot pollute a
# later release's capture with the utility tests' invented tables.
_CONNECTION_MODULES = {
    str(_REPO / "storage" / "sqlite_connection.py"),
    str(_REPO / "storage" / "postgres_connection.py"),
    str(_REPO / "scripts" / "upgrade_schema_fixture.py"),
    # Wraps sqlite3.connect to find opens on the event loop; the store that
    # called it decides, or seeding would silently skip its databases.
    str(_REPO / "tests" / "loop_storage_support.py"),
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
