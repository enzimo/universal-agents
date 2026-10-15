"""Durable retirement of abandoned review claims, with retained audit history.

Expiry never proves completion. Retired jobs cannot be claimed for execution;
InternalImprovementAgent reviews their metadata before archive or deletion.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Iterator

from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from storage.config import StorageSettings
from storage.runtime_snapshot_layout import assemble_snapshot
from storage.runtime_snapshot_maintenance import _connection, _has_table

LOGGER = logging.getLogger(__name__)
REVIEW_EXPIRED = "expired"
REVIEW_ARCHIVED = "archived"
REVIEW_PURGED = "purged"
EVENT_TABLE = "memory_review_cleanup_events"
META_COLUMNS = (
    "review_job_id",
    "owner_team_id",
    "owner_named_agent_type",
    "task_id",
    "status",
    "claim_owner_agent_id",
    "claim_expires_at",
    "created_at",
    "updated_at",
)


def parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def protected_review_workers(conn: Any, team_id: str, *, postgres: bool) -> set[str]:
    """Include recoverable and unsupported runtimes; empty valid snapshots are idle."""
    protected: set[str] = set()
    if _has_table(conn, "agent_launches", postgres=postgres):
        # Protect other hosts and workers that are starting or have uncertain
        # liveness, even before their first task snapshot has been saved.
        protected.update(
            row["agent_id"]
            for row in conn.execute(
                f"SELECT agent_id FROM agent_launches WHERE team_id={conn.dialect.bind} AND state NOT IN ('retired','failed')",
                (team_id,),
            ).fetchall()
        )
    if not _has_table(conn, "active_task_runtime", postgres=postgres):
        return protected
    has_records = _has_table(conn, "active_task_runtime_records", postgres=postgres)
    for owner in conn.execute(
        f"SELECT owner_agent_id, runtime_state_json FROM active_task_runtime WHERE team_id={conn.dialect.bind}",
        (team_id,),
    ).fetchall():
        records = (
            conn.execute(
                f"SELECT section, record_id, payload_json FROM active_task_runtime_records WHERE owner_agent_id={conn.dialect.bind}",
                (owner["owner_agent_id"],),
            ).fetchall()
            if has_records
            else []
        )
        try:
            snapshot = decode_runtime_snapshot(
                assemble_snapshot(
                    owner["runtime_state_json"],
                    {
                        (r["section"], r["record_id"]): r["payload_json"]
                        for r in records
                    },
                )
            )
            if snapshot.tasks:
                protected.add(owner["owner_agent_id"])
        except (ValueError, KeyError, TypeError):
            protected.add(owner["owner_agent_id"])
    return protected


class MemoryReviewCleanupRepository:
    """Maintenance shares the review database, not an independent job queue."""

    def __init__(self, storage: StorageSettings):
        self.storage = storage
        self.postgres = storage.backend == "postgres"

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with _connection(self.storage) as conn:
            if self.postgres:
                # Claiming, task recovery and maintenance must not race across
                # the absence-of-work check and the status transition.
                tables = ["memory_review_jobs"]
                tables += [
                    name
                    for name in (
                        "agent_launches",
                        "active_task_runtime",
                        "active_task_runtime_records",
                    )
                    if _has_table(conn, name, postgres=True)
                ]
                conn.execute(
                    f"LOCK TABLE {', '.join(tables)} IN SHARE ROW EXCLUSIVE MODE"
                )
            else:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {EVENT_TABLE} (
                event_id TEXT PRIMARY KEY, review_job_id TEXT NOT NULL,
                team_id TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL,
                actor_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
            yield conn

    def candidates(
        self,
        team_id: str,
        *,
        before: datetime,
        protected_workers: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        with _connection(self.storage) as conn:
            if not _has_table(conn, "memory_review_jobs", postgres=self.postgres):
                return []
            return self._candidates(
                conn, team_id, before=before, protected_workers=protected_workers
            )

    def _candidates(
        self,
        conn: Any,
        team_id: str,
        *,
        before: datetime,
        protected_workers: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        protected = protected_review_workers(
            conn, team_id, postgres=self.postgres
        ) | set(protected_workers)
        rows = conn.execute(
            f"SELECT {', '.join(META_COLUMNS)} FROM memory_review_jobs WHERE owner_team_id={conn.dialect.bind} AND status='claimed' ORDER BY updated_at, review_job_id",
            (team_id,),
        ).fetchall()
        return [
            dict(row)
            for row in rows
            if (
                (expiry := parse_utc(row["claim_expires_at"])) is not None
                and expiry <= before
                and row["claim_owner_agent_id"]
                and row["claim_owner_agent_id"] not in protected
            )
        ]

    def retire_expired(
        self,
        team_id: str,
        *,
        before: datetime,
        now: datetime,
        actor_id: str,
        limit: int,
        protected_workers: tuple[str, ...] = (),
        expected_ids: set[str] | None = None,
    ) -> list[str]:
        """Set abandoned jobs aside. Do not clear their identity or retry them."""
        if limit < 1 or before > now:
            raise ValueError(
                "Retirement needs a positive batch size and an elapsed cutoff."
            )
        with self.transaction() as conn:
            rows = self._candidates(
                conn, team_id, before=before, protected_workers=protected_workers
            )
            if expected_ids is not None:
                rows = [row for row in rows if row["review_job_id"] in expected_ids]
                if {row["review_job_id"] for row in rows} != expected_ids:
                    raise ValueError(
                        "Review claims changed since preview; no claims were retired."
                    )
            rows = rows[:limit]
            for row in rows:
                self._transition(
                    conn,
                    row,
                    REVIEW_EXPIRED,
                    actor_id,
                    "claim_expired_without_saved_or_live_worker; outcome_unknown",
                    now,
                )
        if rows:
            LOGGER.warning(
                "Abandoned review claims set aside. event=memory_review_claims_retired team_id=%s count=%s job_ids=%s outcome=unknown",
                team_id,
                len(rows),
                [row["review_job_id"] for row in rows],
            )
        return [row["review_job_id"] for row in rows]

    def inspect(self, team_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with _connection(self.storage) as conn:
            if not _has_table(conn, "memory_review_jobs", postgres=self.postgres):
                return []
            return [
                dict(row)
                for row in conn.execute(
                    f"SELECT {', '.join(META_COLUMNS)} FROM memory_review_jobs WHERE owner_team_id={conn.dialect.bind} AND status IN ('expired','archived') ORDER BY CASE WHEN status='expired' THEN 0 ELSE 1 END, updated_at, review_job_id LIMIT {conn.dialect.bind}",
                    (team_id, limit),
                ).fetchall()
            ]

    def selected(self, team_id: str, ids: list[str]) -> list[dict[str, Any]]:
        with _connection(self.storage) as conn:
            return self._selected(conn, team_id, ids)

    def _selected(
        self, conn: Any, team_id: str, ids: list[str]
    ) -> list[dict[str, Any]]:
        rows = []
        for job_id in sorted(set(ids)):
            row = conn.execute(
                f"SELECT {', '.join(META_COLUMNS)} FROM memory_review_jobs WHERE owner_team_id={conn.dialect.bind} AND review_job_id={conn.dialect.bind}",
                (team_id, job_id),
            ).fetchone()
            if row is None:
                raise ValueError("A selected review does not exist in this team.")
            rows.append(dict(row))
        return rows

    def maintain(
        self,
        team_id: str,
        ids: list[str],
        *,
        operation: str,
        expected_digest: str,
        actor_id: str,
        reason: str,
        now: datetime,
        retention_days: int,
        protected_workers: tuple[str, ...] = (),
    ) -> list[str]:
        if (
            operation not in {"archive", "purge"}
            or not ids
            or not reason.strip()
            or retention_days < 1
        ):
            raise ValueError("Select review IDs, archive or purge, and an explanation.")
        with self.transaction() as conn:
            rows = self._selected(conn, team_id, ids)
            if contract_sha256({"jobs": rows}) != expected_digest:
                raise ValueError(
                    "Review state changed; inspect it again before cleanup."
                )
            protected = protected_review_workers(
                conn, team_id, postgres=self.postgres
            ) | set(protected_workers)
            for row in rows:
                if row["claim_owner_agent_id"] in protected:
                    raise ValueError(
                        "A selected review has a live or saved worker; no reviews were changed."
                    )
                self.validate_operation(row, operation, now, retention_days)
            for row in rows:
                target = REVIEW_ARCHIVED if operation == "archive" else REVIEW_PURGED
                self._transition(conn, row, target, actor_id, reason, now)
                if operation == "purge":
                    conn.execute(
                        f"DELETE FROM memory_review_jobs WHERE owner_team_id={conn.dialect.bind} AND review_job_id={conn.dialect.bind}",
                        (team_id, row["review_job_id"]),
                    )
        LOGGER.info(
            "Review cleanup committed. event=memory_review_cleanup team_id=%s operation=%s actor_id=%s job_ids=%s",
            team_id,
            operation,
            actor_id,
            ids,
        )
        return [row["review_job_id"] for row in rows]

    @staticmethod
    def validate_operation(
        row: dict[str, Any], operation: str, now: datetime, retention_days: int
    ) -> None:
        expected = REVIEW_EXPIRED if operation == "archive" else REVIEW_ARCHIVED
        if row["status"] != expected:
            raise ValueError(
                f"Only {expected} reviews can be selected for {operation}."
            )
        if operation == "purge":
            archived_at = parse_utc(row["updated_at"])
            if (
                archived_at is None
                or archived_at + timedelta(days=retention_days) > now
            ):
                raise ValueError(
                    "The archived review retention period has not elapsed."
                )

    @staticmethod
    def _transition(
        conn: Any,
        row: dict[str, Any],
        target: str,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        from uuid import uuid4

        bind = conn.dialect.bind
        conn.execute(
            f"UPDATE memory_review_jobs SET status={bind}, error_text={bind}, updated_at={bind} WHERE review_job_id={bind} AND owner_team_id={bind}",
            (
                target,
                reason,
                now.isoformat(),
                row["review_job_id"],
                row["owner_team_id"],
            ),
        )
        conn.execute(
            f"INSERT INTO {EVENT_TABLE} VALUES ({', '.join([bind] * 8)})",
            (
                f"review_cleanup_{uuid4().hex}",
                row["review_job_id"],
                row["owner_team_id"],
                row["status"],
                target,
                actor,
                reason,
                now.isoformat(),
            ),
        )
