from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

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
            {"user": "ua", "password": "***", "dbname": "ua", "host": "db"},
        ),
        (
            "host=db password='a s3cret b' sslmode=require",
            {"password": "***", "host": "db", "sslmode": "require"},
        ),
        (
            "host=db port=5432 sslpassword=s3cret",
            {"host": "db", "port": "5432", "sslpassword": "***"},
        ),
        (
            "host=db passfile='/run/s3cret'",
            {"host": "db", "passfile": "***"},
        ),
        (
            "host=db user=ua password=s3cret application_name=https://example.com",
            {"host": "db", "user": "ua", "password": "***", "application_name": "https://example.com"},
        ),
        (
            "host=db password='s3cret://tail'",
            {"host": "db", "password": "***"},
        ),
        (
            "postgresql://ua:s3cret@db:5432/ua",
            {"user": "ua", "password": "***", "host": "db", "port": "5432", "dbname": "ua"},
        ),
        (
            "postgres://ua:s3cret@db/ua",
            {"user": "ua", "password": "***", "host": "db", "dbname": "ua"},
        ),
        (
            "postgresql://ua:s3cret?tail@db/ua",
            {"user": "ua", "password": "***", "host": "db", "dbname": "ua"},
        ),
        (
            "postgresql://ua:s3cret#tail@db/ua",
            {"user": "ua", "password": "***", "host": "db", "dbname": "ua"},
        ),
        (
            "postgresql://u%40a:s3cret%3Ftail@db/ua",
            {"user": "u@a", "password": "***", "host": "db", "dbname": "ua"},
        ),
        (
            "postgresql://ua:s3cret@[::1]:5432,db:5433/ua",
            {"user": "ua", "password": "***", "host": "::1,db", "port": "5432,5433", "dbname": "ua"},
        ),
        (
            "postgresql://db/ua?password=s3cret",
            {"password": "***", "host": "db", "dbname": "ua"},
        ),
        (
            "postgresql://ua@db/ua?sslmode=require&sslpassword=s3cret&passfile=/run/s3cret",
            {"user": "ua", "host": "db", "dbname": "ua", "sslmode": "require", "sslpassword": "***", "passfile": "***"},
        ),
    ],
)
def test_redaction_hides_secrets_in_both_connection_string_forms(dsn: str, expected: dict[str, str]) -> None:
    redacted = redact_postgres_dsn(dsn)

    assert SECRET not in redacted
    assert conninfo_to_dict(redacted) == expected


@pytest.mark.parametrize(
    "dsn",
    [
        # libpq rejects the stray word, so the string cannot be split into settings.
        "host=db password=s3cret garbage",
        # An unclosed IPv6 bracket makes the URL unreadable.
        "postgresql://ua:s3cret@[::1/ua",
        # SQLAlchemy's scheme is not a libpq connection string.
        "postgresql+psycopg://ua:s3cret@db/ua",
        "postgresql://ua:s3cret@db/ua?unknown_setting=s3cret",
        "host=db password='s3cret://tail",
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
