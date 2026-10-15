from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_messages.contracts.access_v2 import ChangeApprovalMode
from runtime_hosting.execution_contracts import (
    AutomationJobV2,
    AutomationScheduleKind,
    AutomationScheduleV1,
    ExecutionActivationKind,
    OwnerScopeKind,
    OwnerScopeV1,
    RunAttemptStatus,
)
from runtime_hosting.scheduling import ScheduleCalculator, ScheduleMaterializer
from storage.execution_control_repository import SqliteExecutionControlRepository
from tests.execution_access_support import activation as build_activation


_REVISION_DIGEST = "d" * 64


def _job(
    *,
    job_id: str = "exec_job_schedule",
    schedule: AutomationScheduleV1,
    next_run_at: datetime,
    activation_expires_at: datetime | None = None,
) -> AutomationJobV2:
    owner = OwnerScopeV1(
        scope_kind=OwnerScopeKind.TEAM,
        owner_id="team_alpha",
        team_id="team_alpha",
    )
    staged = AutomationJobV2(
        job_id=job_id,
        name="Schedule test",
        owner=owner,
        revision_id="exec_rev_schedule",
        revision_digest=_REVISION_DIGEST,
        schedule=schedule,
        next_run_at_utc=next_run_at,
    )
    activation = build_activation(
        ExecutionActivationKind.RUN,
        resource_id=job_id,
        resource_digest=staged.activation_resource_digest(),
        owner_scope=owner,
        mode=ChangeApprovalMode.HELD_ACCESS,
        activated_at=next_run_at - timedelta(days=1),
        expires_at=activation_expires_at,
    )
    return AutomationJobV2.model_validate(
        {**staged.model_dump(mode="python"), "activation": activation, "enabled": True}
    )


def test_cron_uses_iana_timezone_across_spring_dst_gap() -> None:
    cursor = datetime(2026, 3, 7, 7, 30, tzinfo=timezone.utc)
    job = _job(
        schedule=AutomationScheduleV1(
            schedule_kind=AutomationScheduleKind.CRON,
            cron_expression="30 2 * * *",
            timezone="America/New_York",
        ),
        next_run_at=cursor,
    )

    due = ScheduleCalculator().calculate(
        job,
        now=datetime(2026, 3, 7, 8, 0, tzinfo=timezone.utc),
    )

    assert due.scheduled_for_utc == (cursor,)
    assert due.next_run_at_utc == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)


def test_materializer_records_missed_deadline_as_terminal(tmp_path: Path) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    cursor = datetime.now(timezone.utc) - timedelta(minutes=5)
    job = _job(
        schedule=AutomationScheduleV1(
            schedule_kind=AutomationScheduleKind.ONE_SHOT,
            one_shot_at_utc=cursor,
            start_deadline_seconds=30,
        ),
        next_run_at=cursor,
    )
    repository.create_job(job)

    assert ScheduleMaterializer(repository).materialize_due_jobs(
        team_id="team_alpha",
        now=datetime.now(timezone.utc),
    ) == 1

    attempt = repository.list_run_attempts(team_id="team_alpha")[0]
    assert attempt.status == RunAttemptStatus.SKIPPED_MISSED
    assert attempt.finished_at_utc is not None


def test_materializer_disables_job_when_exact_activation_expires(tmp_path: Path) -> None:
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    now = datetime.now(timezone.utc)
    cursor = now - timedelta(minutes=1)
    job = _job(
        schedule=AutomationScheduleV1(
            schedule_kind=AutomationScheduleKind.INTERVAL,
            interval_seconds=60,
        ),
        next_run_at=cursor,
        activation_expires_at=now - timedelta(seconds=1),
    )
    repository.create_job(job)

    assert ScheduleMaterializer(repository).materialize_due_jobs(
        team_id="team_alpha", now=now
    ) == 1

    attempt = repository.list_run_attempts(team_id="team_alpha")[0]
    assert attempt.status == RunAttemptStatus.AUTHORIZATION_EXPIRED
    updated = repository.get_job(team_id="team_alpha", job_id=job.job_id)
    assert updated is not None
    assert not updated.enabled
    assert updated.next_run_at_utc is None
