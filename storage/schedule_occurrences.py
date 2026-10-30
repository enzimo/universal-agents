"""Shared occurrence-table setup and facts, independent of worker policy services."""

import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from access_control.identity import DEFAULT_CONTEXT_LIFETIME
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1
from storage.sql_dialect import SqlDialect

# Receipt states whose run may still be working (a merged fire follows its run).
UNFINISHED_RUN_STATES = frozenset({"admitted", "coalesced"})


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


def has_unfinished_runs(
    conn: Any, dialect: SqlDialect, schedule_id: str, *, now: datetime, logger: logging.Logger,
) -> bool:
    """Whether an admitted run of the schedule may still need its identity and access.

    Read inside the schedule's write transaction when deciding to remove it.
    Admission locks the same schedule row, so a final advance cannot miss a
    concurrently admitted run. Two kinds of receipt never keep a schedule,
    and each is logged with its ids:

    - one admitted longer ago than a run's identity lasts
      (``DEFAULT_CONTEXT_LIFETIME``, checked by ``access_control.task_identity``):
      its run can no longer act, so a receipt nothing closed (a lost worker,
      a merged fire whose run ended elsewhere) cannot keep the schedule and
      its access forever;
    - one that cannot be read, so one damaged record cannot stop the cleanup.
    """

    rows = conn.execute(
        f"SELECT occurrence_id, payload_json FROM schedule_occurrences WHERE schedule_id={dialect.bind}",
        (schedule_id,),
    ).fetchall()
    oldest = now - DEFAULT_CONTEXT_LIFETIME
    for row in rows:
        try:
            record = ScheduleOccurrenceV1.model_validate_json(row[1])
        except ValidationError:
            logger.warning(
                "A run receipt cannot be read, so it does not keep its schedule. "
                "event=schedule_receipt_unreadable schedule_id=%s occurrence_id=%s",
                schedule_id, row[0],
            )
            continue
        if record.finished_at_utc is not None or record.state not in UNFINISHED_RUN_STATES:
            continue
        admitted = record.admitted_at_utc or record.created_at_utc
        if admitted < oldest:
            logger.warning(
                "A run receipt was never closed and its run's identity has expired, so it no longer "
                "keeps its schedule. event=schedule_receipt_expired schedule_id=%s task_id=%s "
                "agent_id=%s state=%s admitted_at=%s",
                schedule_id, record.task_id, record.agent_id, record.state, admitted.isoformat(),
            )
            continue
        return True
    return False
