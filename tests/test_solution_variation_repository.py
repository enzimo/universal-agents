import logging
from pathlib import Path

import pytest

from custom_messages.solution_variation import (
    SolutionCandidate,
    SolutionEvaluation,
    SolutionEvaluationOutcome,
    SolutionVariationRun,
    SolutionVariationStatus,
)
from storage.config import resolve_storage_settings
from storage.solution_variation_repository import (
    PostgresSolutionVariationRepository,
    SolutionVariationScopeConflict,
    create_solution_variation_repository,
)
from tests.postgres_test_helpers import patch_fake_postgres


def _settings(tmp_path: Path):
    return resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": "runtime/task_history.db"}},
        base_dir=str(tmp_path),
    )


def _run(*, owner_scope: str = "user:user-a", run_id: str = "run_shared"):
    return SolutionVariationRun(
        run_id=run_id,
        purpose="Compare two materially different implementations.",
        evaluation_criteria=["correctness", "maintenance cost"],
        constraints=["preserve the public contract"],
        owner_agent_id="manager_scope:team-1:TeamArchitect",
        owner_agent_name="Manager",
        owner_access_scope=owner_scope,
        team_id="team-1",
        source_task_id="task-root",
        scope_key="task:task-root",
    )


def test_solution_variation_repository_round_trip_and_owner_scope(
    tmp_path: Path,
) -> None:
    repo = create_solution_variation_repository(
        _settings(tmp_path),
        logger=logging.getLogger("test_solution_variation_repository"),
    )
    first, created = repo.start_run(_run())
    assert created is True
    replayed, replay_created = repo.start_run(_run())
    assert replay_created is False
    assert replayed.run_id == first.run_id

    other_owner, other_created = repo.start_run(_run(owner_scope="user:user-b"))
    assert other_created is True
    assert other_owner.owner_access_scope == "user:user-b"

    candidate = SolutionCandidate(
        run_id=first.run_id,
        candidate_key="direct",
        title="Direct implementation",
        hypothesis="The existing extension seam is sufficient.",
        approach="Add the sidecar without changing Task transport.",
    )
    stored_candidate, candidate_created = repo.record_candidate(
        candidate,
        owner_agent_id=first.owner_agent_id,
        owner_access_scope=first.owner_access_scope,
    )
    assert candidate_created is True
    assert stored_candidate.candidate_key == "direct"

    evaluation = SolutionEvaluation(
        run_id=first.run_id,
        candidate_key="direct",
        evaluation_key="unit-tests",
        outcome=SolutionEvaluationOutcome.SUPPORTS,
        summary="Repository and tool tests pass.",
        hard_constraints_passed=True,
        evidence_refs=["pytest:tests/test_solution_variation_repository.py"],
    )
    _, evaluation_created = repo.record_evaluation(
        evaluation,
        owner_agent_id=first.owner_agent_id,
        owner_access_scope=first.owner_access_scope,
    )
    assert evaluation_created is True

    bundle = repo.get_bundle(
        first.run_id,
        owner_agent_id=first.owner_agent_id,
        owner_access_scope=first.owner_access_scope,
    )
    assert bundle is not None
    assert [item.candidate_key for item in bundle.candidates] == ["direct"]
    assert [item.evaluation_key for item in bundle.evaluations] == ["unit-tests"]
    assert (
        repo.get_bundle(
            first.run_id,
            owner_agent_id=first.owner_agent_id,
            owner_access_scope="user:user-b",
        )
        is not None
    )
    other_bundle = repo.get_bundle(
        other_owner.run_id,
        owner_agent_id=other_owner.owner_agent_id,
        owner_access_scope=other_owner.owner_access_scope,
    )
    assert other_bundle is not None
    assert other_bundle.candidates == ()


def test_solution_variation_repository_compare_and_set_closes_active_scope(
    tmp_path: Path,
) -> None:
    repo = create_solution_variation_repository(_settings(tmp_path))
    run, _ = repo.start_run(_run())
    selected = run.model_copy(
        update={
            "status": SolutionVariationStatus.COMPLETED,
            "revision": 2,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    assert repo.update_run(selected, expected_revision=1) is True
    assert repo.update_run(selected, expected_revision=1) is False
    assert (
        repo.get_active_run(
            owner_agent_id=run.owner_agent_id,
            owner_access_scope=run.owner_access_scope,
            scope_key=run.scope_key,
        )
        is None
    )
    latest = repo.get_latest_run(
        owner_agent_id=run.owner_agent_id,
        owner_access_scope=run.owner_access_scope,
        scope_key=run.scope_key,
    )
    assert latest is not None
    assert latest.status.value == "completed"


def test_solution_variation_repository_rejects_distinct_task_and_plan_runs(
    tmp_path: Path,
) -> None:
    repo = create_solution_variation_repository(_settings(tmp_path))
    task_run, _ = repo.start_run(_run(run_id="run_task"))
    plan_run, created = repo.start_run(
        _run(run_id="run_plan").model_copy(
            update={"scope_key": "plan:plan-first", "plan_id": "plan-first"}
        )
    )
    assert created is True

    with pytest.raises(SolutionVariationScopeConflict, match="Different active"):
        repo.promote_active_task_run_to_plan(
            owner_agent_id=task_run.owner_agent_id,
            owner_access_scope=task_run.owner_access_scope,
            task_scope_key="task:task-root",
            plan_scope_key="plan:plan-first",
            plan_id="plan-first",
        )

    assert (
        repo.get_active_run(
            owner_agent_id=task_run.owner_agent_id,
            owner_access_scope=task_run.owner_access_scope,
            scope_key=task_run.scope_key,
        ).run_id
        == task_run.run_id
    )
    assert (
        repo.get_active_run(
            owner_agent_id=plan_run.owner_agent_id,
            owner_access_scope=plan_run.owner_access_scope,
            scope_key=plan_run.scope_key,
        ).run_id
        == plan_run.run_id
    )


def test_postgres_solution_variation_promotion_locks_scope_rows(monkeypatch) -> None:
    from storage.sql_dialect import POSTGRES
    task_run = _run(run_id="run_postgres_lock")
    plan_active_key = (
        f"{task_run.owner_agent_id}::{task_run.owner_access_scope}::plan:plan-first"
    )
    task_active_key = (
        f"{task_run.owner_agent_id}::{task_run.owner_access_scope}::{task_run.scope_key}"
    )

    class Cursor:
        def __init__(self, *, row=None, rowcount: int = 0):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class Connection:
        dialect = POSTGRES

        def begin_write(self):
            pass

        def __init__(self):
            self.queries = []
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query, params=None):
            self.queries.append((query, params))
            if "SELECT run_data, plan_id, scope_key, revision" in query:
                if params == (plan_active_key,):
                    return Cursor(row=None)
                assert params == (task_active_key,)
                return Cursor(
                    row=(
                        task_run.model_dump_json(),
                        None,
                        task_run.scope_key,
                        task_run.revision,
                    )
                )
            if "UPDATE solution_variation_runs" in query:
                return Cursor(rowcount=1)
            raise AssertionError(f"Unexpected SQL: {query}")

        def commit(self):
            self.commits += 1

    connection = Connection()
    repo = PostgresSolutionVariationRepository.__new__(
        PostgresSolutionVariationRepository
    )
    monkeypatch.setattr(repo, "_connect", lambda: connection)

    promoted, changed = repo.promote_active_task_run_to_plan(
        owner_agent_id=task_run.owner_agent_id,
        owner_access_scope=task_run.owner_access_scope,
        task_scope_key=task_run.scope_key,
        plan_scope_key="plan:plan-first",
        plan_id="plan-first",
    )

    assert changed is True
    assert promoted is not None
    assert promoted.run_id == task_run.run_id
    assert promoted.plan_id == "plan-first"
    select_queries = [
        query
        for query, _ in connection.queries
        if "SELECT run_data, plan_id, scope_key, revision" in query
    ]
    assert len(select_queries) == 2
    assert all("FOR UPDATE" in query for query in select_queries)
    assert connection.commits == 1


def test_solution_variation_repository_accepts_postgres_when_driver_is_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    patch_fake_postgres(monkeypatch)

    repo = create_solution_variation_repository(settings)
    assert repo is not None
