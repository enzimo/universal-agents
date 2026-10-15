from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config_loader import load_config
from storage.config import StorageSettings, resolve_storage_settings
from storage.postgres_connection import connect_postgres
from storage.sqlite_connection import connect_sqlite
from storage.sql_dialect import POSTGRES, SQLITE


def resolve_live_storage_settings(
    *,
    sqlite_fallback_path: str | Path | None = None,
    workspace_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> StorageSettings:
    backend = str(os.getenv("STORAGE_BACKEND", "") or "").strip().lower()
    config = None
    if not backend and config_path:
        config = load_config(str(config_path))
        backend = str(config.get("storage.backend", "sqlite") or "sqlite").strip().lower()
    if not backend:
        backend = "sqlite"
    if backend == "postgresql":
        backend = "postgres"
    if backend == "postgres":
        dsn = _resolve_live_postgres_dsn(config=config)
        return resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": dsn}},
            base_dir=str(workspace_dir or "."),
        )

    sqlite_path = str(
        sqlite_fallback_path
        or os.getenv("TASK_HISTORY_DB_PATH")
        or os.getenv("SQLITE_DB_PATH")
        or "test_results/test_data/task_history.db"
    ).strip()
    return resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": sqlite_path}},
        base_dir=str(workspace_dir or "."),
    )


def storage_identifier(settings: StorageSettings) -> str:
    if settings.backend == "postgres":
        return redact_dsn(settings.postgres_dsn or "")
    return settings.sqlite_path or ""


def redact_dsn(raw_dsn: str) -> str:
    if not raw_dsn:
        return raw_dsn
    parts = urlsplit(raw_dsn)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostinfo = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username, _password = userinfo.split(":", 1)
            userinfo = f"{username}:***"
        netloc = f"{userinfo}@{hostinfo}"
    query = urlencode([(key, "***" if "password" in key.lower() else value) for key, value in parse_qsl(parts.query)])
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def apply_storage_env(env: dict[str, str], settings: StorageSettings) -> dict[str, str]:
    env["STORAGE_BACKEND"] = settings.backend
    if settings.backend == "postgres":
        env["POSTGRES_DSN"] = settings.postgres_dsn or ""
        env.pop("TASK_HISTORY_DB_PATH", None)
        env.pop("SQLITE_DB_PATH", None)
    else:
        sqlite_path = settings.sqlite_path or ""
        env["TASK_HISTORY_DB_PATH"] = sqlite_path
        env["SQLITE_DB_PATH"] = sqlite_path
        env.pop("POSTGRES_DSN", None)
    return env


def _resolve_live_postgres_dsn(config: Any | None = None) -> str:
    explicit = str(os.getenv("LIVE_TEST_POSTGRES_DSN", "")).strip()
    if explicit:
        return explicit

    base_dsn = str(os.getenv("POSTGRES_DSN", "")).strip()
    if not base_dsn and config is not None:
        base_dsn = str(config.get("storage.postgres.dsn", "") or "").strip()
    if not base_dsn:
        return ""

    parts = urlsplit(base_dsn)
    base_db_name = parts.path.lstrip("/")
    if not base_db_name:
        raise RuntimeError("POSTGRES_DSN must include a database name for live-test isolation.")

    override_name = str(os.getenv("LIVE_TEST_POSTGRES_DB_NAME", "")).strip()
    suffix = str(os.getenv("LIVE_TEST_POSTGRES_DB_SUFFIX", "_live_tests")).strip() or "_live_tests"
    live_db_name = override_name or (
        base_db_name if base_db_name.endswith(suffix) else f"{base_db_name}{suffix}"
    )
    return urlunsplit((parts.scheme, parts.netloc, f"/{live_db_name}", parts.query, parts.fragment))


def _ensure_postgres_database_exists(dsn: str) -> None:
    parts = urlsplit(dsn)
    db_name = parts.path.lstrip("/")
    if not db_name:
        raise RuntimeError("Live Postgres DSN must include a database name.")

    admin_db = str(os.getenv("LIVE_TEST_POSTGRES_ADMIN_DB", "postgres")).strip() or "postgres"
    admin_dsn = urlunsplit((parts.scheme, parts.netloc, f"/{admin_db}", parts.query, parts.fragment))
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - only exercised in live Postgres runs
        raise RuntimeError(
            "psycopg is required for Postgres-backed live tests. Install psycopg[binary] in this runtime."
        ) from exc

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))


def reset_postgres_storage(settings: StorageSettings) -> None:
    if settings.backend != "postgres":
        return
    _ensure_postgres_database_exists(settings.postgres_dsn or "")
    with connect_postgres(settings.postgres_dsn or "") as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
        conn.commit()


def query_storage(
    target: StorageSettings | str | Path,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[Any]:
    settings = coerce_storage_settings(target)
    try:
        if settings.backend == "postgres":
            with connect_postgres(settings.postgres_dsn or "") as conn:
                return conn.execute(query, params).fetchall()

        conn = connect_sqlite(settings.sqlite_path or "", row_factory=sqlite3.Row)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc).lower()
        if "no such table" in message or "does not exist" in message:
            return []
        raise


def storage_bind(target: StorageSettings | str | Path) -> str:
    """Test queries use the same native bind syntax as their real adapter."""
    return (POSTGRES if coerce_storage_settings(target).backend == "postgres" else SQLITE).bind


def coerce_storage_settings(target: StorageSettings | str | Path) -> StorageSettings:
    if isinstance(target, StorageSettings):
        return target
    if isinstance(target, Path):
        return resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(target)}},
            base_dir=str(target.parent),
        )
    raw = str(target).strip()
    if raw.startswith("postgresql://") or raw.startswith("postgresql+psycopg://"):
        return resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": raw}},
            base_dir=".",
        )
    return resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": raw}},
        base_dir=str(Path(raw).expanduser().resolve().parent if raw else "."),
    )
