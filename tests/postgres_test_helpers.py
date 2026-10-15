from __future__ import annotations


class FakePostgresCursor:
    def __init__(self) -> None:
        self.description = None
        self.rowcount = 1

    def execute(self, query, params=None) -> None:
        if "SELECT 1" in query:
            self.description = [("?column?",)]
        else:
            self.description = None

    def fetchone(self):
        if self.description:
            return (1,)
        return None

    def fetchall(self):
        return []


class FakePostgresConnection:
    def cursor(self):
        return FakePostgresCursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def patch_fake_postgres(monkeypatch) -> None:
    monkeypatch.setattr("storage.postgres_connection._connect_psycopg", lambda dsn: FakePostgresConnection())
