"""Backend-neutral storage settings resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_POSTGRESQL_ALIAS,
    STORAGE_BACKEND_SQLITE,
    SUPPORTED_STORAGE_BACKENDS,
)
from default_values import STORAGE_DEFAULTS

DEFAULT_SQLITE_STORAGE_PATH = STORAGE_DEFAULTS.task_history_db_path.value


def _normalize_backend(raw_backend: Optional[str]) -> str:
    backend = str(raw_backend or STORAGE_DEFAULTS.backend.value).strip().lower()
    if backend == STORAGE_BACKEND_POSTGRESQL_ALIAS:
        backend = STORAGE_BACKEND_POSTGRES
    if backend not in SUPPORTED_STORAGE_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_STORAGE_BACKENDS))
        raise ValueError(f"Unsupported storage backend {raw_backend!r}. Expected one of: {supported}")
    return backend


def _bool_setting(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _resolve_sqlite_path(
    configured_path: Optional[str],
    *,
    base_dir: Optional[str] = None,
) -> str:
    """Relative database paths resolve against the instance root (``base_dir``)."""

    raw = str(configured_path).strip() if configured_path is not None else ""
    target = Path(raw or DEFAULT_SQLITE_STORAGE_PATH).expanduser()
    if target.is_absolute():
        return str(target.resolve())
    root = Path(str(base_dir or ".")).expanduser().resolve()
    return str((root / target).resolve())


@dataclass(frozen=True)
class SqliteVectorStorageSettings:
    enabled: bool = STORAGE_DEFAULTS.sqlite_vector_enabled.value
    provider: str = STORAGE_DEFAULTS.sqlite_vector_provider.value
    require_extension: bool = STORAGE_DEFAULTS.sqlite_vector_require_extension.value


@dataclass(frozen=True)
class SqliteStorageSettings:
    path: str
    vector: SqliteVectorStorageSettings = SqliteVectorStorageSettings()


@dataclass(frozen=True)
class PostgresStorageSettings:
    dsn: str


@dataclass(frozen=True)
class StorageSettings:
    backend: str
    sqlite: Optional[SqliteStorageSettings] = None
    postgres: Optional[PostgresStorageSettings] = None

    @property
    def sqlite_path(self) -> Optional[str]:
        return self.sqlite.path if self.sqlite else None

    @property
    def sqlite_vector(self) -> Optional[SqliteVectorStorageSettings]:
        return self.sqlite.vector if self.sqlite else None

    @property
    def postgres_dsn(self) -> Optional[str]:
        return self.postgres.dsn if self.postgres else None

    @property
    def current_runtime_supported(self) -> bool:
        return self.backend in SUPPORTED_STORAGE_BACKENDS


def resolve_storage_settings(
    raw_storage: Optional[Mapping[str, Any]],
    *,
    base_dir: Optional[str] = None,
    sqlite_fallback_path: Optional[str] = None,
) -> StorageSettings:
    storage_cfg = dict(raw_storage or {})
    backend = _normalize_backend(storage_cfg.get("backend"))

    sqlite_cfg = dict(storage_cfg.get(STORAGE_BACKEND_SQLITE) or {})
    postgres_cfg = dict(storage_cfg.get(STORAGE_BACKEND_POSTGRES) or {})
    sqlite_vector_cfg = dict(sqlite_cfg.get("vector") or {})

    if backend == STORAGE_BACKEND_SQLITE:
        provider = str(
            sqlite_vector_cfg.get("provider") or STORAGE_DEFAULTS.sqlite_vector_provider.value
        ).strip()
        if provider != STORAGE_DEFAULTS.sqlite_vector_provider.value:
            raise ValueError(
                f"Unsupported storage.sqlite.vector.provider {provider!r}. "
                f"Expected {STORAGE_DEFAULTS.sqlite_vector_provider.value!r}."
            )
        return StorageSettings(
            backend=backend,
            sqlite=SqliteStorageSettings(
                path=_resolve_sqlite_path(
                    sqlite_cfg.get("path") or sqlite_fallback_path,
                    base_dir=base_dir,
                ),
                vector=SqliteVectorStorageSettings(
                    enabled=_bool_setting(
                        sqlite_vector_cfg.get(
                            "enabled",
                            STORAGE_DEFAULTS.sqlite_vector_enabled.value,
                        ),
                        default=STORAGE_DEFAULTS.sqlite_vector_enabled.value,
                    ),
                    provider=provider,
                    require_extension=_bool_setting(
                        sqlite_vector_cfg.get(
                            "require_extension",
                            STORAGE_DEFAULTS.sqlite_vector_require_extension.value,
                        ),
                        default=STORAGE_DEFAULTS.sqlite_vector_require_extension.value,
                    ),
                ),
            ),
        )

    dsn = str(postgres_cfg.get("dsn") or "").strip()
    if not dsn:
        raise ValueError("storage.postgres.dsn is required when storage.backend=postgres")

    return StorageSettings(
        backend=backend,
        postgres=PostgresStorageSettings(dsn=dsn),
    )
