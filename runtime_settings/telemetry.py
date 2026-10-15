"""Telemetry configuration and contracts."""

from __future__ import annotations
import copy
from typing import Any, Dict, Mapping, Optional
from pydantic import BaseModel, Field, field_validator
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_POSTGRESQL_ALIAS,
    SUPPORTED_STORAGE_BACKENDS,
)
from default_values import ACTIVITY_COLLECTOR_DEFAULTS, OTEL_DEFAULTS, STORAGE_DEFAULTS
from env_secrets import get_env_with_file_fallback
from runtime_settings.common import _normalized_optional_string


ACTIVITY_COLLECTOR_USER_TOKEN_ENV = "ACTIVITY_COLLECTOR_USER_TOKEN"


ACTIVITY_COLLECTOR_ADMIN_TOKEN_ENV = "ACTIVITY_COLLECTOR_ADMIN_TOKEN"


ACTIVITY_COLLECTOR_INGEST_TOKEN_ENV = "ACTIVITY_COLLECTOR_INGEST_TOKEN"


class CollectorSqliteStorageSettings(BaseModel):
    path: Optional[str] = STORAGE_DEFAULTS.activity_collector_db_path.value


class CollectorPostgresStorageSettings(BaseModel):
    dsn: Optional[str] = None


class CollectorStorageSettings(BaseModel):
    backend: str = STORAGE_DEFAULTS.backend.value
    sqlite: CollectorSqliteStorageSettings = Field(
        default_factory=CollectorSqliteStorageSettings
    )
    postgres: CollectorPostgresStorageSettings = Field(
        default_factory=CollectorPostgresStorageSettings
    )

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        normalized = str(value or STORAGE_DEFAULTS.backend.value).strip().lower()
        if normalized == STORAGE_BACKEND_POSTGRESQL_ALIAS:
            normalized = STORAGE_BACKEND_POSTGRES
        if normalized not in SUPPORTED_STORAGE_BACKENDS:
            raise ValueError(
                "activity_collector.storage.backend must be 'sqlite' or 'postgres'."
            )
        return normalized


class ActivityCollectorSettings(BaseModel):
    base_url: Optional[str] = ACTIVITY_COLLECTOR_DEFAULTS.base_url.value
    connect_timeout_seconds: float = (
        ACTIVITY_COLLECTOR_DEFAULTS.connect_timeout_seconds.value
    )
    request_timeout_seconds: float = (
        ACTIVITY_COLLECTOR_DEFAULTS.request_timeout_seconds.value
    )
    batch_max_events: int = ACTIVITY_COLLECTOR_DEFAULTS.batch_max_events.value
    flush_interval_seconds: float = (
        ACTIVITY_COLLECTOR_DEFAULTS.flush_interval_seconds.value
    )
    max_buffered_events: int = ACTIVITY_COLLECTOR_DEFAULTS.max_buffered_events.value
    drop_when_unavailable: bool = (
        ACTIVITY_COLLECTOR_DEFAULTS.drop_when_unavailable.value
    )
    bind_host: str = ACTIVITY_COLLECTOR_DEFAULTS.bind_host.value
    bind_port: int = ACTIVITY_COLLECTOR_DEFAULTS.bind_port.value
    user_url_path: str = ACTIVITY_COLLECTOR_DEFAULTS.user_url_path.value
    admin_url_path: str = ACTIVITY_COLLECTOR_DEFAULTS.admin_url_path.value
    otel_trace_base_url: Optional[str] = None
    ingest_token_env_var: str = ACTIVITY_COLLECTOR_DEFAULTS.ingest_token_env_var.value
    storage: CollectorStorageSettings = Field(default_factory=CollectorStorageSettings)

    @field_validator("base_url", "otel_trace_base_url", mode="before")
    @classmethod
    def _normalize_optional_url(cls, value: Any) -> Optional[str]:
        normalized = _normalized_optional_string(value)
        if not normalized:
            return None
        return normalized.rstrip("/")

    @field_validator("bind_host", mode="before")
    @classmethod
    def _normalize_bind_host(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or ACTIVITY_COLLECTOR_DEFAULTS.bind_host.value

    @field_validator("user_url_path", "admin_url_path", mode="before")
    @classmethod
    def _normalize_url_path(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            normalized = "/"
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    @field_validator("ingest_token_env_var", mode="before")
    @classmethod
    def _normalize_ingest_token_env_var(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or ACTIVITY_COLLECTOR_DEFAULTS.ingest_token_env_var.value

    @field_validator("bind_port", mode="before")
    @classmethod
    def _normalize_bind_port(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "activity_collector.bind_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "activity_collector.bind_port must be between 1 and 65535."
            )
        return parsed

    @field_validator(
        "connect_timeout_seconds",
        "request_timeout_seconds",
        "flush_interval_seconds",
        mode="before",
    )
    @classmethod
    def _normalize_float_settings(cls, value: Any, info: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"activity_collector.{info.field_name} must be a number."
            ) from exc
        if parsed <= 0:
            raise ValueError(
                f"activity_collector.{info.field_name} must be greater than 0."
            )
        return parsed

    @field_validator("batch_max_events", "max_buffered_events", mode="before")
    @classmethod
    def _normalize_int_settings(cls, value: Any, info: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"activity_collector.{info.field_name} must be an integer."
            ) from exc
        if parsed < 1:
            raise ValueError(
                f"activity_collector.{info.field_name} must be greater than 0."
            )
        if info.field_name == "batch_max_events" and parsed > 1_000:
            raise ValueError(
                "activity_collector.batch_max_events must be at most 1000."
            )
        if info.field_name == "max_buffered_events" and parsed > 100_000:
            raise ValueError(
                "activity_collector.max_buffered_events must be at most 100000."
            )
        return parsed

    @property
    def enabled(self) -> bool:
        return bool(str(self.base_url or "").strip())

    @property
    def public_user_url(self) -> Optional[str]:
        if not self.enabled:
            return None
        return f"{self.base_url}{self.user_url_path}"

    @property
    def public_admin_url(self) -> Optional[str]:
        if not self.enabled:
            return None
        return f"{self.base_url}{self.admin_url_path}"

    def resolve_user_token(
        self, *, env: Optional[Mapping[str, str]] = None
    ) -> Optional[str]:
        return get_env_with_file_fallback(ACTIVITY_COLLECTOR_USER_TOKEN_ENV, env=env)

    def resolve_admin_token(
        self, *, env: Optional[Mapping[str, str]] = None
    ) -> Optional[str]:
        return get_env_with_file_fallback(ACTIVITY_COLLECTOR_ADMIN_TOKEN_ENV, env=env)

    def resolve_ingest_token(
        self, *, env: Optional[Mapping[str, str]] = None
    ) -> Optional[str]:
        return get_env_with_file_fallback(self.ingest_token_env_var, env=env)

    def require_user_token(self, *, env: Optional[Mapping[str, str]] = None) -> str:
        token = self.resolve_user_token(env=env)
        if not token:
            raise ValueError(
                f"{ACTIVITY_COLLECTOR_USER_TOKEN_ENV} or {ACTIVITY_COLLECTOR_USER_TOKEN_ENV}_FILE "
                "must be set to run the activity collector."
            )
        return token

    def require_admin_token(self, *, env: Optional[Mapping[str, str]] = None) -> str:
        token = self.resolve_admin_token(env=env)
        if not token:
            raise ValueError(
                f"{ACTIVITY_COLLECTOR_ADMIN_TOKEN_ENV} or {ACTIVITY_COLLECTOR_ADMIN_TOKEN_ENV}_FILE "
                "must be set to run the activity collector."
            )
        return token

    def require_ingest_token(self, *, env: Optional[Mapping[str, str]] = None) -> str:
        token = self.resolve_ingest_token(env=env)
        if not token:
            env_var = self.ingest_token_env_var or ACTIVITY_COLLECTOR_INGEST_TOKEN_ENV
            raise ValueError(
                f"{env_var} or {env_var}_FILE must be set to run the activity collector."
            )
        return token


class OTelSettings(BaseModel):
    enabled: bool = OTEL_DEFAULTS.enabled.value
    enable_traces: bool = OTEL_DEFAULTS.enable_traces.value
    enable_metrics: bool = OTEL_DEFAULTS.enable_metrics.value
    otlp_endpoint: Optional[str] = OTEL_DEFAULTS.otlp_endpoint.value
    service_name: Optional[str] = OTEL_DEFAULTS.service_name.value
    headers: Dict[str, str] = Field(
        default_factory=lambda: copy.deepcopy(OTEL_DEFAULTS.headers.value)
    )

    @field_validator("otlp_endpoint", "service_name", mode="before")
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)
