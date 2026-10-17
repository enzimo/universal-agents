from __future__ import annotations

import pytest

from storage.postgres_connection import (
    UNREADABLE_POSTGRES_DSN,
    PostgresCursor,
    redact_postgres_dsn,
)


class _MappingCursor:
    description = [("first",), ("second",)]
    rowcount = 1

    def fetchone(self):
        return {"first": "a", "second": "b"}

    def fetchall(self):
        return [{"first": "a", "second": "b"}]


def test_postgres_connection_cursor_supports_mapping_rows() -> None:
    cursor = PostgresCursor(_MappingCursor())
    row = cursor.fetchone()
    assert row is not None
    assert row["first"] == "a"
    assert row[1] == "b"


# Every connection string below carries the secret "s3cret" somewhere libpq
# would read it; none of it may reach a log line or error message.
SECRET = "s3cret"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "host=db user=ua password=s3cret dbname=ua",
            "user=ua password=*** dbname=ua host=db",
        ),
        ("host=db password='a s3cret b' sslmode=require", "password=*** host=db sslmode=require"),
        ("host=db port=5432 sslpassword=s3cret", "host=db port=5432 sslpassword=***"),
        ("postgresql://ua:s3cret@db:5432/ua", "postgresql://ua:***@db:5432/ua"),
        ("postgresql+psycopg://ua:s3cret@db/ua", "postgresql+psycopg://ua:***@db/ua"),
        ("postgresql://db/ua?password=s3cret", "postgresql://db/ua?password=***"),
        (
            "postgresql://ua@db/ua?sslmode=require&sslpassword=s3cret&passfile=/run/s3cret",
            "postgresql://ua@db/ua?sslmode=require&sslpassword=***&passfile=***",
        ),
    ],
)
def test_redaction_hides_secrets_in_both_connection_string_forms(dsn: str, expected: str) -> None:
    redacted = redact_postgres_dsn(dsn)

    assert redacted == expected
    assert SECRET not in redacted


@pytest.mark.parametrize(
    "dsn",
    [
        # libpq rejects the stray word, so the string cannot be split into settings.
        "host=db password=s3cret garbage",
        # An unclosed IPv6 bracket makes the URL unreadable.
        "postgresql://ua:s3cret@[::1/ua",
    ],
)
def test_an_unreadable_connection_string_is_replaced_as_a_whole(dsn: str) -> None:
    assert redact_postgres_dsn(dsn) == UNREADABLE_POSTGRES_DSN


def test_settings_nobody_listed_are_hidden() -> None:
    # A setting outside the shown list is hidden even when it holds no secret,
    # so a future secret-carrying setting is safe by default.
    assert redact_postgres_dsn("host=db options='-c search_path=s3cret'") == "host=db options=***"


def test_an_empty_connection_string_stays_empty() -> None:
    assert redact_postgres_dsn("") == ""
