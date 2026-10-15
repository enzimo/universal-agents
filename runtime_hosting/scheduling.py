"""Timezone-aware schedule materialization into unique durable run attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from croniter import croniter  # type: ignore[import-untyped]

from runtime_hosting.execution_contracts import (
    AutomationJobV2,
    AutomationScheduleKind,
    MissedRunPolicy,
    OverlapPolicy,
    RunAttemptStatus,
    RunAttemptV1,
)
from storage.execution_control_repository import ExecutionControlRepository


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Schedule timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class DueOccurrences:
    scheduled_for_utc: tuple[datetime, ...]
    next_run_at_utc: datetime | None
    coalesced_occurrences: int = 1


class ScheduleCalculator:
    """Pure schedule calculation with explicit catch-up and DST semantics."""

    def calculate(self, job: AutomationJobV2, *, now: datetime) -> DueOccurrences:
        current = _utc(now)
        cursor = job.next_run_at_utc
        if cursor is None or _utc(cursor) > current:
            return DueOccurrences((), cursor)
        if job.schedule.schedule_kind == AutomationScheduleKind.ONE_SHOT:
            return DueOccurrences((_utc(cursor),), None)
        if job.schedule.schedule_kind == AutomationScheduleKind.INTERVAL:
            return self._interval_occurrences(job, now=current, cursor=_utc(cursor))
        return self._cron_occurrences(job, now=current, cursor=_utc(cursor))

    def _interval_occurrences(
        self,
        job: AutomationJobV2,
        *,
        now: datetime,
        cursor: datetime,
    ) -> DueOccurrences:
        seconds = job.schedule.interval_seconds
        if seconds is None:
            raise ValueError("Interval schedule is missing interval_seconds.")
        interval = timedelta(seconds=seconds)
        due_count = int((now - cursor) // interval) + 1
        next_run = cursor + due_count * interval
        policy = job.schedule.missed_run_policy
        if policy == MissedRunPolicy.SKIP:
            latest = cursor + (due_count - 1) * interval
            return DueOccurrences((latest,), next_run, due_count)
        if policy == MissedRunPolicy.COALESCE:
            latest = cursor + (due_count - 1) * interval
            return DueOccurrences((latest,), next_run, due_count)
        retained = min(due_count, job.schedule.maximum_backlog)
        occurrences = tuple(cursor + index * interval for index in range(retained))
        return DueOccurrences(occurrences, next_run)

    def _cron_occurrences(
        self,
        job: AutomationJobV2,
        *,
        now: datetime,
        cursor: datetime,
    ) -> DueOccurrences:
        expression = job.schedule.cron_expression
        if expression is None:
            raise ValueError("Cron schedule is missing cron_expression.")
        zone = ZoneInfo(job.schedule.timezone)
        local_now = now.astimezone(zone)
        next_run = _utc(croniter(expression, local_now).get_next(datetime))
        policy = job.schedule.missed_run_policy
        if policy in {MissedRunPolicy.COALESCE, MissedRunPolicy.SKIP}:
            latest = _utc(
                croniter(expression, local_now + timedelta(seconds=1)).get_prev(datetime)
            )
            return DueOccurrences((max(cursor, latest),), next_run)

        occurrences: list[datetime] = [cursor]
        iterator = croniter(expression, cursor.astimezone(zone))
        while len(occurrences) < job.schedule.maximum_backlog:
            candidate = _utc(iterator.get_next(datetime))
            if candidate > now:
                break
            occurrences.append(candidate)
        return DueOccurrences(tuple(occurrences), next_run)


@dataclass
class ScheduleMaterializer:
    repository: ExecutionControlRepository
    logger: logging.Logger = logging.getLogger(__name__)
    calculator: ScheduleCalculator = ScheduleCalculator()

    def materialize_due_jobs(
        self,
        *,
        team_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> int:
        current = _utc(now or self.repository.current_time_utc())
        jobs = self.repository.list_due_jobs(team_id=team_id, now=current, limit=limit)
        created_count = 0
        for job in jobs:
            created_count += self._materialize_job(job, now=current)
        return created_count

    def _materialize_job(self, job: AutomationJobV2, *, now: datetime) -> int:
        expected_cursor = job.next_run_at_utc
        if expected_cursor is None:
            return 0
        activation = job.activation
        if activation is None or not activation.is_active(now=now):
            attempt = self._attempt(
                job,
                scheduled_for=_utc(expected_cursor),
                status=RunAttemptStatus.AUTHORIZATION_EXPIRED,
                finished_at=now,
                error_code="activation_expired",
                error_message="Exact execution activation is missing, expired, or revoked.",
            )
            _, created = self.repository.create_run_attempt(attempt)
            self.repository.advance_job_cursor(
                job=job,
                expected_next_run_at_utc=_utc(expected_cursor),
                next_run_at_utc=None,
                enabled=False,
                updated_at_utc=now,
            )
            return int(created)

        due = self.calculator.calculate(job, now=now)
        active_overlap = (
            job.schedule.overlap_policy == OverlapPolicy.SKIP_IF_RUNNING
            and self.repository.has_active_run(team_id=job.owner.team_id, job_id=job.job_id)
        )
        created_count = 0
        for scheduled_for in due.scheduled_for_utc:
            status = RunAttemptStatus.PENDING
            finished_at: datetime | None = None
            error_code: str | None = None
            error_message: str | None = None
            deadline = job.schedule.start_deadline_seconds
            if deadline is not None and scheduled_for + timedelta(seconds=deadline) < now:
                status = RunAttemptStatus.SKIPPED_MISSED
                finished_at = now
                error_code = "start_deadline_missed"
                error_message = "Occurrence exceeded its configured start deadline."
            elif active_overlap:
                status = RunAttemptStatus.SKIPPED_OVERLAP
                finished_at = now
                error_code = "overlap_policy"
                error_message = "A prior run remained active at materialization time."
            attempt = self._attempt(
                job,
                scheduled_for=scheduled_for,
                status=status,
                finished_at=finished_at,
                error_code=error_code,
                error_message=error_message,
                coalesced_occurrences=due.coalesced_occurrences,
            )
            _, created = self.repository.create_run_attempt(attempt)
            created_count += int(created)

        enabled = due.next_run_at_utc is not None
        advanced = self.repository.advance_job_cursor(
            job=job,
            expected_next_run_at_utc=_utc(expected_cursor),
            next_run_at_utc=due.next_run_at_utc,
            enabled=enabled,
            updated_at_utc=now,
        )
        if not advanced:
            self.logger.info(
                "Execution schedule cursor was already advanced",
                extra={"team_id": job.owner.team_id, "job_id": job.job_id},
            )
        return created_count

    @staticmethod
    def _attempt(
        job: AutomationJobV2,
        *,
        scheduled_for: datetime,
        status: RunAttemptStatus,
        finished_at: datetime | None,
        error_code: str | None,
        error_message: str | None,
        coalesced_occurrences: int = 1,
    ) -> RunAttemptV1:
        occurrence_id = f"{job.job_id}:{scheduled_for.isoformat()}"
        return RunAttemptV1(
            occurrence_id=occurrence_id,
            job_id=job.job_id,
            owner=job.owner,
            revision_id=job.revision_id,
            revision_digest=job.revision_digest,
            job_generation=job.generation,
            scheduled_for_utc=scheduled_for,
            status=status,
            finished_at_utc=finished_at,
            error_code=error_code,
            error_message=error_message,
            coalesced_occurrences=coalesced_occurrences,
        )


__all__ = ["DueOccurrences", "ScheduleCalculator", "ScheduleMaterializer"]
