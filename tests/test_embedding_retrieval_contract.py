"""The same scoped candidate and distance contract on actual database engines."""

import json
import os
from uuid import uuid4

import pytest

from storage.memory_graph_repository import (
    PostgresMemoryGraphRepository,
    SqliteMemoryGraphRepository,
)


@pytest.fixture(params=["sqlite", "python", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def graph(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN required")
        repo = PostgresMemoryGraphRepository(dsn)
    else:
        repo = SqliteMemoryGraphRepository(
            str(tmp_path / "graph.db"), vector_enabled=request.param == "sqlite"
        )
    subject = f"retrieval_{uuid4().hex}"
    yield repo, subject
    with repo._connect() as conn:
        conn.execute(f"DELETE FROM memory_entities WHERE subject_id = {conn.dialect.bind}", (subject,))


def add(repo, subject, name, vector):
    return repo.ensure_entity(
        subject_id=subject,
        entity_type="concept",
        canonical_name=name,
        embedding=json.dumps(vector) if vector is not None else None,
    )


def test_older_best_match_survives_more_than_one_hundred_recent_candidates(graph):
    repo, subject = graph
    oldest = add(repo, subject, "oldest exact match", [1.0, 0.0])
    for i in range(105):
        add(repo, subject, f"newer {i}", [0.0, 1.0])
    assert repo.search_entities_by_embedding(
        subject_id=subject, query_embedding=[1.0, 0.0], limit=1
    )[0].entity_id == oldest.entity_id


def test_distance_is_l2_and_unembedded_rows_are_ineligible(graph):
    repo, subject = graph
    exact = add(repo, subject, "exact", [1.0, 0.0])
    add(repo, subject, "larger dot product but farther away", [10.0, 0.0])
    add(repo, subject, "no embedding", None)
    rows = repo.search_entities_by_embedding(
        subject_id=subject, query_embedding=[1.0, 0.0], limit=10
    )
    assert rows[0].entity_id == exact.entity_id
    assert len(rows) == 2
    assert repo.search_entities_by_embedding(
        subject_id=subject + "_other", query_embedding=[1.0, 0.0]
    ) == []
