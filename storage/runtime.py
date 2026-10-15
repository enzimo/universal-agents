"""Storage runtime helpers and repository capability checks."""

from __future__ import annotations

from dataclasses import dataclass
from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE

from .config import StorageSettings


@dataclass(frozen=True)
class StorageBackendCapabilities:
    backend: str
    supports_runtime_repositories: bool
    supports_agent_state_repository: bool


def describe_storage_capabilities(settings: StorageSettings) -> StorageBackendCapabilities:
    sqlite_supported = settings.backend == STORAGE_BACKEND_SQLITE
    postgres_supported = settings.backend == STORAGE_BACKEND_POSTGRES
    runtime_supported = sqlite_supported or postgres_supported
    return StorageBackendCapabilities(
        backend=settings.backend,
        supports_runtime_repositories=runtime_supported,
        supports_agent_state_repository=runtime_supported,
    )


def require_repository_backend(
    settings: StorageSettings,
    *,
    repository_name: str,
    capability_supported: bool,
) -> None:
    if capability_supported:
        return
    raise NotImplementedError(
        f"{repository_name} does not support storage backend {settings.backend!r} yet."
    )
