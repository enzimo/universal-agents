"""Fixture generation must retain every supported table and reject bad captures."""

from contextlib import closing

import pytest

from scripts.upgrade_schema_fixture import build, diff, merge, read_statements, tables
from tests.upgrade_schemas import read_schema_statements

VECTOR = "CREATE VIRTUAL TABLE memory_graph_vectors USING vec0(record_id TEXT, embedding float[3])"


def test_merge_retains_vector_schema_and_diff_reports_it(tmp_path, capsys):
    capture = tmp_path / "capture.sql"
    capture.write_text("CREATE TABLE ordinary (value TEXT);\n" + VECTOR + ";\n")
    fixture = tmp_path / "fixture.sql"
    merge(fixture, [capture], "test")
    with closing(build(read_statements(fixture))) as conn:
        assert set(tables(conn)) == {"ordinary", "memory_graph_vectors"}
    old = tmp_path / "old.sql"
    old.write_text("CREATE TABLE ordinary (value TEXT);\n")
    diff(old, capture)
    assert "new tables: memory_graph_vectors" in capsys.readouterr().out
    old.write_text(capture.read_text().replace("float[3]", "float[2]"))
    diff(old, capture)
    assert "memory_graph_vectors: ~ virtual table definition" in capsys.readouterr().out


@pytest.mark.parametrize(("statement", "message"), [
    ("CREATE VIRTUAL TABLE missing USING unknown_extension(value)", "no such module: unknown_extension"),
    ("CREATE TABLE broken (not valid sql)", "syntax error"),
    ("CREATE INDEX missing_table ON absent(value)", "no such table"),
])
def test_unexpected_replay_failure_does_not_publish_incomplete_fixture(tmp_path, statement, message):
    capture = tmp_path / "capture.sql"
    capture.write_text("CREATE TABLE ordinary (value TEXT);\n" + statement + ";\n")
    fixture = tmp_path / "fixture.sql"
    fixture.write_text("previous fixture\n")
    with pytest.raises(Exception, match=message):
        merge(fixture, [capture], "test")
    assert fixture.read_text() == "previous fixture\n"


def test_merge_keeps_oldest_table_and_only_ignores_expected_conflicts(tmp_path):
    capture = tmp_path / "capture.sql"
    capture.write_text(
        "CREATE TABLE saved (value TEXT);\n"
        "CREATE TABLE saved (value TEXT, added TEXT);\n"
        "CREATE INDEX newer_index ON saved(added);\n"
        "CREATE INDEX value_index ON saved(value);\n"
    )
    fixture = tmp_path / "fixture.sql"
    merge(fixture, [capture], "test")
    with closing(build(read_statements(fixture))) as conn:
        assert list(tables(conn)["saved"]["columns"]) == ["value"]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall() == [("value_index",)]


def test_merge_gives_each_table_the_columns_its_release_added_when_a_store_opened(tmp_path, capsys):
    capture = tmp_path / "0.9.4.sql"
    capture.write_text(
        "-- skipped: tests/test_access.py::needs_cedar\n"
        # First seen before the column it indexes was added: replay still adds
        # the column first, as the store did before indexing it.
        "CREATE INDEX IF NOT EXISTS saved_added ON saved(added);\n"
        "CREATE TABLE IF NOT EXISTS saved (value TEXT);\n"
        "ALTER TABLE saved ADD COLUMN added TEXT;\n"
        "ALTER TABLE saved ADD COLUMN value TEXT;\n"  # A column the table already has.
        "ALTER TABLE test_built_layout ADD COLUMN other TEXT;\n"  # A table only a test created.
    )
    fixture = tmp_path / "fixture.sql"
    merge(fixture, [capture], "test")

    assert "1 test(s) were skipped" in capsys.readouterr().err
    with closing(build(read_statements(fixture))) as conn:
        saved = tables(conn)["saved"]
        assert list(saved["columns"]) == ["value", "added"]
        assert list(saved["extra"]) == ["saved_added"]
    # The fixture holds the resulting tables; the seed runs them in order.
    assert not any(statement.startswith("ALTER") for statement in read_schema_statements(fixture))


TABLE = "CREATE TABLE saved (value TEXT, state TEXT);\n"


@pytest.mark.parametrize(
    ("old", "new", "reported"),
    [
        (  # IF NOT EXISTS keeps the older condition.
            TABLE + "CREATE INDEX open_saved ON saved(value) WHERE state = 'open';\n",
            TABLE + "CREATE INDEX open_saved ON saved(value) WHERE state IN ('open', 'waiting');\n",
            "saved: ~ index open_saved",
        ),
        (  # IF NOT EXISTS keeps the older body.
            TABLE + "CREATE TRIGGER stamp AFTER INSERT ON saved BEGIN UPDATE saved SET state = 'new'; END;\n",
            TABLE + "CREATE TRIGGER stamp AFTER INSERT ON saved BEGIN UPDATE saved SET state = 'open'; END;\n",
            "saved: ~ trigger stamp",
        ),
        (  # Upgraded databases keep firing it.
            TABLE + "CREATE TRIGGER stamp AFTER INSERT ON saved BEGIN UPDATE saved SET state = 'new'; END;\n",
            TABLE,
            "saved: - trigger stamp",
        ),
        (TABLE, "CREATE TABLE saved (value TEXT, state TEXT, added TEXT);\n", "saved: + column added"),
        (TABLE, "CREATE TABLE saved (value TEXT, state TEXT, UNIQUE (value));\n", "saved: + unique constraint"),
    ],
    ids=["index-condition", "trigger-body", "removed-trigger", "added-column", "added-unique-constraint"],
)
def test_diff_reports_what_opening_an_older_database_does_not_change(tmp_path, capsys, old, new, reported):
    (tmp_path / "old.sql").write_text(old)
    (tmp_path / "new.sql").write_text(new)

    assert diff(tmp_path / "old.sql", tmp_path / "new.sql") == 1
    assert reported in capsys.readouterr().out


def test_diff_passes_what_opening_an_older_database_creates(tmp_path, capsys):
    (tmp_path / "old.sql").write_text(TABLE + "CREATE INDEX saved_value ON saved(value);\n")
    (tmp_path / "new.sql").write_text(
        TABLE
        + "CREATE INDEX saved_value ON saved( value );\n"  # Only spacing differs.
        + "CREATE INDEX saved_state ON saved(state);\n"
        + "CREATE TABLE added (value TEXT);\n"
    )

    assert diff(tmp_path / "old.sql", tmp_path / "new.sql") == 0
    out = capsys.readouterr().out
    assert "saved: + index saved_state (created when the store opens)" in out
    assert "new tables: added" in out
    assert "saved_value" not in out
