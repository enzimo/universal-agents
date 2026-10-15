"""Durable owner-scoped control plane for deterministic execution workloads."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from typing import Any, cast, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from runtime_hosting.execution_contracts import (
    EXECUTION_AUTHORITY_SCHEMA_VERSION,
    ActivationRecordV2,
    AutomationJobV2,
    BuildAttestationV1,
    CodeArtifactV1,
    ExecutionRevisionV1,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    ManagedServiceV1,
    RunAttemptStatus,
    RunAttemptV1,
    ServiceDesiredState,
    TERMINAL_RUN_ATTEMPT_STATUSES,
)
from storage.backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings
from storage.postgres_connection import (
    PostgresConnection,
    connect_postgres,
    ensure_storage_parent_dir,
    redact_postgres_dsn,
    validate_postgres_connection,
)
from storage.runtime import describe_storage_capabilities, require_repository_backend
from storage.sqlite_connection import connect_sqlite


_ImmutableModel = CodeArtifactV1 | BuildAttestationV1 | ExecutionRevisionV1 | ActivationRecordV2
_ModelT = TypeVar("_ModelT", bound=_ImmutableModel)
_AuthorityModelT = TypeVar(
    "_AuthorityModelT", AutomationJobV2, ManagedServiceV1, InteractiveSessionV1
)
_CLAIMABLE_STATUSES = (
    RunAttemptStatus.PENDING,
    RunAttemptStatus.CLAIMED,
    RunAttemptStatus.STARTING,
    RunAttemptStatus.RUNNING,
)
# Tables whose records carry execution authority (schema version 2).
_AUTHORITY_TABLES = (
    ("execution_automation_jobs", "job_id"),
    ("execution_managed_services", "service_id"),
    ("execution_interactive_sessions", "session_id"),
)
OLD_EXECUTION_RECORDS_PRESENT = "old_execution_records_present"
RETIRE_OLD_RECORDS_COMMAND = (
    "assistant_cli.py execution-retire-old-records --config <instance_config.yaml>"
)


class ImmutableRecordConflict(RuntimeError):
    """An immutable id already exists with different content."""


class StaleExecutionFence(RuntimeError):
    """A worker attempted to mutate state using an obsolete lease fence."""


class OldExecutionRecordFormat(RuntimeError):
    """A job, service or SSH session was saved before access model 2.

    Its activation names decisions of the removed permission store, which the
    runtime can no longer check, so it is refused rather than guessed at.
    """


@dataclass
class OldExecutionRecords:
    """Records saved before schema version 2, by table: ``(table, record id)``."""

    records: list[tuple[str, str]] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.records)

    def message(self) -> str:
        counts: dict[str, int] = {}
        for table, _record_id in self.records:
            counts[table] = counts.get(table, 0) + 1
        found = ", ".join(f"{count} in {table}" for table, count in sorted(counts.items()))
        return (
            "Execution records saved before access model 2 cannot be used "
            f"(reason_code={OLD_EXECUTION_RECORDS_PRESENT}; {found}). Their activations name "
            "decisions of the removed permission store. Retire them with "
            f"`{RETIRE_OLD_RECORDS_COMMAND}` while Universal Agents is stopped: jobs are "
            "disabled, services are set to stopped (the controller then stops their workloads) "
            "and SSH sessions can no longer be opened. Enable jobs again with "
            "set_execution_job_enabled, start services again with "
            "plan_managed_execution_service_state and apply_change, and create new SSH sessions."
        )


def _record_schema_version(raw: Any) -> Any:
    try:
        payload = json.loads(str(raw))
    except ValueError:
        return None
    return payload.get("schema_version") if isinstance(payload, dict) else None


def _authority_record(model_type: type[_AuthorityModelT], raw: Any) -> _AuthorityModelT:
    """Load a job, service or session; an old-format record fails with a clear message."""

    try:
        return model_type.model_validate_json(raw)
    except ValidationError as exc:
        version = _record_schema_version(raw)
        if version != EXECUTION_AUTHORITY_SCHEMA_VERSION:
            record_id = ""
            try:
                payload = json.loads(str(raw))
                record_id = str(
                    payload.get("job_id") or payload.get("service_id") or payload.get("session_id") or ""
                )
            except ValueError:
                pass
            raise OldExecutionRecordFormat(
                f"Execution record {record_id or '(unknown id)'} has schema version {version!r}, "
                f"not {EXECUTION_AUTHORITY_SCHEMA_VERSION} (reason_code={OLD_EXECUTION_RECORDS_PRESENT}). "
                f"Retire old execution records with `{RETIRE_OLD_RECORDS_COMMAND}`."
            ) from exc
        raise


def _retired_record(table: str, payload: dict[str, Any], now: datetime) -> BaseModel:
    """The old record rewritten without authority: nothing can run from it.

    Jobs are disabled, services are set to stopped (the controller stops their
    workloads without needing authority), and live SSH sessions are marked
    lost. Generations advance so nothing stale can be written over them.
    """

    base = {
        **payload,
        "schema_version": EXECUTION_AUTHORITY_SCHEMA_VERSION,
        "updated_at_utc": now.isoformat(),
    }
    if table == "execution_automation_jobs":
        return AutomationJobV2.model_validate(
            {**base, "activation": None, "enabled": False, "generation": int(payload.get("generation") or 1) + 1}
        )
    if table == "execution_managed_services":
        return ManagedServiceV1.model_validate(
            {
                **base,
                "activation": None,
                "desired_state": ServiceDesiredState.STOPPED.value,
                "generation": int(payload.get("generation") or 1) + 1,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at_utc": None,
            }
        )
    status = str(payload.get("status") or "")
    live = {InteractiveSessionStatus.CONNECTING.value, InteractiveSessionStatus.OPEN.value}
    session = {key: value for key, value in base.items() if key != "interaction_activation"}
    return InteractiveSessionV1.model_validate(
        {
            **session,
            "connect_activation": None,
            "status": InteractiveSessionStatus.LOST.value if status in live else status,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_utc": None,
        }
    )


class ExecutionControlRepository(Protocol):
    def current_time_utc(self) -> datetime: ...
    def put_code_artifact(self, code: CodeArtifactV1) -> CodeArtifactV1: ...
    def put_build_attestation(
        self, build: BuildAttestationV1
    ) -> BuildAttestationV1: ...
    def put_revision(self, revision: ExecutionRevisionV1) -> ExecutionRevisionV1: ...
    def get_revision(
        self, *, team_id: str, revision_id: str
    ) -> ExecutionRevisionV1 | None: ...
    def create_job(self, job: AutomationJobV2) -> AutomationJobV2: ...
    def get_job(self, *, team_id: str, job_id: str) -> AutomationJobV2 | None: ...
    def list_jobs(
        self, *, team_id: str, limit: int = 10_000
    ) -> tuple[AutomationJobV2, ...]: ...
    def update_job(
        self, job: AutomationJobV2, *, expected_generation: int
    ) -> AutomationJobV2: ...
    def list_due_jobs(
        self, *, team_id: str, now: datetime, limit: int
    ) -> tuple[AutomationJobV2, ...]: ...
    def create_run_attempt(
        self, attempt: RunAttemptV1
    ) -> tuple[RunAttemptV1, bool]: ...
    def list_run_attempts(
        self, *, team_id: str, job_id: str | None = None, limit: int = 100
    ) -> tuple[RunAttemptV1, ...]: ...
    def advance_job_cursor(
        self,
        *,
        job: AutomationJobV2,
        expected_next_run_at_utc: datetime,
        next_run_at_utc: datetime | None,
        enabled: bool,
        updated_at_utc: datetime,
    ) -> bool: ...
    def has_active_run(self, *, team_id: str, job_id: str) -> bool: ...
    def claim_next_run_attempt(
        self,
        *,
        team_id: str,
        runner_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> RunAttemptV1 | None: ...
    def renew_run_attempt_lease(
        self,
        *,
        team_id: str,
        run_id: str,
        runner_id: str,
        lease_token: str,
        fence: int,
        lease_ttl_seconds: int,
    ) -> RunAttemptV1: ...
    def update_claimed_run_attempt(
        self,
        attempt: RunAttemptV1,
        *,
        runner_id: str,
        lease_token: str,
        fence: int,
    ) -> RunAttemptV1: ...
    def finish_run_attempt(
        self,
        attempt: RunAttemptV1,
        *,
        runner_id: str,
        lease_token: str,
        fence: int,
    ) -> RunAttemptV1: ...
    def create_managed_service(self, service: ManagedServiceV1) -> ManagedServiceV1: ...
    def update_managed_service(
        self, service: ManagedServiceV1, *, expected_generation: int
    ) -> ManagedServiceV1: ...
    def get_managed_service(
        self, *, team_id: str, service_id: str
    ) -> ManagedServiceV1 | None: ...
    def list_managed_services(
        self, *, team_id: str
    ) -> tuple[ManagedServiceV1, ...]: ...
    def claim_managed_service(
        self,
        *,
        team_id: str,
        reconciler_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> ManagedServiceV1 | None: ...
    def finish_managed_service_reconciliation(
        self,
        service: ManagedServiceV1,
        *,
        reconciler_id: str,
        lease_token: str,
        fence: int,
    ) -> ManagedServiceV1: ...
    def create_interactive_session(
        self, session: InteractiveSessionV1
    ) -> InteractiveSessionV1: ...
    def get_interactive_session(
        self, *, team_id: str, session_id: str
    ) -> InteractiveSessionV1 | None: ...
    def claim_interactive_session(
        self,
        *,
        team_id: str,
        session_id: str,
        broker_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> InteractiveSessionV1: ...
    def update_claimed_interactive_session(
        self,
        session: InteractiveSessionV1,
        *,
        broker_id: str,
        lease_token: str,
        fence: int,
        release: bool = False,
    ) -> InteractiveSessionV1: ...
    def list_interactive_sessions(
        self, *, team_id: str
    ) -> tuple[InteractiveSessionV1, ...]: ...
    def mark_interactive_sessions_lost(
        self, *, team_id: str, updated_at_utc: datetime
    ) -> tuple[InteractiveSessionV1, ...]: ...
    def put_ssh_interaction(self, activation: ActivationRecordV2) -> ActivationRecordV2: ...
    def get_ssh_interaction(
        self, *, team_id: str, session_id: str
    ) -> ActivationRecordV2 | None: ...
    def old_format_records(self, *, team_id: str) -> OldExecutionRecords: ...
    def retire_old_format_records(self, *, team_id: str) -> OldExecutionRecords: ...


def _json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace(" ", "T")
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Persisted execution timestamps must include a timezone.")
    return value.astimezone(timezone.utc).isoformat()


class _RelationalExecutionControlRepository:
    """Shared execution records; concrete adapters own lease transactions."""

    _dialect: SqlDialect

    def _initialize_schema(self) -> None:
        if repositories_are_read_only():
            return
        ensure_storage_parent_dir(self._db_path)
        with self._connect() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.commit()
        self._logger.info(
            "SQLite execution control repository initialized at %s", self._db_path
        )

    def current_time_utc(self) -> datetime:
        """Return the persistence authority's clock for scheduling and leases."""
        with self._connect() as connection:
            return self._database_now(connection)

    def put_code_artifact(self, code: CodeArtifactV1) -> CodeArtifactV1:
        return self._put_immutable(
            table="execution_code_artifacts",
            id_column="code_artifact_id",
            record_id=code.code_artifact_id,
            model=code,
            model_type=CodeArtifactV1,
        )

    def put_build_attestation(self, build: BuildAttestationV1) -> BuildAttestationV1:
        return self._put_immutable(
            table="execution_build_attestations",
            id_column="build_id",
            record_id=build.build_id,
            model=build,
            model_type=BuildAttestationV1,
        )

    def put_revision(self, revision: ExecutionRevisionV1) -> ExecutionRevisionV1:
        return self._put_immutable(
            table="execution_revisions",
            id_column="revision_id",
            record_id=revision.revision_id,
            model=revision,
            model_type=ExecutionRevisionV1,
        )

    def put_ssh_interaction(self, activation: ActivationRecordV2) -> ActivationRecordV2:
        """Record a person's approval to type into one SSH session (once per session).

        Kept apart from the session record, which the broker rewrites on every
        lease renewal, so an approval can never be written over.
        """

        return self._put_immutable(
            table="execution_ssh_interactions",
            id_column="session_id",
            record_id=activation.resource_id,
            model=activation,
            model_type=ActivationRecordV2,
        )

    def get_ssh_interaction(
        self, *, team_id: str, session_id: str
    ) -> ActivationRecordV2 | None:
        row = self._get_owned_runtime(
            table="execution_ssh_interactions",
            id_column="session_id",
            record_id=session_id,
            team_id=team_id,
        )
        return None if row is None else ActivationRecordV2.model_validate_json(row)

    def old_format_records(self, *, team_id: str) -> OldExecutionRecords:
        """Jobs, services and SSH sessions saved before schema version 2."""

        found = OldExecutionRecords()
        with self._connect() as connection:
            for table, id_column in _AUTHORITY_TABLES:
                rows = connection.execute(
                    f"SELECT {id_column} AS record_id, record_json FROM {table} WHERE team_id = {self._dialect.bind} ORDER BY {id_column}",
                    (team_id,),
                ).fetchall()
                found.records += [
                    (table, str(row["record_id"]))
                    for row in rows
                    if _record_schema_version(row["record_json"]) != EXECUTION_AUTHORITY_SCHEMA_VERSION
                ]
        return found

    def retire_old_format_records(self, *, team_id: str) -> OldExecutionRecords:
        """Rewrite old-format records without authority (the operator's explicit command).

        Nothing is migrated: an old activation cannot be checked any more, so
        it is dropped and the record can no longer run (``_retired_record``).
        People enable or start it again through the current access rules.
        """

        retired = OldExecutionRecords()
        with self._connect() as connection:
            self._begin_claim(connection)
            now = self._database_now(connection)
            for table, id_column in _AUTHORITY_TABLES:
                rows = connection.execute(
                    f"SELECT {id_column} AS record_id, record_json FROM {table} WHERE team_id = {self._dialect.bind}",
                    (team_id,),
                ).fetchall()
                for row in rows:
                    if _record_schema_version(row["record_json"]) == EXECUTION_AUTHORITY_SCHEMA_VERSION:
                        continue
                    record = _retired_record(table, json.loads(str(row["record_json"])), now)
                    self._write_retired(connection, str(row["record_id"]), record)
                    retired.records.append((table, str(row["record_id"])))
            connection.commit()
        if retired.present:
            self._logger.warning(
                "Retired execution records saved before access model 2. "
                "event=execution_old_records_retired team_id=%s records=%s",
                team_id,
                ", ".join(f"{table}:{record_id}" for table, record_id in retired.records),
            )
        return retired

    def _write_retired(self, connection: Any, record_id: str, record: BaseModel) -> None:
        bind = self._dialect.bind
        if isinstance(record, AutomationJobV2):
            connection.execute(
                f"""
                UPDATE execution_automation_jobs
                   SET owner_scope_kind = {bind}, owner_id = {bind}, generation = {bind}, enabled = {bind},
                       next_run_at_utc = {bind}, paused_until_utc = {bind}, expires_at_utc = {bind},
                       activation_expires_at_utc = {bind}, activation_revoked_at_utc = {bind}, record_json = {bind}
                 WHERE job_id = {bind}
                """,
                (*self._job_update_values(record), record_id),
            )
        elif isinstance(record, ManagedServiceV1):
            connection.execute(
                f"""
                UPDATE execution_managed_services
                   SET generation = {bind}, desired_or_status = {bind}, lease_owner = NULL, lease_token = NULL,
                       lease_expires_at_utc = NULL, record_json = {bind}
                 WHERE service_id = {bind}
                """,
                (record.generation, record.desired_state.value, _json(record), record_id),
            )
        else:
            assert isinstance(record, InteractiveSessionV1)
            connection.execute(
                f"""
                UPDATE execution_interactive_sessions
                   SET desired_or_status = {bind}, lease_owner = NULL, lease_token = NULL,
                       lease_expires_at_utc = NULL, record_json = {bind}
                 WHERE session_id = {bind}
                """,
                (record.status.value, _json(record), record_id),
            )

    def _put_immutable(
        self,
        *,
        table: str,
        id_column: str,
        record_id: str,
        model: _ModelT,
        model_type: type[_ModelT],
    ) -> _ModelT:
        payload = _json(model)
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {table} ({id_column}, team_id, owner_scope_kind, owner_id, record_json)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT({id_column}) DO NOTHING
                """,
                (
                    record_id,
                    model.owner.team_id,
                    model.owner.scope_kind.value,
                    model.owner.owner_id,
                    payload,
                ),
            )
            row = connection.execute(
                f"SELECT record_json FROM {table} WHERE {id_column} = {self._dialect.bind} AND team_id = {self._dialect.bind}",
                (record_id, model.owner.team_id),
            ).fetchone()
            connection.commit()
        if row is None:
            raise ImmutableRecordConflict(
                "Immutable execution id is already owned by another team."
            )
        if str(row["record_json"]) != payload:
            raise ImmutableRecordConflict(
                f"Immutable execution record {record_id!r} already has different content."
            )
        return cast(_ModelT, model_type.model_validate_json(str(row["record_json"])))

    def get_revision(
        self, *, team_id: str, revision_id: str
    ) -> ExecutionRevisionV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT record_json FROM execution_revisions WHERE revision_id = {self._dialect.bind} AND team_id = {self._dialect.bind}",
                (revision_id, team_id),
            ).fetchone()
        return (
            None
            if row is None
            else ExecutionRevisionV1.model_validate_json(row["record_json"])
        )

    def create_job(self, job: AutomationJobV2) -> AutomationJobV2:
        with self._connect() as connection:
            try:
                connection.execute(
                    f"""
                    INSERT INTO execution_automation_jobs (
                        job_id, team_id, owner_scope_kind, owner_id, generation, enabled,
                        next_run_at_utc, paused_until_utc, expires_at_utc,
                        activation_expires_at_utc, activation_revoked_at_utc, record_json
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    self._job_values(job),
                )
            except Exception as exc:
                if self._is_integrity_error(exc):
                    raise ImmutableRecordConflict(
                        f"Execution job {job.job_id!r} already exists."
                    ) from exc
                raise
            connection.commit()
        return job

    def get_job(self, *, team_id: str, job_id: str) -> AutomationJobV2 | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT record_json FROM execution_automation_jobs WHERE job_id = {self._dialect.bind} AND team_id = {self._dialect.bind}",
                (job_id, team_id),
            ).fetchone()
        return (
            None
            if row is None
            else _authority_record(AutomationJobV2, row["record_json"])
        )

    def list_jobs(
        self,
        *,
        team_id: str,
        limit: int = 10_000,
    ) -> tuple[AutomationJobV2, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM execution_automation_jobs
                 WHERE team_id = {self._dialect.bind} ORDER BY job_id LIMIT {self._dialect.bind}
                """,
                (team_id, int(limit)),
            ).fetchall()
        return tuple(
            _authority_record(AutomationJobV2, row["record_json"]) for row in rows
        )

    def update_job(
        self, job: AutomationJobV2, *, expected_generation: int
    ) -> AutomationJobV2:
        if job.generation != expected_generation + 1:
            raise ValueError(
                "An execution job update must increment generation exactly once."
            )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_automation_jobs
                   SET owner_scope_kind = {self._dialect.bind}, owner_id = {self._dialect.bind}, generation = {self._dialect.bind}, enabled = {self._dialect.bind},
                       next_run_at_utc = {self._dialect.bind}, paused_until_utc = {self._dialect.bind}, expires_at_utc = {self._dialect.bind},
                       activation_expires_at_utc = {self._dialect.bind}, activation_revoked_at_utc = {self._dialect.bind}, record_json = {self._dialect.bind}
                 WHERE job_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND generation = {self._dialect.bind}
                """,
                (
                    *self._job_update_values(job),
                    job.job_id,
                    job.owner.team_id,
                    expected_generation,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise StaleExecutionFence("Execution job generation changed before update.")
        return job

    def list_due_jobs(
        self,
        *,
        team_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[AutomationJobV2, ...]:
        now_iso = _iso(now)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM execution_automation_jobs
                 WHERE team_id = {self._dialect.bind}
                   AND enabled = 1
                   AND next_run_at_utc IS NOT NULL
                   AND next_run_at_utc <= {self._dialect.bind}
                   AND (paused_until_utc IS NULL OR paused_until_utc <= {self._dialect.bind})
                   AND (expires_at_utc IS NULL OR expires_at_utc > {self._dialect.bind})
                 ORDER BY next_run_at_utc, job_id
                 LIMIT {self._dialect.bind}
                """,
                (team_id, now_iso, now_iso, now_iso, int(limit)),
            ).fetchall()
        return tuple(
            _authority_record(AutomationJobV2, row["record_json"]) for row in rows
        )

    def create_run_attempt(self, attempt: RunAttemptV1) -> tuple[RunAttemptV1, bool]:
        payload = _json(attempt)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO execution_run_attempts (
                    run_id, occurrence_id, job_id, team_id, owner_scope_kind, owner_id,
                    status, fence, lease_owner, lease_token, lease_expires_at_utc,
                    scheduled_for_utc, record_json
                ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(occurrence_id) DO NOTHING
                """,
                (
                    attempt.run_id,
                    attempt.occurrence_id,
                    attempt.job_id,
                    attempt.owner.team_id,
                    attempt.owner.scope_kind.value,
                    attempt.owner.owner_id,
                    attempt.status.value,
                    attempt.fence,
                    attempt.lease_owner,
                    attempt.lease_token,
                    _iso(attempt.lease_expires_at_utc),
                    _iso(attempt.scheduled_for_utc),
                    payload,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                f"""
                SELECT record_json FROM execution_run_attempts
                 WHERE occurrence_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                """,
                (attempt.occurrence_id, attempt.owner.team_id),
            ).fetchone()
            connection.commit()
        if row is None:
            raise ImmutableRecordConflict(
                "Run occurrence is already owned by another team."
            )
        return RunAttemptV1.model_validate_json(row["record_json"]), created

    def advance_job_cursor(
        self,
        *,
        job: AutomationJobV2,
        expected_next_run_at_utc: datetime,
        next_run_at_utc: datetime | None,
        enabled: bool,
        updated_at_utc: datetime,
    ) -> bool:
        updated = job.model_copy(
            update={
                "next_run_at_utc": next_run_at_utc,
                "enabled": enabled,
                "updated_at_utc": updated_at_utc,
            }
        )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_automation_jobs
                   SET enabled = {self._dialect.bind}, next_run_at_utc = {self._dialect.bind}, record_json = {self._dialect.bind}
                 WHERE job_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND generation = {self._dialect.bind}
                   AND next_run_at_utc = {self._dialect.bind}
                """,
                (
                    1 if enabled else 0,
                    _iso(next_run_at_utc),
                    _json(updated),
                    job.job_id,
                    job.owner.team_id,
                    job.generation,
                    _iso(expected_next_run_at_utc),
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def has_active_run(self, *, team_id: str, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM execution_run_attempts
                 WHERE team_id = {self._dialect.bind} AND job_id = {self._dialect.bind} AND status IN ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                 LIMIT 1
                """,
                (
                    team_id,
                    job_id,
                    RunAttemptStatus.CLAIMED.value,
                    RunAttemptStatus.STARTING.value,
                    RunAttemptStatus.RUNNING.value,
                ),
            ).fetchone()
        return row is not None

    def get_run_attempt(self, *, team_id: str, run_id: str) -> RunAttemptV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT record_json FROM execution_run_attempts WHERE run_id = {self._dialect.bind} AND team_id = {self._dialect.bind}",
                (run_id, team_id),
            ).fetchone()
        return (
            None
            if row is None
            else RunAttemptV1.model_validate_json(row["record_json"])
        )

    def list_run_attempts(
        self,
        *,
        team_id: str,
        job_id: str | None = None,
        limit: int = 100,
    ) -> tuple[RunAttemptV1, ...]:
        with self._connect() as connection:
            if job_id is None:
                rows = connection.execute(
                    f"""
                    SELECT record_json FROM execution_run_attempts
                     WHERE team_id = {self._dialect.bind} ORDER BY scheduled_for_utc DESC LIMIT {self._dialect.bind}
                    """,
                    (team_id, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT record_json FROM execution_run_attempts
                     WHERE team_id = {self._dialect.bind} AND job_id = {self._dialect.bind}
                     ORDER BY scheduled_for_utc DESC LIMIT {self._dialect.bind}
                    """,
                    (team_id, job_id, int(limit)),
                ).fetchall()
        return tuple(
            RunAttemptV1.model_validate_json(row["record_json"]) for row in rows
        )

    def claim_next_run_attempt(
        self,
        *,
        team_id: str,
        runner_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> RunAttemptV1 | None:
        if lease_ttl_seconds < 1:
            raise ValueError("lease_ttl_seconds must be positive")
        with self._connect() as connection:
            self._begin_claim(connection)
            database_now = self._database_now(connection)
            row = connection.execute(
                self._claim_select_sql(),
                (
                    team_id,
                    *(status.value for status in _CLAIMABLE_STATUSES),
                    _iso(database_now),
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            current = RunAttemptV1.model_validate_json(row["record_json"])
            claimed = current.model_copy(
                update={
                    "status": RunAttemptStatus.CLAIMED,
                    "fence": current.fence + 1,
                    "lease_owner": runner_id,
                    "lease_token": lease_token,
                    "lease_expires_at_utc": database_now
                    + timedelta(seconds=lease_ttl_seconds),
                    "heartbeat_at_utc": database_now,
                    "updated_at_utc": database_now,
                }
            )
            self._write_claimed_attempt(connection, claimed)
            connection.commit()
        return RunAttemptV1.model_validate(claimed.model_dump(mode="python"))

    def renew_run_attempt_lease(
        self,
        *,
        team_id: str,
        run_id: str,
        runner_id: str,
        lease_token: str,
        fence: int,
        lease_ttl_seconds: int,
    ) -> RunAttemptV1:
        with self._connect() as connection:
            self._begin_claim(connection)
            row = connection.execute(
                f"SELECT record_json FROM execution_run_attempts WHERE run_id = {self._dialect.bind} AND team_id = {self._dialect.bind}",
                (run_id, team_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise StaleExecutionFence(
                    "Run attempt no longer exists in this owner scope."
                )
            current = RunAttemptV1.model_validate_json(row["record_json"])
            self._require_fence(
                current,
                runner_id=runner_id,
                lease_token=lease_token,
                fence=fence,
            )
            database_now = self._database_now(connection)
            if (
                current.lease_expires_at_utc is None
                or current.lease_expires_at_utc <= database_now
            ):
                raise StaleExecutionFence(
                    "An expired execution lease cannot be renewed."
                )
            renewed = current.model_copy(
                update={
                    "lease_expires_at_utc": database_now
                    + timedelta(seconds=lease_ttl_seconds),
                    "heartbeat_at_utc": database_now,
                    "updated_at_utc": database_now,
                }
            )
            self._write_claimed_attempt(connection, renewed)
            connection.commit()
        return RunAttemptV1.model_validate(renewed.model_dump(mode="python"))

    def update_claimed_run_attempt(
        self,
        attempt: RunAttemptV1,
        *,
        runner_id: str,
        lease_token: str,
        fence: int,
    ) -> RunAttemptV1:
        if attempt.status in TERMINAL_RUN_ATTEMPT_STATUSES:
            raise ValueError("Use finish_run_attempt for terminal state.")
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_run_attempts
                   SET status = {self._dialect.bind}, lease_expires_at_utc = {self._dialect.bind}, record_json = {self._dialect.bind}
                 WHERE run_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND lease_owner = {self._dialect.bind} AND lease_token = {self._dialect.bind} AND fence = {self._dialect.bind}
                """,
                (
                    attempt.status.value,
                    _iso(attempt.lease_expires_at_utc),
                    _json(attempt),
                    attempt.run_id,
                    attempt.owner.team_id,
                    runner_id,
                    lease_token,
                    fence,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise StaleExecutionFence("Run attempt lease changed before state update.")
        return attempt

    def finish_run_attempt(
        self,
        attempt: RunAttemptV1,
        *,
        runner_id: str,
        lease_token: str,
        fence: int,
    ) -> RunAttemptV1:
        if attempt.status not in TERMINAL_RUN_ATTEMPT_STATUSES:
            raise ValueError("finish_run_attempt requires a terminal status.")
        if attempt.finished_at_utc is None:
            raise ValueError("finish_run_attempt requires finished_at_utc.")
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_run_attempts
                   SET status = {self._dialect.bind}, lease_owner = NULL, lease_token = NULL,
                       lease_expires_at_utc = NULL, record_json = {self._dialect.bind}
                 WHERE run_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND lease_owner = {self._dialect.bind} AND lease_token = {self._dialect.bind} AND fence = {self._dialect.bind}
                """,
                (
                    attempt.status.value,
                    _json(
                        attempt.model_copy(
                            update={
                                "lease_owner": None,
                                "lease_token": None,
                                "lease_expires_at_utc": None,
                            }
                        )
                    ),
                    attempt.run_id,
                    attempt.owner.team_id,
                    runner_id,
                    lease_token,
                    fence,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise StaleExecutionFence(
                "Run attempt lease changed before terminal update."
            )
        return attempt.model_copy(
            update={
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at_utc": None,
            }
        )

    def create_managed_service(self, service: ManagedServiceV1) -> ManagedServiceV1:
        with self._connect() as connection:
            try:
                connection.execute(
                    f"""
                    INSERT INTO execution_managed_services (
                        service_id, team_id, owner_scope_kind, owner_id, generation,
                        observed_generation, desired_or_status, observed_state, active_fence,
                        lease_owner, lease_token, lease_expires_at_utc,
                        last_reconciled_at_utc, record_json
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    self._service_values(service),
                )
            except Exception as exc:
                if self._is_integrity_error(exc):
                    raise ImmutableRecordConflict(
                        f"Managed service {service.service_id!r} already exists."
                    ) from exc
                raise
            connection.commit()
        return service

    def update_managed_service(
        self,
        service: ManagedServiceV1,
        *,
        expected_generation: int,
    ) -> ManagedServiceV1:
        if service.generation != expected_generation + 1:
            raise ValueError(
                "Managed-service updates must increment generation exactly once."
            )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_managed_services
                   SET generation = {self._dialect.bind}, desired_or_status = {self._dialect.bind}, record_json = {self._dialect.bind}
                 WHERE service_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND owner_scope_kind = {self._dialect.bind}
                   AND owner_id = {self._dialect.bind} AND generation = {self._dialect.bind}
                """,
                (
                    service.generation,
                    service.desired_state.value,
                    _json(service),
                    service.service_id,
                    service.owner.team_id,
                    service.owner.scope_kind.value,
                    service.owner.owner_id,
                    expected_generation,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise StaleExecutionFence(
                "Managed-service generation changed before update."
            )
        return service

    def get_managed_service(
        self, *, team_id: str, service_id: str
    ) -> ManagedServiceV1 | None:
        row = self._get_owned_runtime(
            table="execution_managed_services",
            id_column="service_id",
            record_id=service_id,
            team_id=team_id,
        )
        return None if row is None else _authority_record(ManagedServiceV1, row)

    def list_managed_services(self, *, team_id: str) -> tuple[ManagedServiceV1, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM execution_managed_services
                 WHERE team_id = {self._dialect.bind} ORDER BY service_id
                """,
                (team_id,),
            ).fetchall()
        return tuple(
            _authority_record(ManagedServiceV1, row["record_json"]) for row in rows
        )

    def claim_managed_service(
        self,
        *,
        team_id: str,
        reconciler_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> ManagedServiceV1 | None:
        with self._connect() as connection:
            self._begin_claim(connection)
            database_now = self._database_now(connection)
            query = f"""
                SELECT record_json FROM execution_managed_services
                 WHERE team_id = {self._dialect.bind}
                   AND (lease_owner IS NULL OR lease_expires_at_utc IS NULL OR lease_expires_at_utc <= {self._dialect.bind})
                 ORDER BY CASE WHEN generation != observed_generation THEN 0 ELSE 1 END,
                          COALESCE(last_reconciled_at_utc, '0001-01-01T00:00:00+00:00'),
                          service_id
                 LIMIT 1
            """
            if isinstance(self, PostgresExecutionControlRepository):
                query = f"{query.rstrip()} FOR UPDATE SKIP LOCKED"
            row = connection.execute(query, (team_id, _iso(database_now))).fetchone()
            if row is None:
                connection.commit()
                return None
            current = _authority_record(ManagedServiceV1, row["record_json"])
            claimed = current.model_copy(
                update={
                    "active_fence": current.active_fence + 1,
                    "lease_owner": reconciler_id,
                    "lease_token": lease_token,
                    "lease_expires_at_utc": database_now
                    + timedelta(seconds=lease_ttl_seconds),
                    "updated_at_utc": database_now,
                }
            )
            cursor = connection.execute(
                f"""
                UPDATE execution_managed_services
                   SET active_fence = {self._dialect.bind}, lease_owner = {self._dialect.bind}, lease_token = {self._dialect.bind},
                       lease_expires_at_utc = {self._dialect.bind}, record_json = {self._dialect.bind}
                 WHERE service_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                """,
                (
                    claimed.active_fence,
                    claimed.lease_owner,
                    claimed.lease_token,
                    _iso(claimed.lease_expires_at_utc),
                    _json(claimed),
                    claimed.service_id,
                    claimed.owner.team_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StaleExecutionFence("Managed service disappeared during claim.")
            connection.commit()
        return ManagedServiceV1.model_validate(claimed.model_dump(mode="python"))

    def finish_managed_service_reconciliation(
        self,
        service: ManagedServiceV1,
        *,
        reconciler_id: str,
        lease_token: str,
        fence: int,
    ) -> ManagedServiceV1:
        finished = service.model_copy(
            update={
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at_utc": None,
            }
        )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_managed_services
                   SET observed_generation = {self._dialect.bind}, observed_state = {self._dialect.bind},
                       lease_owner = NULL, lease_token = NULL, lease_expires_at_utc = NULL,
                       last_reconciled_at_utc = CURRENT_TIMESTAMP,
                       record_json = {self._dialect.bind}
                 WHERE service_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND lease_owner = {self._dialect.bind}
                   AND lease_token = {self._dialect.bind} AND active_fence = {self._dialect.bind}
                   AND generation = {self._dialect.bind}
                """,
                (
                    finished.observed_generation,
                    finished.observed_state.value,
                    _json(finished),
                    finished.service_id,
                    finished.owner.team_id,
                    reconciler_id,
                    lease_token,
                    fence,
                    # The desired state may change during a reconcile (the
                    # update does not wait for the lease): never write this
                    # older copy over it; the next pass reconciles the new one.
                    finished.generation,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise StaleExecutionFence(
                "Managed-service lease or desired state changed before the observation update."
            )
        return finished

    @staticmethod
    def _service_values(service: ManagedServiceV1) -> tuple[Any, ...]:
        return (
            service.service_id,
            service.owner.team_id,
            service.owner.scope_kind.value,
            service.owner.owner_id,
            service.generation,
            service.observed_generation,
            service.desired_state.value,
            service.observed_state.value,
            service.active_fence,
            service.lease_owner,
            service.lease_token,
            _iso(service.lease_expires_at_utc),
            None,
            _json(service),
        )

    def create_interactive_session(
        self, session: InteractiveSessionV1
    ) -> InteractiveSessionV1:
        with self._connect() as connection:
            try:
                connection.execute(
                    f"""
                    INSERT INTO execution_interactive_sessions (
                        session_id, team_id, owner_scope_kind, owner_id, generation,
                        observed_generation, desired_or_status, lease_owner, lease_token,
                        lease_expires_at_utc, record_json
                    ) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, 0, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                    """,
                    (
                        session.session_id,
                        session.owner.team_id,
                        session.owner.scope_kind.value,
                        session.owner.owner_id,
                        session.fence,
                        session.status.value,
                        session.lease_owner,
                        session.lease_token,
                        _iso(session.lease_expires_at_utc),
                        _json(session),
                    ),
                )
            except Exception as exc:
                if self._is_integrity_error(exc):
                    raise ImmutableRecordConflict(
                        f"Interactive session {session.session_id!r} already exists."
                    ) from exc
                raise
            connection.commit()
        return session

    def claim_interactive_session(
        self,
        *,
        team_id: str,
        session_id: str,
        broker_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> InteractiveSessionV1:
        with self._connect() as connection:
            self._begin_claim(connection)
            database_now = self._database_now(connection)
            row = connection.execute(
                f"""
                SELECT record_json FROM execution_interactive_sessions
                 WHERE session_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                   AND (lease_owner IS NULL OR lease_expires_at_utc IS NULL OR lease_expires_at_utc <= {self._dialect.bind})
                """,
                (session_id, team_id, _iso(database_now)),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise StaleExecutionFence(
                    "Interactive session is already leased or unavailable."
                )
            current = _authority_record(InteractiveSessionV1, row["record_json"])
            claimed = current.model_copy(
                update={
                    "fence": current.fence + 1,
                    "lease_owner": broker_id,
                    "lease_token": lease_token,
                    "lease_expires_at_utc": database_now
                    + timedelta(seconds=lease_ttl_seconds),
                    "updated_at_utc": database_now,
                }
            )
            cursor = connection.execute(
                f"""
                UPDATE execution_interactive_sessions
                   SET generation = {self._dialect.bind}, lease_owner = {self._dialect.bind}, lease_token = {self._dialect.bind}, lease_expires_at_utc = {self._dialect.bind},
                       record_json = {self._dialect.bind}
                 WHERE session_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                """,
                (
                    claimed.fence,
                    claimed.lease_owner,
                    claimed.lease_token,
                    _iso(claimed.lease_expires_at_utc),
                    _json(claimed),
                    session_id,
                    team_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StaleExecutionFence(
                    "Interactive session disappeared during claim."
                )
            connection.commit()
        return InteractiveSessionV1.model_validate(claimed.model_dump(mode="python"))

    def update_claimed_interactive_session(
        self,
        session: InteractiveSessionV1,
        *,
        broker_id: str,
        lease_token: str,
        fence: int,
        release: bool = False,
    ) -> InteractiveSessionV1:
        updated = (
            session.model_copy(
                update={
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at_utc": None,
                }
            )
            if release
            else session
        )
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE execution_interactive_sessions
                   SET desired_or_status = {self._dialect.bind}, lease_owner = {self._dialect.bind}, lease_token = {self._dialect.bind},
                       lease_expires_at_utc = {self._dialect.bind}, record_json = {self._dialect.bind}
                 WHERE session_id = {self._dialect.bind} AND team_id = {self._dialect.bind} AND generation = {self._dialect.bind}
                   AND lease_owner = {self._dialect.bind} AND lease_token = {self._dialect.bind}
                """,
                (
                    updated.status.value,
                    updated.lease_owner,
                    updated.lease_token,
                    _iso(updated.lease_expires_at_utc),
                    _json(updated),
                    updated.session_id,
                    updated.owner.team_id,
                    fence,
                    broker_id,
                    lease_token,
                ),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise StaleExecutionFence(
                "Interactive-session lease changed before update."
            )
        return updated

    def get_interactive_session(
        self, *, team_id: str, session_id: str
    ) -> InteractiveSessionV1 | None:
        row = self._get_owned_runtime(
            table="execution_interactive_sessions",
            id_column="session_id",
            record_id=session_id,
            team_id=team_id,
        )
        return None if row is None else _authority_record(InteractiveSessionV1, row)

    def list_interactive_sessions(
        self,
        *,
        team_id: str,
    ) -> tuple[InteractiveSessionV1, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM execution_interactive_sessions
                 WHERE team_id = {self._dialect.bind} ORDER BY session_id
                """,
                (team_id,),
            ).fetchall()
        return tuple(
            _authority_record(InteractiveSessionV1, row["record_json"]) for row in rows
        )

    def mark_interactive_sessions_lost(
        self,
        *,
        team_id: str,
        updated_at_utc: datetime,
    ) -> tuple[InteractiveSessionV1, ...]:
        recovered: list[InteractiveSessionV1] = []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM execution_interactive_sessions WHERE team_id = {self._dialect.bind}",
                (team_id,),
            ).fetchall()
            for row in rows:
                current = _authority_record(InteractiveSessionV1, row["record_json"])
                if current.status not in {
                    InteractiveSessionStatus.CONNECTING,
                    InteractiveSessionStatus.OPEN,
                }:
                    continue
                lost = current.model_copy(
                    update={
                        "status": InteractiveSessionStatus.LOST,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at_utc": None,
                        "updated_at_utc": updated_at_utc,
                    }
                )
                connection.execute(
                    f"""
                    UPDATE execution_interactive_sessions
                       SET desired_or_status = {self._dialect.bind}, lease_owner = NULL, lease_token = NULL,
                           lease_expires_at_utc = NULL, record_json = {self._dialect.bind}
                     WHERE session_id = {self._dialect.bind} AND team_id = {self._dialect.bind}
                    """,
                    (lost.status.value, _json(lost), lost.session_id, team_id),
                )
                recovered.append(lost)
            connection.commit()
        return tuple(recovered)

    def _get_owned_runtime(
        self,
        *,
        table: str,
        id_column: str,
        record_id: str,
        team_id: str,
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT record_json FROM {table} WHERE {id_column} = {self._dialect.bind} AND team_id = {self._dialect.bind}",
                (record_id, team_id),
            ).fetchone()
        return None if row is None else str(row["record_json"])

    @staticmethod
    def _job_values(job: AutomationJobV2) -> tuple[Any, ...]:
        activation = job.activation
        return (
            job.job_id,
            job.owner.team_id,
            job.owner.scope_kind.value,
            job.owner.owner_id,
            job.generation,
            1 if job.enabled else 0,
            _iso(job.next_run_at_utc),
            _iso(job.paused_until_utc),
            _iso(job.expires_at_utc),
            _iso(activation.expires_at_utc) if activation else None,
            _iso(activation.revoked_at_utc) if activation else None,
            _json(job),
        )

    @classmethod
    def _job_update_values(cls, job: AutomationJobV2) -> tuple[Any, ...]:
        return cls._job_values(job)[2:]

    @staticmethod
    def _database_now(connection: Any) -> datetime:
        row = connection.execute("SELECT CURRENT_TIMESTAMP AS database_now").fetchone()
        return _parse_datetime(row["database_now"])

    @classmethod
    def _claim_select_sql(cls) -> str:
        return f"""
            SELECT record_json FROM execution_run_attempts
             WHERE team_id = {cls._dialect.bind}
               AND status IN ({cls._dialect.bind}, {cls._dialect.bind}, {cls._dialect.bind}, {cls._dialect.bind})
               AND (lease_owner IS NULL OR lease_expires_at_utc IS NULL OR lease_expires_at_utc <= {cls._dialect.bind})
             ORDER BY scheduled_for_utc, run_id
             LIMIT 1
        """

    @staticmethod
    def _write_claimed_attempt(connection: Any, attempt: RunAttemptV1) -> None:
        cursor = connection.execute(
            f"""
            UPDATE execution_run_attempts
               SET status = {connection.dialect.bind}, fence = {connection.dialect.bind}, lease_owner = {connection.dialect.bind}, lease_token = {connection.dialect.bind},
                   lease_expires_at_utc = {connection.dialect.bind}, record_json = {connection.dialect.bind}
             WHERE run_id = {connection.dialect.bind} AND team_id = {connection.dialect.bind}
            """,
            (
                attempt.status.value,
                attempt.fence,
                attempt.lease_owner,
                attempt.lease_token,
                _iso(attempt.lease_expires_at_utc),
                _json(attempt),
                attempt.run_id,
                attempt.owner.team_id,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleExecutionFence("Run attempt disappeared during lease update.")

    @staticmethod
    def _require_fence(
        attempt: RunAttemptV1,
        *,
        runner_id: str,
        lease_token: str,
        fence: int,
    ) -> None:
        if (
            attempt.lease_owner != runner_id
            or attempt.lease_token != lease_token
            or attempt.fence != fence
        ):
            raise StaleExecutionFence("Run attempt lease or fence is stale.")


class SqliteExecutionControlRepository(_RelationalExecutionControlRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: logging.Logger | None = None):
        self._db_path = db_path
        self._logger = logger or logging.getLogger(__name__)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection | PostgresConnection:
        return connect_sqlite(
            self._db_path, logger=self._logger, row_factory=sqlite3.Row
        )

    @staticmethod
    def _begin_claim(connection: Any) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _is_integrity_error(exc: Exception) -> bool:
        return isinstance(exc, sqlite3.IntegrityError)


class PostgresExecutionControlRepository(_RelationalExecutionControlRepository):
    """Postgres implementation for horizontally scaled execution controllers."""

    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: logging.Logger | None = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        if repositories_are_read_only():
            return
        validate_postgres_connection(dsn)
        with self._connect() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.commit()
        self._logger.info(
            "Postgres execution control repository initialized at %s",
            redact_postgres_dsn(dsn),
        )

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn)

    @staticmethod
    def _begin_claim(connection: Any) -> None:
        del connection

    @classmethod
    def _claim_select_sql(cls) -> str:
        return f"{super()._claim_select_sql().rstrip()} FOR UPDATE SKIP LOCKED"

    @staticmethod
    def _is_integrity_error(exc: Exception) -> bool:
        return exc.__class__.__name__ in {"IntegrityError", "UniqueViolation"}


def create_execution_control_repository(
    storage_settings: StorageSettings,
    *,
    logger: logging.Logger | None = None,
) -> ExecutionControlRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="ExecutionControlRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if (
        storage_settings.backend == STORAGE_BACKEND_SQLITE
        and storage_settings.sqlite_path
    ):
        return SqliteExecutionControlRepository(
            storage_settings.sqlite_path, logger=logger
        )
    if (
        storage_settings.backend == STORAGE_BACKEND_POSTGRES
        and storage_settings.postgres_dsn
    ):
        return PostgresExecutionControlRepository(
            storage_settings.postgres_dsn, logger=logger
        )
    raise NotImplementedError(
        f"ExecutionControlRepository has no implementation for backend {storage_settings.backend!r}."
    )


_OWNED_IMMUTABLE_TABLES = (
    ("execution_code_artifacts", "code_artifact_id"),
    ("execution_build_attestations", "build_id"),
    ("execution_revisions", "revision_id"),
    # One person-approved typing activation per SSH session (insert-once).
    ("execution_ssh_interactions", "session_id"),
)
_SCHEMA_STATEMENTS = tuple(
    f"""
    CREATE TABLE IF NOT EXISTS {table} (
        {id_column} TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        owner_scope_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        record_json TEXT NOT NULL
    )
    """
    for table, id_column in _OWNED_IMMUTABLE_TABLES
) + (
    """
    CREATE TABLE IF NOT EXISTS execution_automation_jobs (
        job_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        owner_scope_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        enabled INTEGER NOT NULL,
        next_run_at_utc TEXT,
        paused_until_utc TEXT,
        expires_at_utc TEXT,
        activation_expires_at_utc TEXT,
        activation_revoked_at_utc TEXT,
        record_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_execution_jobs_due
    ON execution_automation_jobs(team_id, enabled, next_run_at_utc)
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_run_attempts (
        run_id TEXT PRIMARY KEY,
        occurrence_id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL,
        team_id TEXT NOT NULL,
        owner_scope_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        status TEXT NOT NULL,
        fence INTEGER NOT NULL,
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at_utc TEXT,
        scheduled_for_utc TEXT NOT NULL,
        record_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_execution_attempts_claim
    ON execution_run_attempts(team_id, status, lease_expires_at_utc, scheduled_for_utc)
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_managed_services (
        service_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        owner_scope_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        observed_generation INTEGER NOT NULL,
        desired_or_status TEXT NOT NULL,
        observed_state TEXT NOT NULL,
        active_fence INTEGER NOT NULL,
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at_utc TEXT,
        last_reconciled_at_utc TEXT,
        record_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_execution_services_owner
    ON execution_managed_services(team_id, owner_scope_kind, owner_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_interactive_sessions (
        session_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        owner_scope_kind TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        observed_generation INTEGER NOT NULL,
        desired_or_status TEXT NOT NULL,
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at_utc TEXT,
        record_json TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_execution_sessions_owner
    ON execution_interactive_sessions(team_id, owner_scope_kind, owner_id)
    """,
)


__all__ = [
    "OLD_EXECUTION_RECORDS_PRESENT",
    "ExecutionControlRepository",
    "ImmutableRecordConflict",
    "OldExecutionRecordFormat",
    "OldExecutionRecords",
    "PostgresExecutionControlRepository",
    "SqliteExecutionControlRepository",
    "StaleExecutionFence",
    "create_execution_control_repository",
]
