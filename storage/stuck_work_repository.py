"""Open "stuck work" incidents, so each is reported once and its recovery too.

Trusted runtime code (``custom_agents.stuck_work``) records every schedule or
task it finds stuck, tells the affected people once the incident has lasted
long enough, and closes the incident when the work recovers, telling the same
people. Rows hold no private content: ids, the reason code, who was told and
when. SQLite and PostgreSQL share the schema.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres, ensure_storage_parent_dir
from storage.sql_dialect import POSTGRES, SQLITE
from storage.sqlite_connection import connect_sqlite

# worker_pool: a worker pool problem administrators must hear about
# (custom_agents.worker_pool_reviews.RecurringPoolProblems).
StuckSubjectKind = Literal["schedule", "task", "worker_pool"]


class StuckWorkIncidentV1(BaseModel):
    """One stuck schedule occurrence or task, from first sight until recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    subject_kind: StuckSubjectKind
    subject_id: str
    # What identifies this incident of the subject (for a schedule, its due
    # time), so a later, separate problem is a new incident.
    incident_key: str
    reason: str
    person_id: str | None = None
    tell_administrators: bool = False
    first_seen_at_utc: datetime
    notified_at_utc: datetime | None = None

    @property
    def incident_id(self) -> str:
        raw = "\x1f".join((self.team_id, self.subject_kind, self.subject_id, self.incident_key))
        return "stuck_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class StuckWorkRepository:
    def __init__(self, settings: StorageSettings) -> None:
        self.settings = settings
        self.dialect = POSTGRES if settings.postgres_dsn else SQLITE
        if not settings.postgres_dsn and not settings.sqlite_path:
            raise ValueError("Stuck-work incidents require configured storage.")
        ensure_storage_parent_dir(settings.sqlite_path)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS stuck_work_incidents (
                incident_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS stuck_work_team ON stuck_work_incidents(team_id, subject_kind)"
            )
            conn.commit()

    def _connect(self) -> Any:
        if self.settings.postgres_dsn:
            return connect_postgres(self.settings.postgres_dsn)
        return connect_sqlite(str(self.settings.sqlite_path), row_factory=sqlite3.Row)

    def open_incidents(
        self, team_id: str, subject_kind: StuckSubjectKind
    ) -> tuple[StuckWorkIncidentV1, ...]:
        p = self.dialect.bind
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM stuck_work_incidents WHERE team_id={p} AND subject_kind={p}",
                (team_id, subject_kind),
            ).fetchall()
        return tuple(StuckWorkIncidentV1.model_validate_json(row[0]) for row in rows)

    def record_seen(self, incident: StuckWorkIncidentV1) -> StuckWorkIncidentV1:
        """Store a newly seen incident; an incident already open keeps its first sight."""

        p = self.dialect.bind
        with self._connect() as conn:
            conn.begin_write()
            conn.execute(
                f"INSERT INTO stuck_work_incidents(incident_id,team_id,subject_kind,subject_id,payload_json) "
                f"VALUES ({p},{p},{p},{p},{p}) ON CONFLICT(incident_id) DO NOTHING",
                (
                    incident.incident_id,
                    incident.team_id,
                    incident.subject_kind,
                    incident.subject_id,
                    incident.model_dump_json(),
                ),
            )
            row = conn.execute(
                f"SELECT payload_json FROM stuck_work_incidents WHERE incident_id={p}",
                (incident.incident_id,),
            ).fetchone()
            conn.commit()
        return StuckWorkIncidentV1.model_validate_json(row[0])

    def mark_notified(self, incident: StuckWorkIncidentV1, at: datetime) -> StuckWorkIncidentV1:
        """Record that people were told; the stored reason becomes the one they were told."""

        p = self.dialect.bind
        told = incident.model_copy(update={"notified_at_utc": at})
        with self._connect() as conn:
            conn.execute(
                f"UPDATE stuck_work_incidents SET payload_json={p} WHERE incident_id={p}",
                (told.model_dump_json(), incident.incident_id),
            )
            conn.commit()
        return told

    def close(self, incident: StuckWorkIncidentV1) -> bool:
        """Remove a recovered incident; True when this call removed it."""

        p = self.dialect.bind
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM stuck_work_incidents WHERE incident_id={p}",
                (incident.incident_id,),
            )
            conn.commit()
            return int(cursor.rowcount) == 1
