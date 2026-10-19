"""Native PostgreSQL connections and repository row decoding."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import uuid4
from types import TracebackType
from .sql_dialect import POSTGRES

# Transaction-level advisory lock keys (``pg_advisory_xact_lock``), one per
# store that serializes its own writes in PostgreSQL the way ``BEGIN
# IMMEDIATE`` does in SQLite. A store's read-then-write steps (a uniqueness
# check before an insert, a read-modify-write of a JSON record) are only
# exact when its writers take turns; PostgreSQL's default READ COMMITTED
# isolation does not make them take turns. Each store has its own key, so
# unrelated stores never wait for each other. Keys must stay distinct.
KNOWLEDGE_WRITE_LOCK_KEY = 7_302_211_551
ACCESS_STORE_WRITE_LOCK_KEY = 7_302_211_552
# Creating the change ledger's tables (storage.change_ledger), from every store that uses them.
CHANGE_LEDGER_SCHEMA_LOCK_KEY = 7_302_211_553


def ensure_storage_parent_dir(path: Optional[str]) -> None:
    raw = str(path or "").strip()
    if not raw:
        return
    if "://" in raw:
        return
    directory = Path(raw).expanduser().resolve().parent
    directory.mkdir(parents=True, exist_ok=True)


# Connection settings that may appear in logs and error messages. Every other
# setting is hidden, so a setting that carries a secret (``password``,
# ``sslpassword``, or one a later libpq adds) never shows just because nobody
# listed it here.
_SHOWN_POSTGRES_SETTINGS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "sslmode",
        "connect_timeout",
        "application_name",
        "target_session_attrs",
    }
)
_HIDDEN_VALUE = "***"
# Shown instead of a connection string that cannot be read, because reading it
# is what tells the redaction which parts are secret.
UNREADABLE_POSTGRES_DSN = "<unreadable PostgreSQL connection string>"


def redact_postgres_dsn(raw_dsn: str) -> str:
    """The connection string with its password and other settings hidden, for logs and errors.

    libpq accepts two forms and parses both here: a URL
    (``postgresql://user:password@host/db?sslmode=require``) and keyword settings
    (``host=db user=ua password=...``). Both become redacted keyword settings.
    The user, host, port and database stay readable; an invalid connection
    string is replaced as a whole.
    """

    if not raw_dsn:
        return raw_dsn
    # URL-valued keyword settings and URI passwords containing ? or # are valid
    # in libpq. Generic URL parsing interprets them differently and can log the
    # password, so use the connection's parser for both forms before formatting.
    from psycopg import Error as PsycopgError
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        settings = conninfo_to_dict(raw_dsn)
        return make_conninfo(
            "",
            **{
                name: value if name in _SHOWN_POSTGRES_SETTINGS else _HIDDEN_VALUE
                for name, value in settings.items()
            },
        )
    except PsycopgError:
        return UNREADABLE_POSTGRES_DSN


# A deployment that runs its own PostgreSQL (the Docker bundle) names that
# server here as host:port. Every storage resolver then points the manager's and
# gateway's connection strings at it in memory, whatever host the saved
# instance_config.yaml or POSTGRES_DSN names; the saved file is never rewritten.
# Holds no secret: user, password and database stay in the saved setting or
# POSTGRES_DSN.
BUNDLED_POSTGRES_ADDRESS_ENV = "TEAM_BUNDLED_POSTGRES_ADDRESS"
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+psycopg"})


def postgres_dsn_at(raw_dsn: str, *, host: str, port: int) -> str:
    """The same connection string pointed at ``host:port`` (user, password, database and options kept)."""

    parts = urlsplit(str(raw_dsn or "").strip())
    if parts.scheme not in _POSTGRES_SCHEMES:
        raise ValueError(f"Unsupported Postgres DSN scheme: {redact_postgres_dsn(str(raw_dsn or ''))!r}")
    if not parts.path.lstrip("/"):
        raise ValueError("The Postgres DSN must include a database name.")
    netloc = host
    if parts.username is not None:
        username = quote(unquote(parts.username), safe="")
        if parts.password is not None:
            netloc = f"{username}:{quote(unquote(parts.password), safe='')}@{netloc}"
        else:
            netloc = f"{username}@{netloc}"
    return urlunsplit((parts.scheme, f"{netloc}:{port}", parts.path, parts.query, parts.fragment))


def bundled_postgres_address(env: Mapping[str, str]) -> tuple[str, int] | None:
    """The deployment's own PostgreSQL server (``BUNDLED_POSTGRES_ADDRESS_ENV``), if it runs one."""

    raw = str(env.get(BUNDLED_POSTGRES_ADDRESS_ENV) or "").strip()
    if not raw:
        return None
    host, separator, port = raw.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise ValueError(f"{BUNDLED_POSTGRES_ADDRESS_ENV} must be host:port, not {raw!r}.")
    return host, int(port)


def with_bundled_postgres_address(dsn: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    """``dsn`` pointed at the deployment's own PostgreSQL when it runs one, else unchanged."""

    address = bundled_postgres_address(env)
    if not dsn or address is None:
        return dsn
    return postgres_dsn_at(dsn, host=address[0], port=address[1])


class DatabaseRow(Mapping[str, Any]):
    """Mapping row wrapper that also supports tuple-style integer indexing."""

    def __init__(self, columns: list[str], values: tuple[Any, ...]):
        self._columns = columns
        self._values = values
        self._mapping = dict(zip(columns, values))

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)


class PostgresCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor
        self._columns = [
            str(description[0]) for description in (cursor.description or [])
        ]

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", 0))

    def fetchone(self) -> Optional[DatabaseRow]:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return DatabaseRow(self._columns, self._row_values(row))

    def fetchall(self) -> list[DatabaseRow]:
        rows = self._cursor.fetchall()
        return [DatabaseRow(self._columns, self._row_values(row)) for row in rows]

    def _row_values(self, row: Any) -> tuple[Any, ...]:
        if isinstance(row, Mapping):
            return tuple(row.get(column) for column in self._columns)
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            return tuple(mapping.get(column) for column in self._columns)
        return tuple(row)


class PostgresConnection:
    dialect = POSTGRES

    def begin_write(self) -> None:
        """Start a write: take the store's write lock when it has one.

        psycopg begins the transaction on the first statement. A store opened
        with ``write_lock_key`` takes its transaction-level advisory lock here,
        released at commit or rollback, so its writers take turns as they do in
        SQLite. Other stores lock the rows they change (SELECT FOR UPDATE),
        because their lock sets are domain-specific.
        """

        if self._write_lock_key is not None:
            self.execute("SELECT pg_advisory_xact_lock(%s)", (self._write_lock_key,))

    def __init__(self, dsn: str, *, write_lock_key: int | None = None):
        self._dsn = dsn
        self._write_lock_key = write_lock_key
        self._conn = _connect_psycopg(dsn)
        from storage.read_only import repositories_are_read_only

        if repositories_are_read_only():
            self._conn.read_only = True

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            self._conn.close()

    def execute(self, query: str, params: Optional[object] = None) -> PostgresCursor:
        cursor = self._conn.cursor()
        if params is None:
            cursor.execute(query)
        else:
            cursor.execute(query, params)
        return PostgresCursor(cursor)

    def commit(self) -> None:
        self._conn.commit()

    def iterate(
        self, query: str, params: object = (), *, batch_size: int = 256
    ) -> Iterator[DatabaseRow]:
        """Use a server cursor so global ranking has bounded candidate memory."""
        with self._conn.cursor(name=f"ua_read_{uuid4().hex}") as cursor:
            cursor.execute(query, params)
            columns = [str(description[0]) for description in cursor.description]
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield DatabaseRow(columns, tuple(row))

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def connect_postgres(dsn: str, *, write_lock_key: int | None = None) -> PostgresConnection:
    """Open a connection; ``write_lock_key`` makes ``begin_write`` take that store's write lock."""

    return PostgresConnection(dsn, write_lock_key=write_lock_key)


def validate_postgres_connection(dsn: str) -> None:
    with connect_postgres(dsn) as conn:
        conn.execute("SELECT 1").fetchone()


def _connect_psycopg(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency check
        raise RuntimeError(
            "psycopg is required for storage.backend=postgres. Install psycopg[binary] in this runtime."
        ) from exc
    return psycopg.connect(dsn)
