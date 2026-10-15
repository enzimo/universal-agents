"""Deterministic entity resolution helpers for the scoped memory graph."""

from __future__ import annotations

import json
from typing import Callable, List, Optional, Protocol, Tuple

from custom_agents.embedding_service import (
    DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
    _build_local_embedding,
    _normalize_tokens,
)
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_POSTGRESQL_ALIAS,
    STORAGE_BACKEND_SQLITE,
)
from custom_messages.contracts.runtime_v1 import TRANSPORT_NATS
from storage.memory_graph_repository import MemoryEntityRow, MemoryGraphRepository


EMBEDDING_DIMENSIONS = DEFAULT_LOCAL_EMBEDDING_DIMENSIONS


class MemoryOwnerSubject(Protocol):
    subject_id: str
    subject_kind: str
    canonical_key: str
    display_name: Optional[str]

KNOWN_ENTITY_PATTERNS = (
    {
        "tokens": (STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_POSTGRESQL_ALIAS),
        "canonical_name": "Postgres",
        "aliases": [STORAGE_BACKEND_POSTGRESQL_ALIAS],
        "entity_type": "concept",
        "bucket": "storage_backend",
    },
    {
        "tokens": (STORAGE_BACKEND_SQLITE,),
        "canonical_name": "SQLite",
        "aliases": [],
        "entity_type": "concept",
        "bucket": "storage_backend",
    },
    {
        "tokens": ("mysql",),
        "canonical_name": "MySQL",
        "aliases": [],
        "entity_type": "concept",
        "bucket": "storage_backend",
    },
    {
        "tokens": ("python",),
        "canonical_name": "Python",
        "aliases": [],
        "entity_type": "concept",
        "bucket": "implementation_language",
    },
    {
        "tokens": ("pytest", "unittest"),
        "canonical_name": "Pytest",
        "aliases": ["unittest"],
        "entity_type": "tool",
        "bucket": "test_runner",
    },
    {
        "tokens": ("yaml", "json"),
        "canonical_name": "YAML",
        "aliases": ["json"],
        "entity_type": "artifact",
        "bucket": "config_format",
    },
    {
        "tokens": ("uv", "poetry", "pip"),
        "canonical_name": "uv",
        "aliases": ["poetry", "pip"],
        "entity_type": "tool",
        "bucket": "python_package_manager",
    },
    {
        "tokens": (TRANSPORT_NATS, "jetstream"),
        "canonical_name": "NATS JetStream",
        "aliases": [TRANSPORT_NATS, "jetstream"],
        "entity_type": "concept",
        "bucket": "runtime_transport",
    },
)


def build_text_embedding(text: str, *, dimensions: int = EMBEDDING_DIMENSIONS) -> str:
    return _build_local_embedding(text, dimensions=dimensions)


def parse_embedding(raw: Optional[str]) -> List[float]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    parsed: List[float] = []
    for value in values:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            parsed.append(0.0)
    return parsed


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def ensure_owner_entity(
    *,
    repository: MemoryGraphRepository,
    subject: MemoryOwnerSubject,
    source_episode_id: Optional[str],
    embed_text_fn: Optional[Callable[[str], str]] = None,
) -> MemoryEntityRow:
    entity_type = {
        "user": "person",
        "team": "organization",
        "agent_role": "concept",
    }.get(subject.subject_kind, "unknown")
    canonical_name = subject.display_name or subject.canonical_key
    return repository.ensure_entity(
        subject_id=subject.subject_id,
        entity_type=entity_type,
        canonical_name=canonical_name,
        aliases=[subject.canonical_key],
        summary_text=f"Durable owner entity for {subject.subject_kind} scope.",
        embedding=(embed_text_fn or build_text_embedding)(f"{canonical_name} {subject.canonical_key}"),
        metadata={
            "owner_subject_id": subject.subject_id,
            "owner_subject_kind": subject.subject_kind,
            "owner_canonical_key": subject.canonical_key,
        },
        source_episode_id=source_episode_id,
    )


def resolve_supporting_entity(
    *,
    repository: MemoryGraphRepository,
    subject_id: str,
    source_episode_id: Optional[str],
    fact_text: str,
    embed_text_fn: Optional[Callable[[str], str]] = None,
) -> Tuple[Optional[MemoryEntityRow], Optional[str]]:
    lowered = " ".join(_normalize_tokens(fact_text))
    for pattern in KNOWN_ENTITY_PATTERNS:
        if not any(token in lowered for token in pattern["tokens"]):
            continue
        entity = repository.ensure_entity(
            subject_id=subject_id,
            entity_type=pattern["entity_type"],
            canonical_name=pattern["canonical_name"],
            aliases=list(pattern["aliases"]),
            summary_text=f"Frequently referenced scoped entity derived from memory facts about {pattern['canonical_name']}.",
            embedding=(embed_text_fn or build_text_embedding)(fact_text),
            metadata={"pattern_tokens": list(pattern["tokens"]), "bucket": pattern["bucket"]},
            source_episode_id=source_episode_id,
        )
        return entity, str(pattern["bucket"])
    return None, None
