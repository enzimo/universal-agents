"""Shared occurrence-table setup and facts, independent of worker policy services."""

from typing import Any

from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1
from storage.sql_dialect import SqlDialect


def ensure_occurrence_schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_occurrences (
        occurrence_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
        task_id TEXT NOT NULL UNIQUE, team_id TEXT NOT NULL,
        owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS schedule_occurrence_schedule ON schedule_occurrences(schedule_id)"
    )


def has_unfinished_runs(conn: Any, dialect: SqlDialect, schedule_id: str) -> bool:
    """Read admitted work inside the schedule's write transaction.

    Admission locks the same schedule row, so a final advance cannot miss
    a concurrently admitted run. Coalesced fires follow their active run.
    """
    rows = conn.execute(
        f"SELECT payload_json FROM schedule_occurrences WHERE schedule_id={dialect.bind}",
        (schedule_id,),
    ).fetchall()
    return any(
        record.finished_at_utc is None and record.state in {"admitted", "coalesced"}
        for record in (ScheduleOccurrenceV1.model_validate_json(row[0]) for row in rows)
    )
