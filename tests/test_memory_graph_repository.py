from __future__ import annotations

import logging
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from storage.config import resolve_storage_settings
import storage.memory_graph_repository as memory_graph_repository_module
from storage.memory_graph_repository import (
    MEMORY_GRAPH_VECTOR_RECORDS_TABLE,
    MEMORY_GRAPH_VECTOR_STATE_TABLE,
    SqliteMemoryGraphRepository,
    create_memory_graph_repository,
)


def _embedding(*values: float) -> str:
    return json.dumps(list(values))


@pytest.mark.parametrize("driver", ["sqlite3", "pysqlite3"])
def test_concurrent_older_memory_graph_opens_keep_existing_records(tmp_path, monkeypatch, caplog, driver):
    sqlite_module = sqlite3 if driver == "sqlite3" else pytest.importorskip("pysqlite3")
    monkeypatch.setattr(memory_graph_repository_module, "sqlite3", sqlite_module)
    path = str(tmp_path / "older.db")
    repo = SqliteMemoryGraphRepository(path, vector_enabled=False)
    entity = repo.ensure_entity(subject_id="person", entity_type="concept", canonical_name="Saved memory")
    with repo._connect() as conn:
        conn.execute("ALTER TABLE memory_entities DROP COLUMN embedding_profile_id")
        connection_type = type(conn)

    # Both opens must see the missing column before either adds it. The losing
    # writer raises its driver's own OperationalError, then re-reads the schema.
    barrier = threading.Barrier(2, timeout=10)
    execute = connection_type.execute

    def competing_add(conn, statement, parameters=()):
        if statement.startswith("ALTER TABLE memory_entities ADD COLUMN embedding_profile_id"):
            barrier.wait()
        return execute(conn, statement, parameters)

    monkeypatch.setattr(connection_type, "execute", competing_add)
    with caplog.at_level(logging.INFO, logger=memory_graph_repository_module.__name__):
        with ThreadPoolExecutor(max_workers=2) as pool:
            opens = [pool.submit(SqliteMemoryGraphRepository, path, vector_enabled=False) for _ in range(2)]
            for opened in opens:
                opened.result(timeout=15)

    with repo._connect() as conn:
        row = conn.execute(
            "SELECT canonical_name, embedding_profile_id FROM memory_entities WHERE entity_id = ?",
            (entity.entity_id,),
        ).fetchone()
    assert tuple(row) == ("Saved memory", None)
    assert "SQLite column was added concurrently" in caplog.text


def test_memory_graph_repository_persists_episodes_entities_claims_and_evidence(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}}
    )
    repo = create_memory_graph_repository(settings, logger=logging.getLogger("test"))

    episode = repo.ensure_episode(
        subject_id="subject_user_1",
        conversation_id="conversation_1",
        task_id="task_1",
        turn_index=1,
        source_kind="conversation_turn",
        request_text="I prefer detailed step-by-step replies.",
        event_time="2026-04-12T12:00:00+00:00",
    )
    owner = repo.ensure_entity(
        subject_id="subject_user_1",
        entity_type="person",
        canonical_name="test-user",
        aliases=["gateway_user:test-user"],
        source_episode_id=episode.episode_id,
    )
    backend = repo.ensure_entity(
        subject_id="subject_user_1",
        entity_type="concept",
        canonical_name="Postgres",
        aliases=["postgresql"],
        source_episode_id=episode.episode_id,
    )
    first_claim = repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=owner.entity_id,
        predicate="prefers",
        object_entity_id=None,
        object_literal="I prefer concise replies.",
        fact_text="I prefer concise replies.",
        claim_type="preference",
        confidence=1.0,
        valid_from_utc="2026-04-11T12:00:00+00:00",
    )
    second_claim = repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=owner.entity_id,
        predicate="prefers_storage_backend",
        object_entity_id=backend.entity_id,
        object_literal=None,
        fact_text="From now on, always use Postgres for shared persistence.",
        claim_type="decision",
        confidence=1.0,
        valid_from_utc="2026-04-12T12:00:00+00:00",
        metadata={"section": "standing_decisions"},
    )
    evidence = repo.add_claim_evidence(
        claim_id=second_claim.claim_id,
        episode_id=episode.episode_id,
        evidence_text=second_claim.fact_text,
        evidence_kind="request",
        span={"section": "standing_decisions"},
    )
    link = repo.ensure_claim_link(
        subject_id="subject_user_1",
        from_claim_id=first_claim.claim_id,
        to_claim_id=second_claim.claim_id,
        link_type="supports",
        confidence=0.75,
        rationale={"reason": "same user preference thread"},
    )

    claims = repo.list_claims_for_subject(subject_id="subject_user_1", limit=10)
    searched = repo.search_claims(subject_id="subject_user_1", query_text="postgres persistence", limit=5)
    evidence_rows = repo.list_evidence_for_claims([second_claim.claim_id])
    links = repo.list_claim_links(subject_id="subject_user_1", claim_ids=[second_claim.claim_id], limit=5)
    episodes = repo.get_episodes([episode.episode_id])

    assert episode.episode_id
    assert owner.entity_id
    assert backend.entity_id
    assert second_claim.claim_id
    assert evidence.claim_evidence_id
    assert link.claim_link_id
    assert any(row.claim_id == second_claim.claim_id for row in claims)
    assert searched and searched[0].claim_id == second_claim.claim_id
    assert evidence_rows and evidence_rows[0].claim_id == second_claim.claim_id
    assert links and links[0].to_claim_id == second_claim.claim_id
    assert episodes and episodes[0].episode_id == episode.episode_id


def test_memory_graph_repository_filters_claims_by_effective_time(tmp_path):
    repo = SqliteMemoryGraphRepository(
        str(tmp_path / "task_history.db"),
        logger=logging.getLogger("test"),
        vector_enabled=False,
    )
    current = repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=None,
        predicate="prefers",
        object_entity_id=None,
        object_literal="current storage",
        fact_text="Current storage preference is Postgres.",
        claim_type="preference",
        confidence=1.0,
        valid_from_utc="2026-04-11T12:00:00+00:00",
        invalid_after_utc=None,
        embedding=_embedding(1.0, 0.0, 0.0),
    )
    future = repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=None,
        predicate="prefers",
        object_entity_id=None,
        object_literal="future storage",
        fact_text="Future storage preference is object storage.",
        claim_type="preference",
        confidence=1.0,
        valid_from_utc="2026-04-14T12:00:00+00:00",
        invalid_after_utc=None,
        embedding=_embedding(0.0, 1.0, 0.0),
    )
    expired = repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=None,
        predicate="prefers",
        object_entity_id=None,
        object_literal="expired storage",
        fact_text="Expired storage preference is SQLite.",
        claim_type="preference",
        confidence=1.0,
        valid_from_utc="2026-04-10T12:00:00+00:00",
        invalid_after_utc="2026-04-12T12:00:00+00:00",
        embedding=_embedding(0.0, 0.0, 1.0),
    )

    active_at_utc = "2026-04-13T12:00:00+00:00"
    active_claims = repo.list_claims_for_subject(
        subject_id="subject_user_1",
        limit=10,
        status="active",
        active_at_utc=active_at_utc,
    )
    searched = repo.search_claims(
        subject_id="subject_user_1",
        query_text="storage preference",
        limit=10,
        active_at_utc=active_at_utc,
    )
    vector_results = repo.search_claims_by_embedding(
        subject_id="subject_user_1",
        query_embedding=[1.0, 0.0, 0.0],
        limit=10,
        active_at_utc=active_at_utc,
    )
    administrative_claims = repo.list_claims_for_subject(
        subject_id="subject_user_1",
        limit=10,
        status="active",
    )

    assert [claim.claim_id for claim in active_claims] == [current.claim_id]
    assert [claim.claim_id for claim in searched] == [current.claim_id]
    assert [claim.claim_id for claim in vector_results] == [current.claim_id]
    assert {claim.claim_id for claim in administrative_claims} == {
        current.claim_id,
        future.claim_id,
        expired.claim_id,
    }


def test_sqlite_memory_graph_repository_loads_sqlite_vec_and_searches_vectors(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}}
    )
    repo = create_memory_graph_repository(settings, logger=logging.getLogger("test"))
    repo.activate_embedding_profile(
        provider="local",
        model_id=None,
        api_base=None,
        dimensions=3,
    )

    near = repo.ensure_entity(
        subject_id="subject_user_1",
        entity_type="concept",
        canonical_name="SQLite",
        embedding=_embedding(1.0, 0.0, 0.0),
    )
    repo.ensure_entity(
        subject_id="subject_user_1",
        entity_type="concept",
        canonical_name="Postgres",
        embedding=_embedding(0.0, 1.0, 0.0),
    )
    near_claim = repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=near.entity_id,
        predicate="prefers_storage_backend",
        object_entity_id=None,
        object_literal="SQLite",
        fact_text="Use SQLite with sqlite-vec for memory graph vectors.",
        claim_type="decision",
        confidence=1.0,
        embedding=_embedding(1.0, 0.0, 0.0),
    )
    repo.create_claim(
        subject_id="subject_user_1",
        subject_entity_id=None,
        predicate="mentions",
        object_entity_id=None,
        object_literal="unrelated",
        fact_text="Unrelated claim.",
        claim_type="observation",
        confidence=1.0,
        embedding=_embedding(0.0, 1.0, 0.0),
    )

    entity_results = repo.search_entities_by_embedding(
        subject_id="subject_user_1",
        query_embedding=[0.95, 0.05, 0.0],
        limit=2,
    )
    claim_results = repo.search_claims_by_embedding(
        subject_id="subject_user_1",
        query_embedding=[0.95, 0.05, 0.0],
        limit=2,
    )

    assert entity_results[0].entity_id == near.entity_id
    assert claim_results[0].claim_id == near_claim.claim_id
    with repo._connect() as conn:
        vector_state = conn.execute(f"SELECT dimensions FROM {MEMORY_GRAPH_VECTOR_STATE_TABLE}").fetchone()
        vector_rows = conn.execute(f"SELECT COUNT(*) FROM {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}").fetchone()[0]
        table_flags = {
            row["name"]: row["strict"]
            for row in conn.execute("PRAGMA table_list").fetchall()
            if row["name"] in {MEMORY_GRAPH_VECTOR_RECORDS_TABLE, MEMORY_GRAPH_VECTOR_STATE_TABLE}
        }
    assert vector_state["dimensions"] == 3
    assert vector_rows == 4
    assert table_flags == {
        MEMORY_GRAPH_VECTOR_RECORDS_TABLE: 1,
        MEMORY_GRAPH_VECTOR_STATE_TABLE: 1,
    }


def test_sqlite_memory_graph_repository_rejects_vector_dimension_mismatch(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}}
    )
    repo = create_memory_graph_repository(settings, logger=logging.getLogger("test"))
    repo.activate_embedding_profile(
        provider="local",
        model_id=None,
        api_base=None,
        dimensions=3,
    )
    repo.ensure_entity(
        subject_id="subject_user_1",
        entity_type="concept",
        canonical_name="SQLite",
        embedding=_embedding(1.0, 0.0, 0.0),
    )

    with pytest.raises(RuntimeError, match="dimension mismatch"):
        repo.ensure_entity(
            subject_id="subject_user_1",
            entity_type="concept",
            canonical_name="sqlite-vec",
            embedding=_embedding(1.0, 0.0),
        )


def test_sqlite_memory_graph_repository_fails_when_required_vector_extension_is_unavailable(
    tmp_path,
    monkeypatch,
):
    def _raise_load_error(*_args, **_kwargs):
        raise RuntimeError("sqlite-vec unavailable")

    monkeypatch.setattr(memory_graph_repository_module, "load_sqlite_vec", _raise_load_error)

    with pytest.raises(RuntimeError, match="sqlite-vec unavailable"):
        SqliteMemoryGraphRepository(str(tmp_path / "task_history.db"), logger=logging.getLogger("test"))
