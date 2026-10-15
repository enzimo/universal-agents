from __future__ import annotations

import logging

import pytest

from custom_agents.memory_graph_embedding_profile_runtime import (
    build_embedding_profile_spec,
    ensure_memory_graph_embedding_profile,
    inspect_memory_graph_embedding_profile,
)
from storage.config import resolve_storage_settings
from storage.memory_graph_repository import create_memory_graph_repository


def test_ensure_memory_graph_embedding_profile_raises_on_mismatch(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}}
    )
    repo = create_memory_graph_repository(settings, logger=logging.getLogger("test"))
    ensure_memory_graph_embedding_profile(
        repository=repo,
        profile_spec=build_embedding_profile_spec({"provider": "local"}),
        logger=logging.getLogger("test"),
    )

    with pytest.raises(Exception, match="embedding profile mismatch"):
        ensure_memory_graph_embedding_profile(
            repository=repo,
            profile_spec=build_embedding_profile_spec(
                {
                    "provider": "openai_compatible",
                    "model_id": "pplx-embed-v1-0.6b",
                    "api_base": "https://embeddings.example.com/v1",
                }
            ),
            logger=logging.getLogger("test"),
        )


def test_inspect_memory_graph_embedding_profile_reports_blocked_state(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}}
    )
    repo = create_memory_graph_repository(settings, logger=logging.getLogger("test"))
    ensure_memory_graph_embedding_profile(
        repository=repo,
        profile_spec=build_embedding_profile_spec({"provider": "local"}),
        logger=logging.getLogger("test"),
    )

    report = inspect_memory_graph_embedding_profile(
        repository=repo,
        profile_spec=build_embedding_profile_spec(
            {
                "provider": "openai_compatible",
                "model_id": "pplx-embed-v1-0.6b",
                "api_base": "https://embeddings.example.com/v1",
            }
        ),
    )

    assert report["startup_blocked"] is True
    assert report["active_profile"]["provider"] == "local"
