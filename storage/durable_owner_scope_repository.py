"""Atomic reconciliation for manager-role-owned durable workflow records.

Runtime agent ids identify one process incarnation.  Objectives, Plans, and
bounded-execution records instead belong to the stable team/manager-role scope.
This repository repairs records written with a runtime id before that invariant
was enforced, without widening authenticated-user, team, or role ownership.
"""

from __future__ import annotations

from storage.sql_dialect import SQLITE, POSTGRES

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from typing import Any, Optional

from custom_agents.durable_owner_scope import MANAGER_SCOPE_PREFIX
from custom_messages.records.objectives import ObjectiveRecord, ObjectiveWorkCheckpoint, ObjectiveWorkInstalledProfile, ObjectiveWorkPolicy, ObjectiveWorkProfileProposal, ObjectiveWorkSession
from custom_messages.records.plans import PlanRecord

from .backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from .config import StorageSettings
from .postgres_connection import connect_postgres, validate_postgres_connection
from .runtime import describe_storage_capabilities, require_repository_backend
from .sqlite_connection import connect_sqlite


@dataclass(frozen=True)
class ManagerOwnerScopeReconciliation:
    """Observable result of one startup owner-scope reconciliation."""

    durable_owner_agent_id: str
    prior_owner_agent_ids: tuple[str, ...] = ()
    objectives: int = 0
    plans: int = 0
    policies: int = 0
    sessions: int = 0
    checkpoints: int = 0
    profile_proposals: int = 0
    installed_profiles: int = 0
    archived_conflicts: int = 0

    @property
    def total_records(self) -> int:
        return (
            self.objectives
            + self.plans
            + self.policies
            + self.sessions
            + self.checkpoints
            + self.profile_proposals
            + self.installed_profiles
            + self.archived_conflicts
        )


@dataclass(frozen=True)
class UserObjectiveAdoption:
    """Result of recovering one canonical-user-owned Objective aggregate."""

    objective_id: str
    owner_canonical_user_id: str
    durable_owner_agent_id: str
    prior_owner_agent_id: Optional[str] = None
    objectives: int = 0
    plans: int = 0
    policies: int = 0
    sessions: int = 0
    checkpoints: int = 0
    archived_conflicts: int = 0

    @property
    def total_records(self) -> int:
        return (
            self.objectives
            + self.plans
            + self.policies
            + self.sessions
            + self.checkpoints
            + self.archived_conflicts
        )


@dataclass(frozen=True)
class _OwnerRow:
    table_name: str
    record_id_column: str
    old_record_id: str
    new_record_id: str
    data_column: str
    data_json: str
    model_type: type[Any]


@dataclass(frozen=True)
class _ArchivedOwnerCollision:
    table_name: str
    source_record_id: str
    target_record_id: str
    selected_record_id: str
    reason: str


class DurableOwnerScopeRepository:
    """Re-key one manager's workflow aggregate inside a single transaction."""

    def __init__(
        self,
        storage_settings: StorageSettings,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._sqlite_path: Optional[str]
        self._postgres_dsn: Optional[str]
        if storage_settings.backend == STORAGE_BACKEND_SQLITE:
            if not storage_settings.sqlite_path:
                raise ValueError("SQLite durable owner reconciliation requires a path.")
            self._sqlite_path = storage_settings.sqlite_path
            self._postgres_dsn = None
        elif storage_settings.backend == STORAGE_BACKEND_POSTGRES:
            if not storage_settings.postgres_dsn:
                raise ValueError("Postgres durable owner reconciliation requires a DSN.")
            self._sqlite_path = None
            self._postgres_dsn = storage_settings.postgres_dsn
            validate_postgres_connection(self._postgres_dsn)
        else:  # pragma: no cover - factory rejects unsupported backends
            raise NotImplementedError(
                f"Durable owner reconciliation does not support {storage_settings.backend!r}."
            )

    @property
    def _dialect(self):
        return SQLITE if self._sqlite_path else POSTGRES

    def _connect(self) -> Any:
        if self._sqlite_path:
            return connect_sqlite(self._sqlite_path, logger=self._logger)
        return connect_postgres(str(self._postgres_dsn))

    @staticmethod
    def _is_runtime_owner_id(owner_agent_id: Any) -> bool:
        owner = str(owner_agent_id or "").strip()
        return bool(owner) and not owner.startswith(f"{MANAGER_SCOPE_PREFIX}:")

    @staticmethod
    def _owner_placeholders(owner_agent_ids: tuple[str, ...], *, bind: str) -> str:
        return ",".join(bind for _ in owner_agent_ids)

    @staticmethod
    def _with_durable_owner(model_type: type[Any], data_json: str, owner: str) -> str:
        record = model_type.model_validate_json(data_json)
        record.owner_agent_id = owner
        return str(record.model_dump_json())

    @staticmethod
    def _with_manager_scope(
        model_type: type[Any],
        data_json: str,
        *,
        owner: str,
        team_id: str,
        named_agent_type: str,
    ) -> str:
        record = model_type.model_validate_json(data_json)
        record.owner_agent_id = owner
        if hasattr(record, "team_id"):
            record.team_id = team_id
        if hasattr(record, "owner_named_agent_type"):
            record.owner_named_agent_type = named_agent_type
        return str(record.model_dump_json())

    @staticmethod
    def _parse_record_timestamp(value: Any) -> datetime:
        rendered = str(value or "").strip()
        if not rendered:
            return datetime.min.replace(tzinfo=timezone.utc)
        if rendered.endswith("Z"):
            rendered = f"{rendered[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(rendered)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _row_freshness(cls, row: _OwnerRow) -> tuple[datetime, datetime]:
        record = row.model_type.model_validate_json(row.data_json)
        return (
            cls._parse_record_timestamp(getattr(record, "updated_at", None)),
            cls._parse_record_timestamp(getattr(record, "created_at", None)),
        )

    @staticmethod
    def _semantic_row_payload(row: _OwnerRow) -> str:
        record = row.model_type.model_validate_json(row.data_json)
        payload = record.model_dump(mode="json")
        payload.pop("owner_agent_id", None)
        payload.pop("owner_agent_name", None)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _select_legacy_row(cls, rows: list[_OwnerRow]) -> _OwnerRow:
        freshness_by_record_id = {
            row.old_record_id: cls._row_freshness(row) for row in rows
        }
        newest_freshness = max(freshness_by_record_id.values())
        newest_rows = [
            row
            for row in rows
            if freshness_by_record_id[row.old_record_id] == newest_freshness
        ]
        if len(newest_rows) == 1:
            return newest_rows[0]
        if len({cls._semantic_row_payload(row) for row in newest_rows}) == 1:
            return min(newest_rows, key=lambda row: row.old_record_id)
        sample = newest_rows[0]
        raise RuntimeError(
            "Durable manager owner reconciliation found ambiguous legacy snapshots: "
            f"table={sample.table_name} new_record_id={sample.new_record_id} "
            f"source_record_ids={sorted(row.old_record_id for row in newest_rows)}."
        )

    @staticmethod
    def _ensure_archive_schema(conn: Any) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS durable_owner_scope_reconciliation_archive (
                archive_id TEXT PRIMARY KEY,
                source_table TEXT NOT NULL,
                source_record_id_column TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                target_record_id TEXT NOT NULL,
                selected_record_id TEXT NOT NULL,
                source_data_column TEXT NOT NULL,
                source_data TEXT NOT NULL,
                reason TEXT NOT NULL,
                archived_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_durable_owner_scope_archive_target
            ON durable_owner_scope_reconciliation_archive(
                source_table, target_record_id, archived_at
            )
            """
        )

    @staticmethod
    def _archive_id(row: _OwnerRow) -> str:
        digest_input = "\0".join(
            (
                row.table_name,
                row.old_record_id,
                row.new_record_id,
                row.data_json,
            )
        )
        return f"owner_scope_archive_{sha256(digest_input.encode('utf-8')).hexdigest()[:32]}"

    @classmethod
    def _archive_and_delete_rows(
        cls,
        conn: Any,
        rows: list[_OwnerRow],
        *,
        selected_record_id: str,
        reason: str,
    ) -> list[_ArchivedOwnerCollision]:
        if not rows:
            return []
        cls._ensure_archive_schema(conn)
        archived_at = datetime.now(timezone.utc).isoformat()
        archived: list[_ArchivedOwnerCollision] = []
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO durable_owner_scope_reconciliation_archive (
                    archive_id, source_table, source_record_id_column,
                    source_record_id, target_record_id, selected_record_id,
                    source_data_column, source_data, reason, archived_at
                ) VALUES ({conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind}, {conn.dialect.bind})
                ON CONFLICT(archive_id) DO NOTHING
                """,
                (
                    cls._archive_id(row),
                    row.table_name,
                    row.record_id_column,
                    row.old_record_id,
                    row.new_record_id,
                    selected_record_id,
                    row.data_column,
                    row.data_json,
                    reason,
                    archived_at,
                ),
            )
            deleted = conn.execute(
                f"DELETE FROM {row.table_name} WHERE {row.record_id_column}={conn.dialect.bind}",
                (row.old_record_id,),
            )
            if deleted.rowcount != 1:
                raise RuntimeError(
                    "Durable manager owner reconciliation lost a superseded row: "
                    f"table={row.table_name} record_id={row.old_record_id}."
                )
            archived.append(
                _ArchivedOwnerCollision(
                    table_name=row.table_name,
                    source_record_id=row.old_record_id,
                    target_record_id=row.new_record_id,
                    selected_record_id=selected_record_id,
                    reason=reason,
                )
            )
        return archived

    @classmethod
    def _resolve_row_collisions(
        cls,
        conn: Any,
        rows: list[_OwnerRow],
        *,
        durable_owner_agent_id: str,
    ) -> tuple[list[_OwnerRow], list[_ArchivedOwnerCollision]]:
        grouped: dict[tuple[str, str], list[_OwnerRow]] = {}
        for row in rows:
            grouped.setdefault((row.table_name, row.new_record_id), []).append(row)

        selected_rows: list[_OwnerRow] = []
        archived: list[_ArchivedOwnerCollision] = []
        for group in grouped.values():
            sample = group[0]
            source_record_ids = {row.old_record_id for row in group}
            existing = conn.execute(
                f"SELECT owner_agent_id, {sample.data_column} "
                f"FROM {sample.table_name} "
                f"WHERE {sample.record_id_column}={conn.dialect.bind} LIMIT 1",
                (sample.new_record_id,),
            ).fetchone()
            if existing is not None and sample.new_record_id not in source_record_ids:
                existing_owner = str(existing[0] or "").strip()
                existing_record = sample.model_type.model_validate_json(existing[1])
                if (
                    existing_owner != durable_owner_agent_id
                    or str(getattr(existing_record, "owner_agent_id", "") or "").strip()
                    != durable_owner_agent_id
                ):
                    raise RuntimeError(
                        "Durable manager owner reconciliation found a target key "
                        "without matching durable ownership: "
                        f"table={sample.table_name} new_record_id={sample.new_record_id}."
                    )
                archived.extend(
                    cls._archive_and_delete_rows(
                        conn,
                        group,
                        selected_record_id=sample.new_record_id,
                        reason="superseded_by_existing_durable_target",
                    )
                )
                continue

            selected = cls._select_legacy_row(group)
            superseded = [row for row in group if row is not selected]
            archived.extend(
                cls._archive_and_delete_rows(
                    conn,
                    superseded,
                    selected_record_id=selected.old_record_id,
                    reason="superseded_legacy_snapshot",
                )
            )
            selected_rows.append(selected)
        return selected_rows, archived

    def _log_archived_collisions(
        self,
        collisions: list[_ArchivedOwnerCollision],
        *,
        durable_owner_agent_id: str,
    ) -> None:
        for collision in collisions:
            self._logger.warning(
                "Archived a superseded durable manager ownership snapshot. "
                "event=durable_manager_scope_collision_archived "
                "table=%s source_record_id=%s target_record_id=%s "
                "selected_record_id=%s durable_owner_agent_id=%s reason=%s",
                collision.table_name,
                collision.source_record_id,
                collision.target_record_id,
                collision.selected_record_id,
                durable_owner_agent_id,
                collision.reason,
            )

    @classmethod
    def _apply_rows(cls, conn: Any, rows: list[_OwnerRow], owner: str) -> None:
        for row in rows:
            updated = conn.execute(
                f"UPDATE {row.table_name} SET {row.record_id_column}={conn.dialect.bind}, "
                f"owner_agent_id={conn.dialect.bind}, {row.data_column}={conn.dialect.bind} "
                f"WHERE {row.record_id_column}={conn.dialect.bind}",
                (
                    row.new_record_id,
                    owner,
                    cls._with_durable_owner(row.model_type, row.data_json, owner),
                    row.old_record_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Durable manager owner reconciliation lost its target row: "
                    f"table={row.table_name} record_id={row.old_record_id}."
                )

    @staticmethod
    def _workflow_rows(
        conn: Any,
        *,
        team_id: str,
        named_agent_type: str,
        durable_owner_agent_id: str,
    ) -> tuple[list[_OwnerRow], list[_OwnerRow], set[str], set[str]]:
        objective_rows: list[_OwnerRow] = []
        plan_rows: list[_OwnerRow] = []
        prior_owner_ids: set[str] = set()
        objective_ids: set[str] = set()

        for row in conn.execute(
            f"""SELECT objective_record_id, objective_id, owner_agent_id,
                      objective_data
               FROM objectives
               WHERE team_id={conn.dialect.bind} AND owner_named_agent_type={conn.dialect.bind}""",
            (team_id, named_agent_type),
        ).fetchall():
            objective_id = str(row[1])
            owner = str(row[2] or "")
            objective_ids.add(objective_id)
            if owner == durable_owner_agent_id or not DurableOwnerScopeRepository._is_runtime_owner_id(owner):
                continue
            prior_owner_ids.add(owner)
            objective_rows.append(
                _OwnerRow(
                    table_name="objectives",
                    record_id_column="objective_record_id",
                    old_record_id=str(row[0]),
                    new_record_id=f"{durable_owner_agent_id}::{objective_id}",
                    data_column="objective_data",
                    data_json=str(row[3]),
                    model_type=ObjectiveRecord,
                )
            )

        for row in conn.execute(
            f"""SELECT plan_record_id, plan_id, owner_agent_id, plan_data,
                      objective_id
               FROM plans
               WHERE team_id={conn.dialect.bind} AND owner_named_agent_type={conn.dialect.bind}""",
            (team_id, named_agent_type),
        ).fetchall():
            plan_id = str(row[1])
            owner = str(row[2] or "")
            if row[4]:
                objective_ids.add(str(row[4]))
            if owner == durable_owner_agent_id or not DurableOwnerScopeRepository._is_runtime_owner_id(owner):
                continue
            prior_owner_ids.add(owner)
            plan_rows.append(
                _OwnerRow(
                    table_name="plans",
                    record_id_column="plan_record_id",
                    old_record_id=str(row[0]),
                    new_record_id=f"{durable_owner_agent_id}::{plan_id}",
                    data_column="plan_data",
                    data_json=str(row[3]),
                    model_type=PlanRecord,
                )
            )
        return objective_rows, plan_rows, prior_owner_ids, objective_ids

    @staticmethod
    def _discover_policy_owner_ids(
        conn: Any,
        *,
        objective_ids: set[str],
        durable_owner_agent_id: str,
    ) -> set[str]:
        if not objective_ids:
            return set()
        discovered: set[str] = set()
        for row in conn.execute(
            "SELECT objective_id, owner_agent_id FROM objective_work_policies"
        ).fetchall():
            owner = str(row[1] or "")
            if (
                str(row[0]) in objective_ids
                and owner != durable_owner_agent_id
                and DurableOwnerScopeRepository._is_runtime_owner_id(owner)
            ):
                discovered.add(owner)
        return discovered

    @classmethod
    def _objective_work_rows(
        cls,
        conn: Any,
        *,
        prior_owner_agent_ids: tuple[str, ...],
        durable_owner_agent_id: str,
    ) -> dict[str, list[_OwnerRow]]:
        rows_by_kind: dict[str, list[_OwnerRow]] = {
            "policies": [],
            "sessions": [],
            "checkpoints": [],
            "profile_proposals": [],
            "installed_profiles": [],
        }
        if not prior_owner_agent_ids:
            return rows_by_kind
        placeholders = cls._owner_placeholders(prior_owner_agent_ids, bind=conn.dialect.bind)
        table_specs = (
            (
                "policies",
                "objective_work_policies",
                "policy_record_id",
                "objective_id",
                None,
                "policy_data",
                ObjectiveWorkPolicy,
            ),
            (
                "sessions",
                "objective_work_sessions",
                "session_record_id",
                "session_id",
                None,
                "session_data",
                ObjectiveWorkSession,
            ),
            (
                "checkpoints",
                "objective_work_checkpoints",
                "checkpoint_record_id",
                "checkpoint_id",
                None,
                "checkpoint_data",
                ObjectiveWorkCheckpoint,
            ),
            (
                "profile_proposals",
                "objective_work_profile_proposals",
                "proposal_record_id",
                "proposal_id",
                "owner_canonical_user_id",
                "proposal_data",
                ObjectiveWorkProfileProposal,
            ),
            (
                "installed_profiles",
                "objective_work_installed_profiles",
                "profile_record_id",
                "profile_name_key",
                "owner_canonical_user_id",
                "profile_data",
                ObjectiveWorkInstalledProfile,
            ),
        )
        for (
            kind,
            table_name,
            record_id_column,
            logical_id_column,
            user_column,
            data_column,
            model_type,
        ) in table_specs:
            selected_columns = (
                f"{record_id_column}, {logical_id_column}, owner_agent_id, {data_column}"
            )
            if user_column:
                selected_columns += f", {user_column}"
            selected = conn.execute(
                f"SELECT {selected_columns} FROM {table_name} "
                f"WHERE owner_agent_id IN ({placeholders})",
                prior_owner_agent_ids,
            ).fetchall()
            for row in selected:
                logical_id = str(row[1])
                new_parts = [durable_owner_agent_id]
                if user_column:
                    new_parts.append(str(row[4]))
                new_parts.append(logical_id)
                rows_by_kind[kind].append(
                    _OwnerRow(
                        table_name=table_name,
                        record_id_column=record_id_column,
                        old_record_id=str(row[0]),
                        new_record_id="::".join(new_parts),
                        data_column=data_column,
                        data_json=str(row[3]),
                        model_type=model_type,
                    )
                )
        return rows_by_kind

    def reconcile_manager_scope(
        self,
        *,
        team_id: str,
        named_agent_type: str,
        durable_owner_agent_id: str,
        include_objective_work: bool,
    ) -> ManagerOwnerScopeReconciliation:
        """Move same-team/role runtime-owned rows to the durable manager scope."""

        if not durable_owner_agent_id.startswith(f"{MANAGER_SCOPE_PREFIX}:"):
            raise ValueError("Durable manager reconciliation requires a manager_scope owner id.")
        with self._connect() as conn:
            (
                objective_rows,
                plan_rows,
                prior_owner_ids,
                objective_ids,
            ) = self._workflow_rows(
                conn,
                team_id=team_id,
                named_agent_type=named_agent_type,
                durable_owner_agent_id=durable_owner_agent_id,
            )
            if include_objective_work:
                prior_owner_ids.update(
                    self._discover_policy_owner_ids(
                        conn,
                        objective_ids=objective_ids,
                        durable_owner_agent_id=durable_owner_agent_id,
                    )
                )
            prior_owners = tuple(sorted(prior_owner_ids))
            work_rows = (
                self._objective_work_rows(
                    conn,
                    prior_owner_agent_ids=prior_owners,
                    durable_owner_agent_id=durable_owner_agent_id,
                )
                if include_objective_work
                else {
                    "policies": [],
                    "sessions": [],
                    "checkpoints": [],
                    "profile_proposals": [],
                    "installed_profiles": [],
                }
            )
            archived_collisions: list[_ArchivedOwnerCollision] = []
            objective_rows, archived = self._resolve_row_collisions(
                conn,
                objective_rows,
                durable_owner_agent_id=durable_owner_agent_id,
            )
            archived_collisions.extend(archived)
            plan_rows, archived = self._resolve_row_collisions(
                conn,
                plan_rows,
                durable_owner_agent_id=durable_owner_agent_id,
            )
            archived_collisions.extend(archived)
            for kind, rows in work_rows.items():
                resolved, archived = self._resolve_row_collisions(
                    conn,
                    rows,
                    durable_owner_agent_id=durable_owner_agent_id,
                )
                work_rows[kind] = resolved
                archived_collisions.extend(archived)
            self._apply_rows(conn, objective_rows, durable_owner_agent_id)
            self._apply_rows(conn, plan_rows, durable_owner_agent_id)
            for rows in work_rows.values():
                self._apply_rows(conn, rows, durable_owner_agent_id)

        result = ManagerOwnerScopeReconciliation(
            durable_owner_agent_id=durable_owner_agent_id,
            prior_owner_agent_ids=prior_owners,
            objectives=len(objective_rows),
            plans=len(plan_rows),
            policies=len(work_rows["policies"]),
            sessions=len(work_rows["sessions"]),
            checkpoints=len(work_rows["checkpoints"]),
            profile_proposals=len(work_rows["profile_proposals"]),
            installed_profiles=len(work_rows["installed_profiles"]),
            archived_conflicts=len(archived_collisions),
        )
        self._log_archived_collisions(
            archived_collisions,
            durable_owner_agent_id=durable_owner_agent_id,
        )
        return result

    @staticmethod
    def _targeted_objective_work_rows(
        conn: Any,
        *,
        objective_id: str,
        prior_owner_agent_id: str,
        durable_owner_agent_id: str,
    ) -> dict[str, list[_OwnerRow]]:
        rows_by_kind: dict[str, list[_OwnerRow]] = {
            "policies": [],
            "sessions": [],
            "checkpoints": [],
        }
        specs = (
            (
                "policies",
                "objective_work_policies",
                "policy_record_id",
                "objective_id",
                "policy_data",
                ObjectiveWorkPolicy,
            ),
            (
                "sessions",
                "objective_work_sessions",
                "session_record_id",
                "session_id",
                "session_data",
                ObjectiveWorkSession,
            ),
            (
                "checkpoints",
                "objective_work_checkpoints",
                "checkpoint_record_id",
                "checkpoint_id",
                "checkpoint_data",
                ObjectiveWorkCheckpoint,
            ),
        )
        for (
            kind,
            table_name,
            record_id_column,
            logical_id_column,
            data_column,
            model_type,
        ) in specs:
            selected = conn.execute(
                f"SELECT {record_id_column}, {logical_id_column}, {data_column} "
                f"FROM {table_name} WHERE objective_id={conn.dialect.bind} AND owner_agent_id={conn.dialect.bind}",
                (objective_id, prior_owner_agent_id),
            ).fetchall()
            rows_by_kind[kind] = [
                _OwnerRow(
                    table_name=table_name,
                    record_id_column=record_id_column,
                    old_record_id=str(row[0]),
                    new_record_id=f"{durable_owner_agent_id}::{row[1]}",
                    data_column=data_column,
                    data_json=str(row[2]),
                    model_type=model_type,
                )
                for row in selected
            ]
        return rows_by_kind

    def adopt_user_owned_objective(
        self,
        *,
        objective_id: str,
        owner_canonical_user_id: str,
        team_id: str,
        named_agent_type: str,
        durable_owner_agent_id: str,
        include_objective_work: bool,
    ) -> UserObjectiveAdoption:
        """Move one authenticated user's orphaned Objective to live custody."""

        objective_id = str(objective_id or "").strip()
        canonical_owner = str(owner_canonical_user_id or "").strip()
        team_id = str(team_id or "").strip()
        named_agent_type = str(named_agent_type or "").strip()
        if not all(
            (
                objective_id,
                canonical_owner,
                team_id,
                named_agent_type,
            )
        ):
            raise ValueError(
                "Objective adoption requires objective, user, team, and agent type."
            )
        if not durable_owner_agent_id.startswith(f"{MANAGER_SCOPE_PREFIX}:"):
            raise ValueError("Objective adoption requires a durable manager scope.")

        with self._connect() as conn:
            selected = conn.execute(
                f"""
                SELECT objective_record_id, owner_agent_id, objective_data, team_id
                FROM objectives
                WHERE objective_id={self._dialect.bind}
                  AND owner_canonical_user_id={self._dialect.bind}
                  AND (team_id={self._dialect.bind} OR team_id IS NULL OR team_id='')
                ORDER BY CASE WHEN owner_agent_id={self._dialect.bind} THEN 0 ELSE 1 END,
                         updated_at DESC
                LIMIT 1
                """,
                (
                    objective_id,
                    canonical_owner,
                    team_id,
                    durable_owner_agent_id,
                ),
            ).fetchone()
            if selected is None:
                raise LookupError("Authenticated user's Objective was not found.")
            objective = ObjectiveRecord.model_validate_json(selected[2])
            if objective.owner_canonical_user_id != canonical_owner:
                raise PermissionError(
                    "Objective canonical ownership differs from its relational owner."
                )
            if objective.team_id and objective.team_id != team_id:
                raise PermissionError("Objective belongs to another team.")
            prior_owner = str(selected[1] or "").strip()
            if prior_owner == durable_owner_agent_id:
                return UserObjectiveAdoption(
                    objective_id=objective_id,
                    owner_canonical_user_id=canonical_owner,
                    durable_owner_agent_id=durable_owner_agent_id,
                )

            objective_rows = [
                _OwnerRow(
                    table_name="objectives",
                    record_id_column="objective_record_id",
                    old_record_id=str(selected[0]),
                    new_record_id=f"{durable_owner_agent_id}::{objective_id}",
                    data_column="objective_data",
                    data_json=self._with_manager_scope(
                        ObjectiveRecord,
                        str(selected[2]),
                        owner=durable_owner_agent_id,
                        team_id=team_id,
                        named_agent_type=named_agent_type,
                    ),
                    model_type=ObjectiveRecord,
                )
            ]
            selected_plans = conn.execute(
                    f"""
                    SELECT plan_record_id, plan_id, plan_data
                    FROM plans
                    WHERE objective_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind}
                    """,
                    (objective_id, prior_owner),
                ).fetchall()
            plan_rows = [
                _OwnerRow(
                    table_name="plans",
                    record_id_column="plan_record_id",
                    old_record_id=str(row[0]),
                    new_record_id=f"{durable_owner_agent_id}::{row[1]}",
                    data_column="plan_data",
                    data_json=self._with_manager_scope(
                        PlanRecord,
                        str(row[2]),
                        owner=durable_owner_agent_id,
                        team_id=team_id,
                        named_agent_type=named_agent_type,
                    ),
                    model_type=PlanRecord,
                )
                for row in selected_plans
            ]
            work_rows = (
                self._targeted_objective_work_rows(
                    conn,
                    objective_id=objective_id,
                    prior_owner_agent_id=prior_owner,
                    durable_owner_agent_id=durable_owner_agent_id,
                )
                if include_objective_work
                else {"policies": [], "sessions": [], "checkpoints": []}
            )
            archived_collisions: list[_ArchivedOwnerCollision] = []
            objective_rows, archived = self._resolve_row_collisions(
                conn,
                objective_rows,
                durable_owner_agent_id=durable_owner_agent_id,
            )
            archived_collisions.extend(archived)
            plan_rows, archived = self._resolve_row_collisions(
                conn,
                plan_rows,
                durable_owner_agent_id=durable_owner_agent_id,
            )
            archived_collisions.extend(archived)
            for kind, rows in work_rows.items():
                resolved, archived = self._resolve_row_collisions(
                    conn,
                    rows,
                    durable_owner_agent_id=durable_owner_agent_id,
                )
                work_rows[kind] = resolved
                archived_collisions.extend(archived)
            self._apply_rows(conn, objective_rows, durable_owner_agent_id)
            self._apply_rows(conn, plan_rows, durable_owner_agent_id)
            for rows in work_rows.values():
                self._apply_rows(conn, rows, durable_owner_agent_id)
            conn.execute(
                f"""
                UPDATE objectives
                SET team_id={self._dialect.bind}, owner_named_agent_type={self._dialect.bind}
                WHERE objective_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind}
                """,
                (
                    team_id,
                    named_agent_type,
                    objective_id,
                    durable_owner_agent_id,
                ),
            )
            conn.execute(
                f"""
                UPDATE plans
                SET team_id={self._dialect.bind}, owner_named_agent_type={self._dialect.bind}
                WHERE objective_id={self._dialect.bind} AND owner_agent_id={self._dialect.bind}
                """,
                (
                    team_id,
                    named_agent_type,
                    objective_id,
                    durable_owner_agent_id,
                ),
            )

        result = UserObjectiveAdoption(
            objective_id=objective_id,
            owner_canonical_user_id=canonical_owner,
            durable_owner_agent_id=durable_owner_agent_id,
            prior_owner_agent_id=prior_owner or None,
            objectives=1,
            plans=len(plan_rows),
            policies=len(work_rows["policies"]),
            sessions=len(work_rows["sessions"]),
            checkpoints=len(work_rows["checkpoints"]),
            archived_conflicts=len(archived_collisions),
        )
        self._log_archived_collisions(
            archived_collisions,
            durable_owner_agent_id=durable_owner_agent_id,
        )
        self._logger.warning(
            "Recovered authenticated user control of an orphaned Objective. "
            "event=user_objective_adopted objective_id=%s "
            "owner_canonical_user_id=%s prior_owner_agent_id=%s "
            "durable_owner_agent_id=%s records=%d",
            result.objective_id,
            result.owner_canonical_user_id,
            result.prior_owner_agent_id,
            result.durable_owner_agent_id,
            result.total_records,
        )
        return result


def create_durable_owner_scope_repository(
    storage_settings: StorageSettings,
    *,
    logger: Optional[logging.Logger] = None,
) -> DurableOwnerScopeRepository:
    capabilities = describe_storage_capabilities(storage_settings)
    require_repository_backend(
        storage_settings,
        repository_name="DurableOwnerScopeRepository",
        capability_supported=capabilities.supports_runtime_repositories,
    )
    return DurableOwnerScopeRepository(storage_settings, logger=logger)


__all__ = [
    "DurableOwnerScopeRepository",
    "ManagerOwnerScopeReconciliation",
    "UserObjectiveAdoption",
    "create_durable_owner_scope_repository",
]
