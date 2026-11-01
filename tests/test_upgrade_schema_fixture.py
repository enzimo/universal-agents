"""Fixture generation must retain every supported table and reject bad captures."""

from contextlib import closing

import pytest

from scripts.upgrade_schema_fixture import build, diff, merge, read_statements, tables

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
