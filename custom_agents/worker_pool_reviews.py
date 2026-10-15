"""Worker pool problems: reviews for TeamArchitect, notices for administrators.

The worker pool controller (``custom_agents.worker_pool_controller``) reports
three kinds of problem per worker type (``*`` for the whole team):

- ``capacity``: a pool, or the team, reached ``capacity_warning_ratio`` of its
  limit while tasks waited ``scale_up_wait_seconds`` or longer;
- ``provider_limited``: ``provider_limit.review_events`` tasks of the type hit
  model provider limits within ``provider_limit.review_window_minutes``, or a
  worker of the type is waiting one out while tasks wait;
- ``start_failed``: a worker the controller needed could not be started.

Each becomes one open incident (``storage.worker_pool_repository``) while it
lasts. TeamArchitect gets a review task when the incident opens and again
every ``review_repeat_after_minutes`` while it stays (the owner's decision of
2026-10-04: the controller notifies TeamArchitect, which chooses A: a larger
pool for a while, B: a larger pool for good, C: let tasks wait, or D: fewer
workers). An incident closes after its condition has been absent for
``review_repeat_after_minutes``.

When the same kind of problem for the same type opened
``recurring_incident_count`` times within ``recurring_incident_window_hours``,
the review tells TeamArchitect to report it to administrators, and trusted
code also posts an Inbox notice to administrators through the stuck-work engine
(``custom_agents.stuck_work``), with a "working again" notice when it ends, so
a problem that keeps coming back never depends on the model alone.

Review tasks are opened by the runtime in the team lead's own queue (like
interruption reviews) and work for the team's maintenance owner, an
administrator (``task_context_runtime.sign_for_maintenance_owner``), so
TeamArchitect's notices reach a person who can act.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Optional

from custom_agents.stuck_work import Recovery, StuckFinding, StuckMessage, report_stuck_work
from custom_agents.worker_pool_limits import worker_pool_repository
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    ManagerEscalationContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage.stuck_work_repository import StuckSubjectKind, StuckWorkIncidentV1
from storage.worker_pool_repository import (
    INCIDENT_CAPACITY,
    INCIDENT_PROVIDER_LIMITED,
    INCIDENT_START_FAILED,
    TEAM_SCOPE,
    IncidentKind,
    WorkerPoolIncidentV1,
)

logger = logging.getLogger(__name__)

WORKER_POOL_REVIEW_TASK_PREFIX = "worker_pool_review_"
WORKER_POOL_SUBJECT: StuckSubjectKind = "worker_pool"

_TITLES = {
    INCIDENT_CAPACITY: "has reached its worker limit while tasks wait",
    INCIDENT_PROVIDER_LIMITED: "keeps hitting the model provider's limits",
    INCIDENT_START_FAILED: "cannot start workers",
}


def _scope_label(agent_type: str) -> str:
    return "The team" if agent_type == TEAM_SCOPE else f"The {agent_type} worker pool"


def _provider_limit_evidence(
    events: list[Any], cooling: Any, *, window_minutes: float
) -> dict[str, Any]:
    """What TA's provider-limit review shows: events in the window and the waiting work."""

    return {
        "provider_limit_events_in_window": len(events),
        "window_minutes": window_minutes,
        "providers": sorted({e.provider for e in events if e.provider}),
        "models": sorted({e.model_id for e in events if e.model_id}),
        "status_codes": sorted({e.status_code for e in events if e.status_code}),
        "workers_waiting_out_the_limit": getattr(cooling, "cooling_down", 0),
        "tasks_waiting": getattr(cooling, "waiting", None),
    }


class WorkerPoolIncidents:
    """One controller pass's incident bookkeeping."""

    def __init__(self, agent: Any, settings: WorkerPoolsConfig) -> None:
        self.agent = agent
        self.settings = settings
        self.team_id = str(agent.team_id)
        self.repository = worker_pool_repository(agent)
        self.runtime = agent.task_manager.operations_runtime
        self.now = datetime.now(timezone.utc)
        # (agent_type, kind) -> evidence, for conditions seen this pass.
        self._seen: dict[tuple[str, IncidentKind], dict[str, Any]] = {}
        self._started_types: set[str] = set()

    # -- what the controller saw -------------------------------------------------

    def _see(self, agent_type: str, kind: IncidentKind, evidence: dict[str, Any]) -> None:
        self._seen[(agent_type, kind)] = evidence

    async def observe_type(self, observation: Any, decision: Any) -> None:
        if decision.at_capacity:
            self._see(
                observation.agent_type,
                INCIDENT_CAPACITY,
                {"pool": observation.as_dict(), "controller": decision.reason},
            )

    def start_succeeded(self, agent_type: str) -> None:
        self._started_types.add(agent_type)

    def start_failed(self, observation: Any, reason: str) -> None:
        self._see(
            observation.agent_type,
            INCIDENT_START_FAILED,
            {"pool": observation.as_dict(), "start_refused_because": reason},
        )

    async def observe_team(self, observations: list[Any], *, team_active: int, team: Any) -> None:
        threshold = math.ceil(self.settings.capacity_warning_ratio * team.effective) if team.effective else 0
        waiting_long = [
            observation.agent_type
            for observation in observations
            if observation.waiting
            and (observation.oldest_wait_seconds or 0.0) >= self.settings.scale_up_wait_seconds
        ]
        if waiting_long and team_active >= threshold:
            self._see(
                TEAM_SCOPE,
                INCIDENT_CAPACITY,
                {
                    "team_workers_active": team_active,
                    "team_limit": team.as_dict(),
                    "types_with_tasks_waiting": waiting_long,
                },
            )

    async def observe_provider_limits(self, observations: list[Any]) -> None:
        window = timedelta(minutes=self.settings.provider_limit.review_window_minutes)
        events = await self.runtime.run(
            partial(self.repository.provider_limit_events, self.team_id, since=self.now - window)
        )
        by_type: dict[str, list[Any]] = {}
        for event in events:
            by_type.setdefault(event.agent_type, []).append(event)
        cooling = {
            observation.agent_type: observation
            for observation in observations
            if observation.cooling_down and observation.waiting
        }
        for agent_type in sorted(set(by_type) | set(cooling)):
            found = by_type.get(agent_type, [])
            if len(found) < self.settings.provider_limit.review_events and agent_type not in cooling:
                continue
            self._see(
                agent_type,
                INCIDENT_PROVIDER_LIMITED,
                _provider_limit_evidence(
                    found,
                    cooling.get(agent_type),
                    window_minutes=self.settings.provider_limit.review_window_minutes,
                ),
            )

    # -- incidents, reviews and notices -------------------------------------------

    async def settle(self) -> None:
        for (agent_type, kind), evidence in self._seen.items():
            try:
                incident, created = await self.runtime.run(
                    partial(
                        self.repository.record_incident,
                        team_id=self.team_id,
                        agent_type=agent_type,
                        kind=kind,
                        details=evidence,
                        now=self.now,
                    )
                )
                if created:
                    logger.warning(
                        "A worker pool problem started. event=worker_pool_incident_opened "
                        "team_id=%s agent_type=%s kind=%s incident_id=%s",
                        self.team_id,
                        agent_type,
                        kind,
                        incident.incident_id,
                    )
                await self._maybe_review(incident, created=created)
            except Exception:
                logger.exception(
                    "Could not record a worker pool problem; retrying next pass. "
                    "event=worker_pool_incident_failed team_id=%s agent_type=%s kind=%s",
                    self.team_id,
                    agent_type,
                    kind,
                )
        await self._close_cleared()
        await report_stuck_work(
            self.agent,
            RecurringPoolProblems(self.agent, self.settings),
            notice_after=timedelta(0),
            now=self.now,
        )

    async def _close_cleared(self) -> None:
        quiet = timedelta(minutes=self.settings.review_repeat_after_minutes)
        for incident in await self.runtime.run(self.repository.list_open_incidents, self.team_id):
            key = (incident.agent_type, incident.kind)
            if key in self._seen:
                continue
            cleared_by_start = (
                incident.kind == INCIDENT_START_FAILED and incident.agent_type in self._started_types
            )
            if not cleared_by_start and self.now - incident.last_seen_at_utc < quiet:
                continue
            await self.runtime.run(partial(self.repository.close_incident, incident, now=self.now))
            logger.info(
                "A worker pool problem ended. event=worker_pool_incident_closed team_id=%s "
                "agent_type=%s kind=%s incident_id=%s reviews=%s",
                self.team_id,
                incident.agent_type,
                incident.kind,
                incident.incident_id,
                incident.review_count,
            )

    def _recurrences(self, incident: WorkerPoolIncidentV1, recent: tuple[WorkerPoolIncidentV1, ...]) -> int:
        return sum(
            1
            for item in recent
            if item.kind == incident.kind and item.agent_type == incident.agent_type
        )

    async def _maybe_review(self, incident: WorkerPoolIncidentV1, *, created: bool) -> None:
        repeat = timedelta(minutes=self.settings.review_repeat_after_minutes)
        due = created or incident.reviewed_at_utc is None or self.now - incident.reviewed_at_utc >= repeat
        if not due:
            return
        window = timedelta(hours=self.settings.recurring_incident_window_hours)
        recent = await self.runtime.run(
            partial(
                self.repository.recent_incidents,
                self.team_id,
                since=self.now - window,
                agent_type=incident.agent_type,
            )
        )
        recurrences = self._recurrences(incident, recent)
        recurring = recurrences >= self.settings.recurring_incident_count
        review_id = await open_review(self.agent, incident, recurrences=recurrences, recurring=recurring, settings=self.settings)
        if review_id:
            await self.runtime.run(
                partial(self.repository.mark_reviewed, incident, review_task_id=review_id, now=self.now)
            )


def _instructions(incident: WorkerPoolIncidentV1, *, recurring: bool, recurrences: int, settings: WorkerPoolsConfig) -> str:
    scope = _scope_label(incident.agent_type)
    lines = [
        f"Worker pool review: {scope} {_TITLES[incident.kind]}.",
        "",
        "Delegated tasks wait in their worker type's shared queue until a free worker takes one; "
        "the worker pool controller starts workers while tasks wait, up to each type's pool size "
        "and the team limit. Load the runtime-operations-workflows skill's worker pool reference, "
        "check the current state with inspect_worker_pools, then decide:",
        "  A. a larger pool for a while: set_worker_pool_limit with an end time (until);",
        "  B. a larger pool for good: set_worker_pool_limit without an end time;",
        "  C. change nothing and let tasks wait their turn;",
        "  D. fewer workers: set_worker_pool_limit with a lower size (for a while or for good).",
        "Sizes stay within the operator ceilings shown; above a ceiling only an administrator can "
        "help (in the configuration), so tell them.",
    ]
    if incident.kind == INCIDENT_PROVIDER_LIMITED:
        lines += [
            "",
            "The model provider is refusing calls (rate limit, quota or overload). More workers "
            "make more calls, so A and B make this worse; choose C, or D when the provider's "
            "limit is lower than the pool. Workers already wait and retry on their own.",
        ]
    elif incident.kind == INCIDENT_START_FAILED:
        lines += [
            "",
            "A worker could not be started; the evidence says why. A pool size does not fix "
            "this. Check the agent type's definition and the operator's denied types and start "
            "limits; tell administrators what they need to change.",
        ]
    else:
        lines += [
            "",
            "Choose A for a burst of work, B if this keeps happening, C if waiting is fine, "
            "D if the model provider or cost calls for fewer workers.",
        ]
    if recurring:
        lines += [
            "",
            f"This problem has come back {recurrences} times within "
            f"{settings.recurring_incident_window_hours:g} hours. Report it to administrators "
            "with notify_user: what happens, the likely cause and your recommendation. "
            "Administrators also got an Inbox notice.",
        ]
    lines += [
        "",
        "Finish with a short task response saying what you decided and why. Do not delegate "
        "the waiting tasks again; they are still queued.",
    ]
    return "\n".join(lines)


async def open_review(
    agent: Any,
    incident: WorkerPoolIncidentV1,
    *,
    recurrences: int,
    recurring: bool,
    settings: WorkerPoolsConfig,
) -> Optional[str]:
    """Admit one review task into the team lead's queue; return its id."""

    from custom_agents.internal_improvement_runtime import internal_improvement_schedule_id
    from custom_agents.task_context_runtime import sign_for_maintenance_owner

    review_id = WORKER_POOL_REVIEW_TASK_PREFIX + hashlib.sha256(
        f"{incident.incident_id}\x1f{incident.review_count}".encode("utf-8")
    ).hexdigest()[:32]
    evidence = {
        "kind": incident.kind,
        "scope": incident.agent_type,
        "incident_id": incident.incident_id,
        "open_since": incident.opened_at_utc.isoformat(),
        "reviews_so_far": incident.review_count,
        "times_this_problem_opened_recently": recurrences,
        "details": incident.details,
    }
    task = Task(
        task_id=review_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=_instructions(incident, recurring=recurring, recurrences=recurrences, settings=settings)
        + "\n\nEvidence:\n"
        + json.dumps(evidence, indent=2, default=str),
    )
    meta = MessageMeta(
        from_agent_name=getattr(agent, "name", None),
        requester_type=RequesterType.AGENT_REQUESTER,
        is_response_requested=False,
        gateway_context=GatewayContext(),
        manager_escalation=ManagerEscalationContext(
            escalation_type="manager_review",
            source_named_agent_type=None if incident.agent_type == TEAM_SCOPE else incident.agent_type,
            original_requester="worker_autonomous",
        ),
    )
    meta = sign_for_maintenance_owner(
        agent,
        meta,
        task_id=review_id,
        maintenance_schedule_id=internal_improvement_schedule_id(str(agent.team_id)),
    )
    header = MessageHeader(from_agent_id=agent.id, to_routing_path=agent.id, agent_signature="")
    try:
        # Admitted directly: a lead-authored message on its own route is dropped
        # as a self-message by the NATS intake.
        await agent._handle_task_request(task_request=task, msg_header=header, msg_meta=meta)
    except Exception:
        logger.exception(
            "Could not give TeamArchitect a worker pool review; retrying next pass. "
            "event=worker_pool_review_failed team_id=%s incident_id=%s",
            agent.team_id,
            incident.incident_id,
        )
        return None
    logger.warning(
        "Gave TeamArchitect a worker pool review. event=worker_pool_review_opened team_id=%s "
        "agent_type=%s kind=%s incident_id=%s review_task_id=%s recurring=%s",
        agent.team_id,
        incident.agent_type,
        incident.kind,
        incident.incident_id,
        review_id,
        recurring,
    )
    return review_id


class RecurringPoolProblems:
    """Stuck-work source: worker pool problems administrators must hear about.

    A recurring problem (see the module docstring), or a worker type that
    cannot start workers, is reported to administrators in the Inbox by
    trusted code, once per incident, with a notice when it ends.
    """

    subject_kind: StuckSubjectKind = WORKER_POOL_SUBJECT

    def __init__(self, agent: Any, settings: WorkerPoolsConfig) -> None:
        self.agent = agent
        self.settings = settings

    def findings(self, now: datetime) -> list[StuckFinding]:
        repository = worker_pool_repository(self.agent)
        team_id = str(self.agent.team_id)
        window = timedelta(hours=self.settings.recurring_incident_window_hours)
        recent = repository.recent_incidents(team_id, since=now - window)
        found = []
        for incident in repository.list_open_incidents(team_id):
            recurrences = sum(
                1 for item in recent if item.kind == incident.kind and item.agent_type == incident.agent_type
            )
            recurring = recurrences >= self.settings.recurring_incident_count
            if not recurring and incident.kind != INCIDENT_START_FAILED:
                continue
            scope = _scope_label(incident.agent_type)
            text = (
                f"{scope} {_TITLES[incident.kind]}"
                + (f" (it came back {recurrences} times within {self.settings.recurring_incident_window_hours:g} hours)" if recurring else "")
                + ". TeamArchitect was given a review and may change pool sizes within the "
                "operator ceilings (agent.worker_pools); check its report, the Status page and "
                "the worker pool settings."
            )
            found.append(
                StuckFinding(
                    subject_kind=WORKER_POOL_SUBJECT,
                    subject_id=f"{incident.agent_type}:{incident.kind}",
                    incident_key=incident.kind,
                    reason=f"worker_pool_{incident.kind}",
                    since=incident.opened_at_utc,
                    person_id=None,
                    tell_administrators=True,
                    message=StuckMessage(title=f"{scope} {_TITLES[incident.kind]}", text=text),
                    chat_route=None,
                )
            )
        return found

    def recovery(self, incident: StuckWorkIncidentV1) -> Recovery:
        agent_type, _, kind = incident.subject_id.partition(":")
        scope = _scope_label(agent_type)
        return Recovery(
            message=StuckMessage(
                title=f"{scope} is working normally again",
                text=f"{scope} no longer shows the problem ({kind.replace('_', ' ')}).",
            ),
            chat_route=None,
        )


__all__ = [
    "RecurringPoolProblems",
    "WORKER_POOL_REVIEW_TASK_PREFIX",
    "WorkerPoolIncidents",
    "open_review",
]
