"""Which tasks are stuck, and what the person each works for is told.

Runs on every pass of the team lead's scheduler loop (``scheduled_runtime``),
next to the stuck-schedule check, and reports through the same engine
(``custom_agents.stuck_work``): an Inbox notice and one chat message once a
condition has lasted its threshold (``agent.schedule_maintenance.stuck_tasks``),
and a notice when the task moves again. No model is involved.

The team lead reads every agent's saved work in its team (``active_task_runtime``,
read-only) and reports one incident per request and condition. A delegated
subtask is reported as the request it belongs to (its root task), so one stuck
request produces one message however many agents work on it. Conditions:

- ``task_owner_gone``: the agent holding unfinished work is gone by the one
  rule of ``custom_agents.agent_liveness`` (its start record ended, its process
  is gone, or, for an agent known only by its heartbeat, it announced its exit,
  has no status or was silent longer than the threshold) and no agent took the
  work over within the threshold. Administrators are told too.
- ``task_handback_unclaimed``: a worker that lost its lease handed the task
  back, the team lead sent it out again, and no agent picked it up.
  Administrators are told too.
- ``task_waiting_for_access_decision``: an access request for the task is still
  waiting. A reminder (such waits may last two days); administrators, who can
  decide, are told too.
- ``task_waiting_for_answer``: the task asked the person a question (a
  recoverable pause) that is still unanswered.
- ``task_no_progress``: in progress on a live agent with no new history entry.

Who is told: the person in the root task's signed context (read with
``check_expiry=False``, which reads facts and authorizes nothing, because a
stuck task can outlive its context), or the owner of the schedule a run
belongs to. Without a verified person only administrators are told.
Administrators' notices never quote the request; tasks are private.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from access_control.identity import IdentityRejected
from custom_agents.instance_delivery_context import configured_owner_route
from custom_agents.agent_liveness import LivenessPurpose, owner_liveness, parse_utc, retain_decisions
from custom_agents.liveness_observation import heartbeat_checks_deferred
from custom_agents.stuck_work import (
    ChatRoute,
    Recovery,
    StuckFinding,
    StuckMessage,
    report_stuck_work,
)
from custom_agents.task_context_runtime import context_verifier
from custom_messages.contracts.access_v2 import AccessRequestStatus, SchedulePermitV1, TaskContextV1
from custom_messages.contracts.runtime_snapshot_v3 import (
    RuntimeSnapshotV3,
    TaskSnapshotRecord,
    decode_runtime_snapshot,
)
from custom_messages.enums import ProgressState
from runtime_settings.stuck_tasks import StuckTaskSettings
from storage.access_repository import AccessRepository
from storage.stuck_work_repository import StuckSubjectKind, StuckWorkIncidentV1
from storage.worker_handback_repository import WorkerHandbackRepository
from time_presentation import format_user_timestamp

logger = logging.getLogger(__name__)

TASK_SUBJECT: StuckSubjectKind = "task"
TASK_OWNER_GONE = "task_owner_gone"
TASK_HANDBACK_UNCLAIMED = "task_handback_unclaimed"
TASK_WAITING_FOR_ACCESS = "task_waiting_for_access_decision"
TASK_WAITING_FOR_ANSWER = "task_waiting_for_answer"
TASK_NO_PROGRESS = "task_no_progress"
# A recoverable pause still waiting for the person (RecoverablePauseRecord.status).
PAUSE_PENDING = "pending"

TERMINAL_STATES = frozenset(
    {ProgressState.COMPLETED, ProgressState.FAILED, ProgressState.CANCELLED, ProgressState.ABANDONED}
)
EXECUTING_STATES = frozenset({ProgressState.IN_PROGRESS, ProgressState.RETRYING})
_LABEL_LENGTH = 80


def stuck_task_settings(agent: Any) -> StuckTaskSettings:
    return StuckTaskSettings.from_schedule_maintenance(getattr(agent, "schedule_maintenance", None))


_utc = parse_utc


@dataclass(frozen=True)
class HeldTask:
    """One unfinished task and the agent whose saved work holds it."""

    owner_agent_id: str
    owner_named_agent_type: str | None
    record: TaskSnapshotRecord


@dataclass(frozen=True)
class OwnerWork:
    """One agent's saved work, decoded."""

    owner_agent_id: str
    owner_named_agent_type: str | None
    updated_at: datetime | None
    snapshot: RuntimeSnapshotV3


def last_activity(record: TaskSnapshotRecord) -> datetime | None:
    """The newest of the task's last history entry and last state change."""
    times = [
        _utc(entry.timestamp) for entry in (record.task.execution_history or [])[-1:]
    ] + [_utc(state[1]) for state in (record.task.task_state or [])[-1:]]
    known = [time for time in times if time is not None]
    return max(known) if known else None


class StuckTasks:
    """The task source for ``report_stuck_work``."""

    subject_kind: StuckSubjectKind = TASK_SUBJECT

    def __init__(self, agent: Any, settings: StuckTaskSettings) -> None:
        self.agent = agent
        self.settings = settings
        self.team_id = str(agent.team_id)

    # -- reading saved work ---------------------------------------------------

    def owners(self) -> list[OwnerWork]:
        rows = self.agent.task_manager.runtime_state_store.peek_task_runtime_by_team(self.team_id)
        owners: list[OwnerWork] = []
        for row in rows:
            try:
                snapshot = decode_runtime_snapshot(row.runtime_state_json)
            except ValueError:
                logger.warning(
                    "Skipped saved work that cannot be read in the stuck-task check. "
                    "event=stuck_task_snapshot_unreadable owner_agent_id=%s",
                    row.owner_agent_id,
                )
                continue
            owners.append(
                OwnerWork(row.owner_agent_id, row.owner_named_agent_type, _utc(row.updated_at), snapshot)
            )
        return owners

    @staticmethod
    def held_tasks(owners: Iterable[OwnerWork]) -> dict[str, HeldTask]:
        held: dict[str, HeldTask] = {}
        for owner in owners:
            for record in owner.snapshot.tasks:
                if record.state not in TERMINAL_STATES:
                    held[record.task_id] = HeldTask(owner.owner_agent_id, owner.owner_named_agent_type, record)
        return held

    @staticmethod
    def root(held: dict[str, HeldTask], task: HeldTask) -> HeldTask:
        """The request a task belongs to: follow parents while some agent holds them."""
        seen = {task.record.task_id}
        current = task
        while (parent := current.record.task.parent_task_id) and parent in held and parent not in seen:
            seen.add(parent)
            current = held[parent]
        return current

    def owner_gone_since(self, owner: OwnerWork, now: datetime) -> datetime | None:
        """When an agent other than the lead stopped, or None while it may still run.

        Whether it is gone is the one rule of ``agent_liveness.owner_liveness``,
        the same as takeover. The person is told only once it has been gone for
        ``owner_gone_after_seconds``, which gives a replacement time to take the
        work over first.
        """
        if owner.owner_agent_id == self.agent.id:
            return None
        decision = owner_liveness(
            self.agent, owner.owner_agent_id, purpose=LivenessPurpose.STUCK_TASK_NOTICE, now=now
        )
        if not decision.gone:
            return None
        since = decision.gone_since(owner.updated_at) or now
        gone_after = timedelta(seconds=self.settings.owner_gone_after_seconds)
        return since if now - since >= gone_after else None

    # -- who and where ----------------------------------------------------------

    def identity_of(self, meta: Any) -> TaskContextV1 | SchedulePermitV1 | None:
        """Read signed identity facts for notices, never to authorize work."""
        context = getattr(meta, "gateway_context", None)
        if context is None:
            return None
        verifier = context_verifier()
        try:
            if context.task_context is not None:
                return verifier.verify(
                    context.task_context, team_id=self.team_id, check_expiry=False
                )
            if context.schedule_run is not None:
                return verifier.verify_permit(context.schedule_run.permit, team_id=self.team_id)
        except (IdentityRejected, ValueError):
            return None
        return None

    def person_of(self, meta: Any) -> str | None:
        """The verified person a request works for, or None."""
        identity = self.identity_of(meta)
        return identity.person_id if identity is not None else None

    def chat_route(self, header: Any, meta: Any, person_id: str | None, agent_ids: set[str]) -> ChatRoute | None:
        """The person's chat: the request's reply route, else the configured user's chat."""
        context = getattr(meta, "gateway_context", None)
        route = getattr(header, "response_routing_key", None)
        if context is not None and route and route not in agent_ids:
            return ChatRoute(route, context)
        configured = (
            configured_owner_route(getattr(self.agent, "instance_config_path", None), person_id)
            if person_id
            else None
        )
        return ChatRoute(*configured) if configured else None

    def _when(self, meta: Any, value: Any) -> str:
        zone = getattr(meta, "request_timezone", None) or getattr(
            getattr(self.agent, "_schedule_store", None), "default_cron_timezone", None
        )
        return format_user_timestamp(value, timezone_name=zone)

    @staticmethod
    def label(task: Any) -> str:
        text = " ".join(str(task.problem_statement or task.task_id).split())
        return text if len(text) <= _LABEL_LENGTH else text[: _LABEL_LENGTH - 1].rstrip() + "…"

    # -- findings ---------------------------------------------------------------

    def _finding(
        self,
        root_task: Any,
        header: Any,
        meta: Any,
        *,
        reason: str,
        since: datetime,
        agent_ids: set[str],
        agent_type: str | None,
    ) -> StuckFinding:
        identity = self.identity_of(meta)
        person = identity.person_id if identity is not None else None
        subject, description = _work_description(identity)
        label, when = self.label(root_task), self._when(meta, since)
        text, admin_text, tell_admins = _texts(reason, when, agent_type, description)
        return StuckFinding(
            subject_kind=TASK_SUBJECT,
            subject_id=root_task.task_id,
            incident_key=reason,
            reason=reason,
            since=since,
            person_id=person,
            tell_administrators=tell_admins,
            message=StuckMessage(title=f'Your request "{label}" {_TITLES[reason]}', text=text),
            chat_route=self.chat_route(header, meta, person, agent_ids),
            administrator_message=StuckMessage(title=f"{subject} {_TITLES[reason]}", text=admin_text),
        )

    def findings(self, now: datetime) -> list[StuckFinding]:
        owners = self.owners()
        held = self.held_tasks(owners)
        agent_ids = {owner.owner_agent_id for owner in owners} | {str(self.agent.id)}
        found: dict[tuple[str, str], StuckFinding] = {}

        def add(task: HeldTask, reason: str, since: datetime) -> None:
            root = self.root(held, task)
            key = (root.record.task_id, reason)
            if key not in found:
                found[key] = self._finding(
                    root.record.task, root.record.header, root.record.meta,
                    reason=reason, since=since, agent_ids=agent_ids,
                    agent_type=task.owner_named_agent_type,
                )

        live_owners = self._owners_gone(owners, held, now, add)
        self._handbacks(now, held, agent_ids, found)
        self._waiting_for_access(now, held, add)
        for owner in owners:
            if owner.owner_agent_id in live_owners:
                self._live_owner_waits(owner, held, now, add)
        return list(found.values())

    def _owners_gone(self, owners: list[OwnerWork], held: dict[str, HeldTask], now: datetime, add: Any) -> set[str]:
        """Report the work of owners that are gone; return the live owners' ids."""
        live = set()
        # Forget decisions about owners whose saved work is gone (agent_liveness).
        retain_decisions(self.agent, LivenessPurpose.STUCK_TASK_NOTICE, [owner.owner_agent_id for owner in owners])
        for owner in owners:
            gone_since = self.owner_gone_since(owner, now)
            if gone_since is None:
                live.add(owner.owner_agent_id)
                continue
            for record in owner.snapshot.tasks:
                if record.state not in TERMINAL_STATES:
                    add(held[record.task_id], TASK_OWNER_GONE, gone_since)
        return live

    def _live_owner_waits(self, owner: OwnerWork, held: dict[str, HeldTask], now: datetime, add: Any) -> None:
        """Unanswered questions and work without progress on a live owner."""
        waiting = timedelta(seconds=self.settings.waiting_on_person_after_seconds)
        no_progress = timedelta(seconds=self.settings.no_progress_after_seconds)
        for pause in owner.snapshot.recoverable_pause_records.values():
            created = _utc(pause.created_at)
            if pause.status == PAUSE_PENDING and created and now - created >= waiting and pause.task_id in held:
                add(held[pause.task_id], TASK_WAITING_FOR_ANSWER, created)
        for record in owner.snapshot.tasks:
            last = last_activity(record)
            if record.state in EXECUTING_STATES and last and now - last >= no_progress:
                add(held[record.task_id], TASK_NO_PROGRESS, last)

    def _waiting_for_access(self, now: datetime, held: dict[str, HeldTask], add: Any) -> None:
        threshold = timedelta(seconds=self.settings.waiting_on_person_after_seconds)
        requests = AccessRepository(self.agent.task_manager.storage_settings).list_requests(
            self.team_id, status=AccessRequestStatus.WAITING
        )
        for request in requests:
            created = _utc(request.created_at_utc)
            if request.task_id in held and created and now - created >= threshold:
                add(held[request.task_id], TASK_WAITING_FOR_ACCESS, created)

    def _handbacks(
        self,
        now: datetime,
        held: dict[str, HeldTask],
        agent_ids: set[str],
        found: dict[tuple[str, str], StuckFinding],
    ) -> None:
        threshold = timedelta(seconds=self.settings.handback_unclaimed_after_seconds)
        repository = WorkerHandbackRepository(self.agent.task_manager.storage_settings)
        for handback, updated in repository.list_redispatched(self.team_id, updated_before=now - threshold):
            task = handback.handed_back
            if task.task_id in held:
                continue  # an agent picked it up
            key = (task.task_id, TASK_HANDBACK_UNCLAIMED)
            found.setdefault(
                key,
                self._finding(
                    task.task, task.header, task.meta,
                    reason=TASK_HANDBACK_UNCLAIMED, since=_utc(updated) or now,
                    agent_ids=agent_ids, agent_type=handback.worker_named_agent_type,
                ),
            )

    # -- recovery -----------------------------------------------------------------

    def recovery(self, incident: StuckWorkIncidentV1) -> Recovery:
        owners = self.owners()
        held = self.held_tasks(owners)
        task = held.get(incident.subject_id)
        if task is None:
            # No saved owner remains. Do not infer a successful result from
            # disappearing custody; the task's history owns its final outcome.
            return Recovery(
                StuckMessage(
                    title="A task reported as stuck is no longer held",
                    text="No agent's saved work holds this task now. Check the task history for its final outcome.",
                ),
                None,
            )
        record = task.record
        agent_ids = {owner.owner_agent_id for owner in owners} | {str(self.agent.id)}
        subject, _ = _work_description(self.identity_of(record.meta))
        text = "The condition reported earlier has cleared. Check the task history for its latest progress."
        return Recovery(
            StuckMessage(
                title=f'The warning for your request "{self.label(record.task)}" has cleared',
                text=text,
            ),
            self.chat_route(record.header, record.meta, incident.person_id, agent_ids),
            StuckMessage(title=f"{subject} warning cleared", text=text),
        )


_TITLES = {
    TASK_OWNER_GONE: "stopped",
    TASK_HANDBACK_UNCLAIMED: "is waiting for an agent",
    TASK_WAITING_FOR_ACCESS: "is waiting for an access decision",
    TASK_WAITING_FOR_ANSWER: "is waiting for your answer",
    TASK_NO_PROGRESS: "has made no progress",
}


def _work_description(identity: TaskContextV1 | SchedulePermitV1 | None) -> tuple[str, str]:
    """Administrator labels follow verified identity, including work for no person."""
    if identity is None:
        return "A task", "a task"
    if identity.person_id is not None:
        return "A task", "a person's task"
    if identity.schedule_id is not None:
        return "System maintenance", "system maintenance"
    return "System work", "system work"


def _texts(reason: str, when: str, agent_type: str | None, work: str) -> tuple[str, str, bool]:
    """(person text, administrator text, whether administrators are told)."""
    kind = agent_type or "agent"
    if reason == TASK_OWNER_GONE:
        return (
            f"The agent working on it stopped responding {when}. The team lead starts "
            "another agent to take the work over. If it does not continue, ask your "
            "assistant about it; an administrator can check the runtime log.",
            f"The {kind} working on {work} stopped responding {when} and no agent "
            "has taken the work over. Check the runtime log if the work does not resume.",
            True,
        )
    if reason == TASK_HANDBACK_UNCLAIMED:
        return (
            f"It was handed to another agent after its first one stopped, but no agent "
            f"has picked it up since {when}. An administrator has been told.",
            f"A task handed back by a stopped {kind} has not been picked up since {when}. "
            "Check that an agent of that type can start, and the runtime log.",
            True,
        )
    if reason == TASK_WAITING_FOR_ACCESS:
        return (
            f"It has been waiting since {when} for someone to decide an access request "
            "in the Inbox (Settings > Inbox). It continues as soon as it is decided.",
            f"A task has been waiting since {when} for an access decision in the Inbox.",
            True,
        )
    if reason == TASK_WAITING_FOR_ANSWER:
        return (
            f"It asked you a question {when} and is paused until you answer it in this "
            "chat.",
            f"A task has been waiting since {when} for its requester's answer.",
            False,
        )
    return (
        f"It has shown no progress since {when}; it may be stuck on a slow tool or "
        "service. You can stop it in Settings > Tasks, or ask your assistant about it.",
        f"A task has shown no progress since {when}.",
        False,
    )


async def report_stuck_tasks(agent: Any, *, now: datetime | None = None) -> None:
    """One pass: report stuck tasks and the ones that moved again."""
    if not getattr(agent, "is_team_lead", False) or not agent.team_id:
        return
    try:
        settings = stuck_task_settings(agent)
        if not settings.enabled:
            return
        # Keep open incidents untouched while the observer catches up. Treating
        # deferred checks as absent findings would falsely announce recovery.
        if heartbeat_checks_deferred(agent):
            return
        await report_stuck_work(
            agent,
            StuckTasks(agent, settings),
            notice_after=timedelta(0),  # each condition has its own threshold
            now=now or datetime.now(timezone.utc),
        )
    except Exception:
        agent._logger.exception(
            "Could not check for stuck tasks; retrying on the next pass. "
            "event=stuck_task_scan_failed team_id=%s",
            agent.team_id,
        )
