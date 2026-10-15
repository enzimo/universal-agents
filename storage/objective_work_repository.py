"""Durable, owner-scoped persistence for bounded Objective execution."""

from __future__ import annotations

from storage.read_only import repositories_are_read_only

from storage.sql_dialect import SQLITE, POSTGRES, SqlDialect

import logging
from typing import Any, List, Optional, Protocol

from custom_messages.records.objectives import ObjectiveWorkCheckpoint, ObjectiveWorkInstalledProfile, ObjectiveWorkPolicy, ObjectiveWorkProfileProposal, ObjectiveWorkSession
from custom_messages.enums import ObjectiveWorkInstalledProfileStatus

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import (
    connect_postgres,
    ensure_storage_parent_dir,
    redact_postgres_dsn,
    validate_postgres_connection,
)
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite


class ObjectiveWorkRepository(Protocol):
    def save_policy(self, policy: ObjectiveWorkPolicy) -> None: ...
    def get_policy(self, objective_id: str, *, owner_agent_id: str) -> Optional[ObjectiveWorkPolicy]: ...
    def list_policies(self, *, owner_agent_id: str, limit: int = 100) -> List[ObjectiveWorkPolicy]: ...
    def save_session(self, session: ObjectiveWorkSession) -> None: ...
    def get_session(self, session_id: str, *, owner_agent_id: str) -> Optional[ObjectiveWorkSession]: ...
    def get_session_by_task(self, task_id: str, *, owner_agent_id: str) -> Optional[ObjectiveWorkSession]: ...
    def list_sessions(self, objective_id: str, *, owner_agent_id: str, limit: int = 50) -> List[ObjectiveWorkSession]: ...
    def save_checkpoint(self, checkpoint: ObjectiveWorkCheckpoint) -> None: ...
    def list_checkpoints(self, objective_id: str, *, owner_agent_id: str, limit: int = 50) -> List[ObjectiveWorkCheckpoint]: ...
    def save_profile_proposal(self, proposal: ObjectiveWorkProfileProposal) -> None: ...
    def get_profile_proposal(self, proposal_id: str, *, owner_agent_id: str, owner_canonical_user_id: str) -> Optional[ObjectiveWorkProfileProposal]: ...
    def list_profile_proposals(self, *, owner_agent_id: str, owner_canonical_user_id: str, limit: int = 50) -> List[ObjectiveWorkProfileProposal]: ...
    def save_installed_profile(self, profile: ObjectiveWorkInstalledProfile) -> None: ...
    def get_installed_profile(self, profile_name: str, *, owner_agent_id: str, owner_canonical_user_id: str, include_removed: bool = False) -> Optional[ObjectiveWorkInstalledProfile]: ...
    def list_installed_profiles(self, *, owner_agent_id: str, owner_canonical_user_id: str, include_removed: bool = False, limit: int = 100) -> List[ObjectiveWorkInstalledProfile]: ...


class _RelationalObjectiveWorkRepository:
    _dialect: SqlDialect


    @staticmethod
    def _require_owner(owner_agent_id: Optional[str]) -> str:
        if not owner_agent_id:
            raise ValueError("ObjectiveWorkRepository access requires owner_agent_id.")
        return owner_agent_id

    def _init_schema(self) -> None:
        if repositories_are_read_only():
            return
        postgres = self._dialect is POSTGRES
        if not postgres:
            ensure_storage_parent_dir(self._db_path)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS objective_work_policies (
                    policy_record_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL,
                    objective_id TEXT NOT NULL, owner_agent_id TEXT NOT NULL,
                    status TEXT NOT NULL, policy_data TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_objective_work_policy_owner_objective
                ON objective_work_policies(owner_agent_id, objective_id)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS objective_work_sessions (
                    session_record_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL, objective_id TEXT NOT NULL,
                    owner_agent_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    status TEXT NOT NULL, session_data TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_objective_work_session_owner_id
                ON objective_work_sessions(owner_agent_id, session_id)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_objective_work_session_objective
                ON objective_work_sessions(owner_agent_id, objective_id, updated_at DESC)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_objective_work_session_task
                ON objective_work_sessions(owner_agent_id, task_id, updated_at DESC)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS objective_work_checkpoints (
                    checkpoint_record_id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, policy_id TEXT NOT NULL,
                    objective_id TEXT NOT NULL, owner_agent_id TEXT NOT NULL,
                    checkpoint_data TEXT NOT NULL, created_at TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_objective_work_checkpoint_objective
                ON objective_work_checkpoints(owner_agent_id, objective_id, created_at DESC)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS objective_work_profile_proposals (
                    proposal_record_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
                    owner_agent_id TEXT NOT NULL, owner_canonical_user_id TEXT NOT NULL,
                    status TEXT NOT NULL, proposal_data TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_objective_work_profile_proposal_owner_id
                ON objective_work_profile_proposals(owner_agent_id, owner_canonical_user_id, proposal_id)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_objective_work_profile_proposal_owner_recent
                ON objective_work_profile_proposals(owner_agent_id, owner_canonical_user_id, created_at DESC)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS objective_work_installed_profiles (
                    profile_record_id TEXT PRIMARY KEY, installed_profile_id TEXT NOT NULL,
                    owner_agent_id TEXT NOT NULL, owner_canonical_user_id TEXT NOT NULL,
                    profile_name TEXT NOT NULL, profile_name_key TEXT NOT NULL,
                    status TEXT NOT NULL, profile_data TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_objective_work_installed_profile_owner_name
                ON objective_work_installed_profiles(owner_agent_id, owner_canonical_user_id, profile_name_key)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_objective_work_installed_profile_owner_recent
                ON objective_work_installed_profiles(owner_agent_id, owner_canonical_user_id, updated_at DESC)"""
            )
            conn.commit()
        target = redact_postgres_dsn(self._dsn or "") if postgres else self._db_path
        self._logger.info("Objective work repository initialized at %s", target)

    def save_policy(self, policy: ObjectiveWorkPolicy) -> None:
        self._require_owner(policy.owner_agent_id)
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO objective_work_policies
                (policy_record_id, policy_id, objective_id, owner_agent_id, status,
                 policy_data, created_at, updated_at) VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(policy_record_id) DO UPDATE SET status=excluded.status,
                policy_data=excluded.policy_data, updated_at=excluded.updated_at""",
                (f"{policy.owner_agent_id}::{policy.objective_id}", policy.policy_id,
                 policy.objective_id, policy.owner_agent_id, policy.status.value,
                 policy.model_dump_json(), policy.created_at, policy.updated_at),
            )
            conn.commit()

    def get_policy(self, objective_id: str, *, owner_agent_id: str) -> Optional[ObjectiveWorkPolicy]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT policy_data FROM objective_work_policies WHERE objective_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind}",
                (objective_id, owner),
            ).fetchone()
        return ObjectiveWorkPolicy.model_validate_json(row[0]) if row else None

    def list_policies(self, *, owner_agent_id: str, limit: int = 100) -> List[ObjectiveWorkPolicy]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT policy_data FROM objective_work_policies WHERE owner_agent_id={self._dialect.bind} ORDER BY updated_at DESC LIMIT {self._dialect.bind}",
                (owner, max(1, min(int(limit), 500))),
            ).fetchall()
        return [ObjectiveWorkPolicy.model_validate_json(row[0]) for row in rows]

    def save_session(self, session: ObjectiveWorkSession) -> None:
        self._require_owner(session.owner_agent_id)
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO objective_work_sessions
                (session_record_id, session_id, policy_id, objective_id, owner_agent_id,
                 task_id, status, session_data, created_at, updated_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(session_record_id) DO UPDATE SET status=excluded.status,
                session_data=excluded.session_data, updated_at=excluded.updated_at""",
                (f"{session.owner_agent_id}::{session.session_id}", session.session_id,
                 session.policy_id, session.objective_id, session.owner_agent_id,
                 session.task_id, session.status.value, session.model_dump_json(),
                 session.created_at, session.updated_at),
            )
            conn.commit()

    def get_session(self, session_id: str, *, owner_agent_id: str) -> Optional[ObjectiveWorkSession]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT session_data FROM objective_work_sessions WHERE session_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind}",
                (session_id, owner),
            ).fetchone()
        return ObjectiveWorkSession.model_validate_json(row[0]) if row else None

    def get_session_by_task(self, task_id: str, *, owner_agent_id: str) -> Optional[ObjectiveWorkSession]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT session_data FROM objective_work_sessions
                WHERE task_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind} ORDER BY updated_at DESC LIMIT 1""",
                (task_id, owner),
            ).fetchone()
        return ObjectiveWorkSession.model_validate_json(row[0]) if row else None

    def list_sessions(self, objective_id: str, *, owner_agent_id: str, limit: int = 50) -> List[ObjectiveWorkSession]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT session_data FROM objective_work_sessions
                WHERE objective_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind} ORDER BY updated_at DESC LIMIT {self._dialect.bind}""",
                (objective_id, owner, max(1, min(int(limit), 500))),
            ).fetchall()
        return [ObjectiveWorkSession.model_validate_json(row[0]) for row in rows]

    def save_checkpoint(self, checkpoint: ObjectiveWorkCheckpoint) -> None:
        self._require_owner(checkpoint.owner_agent_id)
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO objective_work_checkpoints
                (checkpoint_record_id, checkpoint_id, session_id, policy_id,
                 objective_id, owner_agent_id, checkpoint_data, created_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(checkpoint_record_id) DO UPDATE SET checkpoint_data=excluded.checkpoint_data""",
                (f"{checkpoint.owner_agent_id}::{checkpoint.checkpoint_id}",
                 checkpoint.checkpoint_id, checkpoint.session_id, checkpoint.policy_id,
                 checkpoint.objective_id, checkpoint.owner_agent_id,
                 checkpoint.model_dump_json(), checkpoint.created_at),
            )
            conn.commit()

    def list_checkpoints(self, objective_id: str, *, owner_agent_id: str, limit: int = 50) -> List[ObjectiveWorkCheckpoint]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT checkpoint_data FROM objective_work_checkpoints
                WHERE objective_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind} ORDER BY created_at DESC LIMIT {self._dialect.bind}""",
                (objective_id, owner, max(1, min(int(limit), 500))),
            ).fetchall()
        return [ObjectiveWorkCheckpoint.model_validate_json(row[0]) for row in rows]

    def save_profile_proposal(self, proposal: ObjectiveWorkProfileProposal) -> None:
        self._require_owner(proposal.owner_agent_id)
        if not proposal.owner_canonical_user_id:
            raise ValueError("Objective work profile proposals require an owner user.")
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO objective_work_profile_proposals
                (proposal_record_id, proposal_id, owner_agent_id,
                 owner_canonical_user_id, status, proposal_data, created_at, expires_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(proposal_record_id) DO UPDATE SET
                status=excluded.status, proposal_data=excluded.proposal_data""",
                (
                    f"{proposal.owner_agent_id}::{proposal.owner_canonical_user_id}::{proposal.proposal_id}",
                    proposal.proposal_id,
                    proposal.owner_agent_id,
                    proposal.owner_canonical_user_id,
                    proposal.status.value,
                    proposal.model_dump_json(),
                    proposal.created_at,
                    proposal.expires_at,
                ),
            )
            conn.commit()

    def get_profile_proposal(
        self,
        proposal_id: str,
        *,
        owner_agent_id: str,
        owner_canonical_user_id: str,
    ) -> Optional[ObjectiveWorkProfileProposal]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT proposal_data FROM objective_work_profile_proposals
                WHERE proposal_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind} AND owner_canonical_user_id={self._dialect.bind}""",
                (proposal_id, owner, owner_canonical_user_id),
            ).fetchone()
        return ObjectiveWorkProfileProposal.model_validate_json(row[0]) if row else None

    def list_profile_proposals(
        self,
        *,
        owner_agent_id: str,
        owner_canonical_user_id: str,
        limit: int = 50,
    ) -> List[ObjectiveWorkProfileProposal]:
        owner = self._require_owner(owner_agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT proposal_data FROM objective_work_profile_proposals
                WHERE owner_agent_id={self._dialect.bind} AND owner_canonical_user_id={self._dialect.bind}
                ORDER BY created_at DESC LIMIT {self._dialect.bind}""",
                (owner, owner_canonical_user_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [ObjectiveWorkProfileProposal.model_validate_json(row[0]) for row in rows]

    def save_installed_profile(self, profile: ObjectiveWorkInstalledProfile) -> None:
        self._require_owner(profile.owner_agent_id)
        if not profile.owner_canonical_user_id:
            raise ValueError("Installed Objective work profiles require an owner user.")
        profile_name_key = profile.profile_name.casefold()
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO objective_work_installed_profiles
                (profile_record_id, installed_profile_id, owner_agent_id,
                 owner_canonical_user_id, profile_name, profile_name_key, status,
                 profile_data, created_at, updated_at)
                VALUES ({self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind}, {self._dialect.bind})
                ON CONFLICT(profile_record_id) DO UPDATE SET
                installed_profile_id=excluded.installed_profile_id,
                profile_name=excluded.profile_name, status=excluded.status,
                profile_data=excluded.profile_data, updated_at=excluded.updated_at""",
                (
                    f"{profile.owner_agent_id}::{profile.owner_canonical_user_id}::{profile_name_key}",
                    profile.installed_profile_id,
                    profile.owner_agent_id,
                    profile.owner_canonical_user_id,
                    profile.profile_name,
                    profile_name_key,
                    profile.status.value,
                    profile.model_dump_json(),
                    profile.created_at,
                    profile.updated_at,
                ),
            )
            conn.commit()

    def get_installed_profile(
        self,
        profile_name: str,
        *,
        owner_agent_id: str,
        owner_canonical_user_id: str,
        include_removed: bool = False,
    ) -> Optional[ObjectiveWorkInstalledProfile]:
        owner = self._require_owner(owner_agent_id)
        status_clause = "" if include_removed else f" AND status={self._dialect.bind}"
        params: tuple[Any, ...] = (
            profile_name.casefold(),
            owner,
            owner_canonical_user_id,
        )
        if not include_removed:
            params += (ObjectiveWorkInstalledProfileStatus.ACTIVE.value,)
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT profile_data FROM objective_work_installed_profiles
                WHERE profile_name_key={self._dialect.bind} AND owner_agent_id={self._dialect.bind}
                AND owner_canonical_user_id={self._dialect.bind}"""
                + status_clause,
                params,
            ).fetchone()
        return ObjectiveWorkInstalledProfile.model_validate_json(row[0]) if row else None

    def list_installed_profiles(
        self,
        *,
        owner_agent_id: str,
        owner_canonical_user_id: str,
        include_removed: bool = False,
        limit: int = 100,
    ) -> List[ObjectiveWorkInstalledProfile]:
        owner = self._require_owner(owner_agent_id)
        status_clause = "" if include_removed else f" AND status={self._dialect.bind}"
        params: tuple[Any, ...] = (owner, owner_canonical_user_id)
        if not include_removed:
            params += (ObjectiveWorkInstalledProfileStatus.ACTIVE.value,)
        params += (max(1, min(int(limit), 500)),)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT profile_data FROM objective_work_installed_profiles
                WHERE owner_agent_id={self._dialect.bind} AND owner_canonical_user_id={self._dialect.bind}"""
                + status_clause
                + f" ORDER BY updated_at DESC LIMIT {self._dialect.bind}",
                params,
            ).fetchall()
        return [ObjectiveWorkInstalledProfile.model_validate_json(row[0]) for row in rows]


class SqliteObjectiveWorkRepository(_RelationalObjectiveWorkRepository):
    """SQLite lifecycle and database-specific operations."""

    _dialect = SQLITE

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self._db_path = db_path
        self._dsn: Optional[str] = None
        self._logger = logger or logging.getLogger(__name__)
        self._init_schema()


    def _connect(self) -> Any:
        return connect_sqlite(self._db_path, logger=self._logger)


class PostgresObjectiveWorkRepository(_RelationalObjectiveWorkRepository):
    _dialect = POSTGRES

    def __init__(self, dsn: str, logger: Optional[logging.Logger] = None):
        self._dsn = dsn
        self._db_path = ""
        self._logger = logger or logging.getLogger(__name__)
        validate_postgres_connection(dsn)
        self._init_schema()

    def _connect(self) -> Any:
        assert self._dsn is not None
        return connect_postgres(self._dsn)


def create_objective_work_repository(
    settings: StorageSettings, *, logger: Optional[logging.Logger] = None
) -> ObjectiveWorkRepository:
    capabilities = describe_storage_capabilities(settings)
    require_repository_backend(
        settings,
        repository_name="ObjectiveWorkRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    if settings.backend == STORAGE_BACKEND_SQLITE and settings.sqlite_path:
        return SqliteObjectiveWorkRepository(settings.sqlite_path, logger=logger)
    if settings.backend == STORAGE_BACKEND_POSTGRES and settings.postgres_dsn:
        return PostgresObjectiveWorkRepository(settings.postgres_dsn, logger=logger)
    raise NotImplementedError(
        f"ObjectiveWorkRepository has no implementation for backend {settings.backend!r}."
    )
