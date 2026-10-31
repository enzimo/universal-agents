"""Tell people once when a scheduled job's runs fail, and once when a run works again.

Why: a scheduled run that failed on time told nobody. A run's result reaches
its person only through what the run itself delivers, which a failed run
never does, and a failed one-time reminder's schedule is removed right after
its run (2026-10-30: a one-time check failed 0.26 s after it started and its
owner heard nothing).

How it works:

- When a top-level scheduled run ends, the task's terminal snapshot
  transaction keeps one row per schedule (``storage.scheduled_run_problems``):
  the first failed run of the current problem, and the run that finished
  after it. Rows hold ids, routing and the schedule's name, never the
  task's instructions or output.
- On every pass of the team lead's scheduler loop, ``FailingScheduledRuns``
  reports the rows through the stuck-work notices (``custom_agents.stuck_work``),
  one problem per schedule. The person the run worked for, read from its
  original signature (``access_control.task_identity.reported_schedule_run``),
  gets one Inbox notice and one chat message at once; administrators get an
  Inbox notice instead for team or system work. Further failures say
  nothing more. A later run that finishes closes the problem with one
  "working again" notice and message. A schedule that ended, was removed or
  was turned off (a failed one-time reminder among them) closes it with no
  message after its one notice.

What it says (owner's decisions, 2026-10-30): the notice never names a
cause. A failed task's saved state holds only its error text, which notices
leave out, and a typed cause would change a shared message format. Instead it
tells the person what to ask their assistant. The question names the run's
task id, the one internal id in the notice: the assistant reads the run's
history by it (``get_task_history_window``), and cannot list another agent's
finished tasks to find it. It is sent whatever the schedule's ``notify_on_failure`` setting
says: that setting is about the results a run reports
(``custom_agents.schedule_observations``), and a broken run reports none. It
replaces the wait limit's own chat message for scheduled runs, so a run
stopped there is reported once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from access_control.identity import IdentityRejected
from access_control.task_identity import ReportedScheduleRun, reported_schedule_run
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import is_finishing
from custom_agents.stuck_tasks import request_chat_route
from custom_agents.stuck_work import ChatRoute, Recovery, StuckFinding, StuckMessage, report_stuck_work
from custom_agents.task_context_runtime import context_verifier
from storage.scheduled_run_problems import ScheduledRunProblems, ScheduledRunProblemV1, ScheduledRunRecordV1
from storage.stuck_work_repository import (
    StuckSubjectKind,
    StuckWorkIncidentV1,
    StuckWorkRepository,
    stuck_incident_id,
)
from time_presentation import format_user_timestamp

SCHEDULED_RUN_SUBJECT: StuckSubjectKind = "scheduled_run"
SCHEDULED_RUN_FAILED = "scheduled_run_failed"


@dataclass(frozen=True)
class _Schedule:
    """What the notice says about the schedule now: its name, whether it runs again, its time zone."""

    label: str | None
    runs_again: bool
    timezone_name: str | None


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class FailingScheduledRuns:
    """The failing-schedule source for ``report_stuck_work`` (module docstring).

    ``findings`` also deletes rows whose people were told everything: a
    recovery once its incident closed, a problem whose schedule ended once
    it was told (its incident then closes with no message).
    """

    subject_kind: StuckSubjectKind = SCHEDULED_RUN_SUBJECT

    def __init__(self, agent: Any, problems: ScheduledRunProblems, incidents: StuckWorkRepository) -> None:
        self.agent = agent
        self.team_id = str(agent.team_id)
        self.problems = problems
        self.incidents = incidents
        self.store = agent._schedule_store

    # -- who and what -------------------------------------------------------------

    def verified(self, problem: ScheduledRunProblemV1, record: ScheduledRunRecordV1) -> ReportedScheduleRun | None:
        """The run's schedule and person from its signature, when it names the problem's schedule."""
        gateway = record.meta.gateway_context
        try:
            run = reported_schedule_run(
                verifier=context_verifier(),
                team_id=self.team_id,
                task_context=gateway.task_context if gateway is not None else None,
                schedule_run=gateway.schedule_run if gateway is not None else None,
            )
        except (IdentityRejected, ValueError):
            run = None
        if run is None or run.schedule_id != problem.schedule_id:
            self.agent._logger.warning(
                "A scheduled run's saved outcome does not carry a verified identity for its schedule; "
                "it is not reported. event=scheduled_run_failure_identity_refused schedule_id=%s task_id=%s",
                problem.schedule_id,
                record.task_id,
            )
            return None
        return run

    def schedule(self, schedule_id: str, now: datetime) -> _Schedule:
        """The schedule now, or the heartbeat item a launcher's runs name; gone ones run no more."""
        row = self.store.get_row(schedule_id)
        if row is not None:
            expires = _utc(row.get("expires_at_utc"))
            message = self.store.get_scheduled_message(schedule_id)
            return _Schedule(
                label=row.get("schedule_name"),
                runs_again=bool(row.get("enabled")) and not is_finishing(row) and not (expires and expires <= now),
                timezone_name=(message.cron_timezone if message is not None else None)
                or getattr(self.store, "default_cron_timezone", None),
            )
        item = HeartbeatStore(storage_settings=self.agent.task_manager.storage_settings).get_item(schedule_id)
        if item is not None:
            return _Schedule(label=item.title, runs_again=item.enabled, timezone_name=None)
        return _Schedule(label=None, runs_again=False, timezone_name=None)

    def when(self, record: ScheduledRunRecordV1, run: ReportedScheduleRun, schedule: _Schedule) -> str:
        zone = (
            record.meta.request_timezone
            or schedule.timezone_name
            or getattr(self.store, "default_cron_timezone", None)
        )
        return format_user_timestamp(run.started_at_utc, timezone_name=zone)

    def chat_route(self, record: ScheduledRunRecordV1, person_id: str | None) -> ChatRoute | None:
        """The person's chat; none for team or system work, which administrators read in the Inbox."""
        if person_id is None:
            return None
        return request_chat_route(self.agent, record.header, record.meta, person_id, {self.agent.id, record.agent_id})

    # -- findings -------------------------------------------------------------------

    def findings(self, now: datetime) -> list[StuckFinding]:
        open_incidents = {
            incident.incident_id: incident
            for incident in self.incidents.open_incidents(self.team_id, SCHEDULED_RUN_SUBJECT)
        }
        found: list[StuckFinding] = []
        for problem in self.problems.problems(self.team_id):
            try:
                finding = self._finding(problem, open_incidents, now)
            except Exception:
                self.agent._logger.exception(
                    "Could not check a failing scheduled job; retrying on the next pass. "
                    "event=scheduled_run_failure_check_failed schedule_id=%s task_id=%s",
                    problem.schedule_id,
                    problem.failed.task_id,
                )
                continue
            if finding is not None:
                found.append(finding)
        return found

    def _finding(
        self, problem: ScheduledRunProblemV1, open_incidents: dict[str, StuckWorkIncidentV1], now: datetime,
    ) -> StuckFinding | None:
        run = self.verified(problem, problem.failed)
        if run is None:
            self.problems.forget(problem)
            return None
        incident = open_incidents.get(
            stuck_incident_id(self.team_id, SCHEDULED_RUN_SUBJECT, problem.schedule_id, problem.failed.task_id)
        )
        if problem.recovered is not None:
            if self.verified(problem, problem.recovered) is not None:
                if incident is None:
                    # Never told, or its recovery was told and the incident closed.
                    self.problems.forget(problem)
                return None  # an open incident closes with its "working again" message
            self.problems.clear_recovery(problem)
        schedule = self.schedule(problem.schedule_id, now)
        told = incident is not None and incident.notified_at_utc is not None
        if told and not schedule.runs_again:
            # Its people were told and nothing will run again: the incident
            # closes with no message (``recovery`` finds no row).
            self.problems.forget(problem)
            return None
        return StuckFinding(
            subject_kind=SCHEDULED_RUN_SUBJECT,
            subject_id=problem.schedule_id,
            # The first failed run names the problem; a later one is a new problem.
            incident_key=problem.failed.task_id,
            reason=SCHEDULED_RUN_FAILED,
            since=problem.failed.ended_at_utc,
            person_id=run.person_id,
            tell_administrators=False,  # administrators hear of team and system work (person_id None)
            message=_failed_message(
                _label(schedule, problem.failed), self.when(problem.failed, run, schedule),
                problem.failed.task_id, schedule.runs_again,
            ),
            chat_route=self.chat_route(problem.failed, run.person_id),
        )

    def recovery(self, incident: StuckWorkIncidentV1) -> Recovery | None:
        problem = self.problems.problem(self.team_id, incident.subject_id)
        if problem is None or problem.failed.task_id != incident.incident_key or problem.recovered is None:
            return None
        run = self.verified(problem, problem.recovered)
        if run is None:
            return None
        schedule = self.schedule(problem.schedule_id, datetime.now(timezone.utc))
        label = _label(schedule, problem.recovered)
        return Recovery(
            StuckMessage(
                title=f'Scheduled job "{label}" is working again' if label else "A scheduled job is working again",
                text=(
                    f"The run that started {self.when(problem.recovered, run, schedule)} finished after the "
                    "failure reported earlier."
                ),
            ),
            self.chat_route(problem.recovered, run.person_id),
        )


def _label(schedule: _Schedule, record: ScheduledRunRecordV1) -> str | None:
    """The schedule's name now, else the one saved when the run ended; never an internal id."""
    return schedule.label or record.schedule_name or None


def _failed_message(label: str | None, when: str, task_id: str, runs_again: bool) -> StuckMessage:
    """What people read: no cause, and the question to ask their assistant (module docstring)."""
    job = f'the scheduled job "{label}"' if label else "the scheduled job"
    question = (
        f"Why did {job} fail on {when} (run {task_id}), and what had it already done?"
        if label
        else f"Why did {job} that started {when} (run {task_id}) fail, and what had it already done?"
    )
    after = (
        "If later runs fail too, you will not be told again; you will get a message once a run finishes."
        if runs_again
        else "It is not scheduled to run again."
    )
    return StuckMessage(
        title=f'Scheduled job "{label}" stopped before finishing' if label else "A scheduled job stopped before finishing",
        text=(
            f"The run that started {when} stopped before it finished, so its result is not confirmed. "
            f"To find out why, ask your assistant: “{question}” {after}"
        ),
    )


async def report_failing_scheduled_runs(agent: Any, *, now: datetime | None = None) -> None:
    """One pass: report schedules whose runs fail, and the ones that work again (team lead only)."""
    if not getattr(agent, "is_team_lead", False) or not agent.team_id:
        return
    try:
        settings = agent.task_manager.storage_settings
        await report_stuck_work(
            agent,
            FailingScheduledRuns(agent, ScheduledRunProblems(settings), StuckWorkRepository(settings)),
            notice_after=timedelta(0),  # a failed run is told at once
            now=now or datetime.now(timezone.utc),
        )
    except Exception:
        agent._logger.exception(
            "Could not check for failing scheduled jobs; retrying on the next pass. "
            "event=scheduled_run_failure_scan_failed team_id=%s",
            agent.team_id,
        )
