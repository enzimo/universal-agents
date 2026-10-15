"""sqlite-vec loading helpers for SQLite-backed memory graph search."""

from __future__ import annotations

import logging
from typing import Any, Sequence


class SqliteVecLoadError(RuntimeError):
    """Raised when sqlite-vec is required but cannot be loaded."""


def load_sqlite_vec(
    conn: Any,
    *,
    logger: logging.Logger | None = None,
    storage_target: str = "",
    require_extension: bool = True,
) -> str | None:
    """Load sqlite-vec into a SQLite connection and return its version."""

    try:
        import sqlite_vec
    except ImportError as exc:
        return _handle_load_failure(
            exc,
            logger=logger,
            storage_target=storage_target,
            require_extension=require_extension,
            detail="sqlite-vec is not installed. Install sqlite-vec==0.1.9.",
        )

    load_extension = getattr(conn, "load_extension", None)
    if not callable(load_extension):
        return _handle_load_failure(
            AttributeError("SQLite connection does not expose load_extension"),
            logger=logger,
            storage_target=storage_target,
            require_extension=require_extension,
            detail=(
                "The active Python SQLite driver cannot load extensions. "
                "On macOS, install the project's pysqlite3 dependency or use a Python build with "
                "loadable SQLite extension support."
            ),
        )

    enable_load_extension = getattr(conn, "enable_load_extension", None)
    try:
        if callable(enable_load_extension):
            enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception as exc:
        return _handle_load_failure(
            exc,
            logger=logger,
            storage_target=storage_target,
            require_extension=require_extension,
            detail="sqlite-vec failed to load into the active SQLite connection.",
        )
    finally:
        if callable(enable_load_extension):
            try:
                enable_load_extension(False)
            except Exception:
                if logger is not None:
                    logger.warning(
                        "Failed to disable SQLite extension loading after sqlite-vec load.",
                        extra={"component": "sqlite_vec", "storage_target": storage_target},
                        exc_info=True,
                    )

    version_row = conn.execute("SELECT vec_version()").fetchone()
    version = str(version_row[0] if version_row is not None else "")
    if logger is not None:
        logger.info(
            "Loaded sqlite-vec %s for SQLite memory-graph vector search at %s.",
            version,
            storage_target,
        )
    return version


def serialize_float32(values: Sequence[float]) -> bytes:
    import sqlite_vec

    return sqlite_vec.serialize_float32([float(value) for value in values])


def _handle_load_failure(
    exc: Exception,
    *,
    logger: logging.Logger | None,
    storage_target: str,
    require_extension: bool,
    detail: str,
) -> None:
    message = (
        f"{detail} SQLite memory-graph vector support requires sqlite-vec because "
        "storage.sqlite.vector.enabled is true."
    )
    if logger is not None:
        logger.error(
            message,
            extra={"component": "sqlite_vec", "storage_target": storage_target},
            exc_info=True,
        )
    if require_extension:
        raise SqliteVecLoadError(message) from exc
    return None
