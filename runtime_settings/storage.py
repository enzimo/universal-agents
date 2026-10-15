"""Storage configuration and contracts."""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_POSTGRESQL_ALIAS,
    SUPPORTED_STORAGE_BACKENDS,
)
from default_values import STORAGE_DEFAULTS


class SqliteVectorStorageSettings(BaseModel):
    enabled: bool = STORAGE_DEFAULTS.sqlite_vector_enabled.value
    provider: str = STORAGE_DEFAULTS.sqlite_vector_provider.value
    require_extension: bool = STORAGE_DEFAULTS.sqlite_vector_require_extension.value

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        normalized = str(value or STORAGE_DEFAULTS.sqlite_vector_provider.value).strip()
        if normalized != STORAGE_DEFAULTS.sqlite_vector_provider.value:
            raise ValueError("storage.sqlite.vector.provider must be 'sqlite_vec'.")
        return normalized


class ManagerSqliteStorageSettings(BaseModel):
    path: Optional[str] = STORAGE_DEFAULTS.task_history_db_path.value
    vector: SqliteVectorStorageSettings = Field(
        default_factory=SqliteVectorStorageSettings
    )


class ManagerPostgresStorageSettings(BaseModel):
    dsn: Optional[str] = None


class ManagerStorageSettings(BaseModel):
    backend: str = STORAGE_DEFAULTS.backend.value
    sqlite: ManagerSqliteStorageSettings = Field(
        default_factory=ManagerSqliteStorageSettings
    )
    postgres: ManagerPostgresStorageSettings = Field(
        default_factory=ManagerPostgresStorageSettings
    )

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        normalized = str(value or STORAGE_DEFAULTS.backend.value).strip().lower()
        if normalized == STORAGE_BACKEND_POSTGRESQL_ALIAS:
            normalized = STORAGE_BACKEND_POSTGRES
        if normalized not in SUPPORTED_STORAGE_BACKENDS:
            raise ValueError("state.storage.backend must be 'sqlite' or 'postgres'.")
        return normalized


class StateSqliteStorageSettings(BaseModel):
    path: Optional[str] = None


class StatePostgresStorageSettings(BaseModel):
    dsn: Optional[str] = None


class StateStorageSettings(BaseModel):
    backend: str = STORAGE_DEFAULTS.backend.value
    sqlite: StateSqliteStorageSettings = Field(
        default_factory=StateSqliteStorageSettings
    )
    postgres: StatePostgresStorageSettings = Field(
        default_factory=StatePostgresStorageSettings
    )

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        normalized = str(value or STORAGE_DEFAULTS.backend.value).strip().lower()
        if normalized == STORAGE_BACKEND_POSTGRESQL_ALIAS:
            normalized = STORAGE_BACKEND_POSTGRES
        if normalized not in SUPPORTED_STORAGE_BACKENDS:
            raise ValueError("gateway.storage.backend must be 'sqlite' or 'postgres'.")
        return normalized
