"""Backend-neutral persistence for optional solution variation runs."""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from custom_messages.solution_variation import (
    SolutionCandidate,
    SolutionEvaluation,
    SolutionVariationRun,
    SolutionVariationStatus,
)

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    redact_postgres_dsn,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite


@dataclass(frozen=True)
class SolutionVariationBundle:
    run: SolutionVariationRun
    candidates: tuple[SolutionCandidate, ...]
    evaluations: tuple[SolutionEvaluation, ...]


class SolutionVariationScopeConflict(ValueError):
    """Raised when Task and Plan scopes hold different active runs."""


class SolutionVariationRepository(Protocol):
    def start_run(
        self, run: SolutionVariationRun
    ) -> tuple[SolutionVariationRun, bool]: ...

    def get_active_run(
        self,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
        scope_key: str,
    ) -> Optional[SolutionVariationRun]: ...

    def get_latest_run(
        self,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
        scope_key: str,
    ) -> Optional[SolutionVariationRun]: ...

    def promote_active_task_run_to_plan(
        self,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
        task_scope_key: str,
        plan_scope_key: str,
        plan_id: str,
    ) -> tuple[Optional[SolutionVariationRun], bool]: ...

    def get_bundle(
        self,
        run_id: str,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> Optional[SolutionVariationBundle]: ...

    def record_candidate(
        self,
        candidate: SolutionCandidate,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> tuple[SolutionCandidate, bool]: ...

    def record_evaluation(
        self,
        evaluation: SolutionEvaluation,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> tuple[SolutionEvaluation, bool]: ...

    def update_run(
        self,
        run: SolutionVariationRun,
        *,
        expected_revision: int,
    ) -> bool: ...


class _RelationalSolutionVariationRepository:
    """Shared record operations with bounded normalized candidate lineage."""

    _dialect: SqlDialect


    @staticmethod
    def _run_record_id(run: SolutionVariationRun) -> str:
        return f"{run.owner_agent_id}::{run.owner_access_scope}::{run.run_id}"

    @staticmethod
    def _candidate_record_id(
        candidate: SolutionCandidate,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> str:
        return (
            f"{owner_agent_id}::{owner_access_scope}::{candidate.run_id}::"
            f"{candidate.candidate_key}"
        )

    @staticmethod
    def _evaluation_record_id(
        evaluation: SolutionEvaluation,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> str:
        return (
            f"{owner_agent_id}::{owner_access_scope}::{evaluation.run_id}::"
            f"{evaluation.candidate_key}::"
            f"{evaluation.evaluation_key}"
        )

    @staticmethod
    def _active_scope_key(run: SolutionVariationRun) -> Optional[str]:
        if run.status != SolutionVariationStatus.ACTIVE:
            return None
        return "::".join((run.owner_agent_id, run.owner_access_scope, run.scope_key))

    @staticmethod
    def _scoped_active_key(
        owner_agent_id: str,
        owner_access_scope: str,
        scope_key: str,
    ) -> str:
        return "::".join((owner_agent_id, owner_access_scope, scope_key))

    @staticmethod
    def _require_scope(owner_agent_id: str, owner_access_scope: str) -> None:
        if not owner_agent_id or not owner_access_scope:
            raise ValueError(
                "Solution variation access requires manager and user/task scope."
            )

    def _init_schema(self) -> None:
        backend_name = (
            "Postgres"
            if isinstance(self, PostgresSolutionVariationRepository)
            else "SQLite"
        )
        storage_target = (
            redact_postgres_dsn(self._dsn)
            if backend_name == "Postgres"
            else self._db_path
        )
        ensure_storage_parent_dir(self._db_path)
        try:
            with self._connect() as conn:
                self._create_schema(conn)
                conn.commit()
            self._logger.info(
                "%s solution variation repository initialized at %s",
                backend_name,
                storage_target,
            )
        except Exception:
            self._logger.exception(
                "Failed to initialize %s solution variation repository",
                backend_name,
            )
            raise

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS solution_variation_runs (
                run_record_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                owner_agent_id TEXT NOT NULL,
                owner_access_scope TEXT NOT NULL,
                team_id TEXT,
                source_task_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                active_scope_key TEXT,
                plan_id TEXT,
                plan_step_id TEXT,
                objective_id TEXT,
                status TEXT NOT NULL,
                selected_candidate_key TEXT,
                revision INTEGER NOT NULL,
                run_data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_solution_variation_active_scope
            ON solution_variation_runs(active_scope_key)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_variation_task
            ON solution_variation_runs(owner_agent_id, owner_access_scope, source_task_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_variation_plan
            ON solution_variation_runs(owner_agent_id, owner_access_scope, plan_id, updated_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS solution_variation_candidates (
                candidate_record_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                owner_agent_id TEXT NOT NULL,
                owner_access_scope TEXT NOT NULL,
                candidate_key TEXT NOT NULL,
                parent_candidate_key TEXT,
                candidate_data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_variation_candidates_run
            ON solution_variation_candidates(owner_agent_id, owner_access_scope, run_id, created_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS solution_variation_evaluations (
                evaluation_record_id TEXT PRIMARY KEY,
                evaluation_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                owner_agent_id TEXT NOT NULL,
                owner_access_scope TEXT NOT NULL,
                candidate_key TEXT NOT NULL,
                evaluation_key TEXT NOT NULL,
                hard_constraints_passed INTEGER NOT NULL,
                evaluation_data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_solution_variation_evaluations_run
            ON solution_variation_evaluations(owner_agent_id, owner_access_scope, run_id, candidate_key, created_at)
            """
        )

    def _get_run_row(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ):
        return conn.execute(
            f"""
            SELECT run_data
            FROM solution_variation_runs
            WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND owner_access_scope = {self._dialect.bind}
            LIMIT 1
            """,
            (run_id, owner_agent_id, owner_access_scope),
        ).fetchone()

    def start_run(self, run: SolutionVariationRun) -> tuple[SolutionVariationRun, bool]:
        self._require_scope(run.owner_agent_id, run.owner_access_scope)
        if run.status != SolutionVariationStatus.ACTIVE:
            raise ValueError("A new solution variation run must be active.")
        try:
            with self._connect() as conn:
                conn.begin_write()
                existing = conn.execute(
                    f"""
                    SELECT run_data
                    FROM solution_variation_runs
                    WHERE active_scope_key = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (self._active_scope_key(run),),
                ).fetchone()
                if existing:
                    return SolutionVariationRun.model_validate_json(existing[0]), False
                conn.execute(
                    f"""
                    INSERT INTO solution_variation_runs (
                        run_record_id, run_id, owner_agent_id, owner_access_scope,
                        team_id, source_task_id, scope_key, active_scope_key,
                        plan_id, plan_step_id, objective_id, status,
                        selected_candidate_key, revision, run_data, created_at, updated_at
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        self._run_record_id(run),
                        run.run_id,
                        run.owner_agent_id,
                        run.owner_access_scope,
                        run.team_id,
                        run.source_task_id,
                        run.scope_key,
                        self._active_scope_key(run),
                        run.plan_id,
                        run.plan_step_id,
                        run.objective_id,
                        run.status.value,
                        run.selected_candidate_key,
                        run.revision,
                        run.model_dump_json(),
                        run.created_at,
                        run.updated_at,
                    ),
                )
                conn.commit()
                return run, True
        except Exception:
            existing = self.get_active_run(
                owner_agent_id=run.owner_agent_id,
                owner_access_scope=run.owner_access_scope,
                scope_key=run.scope_key,
            )
            if existing is not None:
                return existing, False
            raise

    def get_active_run(
        self,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
        scope_key: str,
    ) -> Optional[SolutionVariationRun]:
        self._require_scope(owner_agent_id, owner_access_scope)
        active_scope_key = self._scoped_active_key(
            owner_agent_id,
            owner_access_scope,
            scope_key,
        )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT run_data
                FROM solution_variation_runs
                WHERE active_scope_key = {self._dialect.bind}
                LIMIT 1
                """,
                (active_scope_key,),
            ).fetchone()
        return SolutionVariationRun.model_validate_json(row[0]) if row else None

    def promote_active_task_run_to_plan(
        self,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
        task_scope_key: str,
        plan_scope_key: str,
        plan_id: str,
    ) -> tuple[Optional[SolutionVariationRun], bool]:
        """Resolve the active Plan run or atomically promote the active Task run."""

        self._require_scope(owner_agent_id, owner_access_scope)
        normalized_task_scope = str(task_scope_key or "").strip()
        normalized_plan_scope = str(plan_scope_key or "").strip()
        normalized_plan_id = str(plan_id or "").strip()
        if not normalized_task_scope.startswith("task:"):
            raise ValueError("task_scope_key must identify a Task scope.")
        if (
            not normalized_plan_id
            or normalized_plan_scope != f"plan:{normalized_plan_id}"
        ):
            raise ValueError("plan_scope_key must identify the supplied Plan.")

        task_active_key = self._scoped_active_key(
            owner_agent_id,
            owner_access_scope,
            normalized_task_scope,
        )
        plan_active_key = self._scoped_active_key(
            owner_agent_id,
            owner_access_scope,
            normalized_plan_scope,
        )
        is_postgres = isinstance(self, PostgresSolutionVariationRepository)
        lock_clause = " FOR UPDATE" if is_postgres else ""

        def _active_row(conn, active_scope_key: str):
            return conn.execute(
                f"""
                SELECT run_data, plan_id, scope_key, revision
                FROM solution_variation_runs
                WHERE active_scope_key = {conn.dialect.bind}
                LIMIT 1{lock_clause}
                """,
                (active_scope_key,),
            ).fetchone()

        def _conflict(
            task_run: SolutionVariationRun,
            plan_run: SolutionVariationRun,
        ) -> SolutionVariationScopeConflict:
            return SolutionVariationScopeConflict(
                "Different active solution variations exist for the originating "
                f"Task ({task_run.run_id}) and current Plan ({plan_run.run_id}). "
                "No run was changed; resolve the persisted conflict explicitly."
            )

        try:
            with self._connect() as conn:
                conn.begin_write()

                # Keep the lock order stable for every promotion attempt.
                plan_row = _active_row(conn, plan_active_key)
                task_row = _active_row(conn, task_active_key)
                if plan_row is None and task_row is None and is_postgres:
                    # A concurrent promoter can move the Task row while the
                    # second SELECT waits for its row lock. Refresh the Plan key
                    # before treating the run as absent.
                    plan_row = _active_row(conn, plan_active_key)

                plan_run = (
                    SolutionVariationRun.model_validate_json(plan_row[0])
                    if plan_row is not None
                    else None
                )
                task_run = (
                    SolutionVariationRun.model_validate_json(task_row[0])
                    if task_row is not None
                    else None
                )
                if plan_run is not None and task_run is not None:
                    raise _conflict(task_run, plan_run)
                if plan_run is not None:
                    if (
                        plan_run.status != SolutionVariationStatus.ACTIVE
                        or plan_run.scope_key != normalized_plan_scope
                        or plan_row[2] != normalized_plan_scope
                        or plan_run.plan_id != normalized_plan_id
                        or plan_row[1] != normalized_plan_id
                        or plan_run.revision != int(plan_row[3])
                    ):
                        raise RuntimeError(
                            "Plan-scoped solution variation columns disagree with its "
                            "serialized record."
                        )
                    conn.commit()
                    return plan_run, False
                if task_run is None:
                    conn.commit()
                    return None, False

                if task_run.status != SolutionVariationStatus.ACTIVE:
                    raise RuntimeError(
                        "Only an active Task-scoped solution variation can be promoted."
                    )
                if (
                    task_run.scope_key != normalized_task_scope
                    or task_row[2] != normalized_task_scope
                    or task_run.revision != int(task_row[3])
                ):
                    raise RuntimeError(
                        "Task-scoped solution variation columns disagree with its serialized record."
                    )
                if task_run.plan_id is not None or task_row[1] is not None:
                    raise SolutionVariationScopeConflict(
                        "A Task-scoped solution variation already belongs to a Plan; "
                        "it cannot be moved to another Plan."
                    )

                promoted = task_run.model_copy(
                    update={
                        "scope_key": normalized_plan_scope,
                        "plan_id": normalized_plan_id,
                        "revision": task_run.revision + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                cursor = conn.execute(
                    f"""
                    UPDATE solution_variation_runs
                    SET scope_key = {self._dialect.bind}, active_scope_key = {self._dialect.bind}, plan_id = {self._dialect.bind},
                        revision = {self._dialect.bind}, run_data = {self._dialect.bind}, updated_at = {self._dialect.bind}
                    WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                      AND owner_access_scope = {self._dialect.bind} AND active_scope_key = {self._dialect.bind}
                      AND scope_key = {self._dialect.bind} AND plan_id IS NULL
                      AND revision = {self._dialect.bind} AND status = {self._dialect.bind}
                    """,
                    (
                        normalized_plan_scope,
                        plan_active_key,
                        normalized_plan_id,
                        promoted.revision,
                        promoted.model_dump_json(),
                        promoted.updated_at,
                        task_run.run_id,
                        owner_agent_id,
                        owner_access_scope,
                        task_active_key,
                        normalized_task_scope,
                        task_run.revision,
                        SolutionVariationStatus.ACTIVE.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "The Task-scoped solution variation changed during Plan promotion."
                    )
                conn.commit()
                return promoted, True
        except SolutionVariationScopeConflict:
            raise
        except Exception as exc:
            # A concurrent direct Plan insert can win the unique Plan-scope key
            # between the reads and update. Report the resulting dual active
            # state as a conflict instead of hiding the Task run.
            plan_run = self.get_active_run(
                owner_agent_id=owner_agent_id,
                owner_access_scope=owner_access_scope,
                scope_key=normalized_plan_scope,
            )
            task_run = self.get_active_run(
                owner_agent_id=owner_agent_id,
                owner_access_scope=owner_access_scope,
                scope_key=normalized_task_scope,
            )
            if plan_run is not None and task_run is not None:
                raise _conflict(task_run, plan_run) from exc
            raise

    def get_latest_run(
        self,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
        scope_key: str,
    ) -> Optional[SolutionVariationRun]:
        self._require_scope(owner_agent_id, owner_access_scope)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT run_data
                FROM solution_variation_runs
                WHERE owner_agent_id = {self._dialect.bind} AND owner_access_scope = {self._dialect.bind} AND scope_key = {self._dialect.bind}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (owner_agent_id, owner_access_scope, scope_key),
            ).fetchone()
        return SolutionVariationRun.model_validate_json(row[0]) if row else None

    def get_bundle(
        self,
        run_id: str,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> Optional[SolutionVariationBundle]:
        self._require_scope(owner_agent_id, owner_access_scope)
        with self._connect() as conn:
            run_row = self._get_run_row(
                conn,
                run_id,
                owner_agent_id=owner_agent_id,
                owner_access_scope=owner_access_scope,
            )
            if run_row is None:
                return None
            candidate_rows = conn.execute(
                f"""
                SELECT candidate_data
                FROM solution_variation_candidates
                WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND owner_access_scope = {self._dialect.bind}
                ORDER BY created_at, candidate_key
                """,
                (run_id, owner_agent_id, owner_access_scope),
            ).fetchall()
            evaluation_rows = conn.execute(
                f"""
                SELECT evaluation_data
                FROM solution_variation_evaluations
                WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND owner_access_scope = {self._dialect.bind}
                ORDER BY created_at, candidate_key, evaluation_key
                """,
                (run_id, owner_agent_id, owner_access_scope),
            ).fetchall()
        return SolutionVariationBundle(
            run=SolutionVariationRun.model_validate_json(run_row[0]),
            candidates=tuple(
                SolutionCandidate.model_validate_json(row[0]) for row in candidate_rows
            ),
            evaluations=tuple(
                SolutionEvaluation.model_validate_json(row[0])
                for row in evaluation_rows
            ),
        )

    def record_candidate(
        self,
        candidate: SolutionCandidate,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> tuple[SolutionCandidate, bool]:
        self._require_scope(owner_agent_id, owner_access_scope)
        with self._connect() as conn:
            run_row = self._get_run_row(
                conn,
                candidate.run_id,
                owner_agent_id=owner_agent_id,
                owner_access_scope=owner_access_scope,
            )
            if run_row is None:
                raise ValueError(
                    "Solution variation run not found in the active owner scope."
                )
            existing = conn.execute(
                f"""
                SELECT candidate_data
                FROM solution_variation_candidates
                WHERE candidate_record_id = {self._dialect.bind}
                LIMIT 1
                """,
                (
                    self._candidate_record_id(
                        candidate,
                        owner_agent_id=owner_agent_id,
                        owner_access_scope=owner_access_scope,
                    ),
                ),
            ).fetchone()
            if existing:
                return SolutionCandidate.model_validate_json(existing[0]), False
            run = SolutionVariationRun.model_validate_json(run_row[0])
            if run.status != SolutionVariationStatus.ACTIVE:
                raise ValueError(
                    "Cannot add a candidate to a closed solution variation."
                )
            if candidate.parent_candidate_key:
                parent = conn.execute(
                    f"""
                    SELECT candidate_record_id
                    FROM solution_variation_candidates
                    WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                      AND owner_access_scope = {self._dialect.bind} AND candidate_key = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (
                        candidate.run_id,
                        owner_agent_id,
                        owner_access_scope,
                        candidate.parent_candidate_key,
                    ),
                ).fetchone()
                if parent is None:
                    raise ValueError(
                        "Candidate parent does not belong to this solution variation."
                    )
            count_row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM solution_variation_candidates
                WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind} AND owner_access_scope = {self._dialect.bind}
                """,
                (candidate.run_id, owner_agent_id, owner_access_scope),
            ).fetchone()
            if count_row and int(count_row[0]) >= run.candidate_limit:
                raise ValueError(
                    f"Candidate limit {run.candidate_limit} has been reached for this run."
                )
            cursor = conn.execute(
                f"""
                INSERT INTO solution_variation_candidates (
                    candidate_record_id, candidate_id, run_id,
                    owner_agent_id, owner_access_scope, candidate_key,
                    parent_candidate_key, candidate_data, created_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(candidate_record_id) DO NOTHING
                """,
                (
                    self._candidate_record_id(
                        candidate,
                        owner_agent_id=owner_agent_id,
                        owner_access_scope=owner_access_scope,
                    ),
                    candidate.candidate_id,
                    candidate.run_id,
                    owner_agent_id,
                    owner_access_scope,
                    candidate.candidate_key,
                    candidate.parent_candidate_key,
                    candidate.model_dump_json(),
                    candidate.created_at,
                ),
            )
            if cursor.rowcount == 0:
                replay = conn.execute(
                    f"""
                    SELECT candidate_data
                    FROM solution_variation_candidates
                    WHERE candidate_record_id = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (
                        self._candidate_record_id(
                            candidate,
                            owner_agent_id=owner_agent_id,
                            owner_access_scope=owner_access_scope,
                        ),
                    ),
                ).fetchone()
                if replay is None:
                    raise RuntimeError(
                        "Concurrent candidate replay could not be reloaded."
                    )
                return SolutionCandidate.model_validate_json(replay[0]), False
            conn.commit()
            return candidate, True

    def record_evaluation(
        self,
        evaluation: SolutionEvaluation,
        *,
        owner_agent_id: str,
        owner_access_scope: str,
    ) -> tuple[SolutionEvaluation, bool]:
        self._require_scope(owner_agent_id, owner_access_scope)
        with self._connect() as conn:
            run_row = self._get_run_row(
                conn,
                evaluation.run_id,
                owner_agent_id=owner_agent_id,
                owner_access_scope=owner_access_scope,
            )
            if run_row is None:
                raise ValueError(
                    "Solution variation run not found in the active owner scope."
                )
            existing = conn.execute(
                f"""
                SELECT evaluation_data
                FROM solution_variation_evaluations
                WHERE evaluation_record_id = {self._dialect.bind}
                LIMIT 1
                """,
                (
                    self._evaluation_record_id(
                        evaluation,
                        owner_agent_id=owner_agent_id,
                        owner_access_scope=owner_access_scope,
                    ),
                ),
            ).fetchone()
            if existing:
                return SolutionEvaluation.model_validate_json(existing[0]), False
            run = SolutionVariationRun.model_validate_json(run_row[0])
            if run.status != SolutionVariationStatus.ACTIVE:
                raise ValueError(
                    "Cannot add an evaluation to a closed solution variation."
                )
            candidate = conn.execute(
                f"""
                SELECT candidate_record_id
                FROM solution_variation_candidates
                WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                  AND owner_access_scope = {self._dialect.bind} AND candidate_key = {self._dialect.bind}
                LIMIT 1
                """,
                (
                    evaluation.run_id,
                    owner_agent_id,
                    owner_access_scope,
                    evaluation.candidate_key,
                ),
            ).fetchone()
            if candidate is None:
                raise ValueError(
                    "Evaluation candidate does not belong to this solution variation."
                )
            cursor = conn.execute(
                f"""
                INSERT INTO solution_variation_evaluations (
                    evaluation_record_id, evaluation_id, run_id,
                    owner_agent_id, owner_access_scope, candidate_key,
                    evaluation_key, hard_constraints_passed,
                    evaluation_data, created_at
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(evaluation_record_id) DO NOTHING
                """,
                (
                    self._evaluation_record_id(
                        evaluation,
                        owner_agent_id=owner_agent_id,
                        owner_access_scope=owner_access_scope,
                    ),
                    evaluation.evaluation_id,
                    evaluation.run_id,
                    owner_agent_id,
                    owner_access_scope,
                    evaluation.candidate_key,
                    evaluation.evaluation_key,
                    int(evaluation.hard_constraints_passed),
                    evaluation.model_dump_json(),
                    evaluation.created_at,
                ),
            )
            if cursor.rowcount == 0:
                replay = conn.execute(
                    f"""
                    SELECT evaluation_data
                    FROM solution_variation_evaluations
                    WHERE evaluation_record_id = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (
                        self._evaluation_record_id(
                            evaluation,
                            owner_agent_id=owner_agent_id,
                            owner_access_scope=owner_access_scope,
                        ),
                    ),
                ).fetchone()
                if replay is None:
                    raise RuntimeError(
                        "Concurrent evaluation replay could not be reloaded."
                    )
                return SolutionEvaluation.model_validate_json(replay[0]), False
            conn.commit()
            return evaluation, True

    def update_run(
        self,
        run: SolutionVariationRun,
        *,
        expected_revision: int,
    ) -> bool:
        self._require_scope(run.owner_agent_id, run.owner_access_scope)
        with self._connect() as conn:
            if run.selected_candidate_key:
                candidate = conn.execute(
                    f"""
                    SELECT candidate_record_id
                    FROM solution_variation_candidates
                    WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                      AND owner_access_scope = {self._dialect.bind} AND candidate_key = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (
                        run.run_id,
                        run.owner_agent_id,
                        run.owner_access_scope,
                        run.selected_candidate_key,
                    ),
                ).fetchone()
                if candidate is None:
                    raise ValueError(
                        "Selected candidate does not belong to this solution variation."
                    )
                evaluation = conn.execute(
                    f"""
                    SELECT hard_constraints_passed
                    FROM solution_variation_evaluations
                    WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                      AND owner_access_scope = {self._dialect.bind} AND candidate_key = {self._dialect.bind}
                      AND evaluation_key = {self._dialect.bind}
                    LIMIT 1
                    """,
                    (
                        run.run_id,
                        run.owner_agent_id,
                        run.owner_access_scope,
                        run.selected_candidate_key,
                        run.selected_evaluation_key,
                    ),
                ).fetchone()
                if evaluation is None or not bool(evaluation[0]):
                    raise ValueError(
                        "Selected candidate requires a passing evaluation from this run."
                    )
            cursor = conn.execute(
                f"""
                UPDATE solution_variation_runs
                SET active_scope_key = {self._dialect.bind}, status = {self._dialect.bind}, selected_candidate_key = {self._dialect.bind},
                    revision = {self._dialect.bind}, run_data = {self._dialect.bind}, updated_at = {self._dialect.bind}
                WHERE run_id = {self._dialect.bind} AND owner_agent_id = {self._dialect.bind}
                  AND owner_access_scope = {self._dialect.bind} AND revision = {self._dialect.bind} AND status = {self._dialect.bind}
                """,
                (
                    self._active_scope_key(run),
                    run.status.value,
                    run.selected_candidate_key,
                    run.revision,
                    run.model_dump_json(),
                    run.updated_at,
                    run.run_id,
                    run.owner_agent_id,
                    run.owner_access_scope,
                    expected_revision,
                    SolutionVariationStatus.ACTIVE.value,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1


class SqliteSolutionVariationRepository(_RelationalSolutionVariationRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path, logger=self._logger)


class PostgresSolutionVariationRepository(_RelationalSolutionVariationRepository):
    """PostgreSQL lifecycle over shared record operations and native SQL."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(self._dsn)
        self._init_schema()

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)


def create_solution_variation_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> SolutionVariationRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="SolutionVariationRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if (
        storage_settings.backend == STORAGE_BACKEND_SQLITE
        and storage_settings.sqlite_path
    ):
        return SqliteSolutionVariationRepository(
            storage_settings.sqlite_path,
            logger=logger,
        )
    if (
        storage_settings.backend == STORAGE_BACKEND_POSTGRES
        and storage_settings.postgres_dsn
    ):
        return PostgresSolutionVariationRepository(
            storage_settings.postgres_dsn,
            logger=logger,
        )
    raise NotImplementedError(
        "SolutionVariationRepository has no implementation for backend "
        f"{storage_settings.backend!r}."
    )
