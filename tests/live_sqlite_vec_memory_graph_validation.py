# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised in live runtime only
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.memory_graph_repository import (
    MEMORY_GRAPH_VECTOR_RECORDS_TABLE,
    MEMORY_GRAPH_VECTOR_STATE_TABLE,
    create_memory_graph_repository,
)
from storage.sqlite_connection import DEFAULT_SQLITE_BUSY_TIMEOUT_MS
from live_storage_utils import resolve_live_storage_settings


logger = logging.getLogger(__name__)
RESULT_DIR = PROJECT_ROOT / "test_results" / "live_sqlite_vec_validation"


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def reset_sqlite_database(path: Path) -> None:
    for candidate in (path, path.with_suffix(path.suffix + "-wal"), path.with_suffix(path.suffix + "-shm")):
        if candidate.exists():
            candidate.unlink()


def exercise_memory_graph_sqlite_vec(report: dict[str, Any], *, config_path: Path) -> None:
    settings = resolve_live_storage_settings(
        sqlite_fallback_path=RESULT_DIR / "task_history.db",
        workspace_dir=PROJECT_ROOT,
        config_path=config_path,
    )
    if settings.backend != "sqlite" or not settings.sqlite_path:
        raise RuntimeError(
            f"Live sqlite-vec validation requires storage.backend=sqlite, got {settings.backend!r}."
        )

    sqlite_path = Path(settings.sqlite_path)
    reset_sqlite_database(sqlite_path)
    repo = create_memory_graph_repository(settings, logger=logger)
    repo.activate_embedding_profile(
        provider="local",
        model_id=None,
        api_base=None,
        dimensions=3,
        metadata={"source": "live_sqlite_vec_validation"},
    )

    sqlite_entity = repo.ensure_entity(
        subject_id="subject_live_sqlite",
        entity_type="concept",
        canonical_name="SQLite sqlite-vec",
        embedding=json.dumps([1.0, 0.0, 0.0]),
    )
    repo.ensure_entity(
        subject_id="subject_live_sqlite",
        entity_type="concept",
        canonical_name="Postgres pgvector",
        embedding=json.dumps([0.0, 1.0, 0.0]),
    )
    sqlite_claim = repo.create_claim(
        subject_id="subject_live_sqlite",
        subject_entity_id=sqlite_entity.entity_id,
        predicate="preferred_backend",
        object_entity_id=None,
        object_literal="sqlite",
        fact_text="SQLite with sqlite-vec is the preferred memory graph vector backend.",
        claim_type="decision",
        confidence=1.0,
        embedding=json.dumps([1.0, 0.0, 0.0]),
    )
    repo.create_claim(
        subject_id="subject_live_sqlite",
        subject_entity_id=None,
        predicate="alternative_backend",
        object_entity_id=None,
        object_literal="postgres",
        fact_text="Postgres remains available as an alternate storage backend.",
        claim_type="observation",
        confidence=1.0,
        embedding=json.dumps([0.0, 1.0, 0.0]),
    )

    entity_results = repo.search_entities_by_embedding(
        subject_id="subject_live_sqlite",
        query_embedding=[0.98, 0.02, 0.0],
        limit=2,
    )
    claim_results = repo.search_claims_by_embedding(
        subject_id="subject_live_sqlite",
        query_embedding=[0.98, 0.02, 0.0],
        limit=2,
    )
    if not entity_results or entity_results[0].entity_id != sqlite_entity.entity_id:
        raise AssertionError(f"sqlite-vec entity search returned unexpected rows: {entity_results!r}")
    if not claim_results or claim_results[0].claim_id != sqlite_claim.claim_id:
        raise AssertionError(f"sqlite-vec claim search returned unexpected rows: {claim_results!r}")

    with repo._connect() as conn:
        vector_state = conn.execute(
            f"SELECT provider, dimensions, sqlite_vec_version FROM {MEMORY_GRAPH_VECTOR_STATE_TABLE}"
        ).fetchone()
        vector_records = conn.execute(f"SELECT COUNT(*) FROM {MEMORY_GRAPH_VECTOR_RECORDS_TABLE}").fetchone()[0]
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        strict_tables = {
            row["name"]: int(row["strict"])
            for row in conn.execute("PRAGMA table_list").fetchall()
            if row["name"] in {MEMORY_GRAPH_VECTOR_RECORDS_TABLE, MEMORY_GRAPH_VECTOR_STATE_TABLE}
        }

    if journal_mode != "wal":
        raise AssertionError(f"Expected SQLite journal_mode=wal, got {journal_mode!r}.")
    if synchronous != 1:
        raise AssertionError(f"Expected SQLite synchronous=NORMAL (1), got {synchronous!r}.")
    if busy_timeout != DEFAULT_SQLITE_BUSY_TIMEOUT_MS:
        raise AssertionError(
            f"Expected SQLite busy_timeout={DEFAULT_SQLITE_BUSY_TIMEOUT_MS}, got {busy_timeout!r}."
        )
    expected_strict_tables = {
        MEMORY_GRAPH_VECTOR_RECORDS_TABLE: 1,
        MEMORY_GRAPH_VECTOR_STATE_TABLE: 1,
    }
    if strict_tables != expected_strict_tables:
        raise AssertionError(f"Expected strict vector metadata tables, got {strict_tables!r}.")

    report["storage"] = {"backend": settings.backend, "sqlite_path": settings.sqlite_path}
    report["checks"] = [
        {
            "name": "sqlite_vec_vector_search",
            "details": {
                "top_entity_id": entity_results[0].entity_id,
                "top_claim_id": claim_results[0].claim_id,
                "vector_records": int(vector_records),
                "vector_provider": vector_state["provider"],
                "vector_dimensions": int(vector_state["dimensions"]),
                "sqlite_vec_version": vector_state["sqlite_vec_version"],
                "journal_mode": journal_mode,
                "synchronous": synchronous,
                "busy_timeout_ms": busy_timeout,
                "strict_tables": strict_tables,
            },
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate live SQLite sqlite-vec memory graph vector support.")
    parser.add_argument("--env", default="debug.env", help="Optional env file to load before resolving storage.")
    parser.add_argument("--config", default="config.yaml", help="Config file used to resolve live storage backend.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.env and load_dotenv is not None and Path(args.env).exists():
        load_dotenv(args.env, override=False)

    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"result_dir": str(RESULT_DIR)}
    exercise_memory_graph_sqlite_vec(report, config_path=Path(args.config))
    write_json_file(RESULT_DIR / "report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
