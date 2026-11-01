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
it (list captures oldest first). ``diff`` lists the columns, indexes and
tables a newer capture adds, drops or changes: each added column on an
existing table needs its upgrade step (``storage.table_columns.ensure_column``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

HEADER = """-- Oldest form of every table, index and trigger the {label} releases created (SQLite),
-- for tests that start from an earlier release's database (--upgrade-from,
-- tests/upgrade_schemas.py). Built by scripts/upgrade_schema_fixture.py merge from
-- --capture-schema runs of: {sources}.
"""


def read_statements(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line.removesuffix(";") for line in lines if line and not line.startswith("--")]


def build(statements: list[str]) -> sqlite3.Connection:
    """Run the statements in order on a new database; the first form of each table wins."""

    conn = sqlite3.connect(":memory:")
    for statement in statements:
        try:
            conn.execute(statement)
        except sqlite3.Error:
            pass  # A later form of a table that already exists, or an index on a column it lacks.
    return conn


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
        indexes = {
            (bool(row[2]), tuple(info[2] for info in conn.execute(f'PRAGMA index_info("{row[1]}")')))
            for row in conn.execute(f'PRAGMA index_list("{name}")')
        }
        extra = [
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger') AND tbl_name = ? AND sql IS NOT NULL",
                (name,),
            )
        ]
        result[name] = {"sql": sql, "columns": columns, "indexes": indexes, "extra": extra}
    return result


def merge(out: Path, captures: list[Path], label: str) -> None:
    merged: dict[str, dict[str, Any]] = {}
    for capture in captures:
        for name, table in tables(build(read_statements(capture))).items():
            merged.setdefault(name, table)
    lines = [HEADER.format(label=label, sources=", ".join(capture.name for capture in captures))]
    for name in sorted(merged):
        lines.append(merged[name]["sql"] + ";\n")
        lines.extend(statement + ";\n" for statement in merged[name]["extra"])
    out.write_text("".join(lines), encoding="utf-8")
    print(f"{out}: {len(merged)} tables")


def diff(fixture: Path, capture: Path) -> int:
    old = tables(build(read_statements(fixture)))
    new = tables(build(read_statements(capture)))
    added_columns = 0
    for name in sorted(old):
        if name not in new:
            print(f"{name}: no longer created")
            continue
        o, n = old[name], new[name]
        for column, spec in n["columns"].items():
            if column not in o["columns"]:
                added_columns += 1
                print(f"{name}: + column {column} {spec[0]} (not null={bool(spec[1])}, default={spec[2]})")
        for column in o["columns"]:
            if column not in n["columns"]:
                print(f"{name}: - column {column}")
            elif o["columns"][column] != n["columns"][column]:
                print(f"{name}: ~ column {column} {o['columns'][column]} -> {n['columns'][column]}")
        for unique, columns in sorted(n["indexes"] - o["indexes"]):
            print(f"{name}: + {'unique ' if unique else ''}index {columns}")
        for unique, columns in sorted(o["indexes"] - n["indexes"]):
            print(f"{name}: - {'unique ' if unique else ''}index {columns}")
    print("new tables:", ", ".join(sorted(set(new) - set(old))) or "none")
    print(f"{added_columns} column(s) added to existing tables; each needs an upgrade step.")
    return 0


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
