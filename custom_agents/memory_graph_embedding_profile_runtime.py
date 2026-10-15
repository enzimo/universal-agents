"""Runtime checks for memory-graph embedding profile consistency."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Dict, Optional

from custom_agents.embedding_service import (
    DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
    create_embedding_service_settings,
)
from storage.memory_graph_repository import (
    MemoryEmbeddingProfileRow,
    MemoryGraphRepository,
    build_embedding_profile_hash,
)


@dataclass(frozen=True)
class EmbeddingProfileSpec:
    provider: str
    model_id: Optional[str]
    api_base: Optional[str]
    dimensions: Optional[int]
    profile_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "api_base": self.api_base,
            "dimensions": self.dimensions,
            "profile_hash": self.profile_hash,
        }


class EmbeddingProfileMismatchError(RuntimeError):
    def __init__(
        self,
        *,
        configured: EmbeddingProfileSpec,
        active: MemoryEmbeddingProfileRow,
        guidance: str,
    ) -> None:
        self.configured = configured
        self.active = active
        super().__init__(
            "Memory graph embedding profile mismatch detected.\n"
            f"Configured profile: provider={configured.provider!r} model_id={configured.model_id!r} "
            f"api_base={configured.api_base!r} dimensions={configured.dimensions!r}\n"
            f"Stored active profile: provider={active.provider!r} model_id={active.model_id!r} "
            f"api_base={active.api_base!r} dimensions={active.dimensions!r}\n"
            f"{guidance}"
        )


def build_embedding_profile_spec(raw: Dict[str, Any]) -> EmbeddingProfileSpec:
    settings = create_embedding_service_settings(raw)
    provider = settings.normalized_provider
    dimensions = settings.dimensions
    if provider == "local":
        dimensions = dimensions or DEFAULT_LOCAL_EMBEDDING_DIMENSIONS
    model_id = settings.model_id
    api_base = str(settings.api_base or "").strip().rstrip("/") or None
    return EmbeddingProfileSpec(
        provider=provider,
        model_id=model_id,
        api_base=api_base,
        dimensions=dimensions,
        profile_hash=build_embedding_profile_hash(
            provider=provider,
            model_id=model_id,
            api_base=api_base,
            dimensions=dimensions,
        ),
    )


def render_embedding_profile_guidance(*, config_path: Optional[str] = None) -> str:
    status_cmd = "assistant_cli.py memory-graph-embedding-status"
    reset_cmd = "assistant_cli.py memory-graph-embedding-reset --archive"
    purge_cmd = "assistant_cli.py memory-graph-embedding-reset --purge"
    if config_path:
        status_cmd = f"{status_cmd} --config {config_path}"
        reset_cmd = f"{reset_cmd} --config {config_path}"
        purge_cmd = f"{purge_cmd} --config {config_path}"
    return (
        "Failing closed to avoid mixing incompatible graph embeddings. "
        f"To proceed, either revert the embedding config to the stored active profile, "
        f"inspect the mismatch with `{status_cmd}`, start a fresh profile with `{reset_cmd}`, "
        f"or delete all graph embedding state with `{purge_cmd}`."
    )


def ensure_memory_graph_embedding_profile(
    *,
    repository: MemoryGraphRepository,
    profile_spec: EmbeddingProfileSpec,
    logger: Optional[logging.Logger] = None,
    config_path: Optional[str] = None,
) -> MemoryEmbeddingProfileRow:
    current = repository.get_active_embedding_profile()
    if current is None:
        activated = repository.activate_embedding_profile(
            provider=profile_spec.provider,
            model_id=profile_spec.model_id,
            api_base=profile_spec.api_base,
            dimensions=profile_spec.dimensions,
            metadata={"profile_hash": profile_spec.profile_hash},
        )
        if logger is not None:
            logger.info(
                "Seeded initial memory-graph embedding profile provider=%s model_id=%s dimensions=%s.",
                activated.provider,
                activated.model_id,
                activated.dimensions,
            )
        return activated

    if current.profile_hash == profile_spec.profile_hash:
        return current

    guidance = render_embedding_profile_guidance(config_path=config_path)
    if logger is not None:
        logger.error(
            "Memory graph embedding profile mismatch: configured provider=%s model_id=%s dimensions=%s "
            "stored provider=%s model_id=%s dimensions=%s. %s",
            profile_spec.provider,
            profile_spec.model_id,
            profile_spec.dimensions,
            current.provider,
            current.model_id,
            current.dimensions,
            guidance,
        )
    raise EmbeddingProfileMismatchError(
        configured=profile_spec,
        active=current,
        guidance=guidance,
    )


def inspect_memory_graph_embedding_profile(
    *,
    repository: MemoryGraphRepository,
    profile_spec: EmbeddingProfileSpec,
) -> Dict[str, Any]:
    active = repository.get_active_embedding_profile()
    archived = repository.list_embedding_profiles(status="archived")
    counts = repository.count_graph_embedding_rows()
    blocked = active is not None and active.profile_hash != profile_spec.profile_hash
    return {
        "configured_profile": profile_spec.to_dict(),
        "active_profile": (
            {
                "embedding_profile_id": active.embedding_profile_id,
                "profile_hash": active.profile_hash,
                "provider": active.provider,
                "model_id": active.model_id,
                "api_base": active.api_base,
                "dimensions": active.dimensions,
                "status": active.status,
                "created_at": active.created_at,
                "archived_at": active.archived_at,
                "metadata": active.metadata,
            }
            if active is not None
            else None
        ),
        "archived_profiles": [
            {
                "embedding_profile_id": row.embedding_profile_id,
                "profile_hash": row.profile_hash,
                "provider": row.provider,
                "model_id": row.model_id,
                "api_base": row.api_base,
                "dimensions": row.dimensions,
                "status": row.status,
                "created_at": row.created_at,
                "archived_at": row.archived_at,
                "metadata": row.metadata,
            }
            for row in archived
        ],
        "graph_row_counts": counts,
        "startup_blocked": blocked,
    }
