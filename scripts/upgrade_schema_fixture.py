#!/usr/bin/env python3
"""Build the in-place upgrade fixture from schema captures, and list what a newer release changes.

A release upgrades an instance in place from every earlier release of its own
minor version and of the previous one, so the tests run on databases holding
the oldest form of every table the previous minor version created
(``--upgrade-from``, tests/upgrade_schemas.py). Capture each release's schema
by running its test suite with ``--capture-schema``, then:

    scripts/upgrade_schema_fixture.py merge tests/fixtures/upgrade/0.9-sqlite.sql 0.9.4.sql 0.9.11.sql
    scripts/upgrade_schema_fixture.py diff tests/fixtures/upgrade/0.9-sqlite.sql 0.10.sql

``merge`` keeps, for each table, the form of the oldest capture that created
it (list captures oldest first), with the columns that release added when a
store opened. ``diff`` lists what a newer capture adds, drops or changes in
those tables. ``CREATE ... IF NOT EXISTS`` creates a missing table, index or
trigger but never changes or removes one an older database already has, so
every other line ``diff`` prints needs an upgrade step (a new column:
``storage.table_columns.ensure_column``) or a note on why the older form is
harmless; ``diff`` then exits with status 1.

Capture a release from a run that passes, with its Cedar authorizer built
(``cargo build -p ua-cedar-authorizer``) so the access tests run:
``scripts/run_tests.sh fast --capture-schema FILE``. A release from before the
recorder (0.9) is captured in a checkout of its tag with
tests/upgrade_schemas.py and this script copied in:
``python -m pytest -p tests.upgrade_schemas -m "not live and not external"
--capture-schema FILE``.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage.sqlite_vec_support import load_sqlite_vec  # noqa: E402

HEADER = """-- Oldest form of every table, index and trigger the {label} releases created (SQLite),
-- with the columns they added when a store opened, for tests that start from an
-- earlier release's database (--upgrade-from, tests/upgrade_schemas.py). Built by
-- scripts/upgrade_schema_fixture.py merge from --capture-schema runs of: {sources}.
"""
VECTOR_NOTE = """-- vec0 takes each instance's embedding dimension; this is the first one a test created.
-- The general upgrade run leaves vec0 tables out, and a dedicated test loads them with a
-- stored embedding (tests/test_upgrade_schemas.py).
"""
SKIPPED = "-- skipped: "
_ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+\S+\s+ADD\s", re.IGNORECASE)


def read_statements(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line.removesuffix(";") for line in lines if line and not line.startswith("--")]


def skipped_tests(path: Path) -> list[str]:
    """The tests a capture's run skipped (tests/upgrade_schemas.py lists them first)."""

    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.removeprefix(SKIPPED) for line in lines if line.startswith(SKIPPED)]


def is_added_column(statement: str) -> bool:
    """``ALTER TABLE ... ADD [COLUMN]``: an older release's upgrade step, run when its store opens."""

    return bool(_ADD_COLUMN.match(statement.strip()))


def _replay_order(statement: str) -> int:
    # Tables, then the columns stores add to them, then indexes, triggers and
    # views: a store adds a column before indexing it, so an older database
    # holds both, whatever order the statements were first seen in.
    text = statement.lstrip().upper()
    if text.startswith(("CREATE TABLE", "CREATE VIRTUAL TABLE")):
        return 0
    return 1 if is_added_column(statement) else 2


def build(statements: list[str]) -> sqlite3.Connection:
    """Run the statements on a new database; the first form of each table wins."""

    # Match the memory store's extension-capable driver on macOS too.
    try:
        driver = importlib.import_module("pysqlite3")
    except ImportError:
        driver = sqlite3
    conn: sqlite3.Connection = driver.connect(":memory:")
    try:
        if any(is_vector_table(statement) for statement in statements):
            load_sqlite_vec(conn)
        for statement in sorted(statements, key=_replay_order):
            try:
                conn.execute(statement)
            except driver.OperationalError as exc:
                if not _expected_conflict(statement, str(exc)):
                    raise
    except BaseException:
        conn.close()
        raise
    return conn


def _expected_conflict(statement: str, message: str) -> bool:
    """Captures repeat definitions, add columns a table already has, and index
    columns a later definition added. No other failure is expected."""

    if re.fullmatch(r"(?:table|index|trigger|view) .+ already exists", message):
        return True
    if is_added_column(statement):
        # A column the table's definition already has, or an older layout's
        # table that only a test created and the release never creates.
        return message.startswith(("duplicate column name:", "no such table:"))
    text = " ".join(statement.upper().split())
    return text.startswith(("CREATE INDEX ", "CREATE UNIQUE INDEX ")) and message.startswith("no such column:")


def is_vector_table(statement: str) -> bool:
    """vec0 tables have a dimension chosen by each instance's embedding profile."""

    return bool(re.search(r"\bUSING\s+vec0\s*\(", statement, re.IGNORECASE))


def tables(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    virtual = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'")
    ]
    result = {}
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if any(name.startswith(table + "_") for table in virtual):
            continue  # A virtual table's own storage tables, which it creates itself.
        columns = {row[1]: row[2:6] for row in conn.execute(f'PRAGMA table_info("{name}")')}
        # Indexes a UNIQUE or PRIMARY KEY constraint creates have no statement
        # of their own; named indexes and triggers are compared by statement.
        constraints = {
            (bool(row[2]), tuple(info[2] for info in conn.execute(f'PRAGMA index_info("{row[1]}")')))
            for row in conn.execute(f'PRAGMA index_list("{name}")')
            if row[3] != "c"
        }
        extra = {
            object_name: (kind, object_sql)
            for kind, object_name, object_sql in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE type IN ('index', 'trigger') AND tbl_name = ? AND sql IS NOT NULL",
                (name,),
            )
        }
        result[name] = {"sql": sql, "columns": columns, "constraints": constraints, "extra": extra}
    return result


def _same_statement(old: str, new: str) -> bool:
    def normal(sql: str) -> str:
        return re.sub(r"\s*([(),;])\s*", r"\1", " ".join(sql.split()))

    return normal(old) == normal(new)


def merge(out: Path, captures: list[Path], label: str) -> None:
    merged: dict[str, dict[str, Any]] = {}
    for capture in captures:
        skipped = skipped_tests(capture)
        if skipped:
            print(
                f"warning: {capture.name}: {len(skipped)} test(s) were skipped in its run, "
                "so tables only they create are missing: " + ", ".join(skipped),
                file=sys.stderr,
            )
        with closing(build(read_statements(capture))) as conn:
            for name, table in tables(conn).items():
                merged.setdefault(name, table)
    lines = [HEADER.format(label=label, sources=", ".join(capture.name for capture in captures))]
    for name in sorted(merged):
        if is_vector_table(merged[name]["sql"]):
            lines.append(VECTOR_NOTE)
        lines.append(merged[name]["sql"] + ";\n")
        lines.extend(sql + ";\n" for _kind, sql in merged[name]["extra"].values())
    out.write_text("".join(lines), encoding="utf-8")
    print(f"{out}: {len(merged)} tables")


def diff(fixture: Path, capture: Path) -> int:
    """Print what the capture changes in the fixture's tables; 1 when something needs an upgrade step."""

    with closing(build(read_statements(fixture))) as conn:
        old = tables(conn)
    with closing(build(read_statements(capture))) as conn:
        new = tables(conn)
    steps = 0

    def needs_step(line: str) -> None:
        nonlocal steps
        steps += 1
        print(line)

    for name in sorted(old):
        if name not in new:
            needs_step(f"{name}: no longer created; upgraded databases keep it")
            continue
        o, n = old[name], new[name]
        if is_vector_table(o["sql"]) and not _same_statement(o["sql"], n["sql"]):
            # PRAGMA table_info omits vec0's column types and dimensions.
            needs_step(f"{name}: ~ virtual table definition {o['sql']} -> {n['sql']}")
        for column, spec in n["columns"].items():
            if column not in o["columns"]:
                needs_step(f"{name}: + column {column} {spec[0]} (not null={bool(spec[1])}, default={spec[2]})")
        for column in o["columns"]:
            if column not in n["columns"]:
                needs_step(f"{name}: - column {column}")
            elif o["columns"][column] != n["columns"][column]:
                needs_step(f"{name}: ~ column {column} {o['columns'][column]} -> {n['columns'][column]}")
        for unique, columns in sorted(n["constraints"] - o["constraints"]):
            needs_step(f"{name}: + {'unique ' if unique else ''}constraint {columns}")
        for unique, columns in sorted(o["constraints"] - n["constraints"]):
            needs_step(f"{name}: - {'unique ' if unique else ''}constraint {columns}")
        for object_name, (kind, sql) in n["extra"].items():
            if object_name not in o["extra"]:
                print(f"{name}: + {kind} {object_name} (created when the store opens)")
            elif not _same_statement(o["extra"][object_name][1], sql):
                needs_step(f"{name}: ~ {kind} {object_name} {o['extra'][object_name][1]} -> {sql}")
        for object_name, (kind, _sql) in o["extra"].items():
            if object_name not in n["extra"]:
                needs_step(f"{name}: - {kind} {object_name}; upgraded databases keep it")
    print("new tables:", ", ".join(sorted(set(new) - set(old))) or "none")
    print(f"{steps} change(s) to existing tables need an upgrade step, or a note on why the older form is harmless.")
    return 1 if steps else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    merge_cmd = commands.add_parser("merge", help="build the fixture from captures, oldest first")
    merge_cmd.add_argument("out", type=Path)
    merge_cmd.add_argument("captures", type=Path, nargs="+")
    merge_cmd.add_argument("--label", default="previous minor version's", help="releases named in the header")
    diff_cmd = commands.add_parser("diff", help="list what a newer capture changes in the fixture's tables")
    diff_cmd.add_argument("fixture", type=Path)
    diff_cmd.add_argument("capture", type=Path)
    args = parser.parse_args(argv)
    if args.command == "merge":
        merge(args.out, args.captures, args.label)
        return 0
    return diff(args.fixture, args.capture)


if __name__ == "__main__":
    sys.exit(main())
